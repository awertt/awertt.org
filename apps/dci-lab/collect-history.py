#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

TARGET_YEARS = tuple(range(2000, 2015))
WEB_ROOT = Path('/var/www/html')
WORK_ROOT = WEB_ROOT / 'dci-history-work'
ARCHIVE_PATH = WEB_ROOT / 'dci-raw-2000-2014.zip'
USER_AGENT = 'Mozilla/5.0 (compatible; DCIResearchCollector/2.0; +https://awertt.org/dci/)'
MAX_BYTES = 100_000_000
WORKERS = 10
RETRIES = 4

ALLOWED_HOST_SUFFIXES = ('dci.org', 'competitionsuite.com')


def log(message: str) -> None:
    print(f'[{dt.datetime.now().strftime("%H:%M:%S")}] {message}', flush=True)


def allowed_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in {'http', 'https'} or not parts.hostname:
        return False
    host = parts.hostname.lower().rstrip('.')
    return any(host == suffix or host.endswith('.' + suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def fetch_once(url: str, timeout: int = 90) -> dict[str, Any]:
    if not allowed_url(url):
        return {'url': url, 'requested_url': url, 'status': 0, 'headers': {}, 'body': b'', 'error': 'host not allowed'}
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/json,text/plain,*/*;q=0.5',
        'Accept-Encoding': 'gzip',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError(f'response exceeded {MAX_BYTES} bytes')
            headers = {k.lower(): v for k, v in response.headers.items()}
            if headers.get('content-encoding', '').lower() == 'gzip':
                body = gzip.decompress(body)
            return {
                'url': response.geturl(),
                'requested_url': url,
                'status': int(getattr(response, 'status', 200)),
                'headers': headers,
                'body': body,
                'error': None,
            }
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_BYTES + 1)
        except Exception:
            body = b''
        return {
            'url': getattr(exc, 'url', url),
            'requested_url': url,
            'status': int(exc.code),
            'headers': {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {},
            'body': body,
            'error': str(exc),
        }
    except Exception as exc:
        return {'url': url, 'requested_url': url, 'status': 0, 'headers': {}, 'body': b'', 'error': repr(exc)}


def fetch(url: str, timeout: int = 90) -> dict[str, Any]:
    result = None
    for attempt in range(RETRIES):
        result = fetch_once(url, timeout)
        status = int(result.get('status') or 0)
        if 200 <= status < 300:
            return result
        if status not in {0, 408, 425, 429, 500, 502, 503, 504}:
            return result
        time.sleep(min(2 ** attempt, 8))
    return result or {'url': url, 'requested_url': url, 'status': 0, 'headers': {}, 'body': b'', 'error': 'unknown fetch failure'}


def text_of(result: dict[str, Any]) -> str:
    content_type = result.get('headers', {}).get('content-type', '')
    match = re.search(r'charset=([\w.-]+)', content_type, re.I)
    charset = match.group(1) if match else 'utf-8'
    try:
        return result.get('body', b'').decode(charset, errors='replace')
    except LookupError:
        return result.get('body', b'').decode('utf-8', errors='replace')


def parse_json(result: dict[str, Any]) -> Any:
    try:
        return json.loads(text_of(result))
    except Exception:
        return None


def recursive_find_competitions(obj: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(obj, list):
        for item in obj:
            found.extend(recursive_find_competitions(item))
    elif isinstance(obj, dict):
        keys = {str(k).lower() for k in obj}
        if 'competitionguid' in keys and ('eventname' in keys or 'competitionname' in keys):
            found.append(obj)
        else:
            for value in obj.values():
                found.extend(recursive_find_competitions(value))
    return found


def safe_name(url: str, extension: str | None = None) -> str:
    parts = urllib.parse.urlsplit(url)
    base = (parts.hostname or 'unknown') + (parts.path or '/index')
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._') or 'index'
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    if extension is None:
        suffix = Path(parts.path).suffix
        extension = suffix if suffix and len(suffix) <= 8 else '.bin'
    if extension and not extension.startswith('.'):
        extension = '.' + extension
    return f'{base[:160]}__{digest}{extension or ""}'


def save_result(result: dict[str, Any], folder: Path, extension: str | None = None) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=True)
    requested = result.get('requested_url') or result.get('url') or 'unknown'
    content_type = result.get('headers', {}).get('content-type', '').lower()
    if extension is None:
        if 'json' in content_type:
            extension = '.json'
        elif 'html' in content_type:
            extension = '.html'
        elif 'text/' in content_type:
            extension = '.txt'
    path = folder / safe_name(requested, extension)
    body = result.get('body', b'')
    path.write_bytes(body)
    meta = {
        'requested_url': requested,
        'final_url': result.get('url'),
        'status': result.get('status'),
        'content_type': result.get('headers', {}).get('content-type'),
        'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'bytes': len(body),
        'sha256': hashlib.sha256(body).hexdigest(),
        'file': str(path.relative_to(WORK_ROOT)),
        'error': result.get('error'),
    }
    path.with_suffix(path.suffix + '.meta.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    return meta


def normalize_resource_url(value: str) -> str:
    return urllib.parse.urljoin('https://api.competitionsuite.com/', value)


def main() -> int:
    if WORK_ROOT.exists():
        shutil.rmtree(WORK_ROOT)
    api_dir = WORK_ROOT / 'api'
    score_dir = WORK_ROOT / 'raw' / 'scores'
    api_dir.mkdir(parents=True)
    score_dir.mkdir(parents=True)

    manifest: dict[str, Any] = {
        'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'target_years': list(TARGET_YEARS),
        'files': [],
        'competition_records': [],
        'discovered_score_urls': [],
        'notes': ['Historical CompetitionSuite collection for DCI Division I/II/III and World/Open Class.'],
    }

    competitions: dict[str, dict[str, Any]] = {}
    year_counts: Counter[int] = Counter()

    for year in TARGET_YEARS:
        url = 'https://api.competitionsuite.com/2018-03/competitions?' + urllib.parse.urlencode({
            'year': str(year),
            'includePractice': 'false',
        })
        log(f'Fetching competition list for {year}')
        result = fetch(url)
        manifest['files'].append(save_result(result, api_dir / 'competition_lists', '.json'))
        if int(result.get('status') or 0) != 200:
            raise SystemExit(f'Competition list failed for {year}: HTTP {result.get("status")} {result.get("error") or ""}')
        records = recursive_find_competitions(parse_json(result))
        if not records:
            raise SystemExit(f'CompetitionSuite returned no competition records for {year}.')
        for record in records:
            guid = str(record.get('CompetitionGuid') or record.get('competitionGuid') or '').lower()
            if not guid:
                continue
            item = dict(record)
            item['_year'] = year
            item['_organization_candidate'] = ''
            item['_source_url'] = url
            item['_event_guid'] = str(item.get('EventGuid') or item.get('eventGuid') or '')
            competitions.setdefault(guid, item)
        year_counts[year] = sum(1 for r in competitions.values() if int(r['_year']) == year)
        log(f'{year}: {year_counts[year]} competition records')

    manifest['competition_records'] = sorted(
        competitions.values(),
        key=lambda r: (int(r['_year']), str(r.get('Date') or ''), str(r.get('EventName') or '')),
    )

    urls: set[str] = set()
    mandatory_by_guid: dict[str, str] = {}
    for guid, record in competitions.items():
        for field in ('PerformancesUrl', 'RecapUrl', 'CategoryRecapUrl'):
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                url = normalize_resource_url(value.strip())
                if allowed_url(url):
                    urls.add(url)
                    if field == 'PerformancesUrl':
                        mandatory_by_guid[guid] = url

    manifest['discovered_score_urls'] = sorted(urls)
    log(f'Fetching {len(urls)} performance and recap resources')

    def fetch_and_save(url: str) -> dict[str, Any]:
        return save_result(fetch(url), score_dir)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_and_save, url): url for url in sorted(urls)}
        for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
            url = futures[future]
            try:
                meta = future.result()
            except Exception as exc:
                meta = {'requested_url': url, 'status': 0, 'error': repr(exc), 'file': None}
            manifest['files'].append(meta)
            if completed % 50 == 0 or completed == len(futures):
                log(f'Fetched {completed}/{len(futures)} resources')

    successful_urls = {
        item.get('requested_url')
        for item in manifest['files']
        if int(item.get('status') or 0) == 200
    }
    missing = [(guid, url) for guid, url in mandatory_by_guid.items() if url not in successful_urls]
    if missing:
        log(f'Retrying {len(missing)} missing performance payloads sequentially')
        for guid, url in missing:
            meta = save_result(fetch(url, timeout=120), score_dir)
            manifest['files'].append(meta)

    successful_urls = {
        item.get('requested_url')
        for item in manifest['files']
        if int(item.get('status') or 0) == 200
    }
    missing = [(guid, url) for guid, url in mandatory_by_guid.items() if url not in successful_urls]
    if missing:
        (WORK_ROOT / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        raise SystemExit(f'{len(missing)} mandatory performance payloads still failed. First: {missing[0]}')

    status_counts = Counter(str(item.get('status') or 0) for item in manifest['files'])
    manifest['status_counts'] = dict(status_counts)
    manifest['summary'] = {
        'competition_records': len(competitions),
        'score_urls_attempted': len(urls),
        'files_recorded': len(manifest['files']),
        'successful_2xx': sum(1 for item in manifest['files'] if 200 <= int(item.get('status') or 0) < 300),
        'performance_payloads_complete': len(mandatory_by_guid),
        'year_counts': dict(sorted(year_counts.items())),
    }
    (WORK_ROOT / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    (WORK_ROOT / 'diagnostics.txt').write_text(json.dumps(manifest['summary'], indent=2) + '\n', encoding='utf-8')

    ARCHIVE_PATH.unlink(missing_ok=True)
    log(f'Creating {ARCHIVE_PATH}')
    with zipfile.ZipFile(ARCHIVE_PATH, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in WORK_ROOT.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(WORK_ROOT.parent))
    os.chmod(ARCHIVE_PATH, 0o640)
    log('Historical collection complete')
    print(json.dumps(manifest['summary'], indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
