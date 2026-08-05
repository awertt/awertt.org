#!/usr/bin/env python3
"""Append 2000-2014 DCI junior-corps totals to the live SQLite database.

Historical rows provide total scores and placements only. The source's `Class`
column is not used for DCI division assignment because it changes for the same
corps between events. Corps are instead assigned by their known Division I /
World Class move-up season; other non-all-age junior corps are Open Class.
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
USER_AGENT = "awertt-dci-history-import/3.0 (+https://awertt.org/dci/)"


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).casefold()).strip()


ALIASES = {
    "academy": "The Academy",
    "cadets": "The Cadets",
    "cadets of bergen county": "The Cadets",
    "cavaliers": "The Cavaliers",
    "crown": "Carolina Crown",
    "sc vanguard": "Santa Clara Vanguard",
    "scv": "Santa Clara Vanguard",
    "santa clara": "Santa Clara Vanguard",
    "vanguard": "Santa Clara Vanguard",
    "cascades": "Seattle Cascades",
    "spirit": "Spirit of Atlanta",
    "spirit from jsu": "Spirit of Atlanta",
    "magic": "The Magic",
    "magic of orlando": "The Magic",
    "capital regiment drum bugle corps": "Capital Regiment",
}

ALL_AGE = {key(name) for name in {
    "Alliance", "Atlanta CV", "Atlanta CorpsVets", "Brigadiers", "Syracuse Brigadiers",
    "Bushwackers", "Caballeros", "Hawthorne Caballeros", "Carolina Gold", "Chops Inc",
    "Connecticut Hurricanes", "CorpsVets", "Empire Statesmen", "Excelsior", "Fusion Core",
    "Govenaires", "Govies", "Grenadiers", "Kingston Grenadiers", "Heat Wave",
    "Heat Wave of Florida", "Hurricanes", "Kilties", "Decorah Kilties", "Minnesota Brass",
    "New York Skyliners", "Skyliners", "Reading Buccaneers", "Renegades",
    "Rochester Crusaders", "Sunrisers", "White Sabers", "SoCal Dream", "Dream",
    "Frontier", "Gulf Coast Sound", "Lakeshoremen", "Music City Legend",
    "Tampa Bay Thunder", "Shenandoah Sound", "Sun Devils", "Vigilantes",
    "High Country Brass", "Cincinnati Tradition", "Kidsgrove Scouts", "Cadets2",
    "Heartliner", "Mon Valley Express", "Steel City Ambassadors", "Windsor Regiment",
    "Yankee Rebels", "Archer-Epler Musketeers", "Blessed Sacrament Golden Knights",
}}

WORLD_FIXED = {key(name) for name in {
    "Blue Devils", "Blue Knights", "Bluecoats", "Boston Crusaders", "Carolina Crown",
    "Colts", "Crossmen", "Glassmen", "Kiwanis Kavaliers", "Madison Scouts",
    "Phantom Regiment", "Pioneer", "Southwind", "Spirit of Atlanta", "Tarheel Sun",
    "The Cadets", "The Cavaliers", "The Magic", "Troopers", "Santa Clara Vanguard",
}}

WORLD_START = {key(name): year for name, year in {
    "Capital Regiment": 2002,
    "Seattle Cascades": 2002,
    "Mandarins": 2003,
    "Pacific Crest": 2003,
    "Esperanza": 2004,
    "Blue Stars": 2006,
    "The Academy": 2007,
    "Jersey Surf": 2009,
    "Teal Sound": 2010,
    "Oregon Crusaders": 2013,
}.items()}


def canonical_corps(name: str) -> str:
    stripped = clean(name)
    return ALIASES.get(key(stripped), stripped)


def column(row: dict[str, str], *names: str) -> str:
    lowered = {key(name): value for name, value in row.items()}
    for name in names:
        if key(name) in lowered:
            return clean(lowered[key(name)])
    return ""


def parse_date(value: str, year: int) -> str:
    value = clean(value)
    if re.fullmatch(r"\d{1,2}/\d{1,2}", value):
        month, day = map(int, value.split("/"))
        return dt.date(year, month, day).isoformat()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(value)


def parse_score(value: str) -> float | None:
    value = clean(value).replace("*", "")
    if not value or key(value) in {"n a", "na", "exh", "exhibition", "dns", "dnf"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    score = float(match.group())
    return score if 0 <= score <= 100 else None


def division_for(source_class: str, corps_name: str, year: int) -> str | None:
    source_key = key(source_class)
    corps_key = key(canonical_corps(corps_name))
    if any(token in source_key for token in ("dca", "all age", "senior", "mini corps", "alumni", "soundsport")):
        return None
    if corps_key in ALL_AGE:
        return None
    if corps_key in WORLD_FIXED:
        return "World Class"
    move_up = WORLD_START.get(corps_key)
    if move_up is not None and year >= move_up:
        return "World Class"
    return "Open Class"


def fetch_csv(year: int) -> tuple[str, bytes]:
    url = RAW_TEMPLATE.format(year=year)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
        if int(getattr(response, "status", 200)) != 200:
            raise RuntimeError(f"HTTP {response.status} for {url}")
    return url, body


def event_id_for(year: int, date: str, city: str, state: str, event_name: str) -> str:
    payload = "|".join((str(year), date, key(city), key(state), key(event_name)))
    return "hist-gh-" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def remove_previous(con: sqlite3.Connection) -> None:
    ids = [row[0] for row in con.execute("SELECT event_id FROM events WHERE event_id LIKE 'hist-gh-%'")]
    if ids:
        marks = ",".join("?" for _ in ids)
        class_ids = [row[0] for row in con.execute(f"SELECT event_class_id FROM event_classes WHERE event_id IN ({marks})", ids)]
        if class_ids:
            cmarks = ",".join("?" for _ in class_ids)
            perf_ids = [row[0] for row in con.execute(f"SELECT performance_id FROM performances WHERE event_class_id IN ({cmarks})", class_ids)]
            if perf_ids:
                pmarks = ",".join("?" for _ in perf_ids)
                con.execute(f"DELETE FROM score_values WHERE performance_id IN ({pmarks})", perf_ids)
                con.execute(f"DELETE FROM performances WHERE performance_id IN ({pmarks})", perf_ids)
            con.execute(f"DELETE FROM scheduled_appearances WHERE event_class_id IN ({cmarks})", class_ids)
            con.execute(f"DELETE FROM judge_assignments WHERE event_class_id IN ({cmarks})", class_ids)
            con.execute(f"DELETE FROM event_classes WHERE event_class_id IN ({cmarks})", class_ids)
        con.execute(f"DELETE FROM event_interruptions WHERE event_id IN ({marks})", ids)
        con.execute(f"DELETE FROM events WHERE event_id IN ({marks})", ids)
    con.execute("DELETE FROM coverage WHERE season_year BETWEEN 2000 AND 2014")
    con.execute("DELETE FROM seasons WHERE season_year BETWEEN 2000 AND 2014")
    con.execute("DROP TABLE IF EXISTS historical_import_notes")


def corps_id_for(con: sqlite3.Connection, name: str) -> int:
    row = con.execute("SELECT corps_id FROM corps WHERE lower(corps_name)=lower(?) LIMIT 1", (name,)).fetchone()
    if row:
        return int(row[0])
    return int(con.execute("INSERT INTO corps(corps_name,org_group_identifier) VALUES(?,NULL)", (name,)).lastrowid)


def read_year(year: int) -> tuple[str, bytes, list[dict[str, object]], int]:
    source_url, body = fetch_csv(year)
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig", errors="replace")))
    accepted: dict[tuple[object, ...], dict[str, object]] = {}
    skipped = 0
    for raw in reader:
        try:
            if int(column(raw, "Year") or year) != year:
                continue
            date = parse_date(column(raw, "Date"), year)
            city = column(raw, "City") or "Unknown city"
            state = column(raw, "State", "Region")
            corps = canonical_corps(column(raw, "Corps", "Group"))
            division = division_for(column(raw, "Class", "Division"), corps, year)
            score = parse_score(column(raw, "Score", "Total"))
            event_name = column(raw, "Event_Name", "Event", "Show")
            if not event_name:
                event_name = f"Historical DCI score event - {', '.join(v for v in (city, state) if v)}"
            if not corps or division is None or score is None:
                skipped += 1
                continue
            item = {"date": date, "city": city, "state": state, "corps": corps,
                    "division": division, "score": score, "event_name": event_name}
            dedupe = (date, key(city), key(state), key(event_name), key(corps), division, score)
            accepted[dedupe] = item
        except Exception:
            skipped += 1
    return source_url, body, list(accepted.values()), skipped


def load_year(con: sqlite3.Connection, year: int) -> dict[str, int]:
    source_url, body, accepted, skipped = read_year(year)
    grouped: dict[tuple[str, str, str, str], list[dict[str, object]]] = defaultdict(list)
    for item in accepted:
        grouped[(str(item["date"]), str(item["city"]), str(item["state"]), str(item["event_name"]))].append(item)

    con.execute("INSERT INTO seasons(season_year,source_season_guid) VALUES(?,NULL)", (year,))
    stats = {"events": 0, "appearances": 0, "world": 0, "open": 0, "skipped": skipped}

    for (date, city, state, event_name), event_rows in sorted(grouped.items()):
        event_id = event_id_for(year, date, city, state, event_name)
        location = ", ".join(value for value in (city, state) if value)
        con.execute(
            """INSERT INTO events(event_id,season_year,event_date,event_name,competition_name,
               location_text,city,state,venue,chief_judge,competition_guid,event_guid,source_url,
               recap_url,category_recap_url,performances_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, year, date, event_name, event_name, location, city, state, None, None,
             None, None, source_url, None, None, source_url),
        )
        stats["events"] += 1
        for division in ("World Class", "Open Class"):
            division_rows = [item for item in event_rows if item["division"] == division]
            if not division_rows:
                continue
            class_id = int(con.execute(
                "INSERT INTO event_classes(event_id,division_name,division_initials,round_name) VALUES(?,?,?,NULL)",
                (event_id, division, "WC" if division == "World Class" else "OC"),
            ).lastrowid)
            ordered = sorted(division_rows, key=lambda item: (-float(item["score"]), str(item["corps"])))
            counts: dict[float, int] = defaultdict(int)
            for item in ordered:
                counts[float(item["score"])] += 1
            for source_order, item in enumerate(ordered, 1):
                score = float(item["score"])
                placement = 1 + sum(float(other["score"]) > score for other in ordered)
                tied = int(counts[score] > 1)
                corps_id = corps_id_for(con, str(item["corps"]))
                performance_id = int(con.execute(
                    """INSERT INTO performances(event_class_id,corps_id,round_name,placement,
                       placement_tied,subtotal_score,subtotal_rank,total_score,performed,no_score_reason,
                       source_order) VALUES(?,?,NULL,?,?,NULL,NULL,?,1,NULL,?)""",
                    (class_id, corps_id, placement, tied, score, source_order),
                ).lastrowid)
                con.execute(
                    """INSERT INTO score_values(performance_id,value_type,category_name,caption_name,
                       subcaption_name,initials,score,rank,judge_id,source_order)
                       VALUES(?,'total',NULL,'Total',NULL,'TOT',?,?,NULL,1)""",
                    (performance_id, score, placement),
                )
                con.execute(
                    """INSERT INTO scheduled_appearances(event_id,event_class_id,corps_id,performed,
                       no_score_reason,source_performance_id) VALUES(?,?,?,1,NULL,?)""",
                    (event_id, class_id, corps_id, str(performance_id)),
                )
                stats["appearances"] += 1
                stats["world" if division == "World Class" else "open"] += 1

    con.execute(
        """INSERT INTO coverage(season_year,competitions,events_with_target_divisions,appearances,
           scored_performances,nonperformed_appearances,score_values,judge_assignments)
           VALUES(?,?,?,?,?,0,?,0)""",
        (year, stats["events"], stats["events"], stats["appearances"], stats["appearances"], stats["appearances"]),
    )
    con.execute(
        """INSERT INTO historical_import_notes(season_year,source_url,source_sha256,imported_at,
           coverage_level,notes) VALUES(?,?,?,?,?,?)""",
        (year, source_url, hashlib.sha256(body).hexdigest(), dt.datetime.now(dt.timezone.utc).isoformat(),
         "totals_and_placements_only",
         "Class assignment uses known World Class move-up seasons because the source Class field varies by event. Detailed captions, judges, penalties, venues, event names, missed appearances, and interruptions are incomplete."),
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    if not args.database.is_file():
        raise SystemExit(f"Database not found: {args.database}")
    con = sqlite3.connect(args.database)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        with con:
            remove_previous(con)
            con.execute(
                """CREATE TABLE historical_import_notes(season_year INTEGER PRIMARY KEY,
                   source_url TEXT NOT NULL,source_sha256 TEXT NOT NULL,imported_at TEXT NOT NULL,
                   coverage_level TEXT NOT NULL,notes TEXT NOT NULL)"""
            )
            for year in YEARS:
                stats = load_year(con, year)
                if stats["appearances"] <= 0 or stats["world"] <= 0 or stats["open"] <= 0:
                    raise RuntimeError(f"Incomplete classified history for {year}: {stats}")
                print(f"{year}: events={stats['events']} appearances={stats['appearances']} world={stats['world']} open={stats['open']} skipped={stats['skipped']}", flush=True)
        con.execute("ANALYZE")
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        years = [row[0] for row in con.execute("SELECT DISTINCT season_year FROM v_performance_summary ORDER BY season_year")]
        historical = con.execute("SELECT COUNT(*) FROM v_performance_summary WHERE season_year BETWEEN 2000 AND 2014").fetchone()[0]
        ties = con.execute("SELECT COUNT(*) FROM v_performance_summary WHERE season_year BETWEEN 2000 AND 2014 AND placement_tied=1").fetchone()[0]
        required = list(range(2000, 2020)) + list(range(2022, 2027))
        if integrity != "ok" or years != required or historical <= 0:
            raise RuntimeError(f"Validation failed: integrity={integrity!r}, years={years!r}, historical={historical}")
        print(f"Validated: historical appearances={historical}; tied appearances={ties}; integrity={integrity}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
