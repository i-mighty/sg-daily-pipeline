"""PostgreSQL database layer for the AI Sales pipeline.

Drop-in replacement for the previous SQLite layer — same public function API,
single-tenant. Both the web dashboard and the cron worker share one database,
which is why this moved off a per-service SQLite volume.

Env:
  DATABASE_URL — Postgres connection string (Railway provides this; both the
                 web and cron services reference ${{Postgres.DATABASE_URL}}).
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

BASE_DIR = Path(__file__).parent

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _display_url(url: str) -> str:
    """Credential-free connection string for log messages."""
    if not url:
        return "<no DATABASE_URL>"
    try:
        p = urlparse(url)
        return f"postgres://{p.hostname}:{p.port or 5432}{p.path}"
    except Exception:
        return "postgres"


# Kept for backward-compat with callers that print db.DB_PATH (e.g. discover_leads).
DB_PATH = _display_url(DATABASE_URL)


SCHEMA = """
CREATE TABLE IF NOT EXISTS modes (
    id               SERIAL PRIMARY KEY,
    name             TEXT    UNIQUE NOT NULL,
    label            TEXT    NOT NULL,
    description      TEXT    DEFAULT '',
    analysis_prompt  TEXT    NOT NULL DEFAULT '',
    discovery_prompt TEXT    NOT NULL DEFAULT '',
    discover_count   INTEGER DEFAULT 5,
    queue_size       INTEGER DEFAULT 8,
    is_active        INTEGER DEFAULT 1,
    created_at       TEXT    DEFAULT (now()::text),
    updated_at       TEXT    DEFAULT (now()::text)
);

CREATE TABLE IF NOT EXISTS leads (
    id               SERIAL PRIMARY KEY,
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
    created_at       TEXT    DEFAULT (now()::text),
    updated_at       TEXT    DEFAULT (now()::text)
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           SERIAL PRIMARY KEY,
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
    id         SERIAL PRIMARY KEY,
    run_date   TEXT    NOT NULL,
    mode       TEXT    DEFAULT 'sg-daily',
    queue_json TEXT,
    queue_md   TEXT,
    report_sent INTEGER DEFAULT 0,
    created_at  TEXT   DEFAULT (now()::text)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_run_date_mode ON queue_entries(run_date, mode);
"""


@contextmanager
def get_db():
    """Yield a psycopg2 connection (RealDictCursor). Commits on success, rolls back on error."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set.")
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _all(sql: str, params=None) -> list[dict]:
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return [dict(r) for r in cur.fetchall()]


def _one(sql: str, params=None) -> dict | None:
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
    return dict(row) if row else None


def _exec(sql: str, params=None):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())


def _seed_default_modes():
    """Insert default modes if the table is empty."""
    if _one("SELECT 1 AS x FROM modes LIMIT 1"):
        return
    try:
        from default_modes import DEFAULT_MODES
    except ImportError:
        return
    for m in DEFAULT_MODES:
        _exec(
            """INSERT INTO modes
               (name, label, description, analysis_prompt, discovery_prompt,
                discover_count, queue_size, is_active)
               VALUES (%(name)s,%(label)s,%(description)s,%(analysis_prompt)s,
                       %(discovery_prompt)s,%(discover_count)s,%(queue_size)s,%(is_active)s)
               ON CONFLICT(name) DO NOTHING""",
            m,
        )


def init_db():
    """Create tables (idempotent) and seed default modes if empty."""
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
    _seed_default_modes()


# ── Mode CRUD ─────────────────────────────────────────────────────────────────

