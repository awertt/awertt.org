#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import tempfile
import zipfile
from pathlib import Path


def find_manifest(zf: zipfile.ZipFile) -> str:
    for name in zf.namelist():
        if name.endswith('/manifest.json') or name == 'manifest.json':
            return name
    raise SystemExit(f'No manifest.json in {zf.filename}')


def archive_root(manifest_name: str) -> str:
    return manifest_name.rsplit('/', 1)[0] if '/' in manifest_name else ''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('archives', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()

    merged = {
        'created_at': None,
        'target_years': [],
        'files': [],
        'competition_records': [],
        'discovered_score_urls': [],
        'notes': ['Merged historical and modern official raw archives.'],
    }
    file_seen = set()
    competition_by_guid = {}
    discovered = set()
    years = set()

    with tempfile.TemporaryDirectory(prefix='dci-raw-merge-') as temp_name:
        temp = Path(temp_name)
        out_root = temp / 'dci-raw-work'
        out_root.mkdir()

        for archive in args.archives:
            if not archive.is_file():
                raise SystemExit(f'Archive not found: {archive}')
            with zipfile.ZipFile(archive) as zf:
                manifest_name = find_manifest(zf)
                root = archive_root(manifest_name)
                manifest = json.loads(zf.read(manifest_name))
                if merged['created_at'] is None:
                    merged['created_at'] = manifest.get('created_at')
                years.update(int(y) for y in manifest.get('target_years', []))
                discovered.update(manifest.get('discovered_score_urls', []))
                merged['notes'].extend(manifest.get('notes', []))

                for rec in manifest.get('competition_records', []):
                    guid = str(rec.get('CompetitionGuid') or rec.get('competitionGuid') or '').lower()
                    if guid:
                        competition_by_guid[guid] = rec

                for item in manifest.get('files', []):
                    rel = item.get('file')
                    key = (item.get('requested_url'), rel, item.get('sha256'))
                    if key not in file_seen:
                        file_seen.add(key)
                        merged['files'].append(item)
                    if not rel:
                        continue
                    source_name = f'{root}/{rel}' if root else rel
                    try:
                        info = zf.getinfo(source_name)
                    except KeyError:
                        continue
                    target = out_root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        target.write_bytes(zf.read(info))
                    meta_name = source_name + '.meta.json'
                    try:
                        meta_info = zf.getinfo(meta_name)
                    except KeyError:
                        continue
                    meta_target = out_root / (rel + '.meta.json')
                    if not meta_target.exists():
                        meta_target.write_bytes(zf.read(meta_info))

        merged['target_years'] = sorted(years)
        merged['competition_records'] = sorted(
            competition_by_guid.values(),
            key=lambda r: (int(r.get('_year') or 0), str(r.get('Date') or ''), str(r.get('EventName') or '')),
        )
        merged['discovered_score_urls'] = sorted(discovered)
        merged['summary'] = {
            'competition_records': len(merged['competition_records']),
            'files_recorded': len(merged['files']),
            'target_years': merged['target_years'],
        }
        (out_root / 'manifest.json').write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding='utf-8')
        (out_root / 'diagnostics.txt').write_text(json.dumps(merged['summary'], indent=2) + '\n', encoding='utf-8')

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.unlink(missing_ok=True)
        with zipfile.ZipFile(args.output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as out:
            for path in out_root.rglob('*'):
                if path.is_file():
                    out.write(path, path.relative_to(temp))

    print(json.dumps(merged['summary'], indent=2, sort_keys=True))
    print(f'Wrote {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
