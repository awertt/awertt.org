#!/usr/bin/env python3
"""Append total-score-only DCI history for 2000-2014 to the normalized database.

The modern 2015+ portion remains sourced from official CompetitionSuite payloads.
Historical totals come from the public dci_score_history compilation. Missing
historical captions, judges, penalties, venues, and event names remain null and
are explicitly flagged rather than invented.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import re
import sqlite3
import urllib.request
from collections import defaultdict
from pathlib import Path

YEARS = range(2000, 2015)
RAW_TEMPLATE = "https://raw.githubusercontent.com/GetHorizontal63/dci_score_history/main/data/years/{year}_dci_data.csv"
SOURCE_REPO = "https://github.com/GetHorizontal63/dci_score_history"
USER_AGENT = "awertt-dci-history-import/1.0 (+https://awertt.org/dci/)"

ALL_AGE_CORPS = {
    "alliance", "atlanta cv", "brigadiers", "bushwackers", "caballeros",
    "carolina gold", "chops inc", "connecticut hurricanes", "corpsvets",
    "empire statesmen", "excelsior", "fusion core", "govenaires",
    "grenadiers", "hawthorne caballeros", "heat wave of florida", "hurricanes",
    "kilties", "kingston grenadiers", "minnesota brass", "new york skyliners",
    "reading buccaneers", "renegades", "rochester crusaders", "skyliners",
    "sunrisers", "syracuse brigadiers", "white sabers",
}
ALIASES = {
    "academy": "The Academy", "cadets": "The Cadets",
    "cadets of bergen county": "The Cadets", "cavaliers": "The Cavaliers",
    "sc vanguard": "Santa Clara Vanguard", "scv": "Santa Clara Vanguard",
    "santa clara": "Santa Clara Vanguard", "vanguard": "Santa Clara Vanguard",
    "cascades": "Seattle Cascades", "spirit": "Spirit of Atlanta",
    "spirit from jsu": "Spirit of Atlanta", "magic": "The Magic",
    "magic of orlando": "The Magic",
    "capital regiment drum & bugle corps": "Capital Regiment",
}
WORLD_START = {
    "mandarins": 2003, "pacific crest": 2003, "esperanza": 2004,
    "blue stars": 2006, "the academy": 2007, "jersey surf": 2009,
    "teal sound": 2010, "oregon crusaders": 2013,
}
ALWAYS_WORLD = {
    "blue devils", "blue knights", "bluecoats", "boston crusaders",
    "carolina crown", "colts", "crossmen", "glassmen", "kiwanis kavaliers",
    "madison scouts", "phantom regiment", "pioneer", "southwind",
    "spirit of atlanta", "the cadets", "the cavaliers", "the magic",
    "troopers", "santa clara vanguard",
}


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def key(value) -> str:
    return clean(value).casefold()


def canonical_corps(name: str) -> str:
    stripped = clean(name)
    return ALIASES.get(stripped.casefold(), stripped)


def parse_date(value: str, year: int) -> str:
    value = clean(value)
    if re.fullmatch(r"\d{1,2}/\d{1,2}", value):
        month, day = (int(part) for part in value.split("/"))
        return dt.date(year, month, day).isoformat()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"unrecognized date {value!r}")


def parse_score(value: str) -> float | None:
    value = clean(value).replace("*", "")
    if not value or value.casefold() in {"n/a", "na", "exh", "exhibition", "dns", "dnf"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    score = float(match.group())
    return score if 0 <= score <= 100 else None


def normalize_division(source_class: str, corps_name: str, year: int) -> str | None:
    label = key(source_class)
    corps = key(corps_name)
    blocked = ("dca", "all age", "all-age", "senior", "mini corps", "alumni", "soundsport")
    if any(token in label for token in blocked) or corps in ALL_AGE_CORPS:
        return None
    canon = key(canonical_corps(corps_name))
    if any(token in label for token in ("division ii", "division 2", "division iii", "division 3")):
        return "Open Class"
    if "open class" in label:
        return "Open Class"
    if canon in ALWAYS_WORLD:
        return "World Class"
    if canon in WORLD_START:
        return "World Class" if year >= WORLD_START[canon] else "Open Class"
    if any(token in label for token in ("division i", "division 1")):
        return "World Class"
    if "world" in label:
        return "Open Class" if year <= 2007 else "World Class"
    if not label:
        return "Open Class"
    return None


def fetch_year(year: int, csv_dir: Path | None) -> tuple[str, bytes]:
    if csv_dir:
        path = csv_dir / f"{year}_dci_data.csv"
        return path.as_uri(), path.read_bytes()
    url = RAW_TEMPLATE.format(year=year)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
        if int(getattr(response, "status", 200)) != 200:
            raise RuntimeError(f"HTTP {response.status} for {url}")
        return url, body


def find_column(row: dict[str, str], *names: str) -> str:
    lowered = {key(k): v for k, v in row.items()}
    for name in names:
        if key(name) in lowered:
            return clean(lowered[key(name)])
    return ""


def total_caption_id(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT caption_definition_id FROM caption_definitions WHERE level='total' LIMIT 1").fetchone()
    if row:
        return int(row[0])
    cursor = con.execute(
        """INSERT INTO caption_definitions
           (level,category_name,caption_name,subcaption_name,initials,normalized_key,is_penalty)
           VALUES ('total',NULL,'Total',NULL,'TOT','total||total||tot',0)"""
    )
    return int(cursor.lastrowid)


def remove_previous_import(con: sqlite3.Connection) -> None:
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
            con.execute(f"DELETE FROM scheduled_appearances WHERE event_class_id IN ({cmarks})", class_ids)
            con.execute(f"DELETE FROM judge_assignments WHERE event_class_id IN ({cmarks})", class_ids)
            con.execute(f"DELETE FROM event_classes WHERE event_class_id IN ({cmarks})", class_ids)
        con.execute(f"DELETE FROM event_interruptions WHERE event_id IN ({marks})", event_ids)
        con.execute(f"DELETE FROM sources WHERE event_id IN ({marks})", event_ids)
        con.execute(f"DELETE FROM events WHERE event_id IN ({marks})", event_ids)
    con.execute("DELETE FROM coverage WHERE season_id BETWEEN 2000 AND 2014")
    con.execute("DELETE FROM data_issues WHERE season_year BETWEEN 2000 AND 2014 AND issue_type LIKE 'historical_%'")
    con.execute("DELETE FROM seasons WHERE year BETWEEN 2000 AND 2014")


def ensure_corps(con: sqlite3.Connection, name: str, year: int) -> int:
    row = con.execute("SELECT corps_id FROM corps WHERE lower(canonical_name)=lower(?) LIMIT 1", (name,)).fetchone()
    if row:
        con.execute("UPDATE corps SET latest_season=MAX(COALESCE(latest_season,0),?) WHERE corps_id=?", (year, row[0]))
        return int(row[0])
    cursor = con.execute("INSERT INTO corps(org_group_identifier,canonical_name,latest_season) VALUES(NULL,?,?)", (name, year))
    return int(cursor.lastrowid)


def event_identifier(year: int, date: str, city: str, state: str, event_name: str) -> str:
    payload = "|".join((str(year), date, key(city), key(state), key(event_name)))
    return "hist-gh-" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def import_rows(con: sqlite3.Connection, year: int, source_url: str, body: bytes) -> dict[str, int]:
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig", errors="replace")))
    accepted = []
    skipped = 0
    for raw in reader:
        try:
            if int(find_column(raw, "Year") or year) != year:
                continue
            date = parse_date(find_column(raw, "Date"), year)
            city = find_column(raw, "City") or "Unknown city"
            state = find_column(raw, "State", "Region")
            corps = canonical_corps(find_column(raw, "Corps", "Group"))
            division = normalize_division(find_column(raw, "Class", "Division"), corps, year)
            score_text = find_column(raw, "Score", "Total")
            score = parse_score(score_text)
            event_name = find_column(raw, "Event_Name", "Event", "Show")
            if not event_name:
                event_name = f"Historical DCI score event - {', '.join(v for v in (city, state) if v)}"
            if not corps or not division or score is None:
                skipped += 1
                continue
            accepted.append({"date": date, "city": city, "state": state, "corps": corps,
                             "division": division, "score": score,
                             "score_text": score_text or f"{score:.3f}", "event_name": event_name})
        except Exception:
            skipped += 1

    unique = {}
    for row in accepted:
        dedupe = (row["date"], key(row["city"]), key(row["state"]), key(row["event_name"]),
                  key(row["corps"]), row["division"], row["score"])
        unique[dedupe] = row
    grouped = defaultdict(list)
    for row in unique.values():
        grouped[(row["date"], row["city"], row["state"], row["event_name"])].append(row)

    caption_id = total_caption_id(con)
    source_sha = hashlib.sha256(body).hexdigest()
    counts = {"events": 0, "performances": 0, "world": 0, "open": 0, "skipped": skipped}

    for (date, city, state, event_name), event_rows in sorted(grouped.items()):
        event_id = event_identifier(year, date, city, state, event_name)
        location = ", ".join(v for v in (city, state) if v)
        con.execute(
            """INSERT INTO events(event_id,season_id,event_name,event_date,location_text,city,region,
               venue_name,venue_address,show_type,chief_judge,competition_name,competition_level,
               event_guid,season_guid,org_competition_id,scores_released,recap_released,
               category_recap_released,practice,dci_event_url,dci_event_match_score,published_at,
               modified_at,official_recap_url,official_category_recap_url,official_performances_url,
               source_archive_path,event_status,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, year, event_name, date, location, city, state, None, None,
             "Historical total-score record", None, event_name, None, None, None, None,
             1, 0, 0, 0, None, None, None, None, None, None, source_url,
             f"github:GetHorizontal63/dci_score_history/{year}", "completed",
             "Historical total-score-only import; captions and judges unavailable."),
        )
        counts["events"] += 1
        con.execute(
            """INSERT INTO sources(event_id,source_type,url,retrieved_at,http_status,content_type,archive_path,sha256)
               VALUES(?,?,?,?,?,?,?,?)""",
            (event_id, "historical_totals_csv", source_url, dt.datetime.now(dt.timezone.utc).isoformat(),
             200, "text/csv", f"data/years/{year}_dci_data.csv", source_sha),
        )
        for division in ("World Class", "Open Class"):
            division_rows = [row for row in event_rows if row["division"] == division]
            if not division_rows:
                continue
            cursor = con.execute(
                "INSERT INTO event_classes(event_id,division_name,division_initials,round_code,round_name) VALUES(?,?,?,?,?)",
                (event_id, division, "WC" if division == "World Class" else "OC", None, None),
            )
            class_id = int(cursor.lastrowid)
            ordered = sorted(division_rows, key=lambda row: (-float(row["score"]), str(row["corps"])))
            for source_index, row in enumerate(ordered, 1):
                corps_id = ensure_corps(con, row["corps"], year)
                cursor = con.execute(
                    """INSERT INTO performances(event_class_id,corps_id,group_name,performed,no_score_reason,
                       total_score,total_score_text,placement,placement_tied,subtotal_score,
                       subtotal_score_text,subtotal_rank,source_performances_url,source_performance_index)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (class_id, corps_id, row["corps"], 1, None, row["score"], row["score_text"],
                     None, 0, None, None, None, source_url, source_index),
                )
                performance_id = int(cursor.lastrowid)
                con.execute(
                    """INSERT INTO score_values(performance_id,caption_definition_id,judge_assignment_id,
                       score_level,category_name,caption_name,subcaption_name,initials,score,score_text,
                       rank,source_order,is_penalty) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (performance_id, caption_id, None, "total", None, "Total", None, "TOT",
                     row["score"], row["score_text"], None, 1, 0),
                )
                counts["performances"] += 1
                counts["world" if division == "World Class" else "open"] += 1

            con.execute(
                """UPDATE performances AS p SET
                   placement=(SELECT 1+COUNT(*) FROM performances p2
                              WHERE p2.event_class_id=p.event_class_id AND p2.total_score>p.total_score),
                   placement_tied=(SELECT CASE WHEN COUNT(*)>1 THEN 1 ELSE 0 END FROM performances p3
                                   WHERE p3.event_class_id=p.event_class_id AND p3.total_score=p.total_score)
                   WHERE p.event_class_id=?""", (class_id,))
            con.execute(
                """UPDATE score_values SET rank=(SELECT placement FROM performances p
                   WHERE p.performance_id=score_values.performance_id)
                   WHERE performance_id IN (SELECT performance_id FROM performances WHERE event_class_id=?)
                   AND score_level='total'""", (class_id,))
    return counts


