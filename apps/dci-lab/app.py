import base64
import os
import re
import sqlite3
from functools import wraps
from pathlib import Path
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DCI_DB_PATH", BASE_DIR / "data" / "dci_scores_master.sqlite"))
SQL_PASSWORD = os.environ.get("DCI_SQL_PASSWORD", "")

app = Flask(__name__)
app.config.update(JSON_SORT_KEYS=False)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


def db():
    if not DB_PATH.exists():
        raise RuntimeError(f"Database not found at {DB_PATH}")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def rows(sql, params=()):
    with db() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def scalar(sql, params=()):
    with db() as con:
        value = con.execute(sql, params).fetchone()
        return value[0] if value else None


def basic_auth_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not SQL_PASSWORD:
            abort(404)
        auth = request.authorization
        if not auth or auth.password != SQL_PASSWORD:
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="DCI SQL"'})
        return fn(*args, **kwargs)
    return wrapped


@app.context_processor
def helpers():
    return {"years": list(range(2026, 2014, -1)), "fmt": lambda x: "" if x is None else f"{x:.3f}".rstrip("0").rstrip(".")}


@app.route("/")
def dashboard():
    year = request.args.get("year", 2026, type=int)
    latest = rows("""
        WITH ranked AS (
          SELECT corps_name,event_date,event_name,total_score,placement,
                 ROW_NUMBER() OVER (PARTITION BY corps_name ORDER BY event_date DESC,performance_id DESC) rn
          FROM v_performance_summary
          WHERE season_year=? AND performed=1 AND total_score IS NOT NULL
        )
        SELECT corps_name,event_date,event_name,total_score,placement
        FROM ranked WHERE rn=1 ORDER BY total_score DESC
    """, (year,))
    stats = {
        "events": scalar("SELECT COUNT(DISTINCT event_id) FROM v_performance_summary WHERE season_year=?", (year,)),
        "performances": scalar("SELECT COUNT(*) FROM v_performance_summary WHERE season_year=? AND performed=1", (year,)),
        "corps": scalar("SELECT COUNT(DISTINCT corps_name) FROM v_performance_summary WHERE season_year=?", (year,)),
        "top": latest[0] if latest else None,
    }
    colts = rows("""
        SELECT event_date,event_name,total_score,placement
        FROM v_performance_summary
        WHERE season_year=? AND corps_name='Colts' AND performed=1
        ORDER BY event_date,performance_id
    """, (year,))
    return render_template("dashboard.html", year=year, latest=latest, stats=stats, colts=colts)


@app.route("/corps/<path:corps_name>")
def corps(corps_name):
    exists = scalar("SELECT COUNT(*) FROM v_performance_summary WHERE lower(corps_name)=lower(?)", (corps_name,))
    if not exists:
        abort(404)
    canon = scalar("SELECT corps_name FROM v_performance_summary WHERE lower(corps_name)=lower(?) LIMIT 1", (corps_name,))
    history = rows("""
        WITH last_show AS (
          SELECT season_year,event_date,event_name,total_score,placement,
                 ROW_NUMBER() OVER (PARTITION BY season_year ORDER BY event_date DESC,performance_id DESC) rn
          FROM v_performance_summary WHERE corps_name=? AND performed=1
        ), summary AS (
          SELECT season_year,COUNT(*) shows,MIN(total_score) low,MAX(total_score) high,AVG(total_score) average
          FROM v_performance_summary WHERE corps_name=? AND performed=1 GROUP BY season_year
        )
        SELECT s.*,l.event_date last_date,l.event_name last_event,l.total_score last_score,l.placement last_place
        FROM summary s LEFT JOIN last_show l ON l.season_year=s.season_year AND l.rn=1
        ORDER BY s.season_year
    """, (canon, canon))
    scores = rows("""
        SELECT season_year,event_date,event_name,total_score,placement,event_id
        FROM v_performance_summary WHERE corps_name=? AND performed=1
        ORDER BY event_date,performance_id
    """, (canon,))
    rivals = rows("""
        WITH mine AS (
          SELECT event_id,division_name,total_score FROM v_performance_summary
          WHERE corps_name=? AND performed=1
        )
        SELECT p.corps_name,COUNT(*) meetings,
               SUM(CASE WHEN m.total_score>p.total_score THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN m.total_score<p.total_score THEN 1 ELSE 0 END) losses,
               ROUND(AVG(m.total_score-p.total_score),3) avg_margin
        FROM mine m JOIN v_performance_summary p
          ON p.event_id=m.event_id AND p.division_name=m.division_name
        WHERE p.corps_name<>? AND p.performed=1
        GROUP BY p.corps_name HAVING COUNT(*)>=3
        ORDER BY meetings DESC,ABS(avg_margin) ASC LIMIT 20
    """, (canon, canon))
    return render_template("corps.html", corps=canon, history=history, scores=scores, rivals=rivals)


