"""SQLite database layer for the AI Sales pipeline."""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent

# DB_PATH: /data/pipeline.db on Railway (persistent volume), data/pipeline.db locally
_default_db = BASE_DIR / "data" / "pipeline.db"
DB_PATH = Path(os.environ.get("DB_PATH", str(_default_db)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url              TEXT    UNIQUE NOT NULL,
    company_name     TEXT,
    priority         TEXT    DEFAULT 'medium',
    industry_hint    TEXT,
    notes            TEXT,
    assigned_to      TEXT,
    mode             TEXT    DEFAULT 'sg-daily',
    lead_category    TEXT,
    status           TEXT    DEFAULT 'pending',
    prospect_score   REAL,
    grade            TEXT,
    label            TEXT,
    ooh_presence     TEXT,
    sg_usp           TEXT,
    key_decision_maker TEXT,
    recommended_action TEXT,
    outreach_status  TEXT    DEFAULT 'pending',
    outreach_sent_date TEXT,
    analysis_date    TEXT,
    output_folder    TEXT,
    error_message    TEXT,
    analysis_json    TEXT,
    created_at       TEXT    DEFAULT (datetime('now')),
    updated_at       TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type     TEXT,
    started_at   TEXT,
    completed_at TEXT,
    discovered   INTEGER DEFAULT 0,
    analyzed     INTEGER DEFAULT 0,
    queued       INTEGER DEFAULT 0,
    status       TEXT    DEFAULT 'running',
    error_log    TEXT
);

CREATE TABLE IF NOT EXISTS queue_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date   TEXT    NOT NULL,
    queue_json TEXT,
    queue_md   TEXT,
    report_sent INTEGER DEFAULT 0,
    created_at  TEXT   DEFAULT (datetime('now'))
);
"""


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)


# ── Lead CRUD ─────────────────────────────────────────────────────────────────

def upsert_lead(lead: dict):
    """Insert or update a lead row. URL is the unique key."""
    fields = [
        "url", "company_name", "priority", "industry_hint", "notes",
        "assigned_to", "mode", "lead_category", "status",
        "prospect_score", "grade", "label", "ooh_presence", "sg_usp",
        "key_decision_maker", "recommended_action", "outreach_status",
        "outreach_sent_date", "analysis_date", "output_folder",
        "error_message", "analysis_json",
    ]
    row = {f: lead.get(f) for f in fields if f in lead or f == "url"}
    row["updated_at"] = datetime.now().isoformat()

    cols   = ", ".join(row.keys())
    params = ", ".join(f":{k}" for k in row.keys())
    update = ", ".join(f"{k}=excluded.{k}" for k in row.keys() if k != "url")

    sql = f"""
        INSERT INTO leads ({cols}) VALUES ({params})
        ON CONFLICT(url) DO UPDATE SET {update}
    """
    with get_db() as conn:
        conn.execute(sql, row)


def get_leads(status: str | None = None, mode: str | None = None) -> list[dict]:
    """Return leads as list of dicts, optionally filtered."""
    sql    = "SELECT * FROM leads WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if mode:
        sql += " AND mode=?"
        params.append(mode)
    sql += " ORDER BY created_at ASC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_analyses() -> list[dict]:
    """
    Return all leads that have a completed analysis_json,
    sorted by prospect_score descending. Adds _slug/_md_path/_pdf_path
    to keep Streamlit pages working without changes.
    """
    sql = """
        SELECT * FROM leads
        WHERE analysis_json IS NOT NULL AND status='done'
        ORDER BY prospect_score DESC NULLS LAST
    """
    with get_db() as conn:
        rows = conn.execute(sql).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        try:
            analysis = json.loads(d["analysis_json"])
        except Exception:
            analysis = {}
        analysis.update({k: d[k] for k in d if k not in analysis or d[k] is not None})
        # Restore file path helpers for UI
        folder = d.get("output_folder") or ""
        analysis["_slug"]     = Path(folder).name if folder else ""
        analysis["_md_path"]  = str(Path(folder) / "PROSPECT-ANALYSIS.md") if folder else ""
        analysis["_pdf_path"] = str(Path(folder) / "prospect-analysis.pdf") if folder else ""
        results.append(analysis)
    return results


def get_existing_urls() -> set[str]:
    with get_db() as conn:
        rows = conn.execute("SELECT url FROM leads").fetchall()
    return {r["url"].strip().lower() for r in rows}


def mark_outreach_sent(url: str, status: str = "sent"):
    with get_db() as conn:
        conn.execute(
            "UPDATE leads SET outreach_status=?, outreach_sent_date=?, updated_at=? WHERE url=?",
            (status, datetime.now().strftime("%Y-%m-%d"), datetime.now().isoformat(), url),
        )


# ── Pipeline run logging ──────────────────────────────────────────────────────

def start_pipeline_run(run_type: str = "cron") -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO pipeline_runs (run_type, started_at, status) VALUES (?, ?, 'running')",
            (run_type, datetime.now().isoformat()),
        )
        return cur.lastrowid


def finish_pipeline_run(run_id: int, discovered: int, analyzed: int,
                         queued: int, status: str = "completed", error_log: str = ""):
    with get_db() as conn:
        conn.execute(
            """UPDATE pipeline_runs
               SET completed_at=?, discovered=?, analyzed=?, queued=?,
                   status=?, error_log=?
               WHERE id=?""",
            (datetime.now().isoformat(), discovered, analyzed, queued,
             status, error_log, run_id),
        )


def get_pipeline_runs(limit: int = 30) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Queue entries ─────────────────────────────────────────────────────────────

def save_queue(run_date: str, queue_json: list, queue_md: str):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO queue_entries (run_date, queue_json, queue_md)
               VALUES (?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (run_date, json.dumps(queue_json), queue_md),
        )


def get_queue(run_date: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM queue_entries WHERE run_date=? ORDER BY id DESC LIMIT 1",
            (run_date,),
        ).fetchone()
    return dict(row) if row else None


# ── Bootstrap ─────────────────────────────────────────────────────────────────

init_db()
