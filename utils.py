"""Shared utilities for the Streamlit app."""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
RESULTS  = BASE_DIR / "results"
SCRIPTS  = BASE_DIR / "scripts"

load_dotenv(BASE_DIR / ".env")

import db  # noqa: E402 — initialises DB on import

# ── Data loading ──────────────────────────────────────────────────────────────

def load_prospects() -> list[dict]:
    return db.get_leads()


def save_prospects(rows: list[dict]):
    for row in rows:
        db.upsert_lead(row)


def load_analyses() -> list[dict]:
    return db.get_analyses()


def load_queue_files() -> list[Path]:
    """Return all DAILY-QUEUE-*.md files, newest first (kept for Saved Queue Files tab)."""
    return sorted(BASE_DIR.glob("DAILY-QUEUE-*.md"), reverse=True)


def mark_outreach_sent(url: str, status: str = "sent"):
    db.mark_outreach_sent(url, status)


# ── Score helpers ─────────────────────────────────────────────────────────────

def grade(score) -> str:
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "?"
    if s >= 90: return "A+"
    if s >= 75: return "A"
    if s >= 60: return "B"
    if s >= 40: return "C"
    return "D"


def grade_color(g: str) -> str:
    return {"A+": "#27ae60", "A": "#27ae60", "B": "#2980b9",
            "C": "#f39c12", "D": "#e74c3c"}.get(g, "#888")


def score_color(score) -> str:
    return grade_color(grade(score))


def grade_emoji(g: str) -> str:
    return {"A+": "🔥", "A": "✅", "B": "🔵", "C": "🟡", "D": "🔴"}.get(g, "⚪")


def score_emoji(score) -> str:
    return grade_emoji(grade(score))


def dm_name(analysis: dict) -> str:
    dm = analysis.get("key_decision_maker", {})
    if isinstance(dm, dict):
        return dm.get("name", "—")
    return "—"


def dm_email(analysis: dict) -> str:
    dm = analysis.get("key_decision_maker", {})
    em = analysis.get("outreach_email", {})
    if isinstance(em, dict) and em.get("to_email"):
        return em["to_email"]
    if isinstance(dm, dict):
        return dm.get("email_pattern", "—")
    return "—"


# ── Script runner ─────────────────────────────────────────────────────────────

def stream_script(script_args: list[str]):
    """Yield stdout lines from a script. Last line is __EXIT_CODE__N."""
    proc = subprocess.Popen(
        [sys.executable] + script_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(BASE_DIR),
        env={**os.environ},
    )
    for line in proc.stdout:
        yield line.rstrip()
    proc.wait()
    yield f"__EXIT_CODE__{proc.returncode}"