@app.route("/head-to-head")
def head_to_head():
    a = request.args.get("a", "Colts")
    b = request.args.get("b", "Troopers")
    meetings = rows("""
        SELECT a.season_year,a.event_date,a.event_name,a.total_score score_a,b.total_score score_b,
               ROUND(a.total_score-b.total_score,3) margin,a.placement place_a,b.placement place_b
        FROM v_performance_summary a JOIN v_performance_summary b
          ON a.event_id=b.event_id AND a.division_name=b.division_name
        WHERE lower(a.corps_name)=lower(?) AND lower(b.corps_name)=lower(?)
          AND a.performed=1 AND b.performed=1
        ORDER BY a.event_date,a.performance_id
    """, (a, b))
    summary = {
        "meetings": len(meetings),
        "a_wins": sum(1 for r in meetings if r["margin"] > 0),
        "b_wins": sum(1 for r in meetings if r["margin"] < 0),
        "ties": sum(1 for r in meetings if r["margin"] == 0),
        "avg_margin": round(sum(r["margin"] for r in meetings) / len(meetings), 3) if meetings else None,
    }
    return render_template("head_to_head.html", a=a, b=b, meetings=meetings, summary=summary)


@app.route("/records")
def records():
    closest = rows("""
        SELECT a.season_year,a.event_date,a.event_name,a.corps_name corps_a,b.corps_name corps_b,
               a.total_score score_a,b.total_score score_b,ROUND(ABS(a.total_score-b.total_score),3) margin
        FROM v_performance_summary a JOIN v_performance_summary b
          ON a.event_id=b.event_id AND a.division_name=b.division_name AND a.performance_id<b.performance_id
        WHERE a.performed=1 AND b.performed=1 AND a.total_score IS NOT NULL AND b.total_score IS NOT NULL
        ORDER BY margin ASC,a.event_date DESC LIMIT 50
    """)
    highs = rows("""
        SELECT season_year,event_date,event_name,corps_name,total_score,placement
        FROM v_performance_summary WHERE performed=1 AND total_score IS NOT NULL
        ORDER BY total_score DESC,event_date DESC LIMIT 50
    """)
    jumps = rows("""
        WITH ordered AS (
          SELECT corps_name,season_year,event_date,event_name,total_score,
                 LAG(total_score) OVER (PARTITION BY corps_name,season_year ORDER BY event_date,performance_id) previous_score
          FROM v_performance_summary WHERE performed=1 AND total_score IS NOT NULL
        )
        SELECT *,ROUND(total_score-previous_score,3) jump FROM ordered
        WHERE previous_score IS NOT NULL ORDER BY jump DESC LIMIT 50
    """)
    return render_template("records.html", closest=closest, highs=highs, jumps=jumps)


@app.route("/query")
def query_page():
    corps_name = request.args.get("corps", "Colts")
    year = request.args.get("year", type=int)
    opponent = request.args.get("opponent", "")
    params = [corps_name]
    where = ["lower(a.corps_name)=lower(?)", "a.performed=1"]
    join = ""
    select = "a.season_year,a.event_date,a.event_name,a.division_name,a.placement,a.total_score"
    if year:
        where.append("a.season_year=?")
        params.append(year)
    if opponent:
        join = "JOIN v_performance_summary b ON b.event_id=a.event_id AND b.division_name=a.division_name"
        where.extend(["lower(b.corps_name)=lower(?)", "b.performed=1"])
        params.append(opponent)
        select += ",b.placement opponent_place,b.total_score opponent_score,ROUND(a.total_score-b.total_score,3) margin"
    result = rows(f"SELECT {select} FROM v_performance_summary a {join} WHERE {' AND '.join(where)} ORDER BY a.event_date,a.performance_id LIMIT 1000", params)
    corps_list = rows("SELECT DISTINCT corps_name FROM v_performance_summary ORDER BY corps_name")
    return render_template("query.html", result=result, corps_name=corps_name, year=year, opponent=opponent, corps_list=corps_list)


BLOCKED = re.compile(r"\b(attach|detach|pragma|insert|update|delete|drop|alter|create|replace|vacuum|reindex|trigger)\b", re.I)


@app.route("/sql", methods=["GET", "POST"])
@basic_auth_required
def sql_console():
    sql = request.form.get("sql", "SELECT * FROM v_performance_summary WHERE corps_name='Colts' ORDER BY event_date DESC LIMIT 25")
    result, error, columns = [], None, []
    if request.method == "POST":
        cleaned = sql.strip()
        if not re.match(r"^(select|with)\b", cleaned, re.I) or BLOCKED.search(cleaned) or ";" in cleaned.rstrip(";"):
            error = "Only one read-only SELECT or WITH query is allowed."
        else:
            try:
                with db() as con:
                    cur = con.execute(cleaned)
                    columns = [d[0] for d in cur.description] if cur.description else []
                    result = [dict(r) for r in cur.fetchmany(1000)]
            except Exception as exc:
                error = str(exc)
    return render_template("sql.html", sql=sql, result=result, columns=columns, error=error)


@app.route("/api/corps/<path:corps_name>/scores")
def corps_scores_api(corps_name):
    return jsonify(rows("""
        SELECT season_year,event_date,event_name,total_score,placement
        FROM v_performance_summary WHERE lower(corps_name)=lower(?) AND performed=1
        ORDER BY event_date,performance_id
    """, (corps_name,)))


@app.route("/health")
def health():
    try:
        ok = scalar("PRAGMA integrity_check")
        return jsonify(status="ok", database=str(DB_PATH), integrity=ok)
    except Exception as exc:
        return jsonify(status="error", error=str(exc)), 500


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "3001")), debug=False)
