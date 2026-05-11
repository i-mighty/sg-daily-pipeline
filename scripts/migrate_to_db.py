#!/usr/bin/env python3
"""
One-time migration: imports existing prospects.csv and results/*/prospect-data.json
into the SQLite database. Safe to run multiple times (upserts, won't duplicate).

Usage:
    python scripts/migrate_to_db.py
"""

import csv
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

import db  # noqa: E402 — initialises DB on import


def migrate():
    csv_path = BASE_DIR / "prospects.csv"
    results  = BASE_DIR / "results"

    # ── 1. Import prospects.csv ───────────────────────────────────────────────
    csv_count = 0
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                # Strip empty strings to None for numeric fields
                if row.get("prospect_score") == "":
                    row["prospect_score"] = None
                db.upsert_lead(row)
                csv_count += 1
        print(f"Imported {csv_count} rows from prospects.csv")
    else:
        print("prospects.csv not found — skipping")

    # ── 2. Import analysis JSON files ─────────────────────────────────────────
    json_count = 0
    for p in results.glob("*/prospect-data.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            url  = data.get("url", "")
            if not url:
                continue

            folder = str(p.parent)
            dm     = data.get("key_decision_maker", {})
            ooh    = data.get("ooh_presence", {})

            db.upsert_lead({
                "url":               url,
                "company_name":      data.get("company_name", ""),
                "mode":              data.get("mode", "sg-daily"),
                "lead_category":     data.get("lead_category", ""),
                "status":            "done",
                "prospect_score":    data.get("prospect_score"),
                "grade":             data.get("grade", ""),
                "label":             data.get("label", ""),
                "ooh_presence":      "Yes" if (isinstance(ooh, dict) and ooh.get("detected")) else "",
                "sg_usp":            data.get("sg_usp", ""),
                "key_decision_maker": dm.get("name", "") if isinstance(dm, dict) else "",
                "recommended_action": data.get("recommended_action", ""),
                "outreach_status":   data.get("outreach_status", "pending"),
                "analysis_date":     data.get("analysis_date", ""),
                "output_folder":     folder,
                "analysis_json":     json.dumps(data),
            })
            json_count += 1
        except Exception as e:
            print(f"  [skip] {p}: {e}")

    print(f"Imported {json_count} analysis JSON files")
    print(f"\nDone. DB at: {db.DB_PATH}")
    print(f"Leads in DB: {len(db.get_leads())}")


if __name__ == "__main__":
    migrate()
