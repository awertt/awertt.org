#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run this as root." >&2
  exit 1
fi

REPO_DIR=/opt/awertt-dci/repo
APP_DIR="$REPO_DIR/apps/dci-lab"
DATA_DIR=/var/lib/awertt-dci
DB_FILE="$DATA_DIR/dci_scores_master.sqlite"
NEW_DB="$DATA_DIR/dci_scores_master.sqlite.new"
HISTORY_RAW=/var/www/html/dci-raw-2000-2014.zip
FULL_RAW=/var/www/html/dci-raw-2000-2026.zip
BUILDER="$APP_DIR/build-full-database.py"
BUILDER_B64=/tmp/awertt-dci-builder.b64
BUILDER_GZ=/tmp/awertt-dci-builder.py.gz

mkdir -p "$DATA_DIR"

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "The DCI app repository is not installed at $REPO_DIR" >&2
  exit 1
fi

echo "Updating the DCI Data Lab code..."
git -C "$REPO_DIR" fetch --prune origin
git -C "$REPO_DIR" reset --hard origin/main

# Put the tie-aware pages live immediately while the historical collection runs.
systemctl restart awertt-dci.service

rm -f "$BUILDER_B64" "$BUILDER_GZ" "$BUILDER"
cat "$APP_DIR"/builder-parts/part*.txt > "$BUILDER_B64"
base64 -d "$BUILDER_B64" > "$BUILDER_GZ"
gzip -t "$BUILDER_GZ"
gzip -dc "$BUILDER_GZ" > "$BUILDER"
python3 -m py_compile "$BUILDER"
chmod 700 "$BUILDER" "$APP_DIR/collect-history.py" "$APP_DIR/merge-raw-archives.py"

CURRENT_RAW="$(python3 - <<'PY'
import json, zipfile
from pathlib import Path
candidates=[]
for path in Path('/var/www/html').glob('dci-raw-*.zip'):
    if path.name in {'dci-raw-2000-2014.zip','dci-raw-2000-2026.zip'}:
        continue
    try:
        with zipfile.ZipFile(path) as zf:
            names=[n for n in zf.namelist() if n.endswith('/manifest.json') or n=='manifest.json']
            if not names:
                continue
            m=json.loads(zf.read(names[0]))
            years={int(y) for y in m.get('target_years',[])}
            if {2015,2016,2017,2018,2019,2022,2023,2024,2025,2026}.issubset(years):
                candidates.append((path.stat().st_mtime,path))
    except Exception:
        pass
if not candidates:
    raise SystemExit('Could not find the existing 2015-2019/2022-2026 raw archive in /var/www/html.')
print(max(candidates)[1])
PY
)"

echo "Using existing modern archive: $CURRENT_RAW"

HISTORY_OK=0
if [[ -s "$HISTORY_RAW" ]]; then
  if python3 - "$HISTORY_RAW" <<'PY'
import json,sys,zipfile
p=sys.argv[1]
with zipfile.ZipFile(p) as zf:
    names=[n for n in zf.namelist() if n.endswith('/manifest.json') or n=='manifest.json']
    if not names:
        raise SystemExit(1)
    m=json.loads(zf.read(names[0]))
    years={int(y) for y in m.get('target_years',[])}
    summary=m.get('summary',{})
    if years != set(range(2000,2015)):
        raise SystemExit(1)
    if int(summary.get('performance_payloads_complete') or 0) <= 0:
        raise SystemExit(1)
PY
  then
    HISTORY_OK=1
  fi
fi

if [[ "$HISTORY_OK" -eq 1 ]]; then
  echo "Reusing validated historical archive: $HISTORY_RAW"
else
  echo "Collecting official CompetitionSuite history for 2000-2014..."
  rm -f "$HISTORY_RAW"
  python3 "$APP_DIR/collect-history.py"
fi

echo "Merging historical and modern raw archives..."
python3 "$APP_DIR/merge-raw-archives.py" \
  "$CURRENT_RAW" "$HISTORY_RAW" \
  --output "$FULL_RAW"

echo "Building the expanded database..."
rm -f "$NEW_DB"
python3 "$BUILDER" "$FULL_RAW" "$NEW_DB"

python3 - "$NEW_DB" <<'PY'
import sqlite3,sys
p=sys.argv[1]
con=sqlite3.connect(p)
check=con.execute('PRAGMA integrity_check').fetchone()[0]
years=[r[0] for r in con.execute('SELECT season_year FROM coverage WHERE events_with_target_divisions>0 ORDER BY season_year')]
count=con.execute('SELECT COUNT(*) FROM v_performance_summary').fetchone()[0]
ties=con.execute('SELECT COUNT(*) FROM performances WHERE placement_tied=1').fetchone()[0]
con.close()
required=list(range(2000,2020))+list(range(2022,2027))
if check!='ok' or years!=required:
    raise SystemExit(f'Final validation failed: integrity={check!r}, years={years!r}')
print(f'Final validation: integrity={check}; appearances={count}; tied performances={ties}; years={years[0]}-{years[-1]} excluding 2020-21')
PY

if [[ -f "$DB_FILE" ]]; then
  BACKUP="$DATA_DIR/dci_scores_master.sqlite.pre-2000-expansion.$(date +%Y%m%d-%H%M%S)"
  cp -a "$DB_FILE" "$BACKUP"
  echo "Previous database backed up to $BACKUP"
fi
chown dciweb:dciweb "$NEW_DB"
chmod 640 "$NEW_DB"
mv -f "$NEW_DB" "$DB_FILE"

systemctl restart awertt-dci.service
sleep 2

echo
echo "=== APP HEALTH ==="
curl -fsS http://127.0.0.1:3001/health
echo

echo
echo "=== SEASON COVERAGE ==="
python3 - "$DB_FILE" <<'PY'
import sqlite3,sys
con=sqlite3.connect(sys.argv[1])
print('year | competitions | events | appearances | scored | no-score | values | judge assignments')
for r in con.execute('SELECT * FROM coverage ORDER BY season_year'):
    print(' | '.join(str(v) for v in r))
con.close()
PY

echo
echo "Expanded DCI Data Lab is live:"
echo "https://awertt.org/dci/"
echo "https://awertt.org/dci/records"
echo "https://awertt.org/dci/corps/Colts"