def get_modes(active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM modes"
    if active_only:
        sql += " WHERE is_active=1"
    sql += " ORDER BY id ASC"
    return _all(sql)


def get_mode(name: str) -> dict | None:
    return _one("SELECT * FROM modes WHERE name=%s", (name,))


def upsert_mode(mode: dict):
    fields = ["name", "label", "description", "analysis_prompt", "discovery_prompt",
              "discover_count", "queue_size", "is_active"]
    row = {f: mode.get(f) for f in fields}
    row["updated_at"] = datetime.now().isoformat()
    cols   = ", ".join(row.keys())
    params = ", ".join(f"%({k})s" for k in row.keys())
    update = ", ".join(f"{k}=EXCLUDED.{k}" for k in row if k != "name")
    _exec(
        f"INSERT INTO modes ({cols}) VALUES ({params}) "
        f"ON CONFLICT(name) DO UPDATE SET {update}",
        row,
    )


def delete_mode(name: str):
    _exec("DELETE FROM modes WHERE name=%s", (name,))


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
    params = ", ".join(f"%({k})s" for k in row.keys())
    update = ", ".join(f"{k}=EXCLUDED.{k}" for k in row.keys() if k != "url")

    _exec(
        f"INSERT INTO leads ({cols}) VALUES ({params}) "
        f"ON CONFLICT(url) DO UPDATE SET {update}",
        row,
    )


def get_leads(status: str | None = None, mode: str | None = None) -> list[dict]:
    """Return leads as list of dicts, optionally filtered."""
    sql    = "SELECT * FROM leads WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=%s"
        params.append(status)
    if mode:
        sql += " AND mode=%s"
        params.append(mode)
    sql += " ORDER BY created_at ASC"
    return _all(sql, params)


def get_analyses() -> list[dict]:
    """
    Return all leads that have a completed analysis_json,
    sorted by prospect_score descending. Adds _slug/_md_path/_pdf_path
    to keep Streamlit pages working without changes.
    """
    rows = _all(
        "SELECT * FROM leads "
        "WHERE analysis_json IS NOT NULL AND status='done' "
        "ORDER BY prospect_score DESC NULLS LAST"
    )

    results = []
    for d in rows:
        try:
            analysis = json.loads(d["analysis_json"])
        except Exception:
            analysis = {}
        analysis.update({k: d[k] for k in d if k not in analysis or d[k] is not None})
        folder = d.get("output_folder") or ""
        analysis["_slug"]     = Path(folder).name if folder else ""
        analysis["_md_path"]  = str(Path(folder) / "PROSPECT-ANALYSIS.md") if folder else ""
        analysis["_pdf_path"] = str(Path(folder) / "prospect-analysis.pdf") if folder else ""
        results.append(analysis)
    return results


def get_existing_urls() -> set[str]:
    return {r["url"].strip().lower() for r in _all("SELECT url FROM leads")}


def mark_outreach_sent(url: str, status: str = "sent"):
    _exec(
        "UPDATE leads SET outreach_status=%s, outreach_sent_date=%s, updated_at=%s WHERE url=%s",
        (status, datetime.now().strftime("%Y-%m-%d"), datetime.now().isoformat(), url),
    )


# ── Pipeline run logging ──────────────────────────────────────────────────────

def start_pipeline_run(run_type: str = "cron") -> int:
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_runs (run_type, started_at, status) "
            "VALUES (%s, %s, 'running') RETURNING id",
            (run_type, datetime.now().isoformat()),
        )
        return cur.fetchone()["id"]


def finish_pipeline_run(run_id: int, discovered: int, analyzed: int,
                        queued: int, status: str = "completed", error_log: str = ""):
    _exec(
        """UPDATE pipeline_runs
           SET completed_at=%s, discovered=%s, analyzed=%s, queued=%s,
               status=%s, error_log=%s
           WHERE id=%s""",
        (datetime.now().isoformat(), discovered, analyzed, queued,
         status, error_log, run_id),
    )


def get_pipeline_runs(limit: int = 30) -> list[dict]:
    return _all("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT %s", (limit,))


# ── Queue entries ─────────────────────────────────────────────────────────────

def save_queue(run_date: str, queue_json: list, queue_md: str, mode: str = "sg-daily"):
    _exec(
        """INSERT INTO queue_entries (run_date, queue_json, queue_md, mode)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT(run_date, mode) DO UPDATE SET
               queue_json=EXCLUDED.queue_json,
               queue_md=EXCLUDED.queue_md""",
        (run_date, json.dumps(queue_json), queue_md, mode),
    )


def get_queue(run_date: str, mode: str = "sg-daily") -> dict | None:
    return _one(
        "SELECT * FROM queue_entries WHERE run_date=%s AND mode=%s ORDER BY id DESC LIMIT 1",
        (run_date, mode),
    )


def get_all_queues(run_date: str) -> list[dict]:
    """Return all mode queues for a given date."""
    return _all("SELECT * FROM queue_entries WHERE run_date=%s ORDER BY mode ASC", (run_date,))


# ── Bootstrap ─────────────────────────────────────────────────────────────────

if DATABASE_URL:
    init_db()
