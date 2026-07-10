#!/usr/bin/env python3
"""
Send personalised outreach emails to today's queued prospects via Resend.

Env vars (SMTP — see emailer.py):
    SMTP_USER / SMTP_PASSWORD   Gmail address + App Password
    EMAIL_FROM                  From address (defaults to SMTP_USER)

Usage:
    python scripts/send_outreach.py              # send today's queue
    python scripts/send_outreach.py --dry-run    # preview without sending
    python scripts/send_outreach.py --date 2026-05-11
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

import db  # noqa: E402
from email_verify import check_sendable  # noqa: E402
from emailer import email_configured, send_email  # noqa: E402


def build_outreach_email(lead: dict) -> dict | None:
    """Extract validated outreach fields from a lead's analysis_json.

    Returns None (= skip) unless the recipient address passes the send-time
    verification gate: real address (not a pattern template), acceptable role
    inbox, deliverable domain, and confidence above EMAIL_MIN_CONFIDENCE.
    """
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

    # NOTE: never fall back to dm["email_pattern"] — that is a pattern template
    # ("first.last@acme.com"), not an address, and used to get sent literally.
    to_email = email_data.get("to_email") or dm.get("email", "")
    to_name  = email_data.get("to_name")  or dm.get("name", "")
    to_title = email_data.get("to_title") or dm.get("title", "")
    subject  = email_data.get("subject_a") or email_data.get("subject", "")
    body     = email_data.get("body", "").strip()
    cta      = email_data.get("cta", "Are you free for a 10-minute call?")

    if not to_email or "@" not in to_email or not body:
        return None

    ok, reason = check_sendable(to_email, lead.get("url", ""),
                                confidence=dm.get("email_confidence"))
    if not ok:
        print(f"  [BLOCK] {lead.get('company_name', '?')} <{to_email}> — {reason}")
        return None

    # Strip em dashes regardless of what the model generated
    body = body.replace("—", ",").replace("–", "-")

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


def send_one(lead: dict, dry_run: bool = False) -> dict:
    """Send the outreach email for a single lead. Returns a result dict.

    Used by the Prospect detail page so an operator can send one prospect's email
    on demand (the cron's send_outreach() handles the daily queue in bulk).
    """
    company = lead.get("company_name", lead.get("url", "?"))
    if not email_configured():
        return {"company": company, "status": "error", "error": "SMTP_USER/SMTP_PASSWORD not set"}

    outreach = build_outreach_email(lead)
    if not outreach:
        return {"company": company, "status": "skipped", "reason": "no valid email or body in analysis"}

    if dry_run:
        return {"company": company, "status": "dry_run", **outreach}

    try:
        to_field = (f"{outreach['to_name']} <{outreach['to_email']}>"
                    if outreach["to_name"] else outreach["to_email"])
        html_body = outreach["body"].replace("\n", "<br>")
        html = ('<html><body style="font-family:Arial,sans-serif;font-size:14px;'
                f'line-height:1.6;color:#222">{html_body}</body></html>')
        send_email(to_field, outreach["subject"], html=html, text=outreach["body"],
                   from_name="The SG Daily")
        db.mark_outreach_sent(lead["url"], "sent")
        return {"company": company, "status": "sent", **outreach}
    except Exception as e:
        return {"company": company, "status": "error", "error": str(e)}


def send_outreach(date_str: str, dry_run: bool = False, mode: str = "sg-daily") -> list[dict]:
    """Send outreach emails for the given date's queue. Returns results list."""
    if not email_configured():
        print("ERROR: SMTP_USER and SMTP_PASSWORD must be set.")
        return []

    queue_entry = db.get_queue(date_str, mode=mode)
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
        and a.get("mode", "sg-daily") == mode
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

            send_email(
                to_field, outreach["subject"],
                html=html, text=outreach["body"], from_name=from_name,
            )

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
    parser.add_argument("--mode",    default="sg-daily", help="Mode to send outreach for")
    parser.add_argument("--dry-run", action="store_true", help="Preview without sending")
    parser.add_argument("--date",    default=datetime.now().strftime("%Y-%m-%d"),
                        help="Queue date (default: today)")
    args = parser.parse_args()
    send_outreach(args.date, dry_run=args.dry_run, mode=args.mode)
