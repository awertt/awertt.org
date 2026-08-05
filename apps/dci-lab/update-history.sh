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
python3 - "$APP_DIR/append-historical-totals.py" "$NEW_DB" <<'PY'
import importlib.util
import sqlite3
import sys
from pathlib import Path

module_path = Path(sys.argv[1])
database_path = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("dci_historical_import", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def table_exists(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def columns(con, table):
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]


def remove_previous_import(con):
    event_ids = [r[0] for r in con.execute("SELECT event_id FROM events WHERE event_id LIKE 'hist-gh-%'")]
    if event_ids:
        marks = ",".join("?" for _ in event_ids)
        class_ids = [r[0] for r in con.execute(f"SELECT event_class_id FROM event_classes WHERE event_id IN ({marks})", event_ids)]
        if class_ids:
            cmarks = ",".join("?" for _ in class_ids)
            perf_ids = [r[0] for r in con.execute(f"SELECT performance_id FROM performances WHERE event_class_id IN ({cmarks})", class_ids)]
            if perf_ids:
                pmarks = ",".join("?" for _ in perf_ids)
                con.execute(f"DELETE FROM score_values WHERE performance_id IN ({pmarks})", perf_ids)
                con.execute(f"DELETE FROM performances WHERE performance_id IN ({pmarks})", perf_ids)
            for table in ("scheduled_appearances", "judge_assignments"):
                if table_exists(con, table):
                    con.execute(f"DELETE FROM {table} WHERE event_class_id IN ({cmarks})", class_ids)
            con.execute(f"DELETE FROM event_classes WHERE event_class_id IN ({cmarks})", class_ids)
        if table_exists(con, "event_interruptions"):
            con.execute(f"DELETE FROM event_interruptions WHERE event_id IN ({marks})", event_ids)
        if table_exists(con, "sources"):
            con.execute(f"DELETE FROM sources WHERE event_id IN ({marks})", event_ids)
        con.execute(f"DELETE FROM events WHERE event_id IN ({marks})", event_ids)

    coverage_cols = set(columns(con, "coverage"))
    coverage_year = "season_id" if "season_id" in coverage_cols else "season_year"
    con.execute(f"DELETE FROM coverage WHERE {coverage_year} BETWEEN 2000 AND 2014")
    if table_exists(con, "data_issues"):
        con.execute("DELETE FROM data_issues WHERE season_year BETWEEN 2000 AND 2014 AND issue_type LIKE 'historical_%'")
    if table_exists(con, "seasons"):
        season_cols = set(columns(con, "seasons"))
        season_year_col = "year" if "year" in season_cols else "season_year"
        con.execute(f"DELETE FROM seasons WHERE {season_year_col} BETWEEN 2000 AND 2014")


def refresh_coverage(con, year, stats):
    unique_corps = con.execute(
        """SELECT COUNT(DISTINCT corps_id) FROM performances p
           JOIN event_classes ec ON ec.event_class_id=p.event_class_id
           JOIN events e ON e.event_id=ec.event_id WHERE e.season_id=?""", (year,)
    ).fetchone()[0]
    value_count = con.execute(
        """SELECT COUNT(*) FROM score_values sv JOIN performances p ON p.performance_id=sv.performance_id
           JOIN event_classes ec ON ec.event_class_id=p.event_class_id
           JOIN events e ON e.event_id=ec.event_id WHERE e.season_id=?""", (year,)
    ).fetchone()[0]

    available = columns(con, "coverage")
    values = {
        "season_id": year,
        "season_year": year,
        "competitions_total": stats["events"],
        "competition_records": stats["events"],
        "relevant_events": stats["events"],
        "events_with_target_divisions": stats["events"],
        "events_with_world_or_open": stats["events"],
        "world_class_appearances": stats["world"],
        "open_class_appearances": stats["open"],
        "scored_performances": stats["performances"],
        "nonperformed_appearances": 0,
        "score_values": value_count,
        "judge_assignments": 0,
        "unique_corps": unique_corps,
        "unique_judges": 0,
        "interruptions": 0,
        "venues_found": 0,
        "venues_missing": stats["events"],
        "source_2xx": stats["events"],
        "source_failures": 0,
        "status": "historical_totals_only",
        "notes": "Totals/placements imported from dci_score_history. Caption, subcaption, judge, venue, event-name, penalty, and cancellation coverage is incomplete.",
    }
    insert_cols = [name for name in available if name in values]
    if not insert_cols:
        raise RuntimeError(f"Unsupported coverage schema: {available!r}")
    sql = f"INSERT OR REPLACE INTO coverage ({','.join(insert_cols)}) VALUES ({','.join('?' for _ in insert_cols)})"
    con.execute(sql, [values[name] for name in insert_cols])

    if table_exists(con, "data_issues"):
        con.execute(
            """INSERT INTO data_issues(season_year,event_id,performance_id,severity,issue_type,
               description,source_url,resolved) VALUES(?,NULL,NULL,'info','historical_totals_only',?,?,0)""",
            (year, "Historical season contains total scores and placements only. Detailed recap values and judges were not available and were not fabricated.", module.RAW_TEMPLATE.format(year=year)),
        )
        con.execute(
            """INSERT INTO data_issues(season_year,event_id,performance_id,severity,issue_type,
               description,source_url,resolved) VALUES(?,NULL,NULL,'warning','historical_classification_limit',?,?,0)""",
            (year, "Historical source documentation notes rolling corrections to circuit/class designations. Explicit DCA/all-age records were excluded; remaining junior corps were normalized using source labels and known move-up years.", module.SOURCE_REPO),
        )


module.remove_previous_import = remove_previous_import
module.refresh_coverage = refresh_coverage
sys.argv = [str(module_path), str(database_path)]
raise SystemExit(module.main())
PY

python3 - "$NEW_DB" <<'PY'
import sqlite3,sys
p=sys.argv[1]
con=sqlite3.connect(p)
check=con.execute('PRAGMA integrity_check').fetchone()[0]
years=[r[0] for r in con.execute('SELECT DISTINCT season_year FROM v_performance_summary ORDER BY season_year')]
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
cols=[r[1] for r in con.execute('PRAGMA table_info(coverage)')]
print(' | '.join(cols))
for r in con.execute('SELECT * FROM coverage ORDER BY 1'):
    print(' | '.join(str(v) for v in r))
con.close()
PY

echo
echo "Expanded DCI Data Lab is live:"
echo "https://awertt.org/dci/"
echo "https://awertt.org/dci/records"
echo "https://awertt.org/dci/corps/Colts"
