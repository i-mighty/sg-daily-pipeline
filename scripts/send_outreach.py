#!/usr/bin/env python3
"""
Send personalised outreach emails to today's queued prospects via Resend.

Env vars:
    RESEND_API_KEY   Resend API key
    EMAIL_FROM       Verified Resend sender address (e.g. outreach@yourdomain.com)

Usage:
    python scripts/send_outreach.py              # send today's queue
    python scripts/send_outreach.py --dry-run    # preview without sending
    python scripts/send_outreach.py --date 2026-05-11
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import resend

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

import db  # noqa: E402


def build_outreach_email(lead: dict) -> dict | None:
    """Extract validated outreach fields from a lead's analysis_json."""
    raw = lead.get("analysis_json") or ""
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}

    email_data = data.get("outreach_email") or {}
    if not isinstance(email_data, dict):
        email_data = {}

    dm = data.get("key_decision_maker") or {}
    if not isinstance(dm, dict):
        dm = {}

    to_email = email_data.get("to_email") or dm.get("email_pattern", "")
    to_name  = email_data.get("to_name")  or dm.get("name", "")
    to_title = email_data.get("to_title") or dm.get("title", "")
    subject  = email_data.get("subject_a") or email_data.get("subject", "")
    body     = email_data.get("body", "").strip()
    cta      = email_data.get("cta", "Are you free for a 10-minute call?")

    if not to_email or "@" not in to_email or not body:
        return None

    if cta and cta not in body:
        body = f"{body}\n\n{cta}"

    return {
        "to_email": to_email,
        "to_name":  to_name,
        "to_title": to_title,
        "subject":  subject or f"Quick question about {lead.get('company_name', '')}",
        "body":     body,
        "company":  lead.get("company_name", ""),
        "url":      lead.get("url", ""),
    }


def send_outreach(date_str: str, dry_run: bool = False) -> list[dict]:
    """Send outreach emails for the given date's queue. Returns results list."""
    api_key   = os.environ.get("RESEND_API_KEY", "")
    from_addr = os.environ.get("EMAIL_FROM", "")

    if not api_key or not from_addr:
        print("ERROR: RESEND_API_KEY and EMAIL_FROM must be set.")
        return []

    resend.api_key = api_key

    queue_entry = db.get_queue(date_str)
    if not queue_entry:
        print(f"No queue found for {date_str}.")
        return []

    queued_names = set()
    try:
        for row in json.loads(queue_entry["queue_json"] or "[]"):
            queued_names.add(row.get("Company Name", "").strip().lower())
    except Exception:
        pass

    if not queued_names:
        print("Queue is empty.")
        return []

    analyses = db.get_analyses()
    targets = [
        a for a in analyses
        if a.get("company_name", "").strip().lower() in queued_names
        and (a.get("outreach_status") or "").lower() not in {"sent", "replied", "converted"}
    ]

    if not targets:
        print("All queued leads already marked as sent, or no analyses matched.")
        return []

    print(f"\nSending outreach to {len(targets)} prospects{' [DRY RUN]' if dry_run else ''}...\n")

    from_name = "The SG Daily"
    results   = []

    for lead in targets:
        outreach = build_outreach_email(lead)
        company  = lead.get("company_name", lead.get("url", "?"))

        if not outreach:
            print(f"  [SKIP]  {company} — no valid email or body in analysis")
            results.append({"company": company, "status": "skipped", "reason": "missing email/body"})
            continue

        print(f"  [{'DRY' if dry_run else 'SEND'}]  {company}")
        print(f"          To:      {outreach['to_name']} <{outreach['to_email']}>")
        print(f"          Subject: {outreach['subject']}")
        print(f"          Body:    {outreach['body'][:80].replace(chr(10),' ')}...")

        if dry_run:
            results.append({"company": company, "status": "dry_run", **outreach})
            continue

        try:
            to_field = (
                f"{outreach['to_name']} <{outreach['to_email']}>"
                if outreach["to_name"] else outreach["to_email"]
            )
            html_body = outreach["body"].replace("\n", "<br>")
            html = (
                '<html><body style="font-family:Arial,sans-serif;font-size:14px;'
                f'line-height:1.6;color:#222">{html_body}</body></html>'
            )

            resend.Emails.send({
                "from":    f"{from_name} <{from_addr}>",
                "to":      [to_field],
                "subject": outreach["subject"],
                "text":    outreach["body"],
                "html":    html,
            })

            db.mark_outreach_sent(lead["url"], "sent")
            print(f"          ✓ Sent")
            results.append({"company": company, "status": "sent", **outreach})

            time.sleep(1)  # avoid hitting Resend rate limits

        except Exception as e:
            print(f"          ✗ Error: {e}")
            results.append({"company": company, "status": "error", "error": str(e)})

    sent = sum(1 for r in results if r["status"] == "sent")
    skip = sum(1 for r in results if r["status"] == "skipped")
    err  = sum(1 for r in results if r["status"] == "error")
    print(f"\nOutreach complete: {sent} sent, {skip} skipped, {err} errors")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send outreach emails to today's queue")
    parser.add_argument("--dry-run", action="store_true", help="Preview without sending")
    parser.add_argument("--date",    default=datetime.now().strftime("%Y-%m-%d"),
                        help="Queue date (default: today)")
    args = parser.parse_args()
    send_outreach(args.date, dry_run=args.dry_run)
