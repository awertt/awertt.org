#!/usr/bin/env python3
import json
import sqlite3

path = "/var/lib/awertt-dci/dci_scores_master.sqlite"
con = sqlite3.connect(path)
try:
    names = (
        "seasons", "events", "event_classes", "corps", "caption_definitions",
        "performances", "score_values", "sources", "coverage", "data_issues",
        "scheduled_appearances", "judge_assignments", "event_interruptions",
        "v_performance_summary",
    )
    for name in names:
        print("SCHEMA", name, json.dumps([row[1] for row in con.execute(f"PRAGMA table_info({name})")]), flush=True)
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='v_performance_summary'").fetchone()
    print("VIEWSQL", row[0] if row else None, flush=True)
    print("SEASONS_SAMPLE", con.execute("SELECT * FROM seasons ORDER BY 1 LIMIT 3").fetchall(), flush=True)
    print("COVERAGE_SAMPLE", con.execute("SELECT * FROM coverage ORDER BY 1 LIMIT 3").fetchall(), flush=True)
finally:
    con.close()
