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

mkdir -p "$DATA_DIR"

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "The DCI app repository is not installed at $REPO_DIR" >&2
  exit 1
fi
if [[ ! -s "$DB_FILE" ]]; then
  echo "The current DCI database is missing: $DB_FILE" >&2
  exit 1
fi

echo "Updating the DCI Data Lab code..."
git -C "$REPO_DIR" fetch --prune origin
git -C "$REPO_DIR" reset --hard origin/main

# Deploy the tie-aware application before rebuilding the database.
systemctl restart awertt-dci.service

python3 -m py_compile "$APP_DIR/append-historical-live.py"
chmod 700 "$APP_DIR/append-historical-live.py"

rm -f "$NEW_DB"
cp -a "$DB_FILE" "$NEW_DB"
chown root:root "$NEW_DB"
chmod 600 "$NEW_DB"

echo "Importing 2000-2014 total-score and placement history..."
python3 "$APP_DIR/append-historical-live.py" "$NEW_DB"

python3 - "$NEW_DB" <<'PY'
import sqlite3,sys
path=sys.argv[1]
con=sqlite3.connect(path)
check=con.execute('PRAGMA integrity_check').fetchone()[0]
years=[row[0] for row in con.execute('SELECT DISTINCT season_year FROM v_performance_summary ORDER BY season_year')]
historical=con.execute('SELECT COUNT(*) FROM v_performance_summary WHERE season_year BETWEEN 2000 AND 2014').fetchone()[0]
historical_ties=con.execute('SELECT COUNT(*) FROM v_performance_summary WHERE season_year BETWEEN 2000 AND 2014 AND placement_tied=1').fetchone()[0]
notes=con.execute('SELECT COUNT(*) FROM historical_import_notes').fetchone()[0]
con.close()
required=list(range(2000,2020))+list(range(2022,2027))
if check!='ok' or years!=required or historical<=0 or notes!=15:
    raise SystemExit(f'Final validation failed: integrity={check!r}, years={years!r}, historical={historical}, notes={notes}')
print(f'Final validation: integrity={check}; historical appearances={historical}; historical tied appearances={historical_ties}; seasons={len(years)}')
PY

BACKUP="$DATA_DIR/dci_scores_master.sqlite.pre-2000-expansion.$(date +%Y%m%d-%H%M%S)"
cp -a "$DB_FILE" "$BACKUP"
echo "Previous database backed up to $BACKUP"

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
print('season_year | competitions | relevant events | appearances | scored | no-score | score values | judge assignments')
for row in con.execute('SELECT * FROM coverage ORDER BY season_year'):
    print(' | '.join(str(value) for value in row))
con.close()
PY

echo
echo "Expanded DCI Data Lab is live:"
echo "https://awertt.org/dci/"
echo "https://awertt.org/dci/records"
echo "https://awertt.org/dci/corps/Colts"
