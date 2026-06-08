#!/usr/bin/env python3
"""
One-time migration: copy modes + leads + queue_entries from the old SQLite
volume DB into Postgres. Idempotent (upserts by unique key).

Run inside the web container (which still has /data/pipeline.db AND DATABASE_URL):
    /opt/venv/bin/python scripts/migrate_sqlite_to_pg.py
    /opt/venv/bin/python scripts/migrate_sqlite_to_pg.py --sqlite /data/pipeline.db
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

import db  # noqa: E402  (Postgres layer — DATABASE_URL must be set)


def main():
    ap = argparse.ArgumentParser(description="Migrate SQLite pipeline DB into Postgres")
    ap.add_argument("--sqlite", default="/data/pipeline.db", help="Path to old SQLite DB")
    args = ap.parse_args()

    src_path = Path(args.sqlite)
    if not src_path.exists():
        sys.exit(f"ERROR: SQLite file not found: {src_path}")

    if not db.DATABASE_URL:
        sys.exit("ERROR: DATABASE_URL is not set (need the Postgres target).")

    print(f"Source SQLite : {src_path}")
    print(f"Target Postgres: {db.DB_PATH}\n")

    db.init_db()  # ensure Postgres tables exist

    src = sqlite3.connect(str(src_path))
    src.row_factory = sqlite3.Row

    def table_exists(name: str) -> bool:
        return src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    # ── modes ──
    n_modes = 0
    if table_exists("modes"):
        for r in src.execute("SELECT * FROM modes").fetchall():
            db.upsert_mode(dict(r))
            n_modes += 1
    print(f"modes migrated  : {n_modes}")

    # ── leads ──
    n_leads = 0
    if table_exists("leads"):
        for r in src.execute("SELECT * FROM leads").fetchall():
            db.upsert_lead(dict(r))
            n_leads += 1
    print(f"leads migrated  : {n_leads}")

    # ── queue_entries ──
    n_queues = 0
    if table_exists("queue_entries"):
        cols = {c[1] for c in src.execute("PRAGMA table_info(queue_entries)").fetchall()}
        for r in src.execute("SELECT * FROM queue_entries").fetchall():
            d = dict(r)
            try:
                qjson = json.loads(d.get("queue_json") or "[]")
            except Exception:
                qjson = []
            mode = d.get("mode") if "mode" in cols else "sg-daily"
            db.save_queue(d["run_date"], qjson, d.get("queue_md") or "", mode or "sg-daily")
            n_queues += 1
    print(f"queues migrated : {n_queues}")

    src.close()

    # ── verify ──
    modes = db.get_modes()
    leads = db.get_leads()
    print("\nPostgres now has:")
    print(f"  modes: {[(m['name'], 'active' if m['is_active'] else 'inactive') for m in modes]}")
    print(f"  leads: {len(leads)} "
          f"({sum(1 for l in leads if l['status']=='done')} done, "
          f"{sum(1 for l in leads if l['status']=='pending')} pending)")
    print("\nDone.")


if __name__ == "__main__":
    main()
