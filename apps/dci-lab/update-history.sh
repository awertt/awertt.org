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

# Put the tie-aware pages live before the database expansion starts.
systemctl restart awertt-dci.service

python3 -m py_compile "$APP_DIR/append-historical-totals.py"
chmod 700 "$APP_DIR/append-historical-totals.py"

rm -f "$NEW_DB"
cp -a "$DB_FILE" "$NEW_DB"
chown root:root "$NEW_DB"
chmod 600 "$NEW_DB"

echo "Importing 2000-2014 total-score history..."
python3 "$APP_DIR/append-historical-totals.py" "$NEW_DB"

python3 - "$NEW_DB" <<'PY'
import sqlite3,sys
p=sys.argv[1]
con=sqlite3.connect(p)
check=con.execute('PRAGMA integrity_check').fetchone()[0]
years=[r[0] for r in con.execute('SELECT season_id FROM coverage WHERE relevant_events>0 ORDER BY season_id')]
historical=con.execute('SELECT COUNT(*) FROM v_performance_summary WHERE season_year BETWEEN 2000 AND 2014').fetchone()[0]
ties=con.execute('SELECT COUNT(*) FROM performances WHERE placement_tied=1').fetchone()[0]
con.close()
required=list(range(2000,2020))+list(range(2022,2027))
if check!='ok' or years!=required or historical<=0:
    raise SystemExit(f'Final validation failed: integrity={check!r}, years={years!r}, historical={historical}')
print(f'Final validation: integrity={check}; historical appearances={historical}; tied performances={ties}; years={years[0]}-{years[-1]} excluding 2020-21')
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
print('year | competitions | events | world | open | scored | no-score | values | corps | judges | venues | venue gaps | sources | failures | status')
for r in con.execute('SELECT * FROM coverage ORDER BY season_id'):
    print(' | '.join(str(v) for v in r[:-1]))
con.close()
PY

echo
echo "Expanded DCI Data Lab is live:"
echo "https://awertt.org/dci/"
echo "https://awertt.org/dci/records"
echo "https://awertt.org/dci/corps/Colts"
