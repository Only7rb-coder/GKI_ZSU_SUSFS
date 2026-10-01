import base64
import binascii
import http.client
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://android.googlesource.com/kernel/common/+/refs/heads"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

# (android版本, 内核版本): (起始日期, 结束日期, deprecated截止日期)
# 结束日期为 None 表示活跃版本，运行时自动使用当前月份
TARGETS = {
    ("android12", "5.10"): ("2021-08", "2025-12", "2024-08"),
    ("android13", "5.15"): ("2022-06", "2025-12", "2024-09"),
    ("android14", "6.1"):  ("2023-06", None,       "2024-09"),
    ("android15", "6.6"):  ("2024-10", None,       ""),
    ("android16", "6.12"): ("2025-06", None,       ""),
}

TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    http.client.RemoteDisconnected,
    ConnectionResetError,
    OSError,
    binascii.Error,
)

# Cache the public branch list once per run. The updater previously made a
# request for every missing month, including branches that never existed.
_BRANCH_CACHE: set[str] | None = None


class FetchError(RuntimeError):
    """Raised when an upstream request fails after retries."""


def get_end_date(end: str | None) -> str:
    """返回结束日期：如果为 None 则使用当前月份"""
    if end is not None:
        return end
    return datetime.now(timezone.utc).strftime("%Y-%m")


def make_date_range(start: str, end: str) -> list[str]:
    """生成从 start 到 end 的 YYYY-MM 列表"""
    sy, sm = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    dates = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        dates.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return dates


def available_branches() -> set[str]:
    """Return live and deprecated branches without probing every month."""
    global _BRANCH_CACHE
    if _BRANCH_CACHE is not None:
        return _BRANCH_CACHE
    try:
        output = subprocess.check_output(
            ["git", "ls-remote", "--heads",
             "https://android.googlesource.com/kernel/common"],
            text=True, stderr=subprocess.STDOUT, timeout=45,
        )
    except (subprocess.SubprocessError, OSError) as error:
        raise FetchError(f"failed to list AOSP branches: {error}") from error
    _BRANCH_CACHE = {
        line.rsplit("refs/heads/", 1)[1]
        for line in output.splitlines()
        if "refs/heads/" in line
    }
    return _BRANCH_CACHE


def try_fetch(url: str, attempts: int = 5) -> str | None:
    """Fetch and decode a googlesource file; return None only for HTTP 404."""
    request = urllib.request.Request(url, headers={"User-Agent": "GKI-data-updater"})
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                encoded = b"".join(response.read().split())
                content = base64.b64decode(encoded, validate=True)
                return content.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            last_error = error
            # Gitiles returns 429 when the runner probes too aggressively.
            # Respect Retry-After when supplied, otherwise use exponential backoff.
            if error.code not in (408, 425, 429) and error.code < 500:
                raise FetchError(f"failed to fetch {url}: {error}") from error
            retry_after = error.headers.get("Retry-After")
            try:
                delay = min(120, max(1, int(retry_after))) if retry_after else min(120, 2 ** attempt)
            except (TypeError, ValueError):
                delay = min(120, 2 ** attempt)
        except TRANSIENT_ERRORS as error:
            last_error = error
            delay = min(120, 2 ** attempt)

        if attempt < attempts:
            print(f"  transient AOSP request failure; retrying in {delay}s", flush=True)
            time.sleep(delay)

    raise FetchError(f"failed to fetch {url}: {last_error}")


def fetch_makefile(android_ver: str, kernel_ver: str, date: str,
                   dep_cutoff: str) -> str | None:
    """获取日期分支 Makefile，优先尝试预期路径，失败则回退"""
    branch = f"{android_ver}-{kernel_ver}-{date}"
    if dep_cutoff and date <= dep_cutoff:
        paths = [f"deprecated/{branch}", branch]
    else:
        paths = [branch, f"deprecated/{branch}"]

    branches = available_branches()
    for p in paths:
        if p not in branches:
            continue
        url = f"{BASE_URL}/{p}/Makefile?format=TEXT"
        text = try_fetch(url)
        if text is not None:
            return text
    return None


def fetch_lts(android_ver: str, kernel_ver: str) -> str | None:
    """获取 LTS 分支 Makefile"""
    lts_branch = f"{android_ver}-{kernel_ver}-lts"
    url = f"{BASE_URL}/{lts_branch}/Makefile?format=TEXT"
    return try_fetch(url)


def parse_version(makefile_text: str) -> tuple[str, str, str] | None:
    """从 Makefile 提取 VERSION, PATCHLEVEL, SUBLEVEL"""
    vals = {}
    for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL"):
        m = re.search(rf"^{key}\s*=\s*(\d+)", makefile_text, re.MULTILINE)
        if not m:
            return None
        vals[key] = m.group(1)
    return vals["VERSION"], vals["PATCHLEVEL"], vals["SUBLEVEL"]


def json_path(android_ver: str, kernel_ver: str) -> str:
    """返回对应的 JSON 文件路径"""
    return os.path.join(DATA_DIR, android_ver, f"{kernel_ver}.json")
