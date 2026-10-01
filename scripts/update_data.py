"""Incrementally refresh GKI version data and validate its schema."""
from __future__ import annotations
import copy, json, os, sys, time
from gki_fetch import TARGETS, fetch_lts, fetch_makefile, get_end_date, json_path, make_date_range, parse_version
from prepare_matrix import validate_data

def update_target(android_ver: str, kernel_ver: str, date_start: str, date_end: str | None, dep_cutoff: str) -> bool:
    path = json_path(android_ver, kernel_ver)
    end = get_end_date(date_end)
    is_k510 = kernel_ver == '5.10'
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        validate_data(data, path, android_ver, kernel_ver)
    else:
        data = {'android_version': android_ver, 'kernel_version': kernel_ver, 'entries': []}
        if not is_k510:
            data['lts'] = None
    original = copy.deepcopy(data)
    entries = data.setdefault('entries', [])
    existing = {e.get('date') for e in entries}
    for date in make_date_range(date_start, end):
        if date in existing:
            continue
        label = f'{android_ver}-{kernel_ver}-{date}'
        print(f'    [{label}] ', end='', flush=True)
        text = fetch_makefile(android_ver, kernel_ver, date, dep_cutoff)
        if text is None:
            print('not found, skip'); continue
        ver = parse_version(text)
        if ver is None:
            raise RuntimeError(f'failed to parse Makefile for {label}')
        entry = {'date': date, 'kernel': '.'.join(ver)}
        if is_k510:
            entry['revision'] = 'r1'
        entries.append(entry)
        print(f"-> {entry['kernel']}")
        time.sleep(0.2)
    entries.sort(key=lambda e: e.get('date',''))
    lts_text = fetch_lts(android_ver, kernel_ver)
    if lts_text is None:
        raise RuntimeError(f'LTS branch not found: {android_ver}-{kernel_ver}-lts')
    ver = parse_version(lts_text)
    if ver is None:
        raise RuntimeError(f'failed to parse LTS for {android_ver}/{kernel_ver}')
    data['lts'] = '.'.join(ver)
    if is_k510:
        data.setdefault('lts_revision', 'r1')
    validate_data(data, path, android_ver, kernel_ver)
    changed = data != original
    if changed:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False); f.write('\n')
        os.replace(tmp, path)
        print(f'  => Saved {len(entries)} entries to {path}')
    else:
        print('  => No changes')
    return changed

def main() -> int:
    changed = False
    for (android_ver, kernel_ver), (start, end, cutoff) in TARGETS.items():
        print(f'\n=== {android_ver} / {kernel_ver} ===')
        changed |= update_target(android_ver, kernel_ver, start, end, cutoff)
    print('\nData updated.' if changed else '\nAll data up-to-date.')
    return 0

if __name__ == '__main__':
    try: sys.exit(main())
    except Exception as error:
        print(f'\nFATAL: {error}', file=sys.stderr); sys.exit(1)