def refresh_coverage(con: sqlite3.Connection, year: int, stats: dict[str, int]) -> None:
    unique_corps = con.execute(
        """SELECT COUNT(DISTINCT corps_id) FROM performances p
           JOIN event_classes ec ON ec.event_class_id=p.event_class_id
           JOIN events e ON e.event_id=ec.event_id WHERE e.season_id=?""", (year,)).fetchone()[0]
    values = con.execute(
        """SELECT COUNT(*) FROM score_values sv JOIN performances p ON p.performance_id=sv.performance_id
           JOIN event_classes ec ON ec.event_class_id=p.event_class_id
           JOIN events e ON e.event_id=ec.event_id WHERE e.season_id=?""", (year,)).fetchone()[0]
    con.execute(
        """INSERT OR REPLACE INTO coverage(season_id,competitions_total,relevant_events,
           world_class_appearances,open_class_appearances,scored_performances,
           nonperformed_appearances,score_values,unique_corps,unique_judges,venues_found,
           venues_missing,source_2xx,source_failures,status,notes)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (year, stats["events"], stats["events"], stats["world"], stats["open"],
         stats["performances"], 0, values, unique_corps, 0, 0, stats["events"],
         stats["events"], 0, "historical_totals_only",
         "Totals/placements imported from dci_score_history. Caption, subcaption, judge, venue, event-name, penalty, and cancellation coverage is incomplete."),
    )
    con.execute(
        """INSERT INTO data_issues(season_year,event_id,performance_id,severity,issue_type,
           description,source_url,resolved) VALUES(?,NULL,NULL,'info','historical_totals_only',?,?,0)""",
        (year, "Historical season contains total scores and placements only. Detailed recap values and judges were not available from CompetitionSuite and were not fabricated.", RAW_TEMPLATE.format(year=year)),
    )
    con.execute(
        """INSERT INTO data_issues(season_year,event_id,performance_id,severity,issue_type,
           description,source_url,resolved) VALUES(?,NULL,NULL,'warning','historical_classification_limit',?,?,0)""",
        (year, "Historical source documentation notes rolling corrections to circuit/class designations. Explicit DCA/all-age records were excluded; remaining junior corps were normalized using source labels and known move-up years.", SOURCE_REPO),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--csv-dir", type=Path)
    args = parser.parse_args()
    if not args.database.is_file():
        raise SystemExit(f"Database not found: {args.database}")

    con = sqlite3.connect(args.database)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        with con:
            remove_previous_import(con)
            for year in YEARS:
                con.execute("INSERT OR REPLACE INTO seasons(season_id,year,included) VALUES(?,?,1)", (year, year))
                source_url, body = fetch_year(year, args.csv_dir)
                stats = import_rows(con, year, source_url, body)
                if stats["performances"] <= 0:
                    raise RuntimeError(f"No accepted historical performances for {year}")
                refresh_coverage(con, year, stats)
                print(f"{year}: events={stats['events']} performances={stats['performances']} world={stats['world']} open={stats['open']} skipped={stats['skipped']}", flush=True)
        con.execute("ANALYZE")
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        historical = con.execute("SELECT COUNT(*) FROM v_performance_summary WHERE season_year BETWEEN 2000 AND 2014").fetchone()[0]
        years = [r[0] for r in con.execute("SELECT season_id FROM coverage WHERE relevant_events>0 ORDER BY season_id")]
        if integrity != "ok" or historical <= 0:
            raise RuntimeError(f"validation failed: integrity={integrity!r}, historical={historical}")
        print(f"Historical import validated: {historical} appearances; integrity={integrity}; coverage={years}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
