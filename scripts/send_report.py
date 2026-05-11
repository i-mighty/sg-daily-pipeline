#!/usr/bin/env python3
"""
Send a daily pipeline report via Gmail SMTP.

Environment variables required:
    EMAIL_TO        recipient address (e.g. dailyasia9@gmail.com)
    EMAIL_FROM      sender address (your Gmail)
    SMTP_USER       same as EMAIL_FROM
    SMTP_PASS       Gmail App Password (16-char, no spaces)
    SMTP_HOST       smtp.gmail.com  (default)
    SMTP_PORT       587             (default)

Usage:
    python scripts/send_report.py
    python scripts/send_report.py --run-id 3   # attach stats from a specific pipeline run
"""

import os
import smtplib
import sys
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

import db  # noqa: E402


# ── HTML template ─────────────────────────────────────────────────────────────

def _score_color(score) -> str:
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "#888"
    if s >= 75: return "#27ae60"
    if s >= 60: return "#2980b9"
    if s >= 40: return "#f39c12"
    return "#e74c3c"


def _grade_emoji(score) -> str:
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "⚪"
    if s >= 90: return "🔥"
    if s >= 75: return "✅"
    if s >= 60: return "🔵"
    if s >= 40: return "🟡"
    return "🔴"


def build_html(run_stats: dict | None = None) -> str:
    today        = datetime.now().strftime("%B %d, %Y")
    analyses     = db.get_analyses()
    all_leads    = db.get_leads()
    pending      = [l for l in all_leads if l.get("status") == "pending"]
    errors       = [l for l in all_leads if l.get("status") == "error"]
    queue_entry  = db.get_queue(datetime.now().strftime("%Y-%m-%d"))

    # Top 5 prospects
    top5 = analyses[:5]

    # Today's queue
    queue_leads = []
    if queue_entry and queue_entry.get("queue_json"):
        import json
        try:
            queue_leads = json.loads(queue_entry["queue_json"])
        except Exception:
            pass

    # Run stats block
    if run_stats:
        discovered = run_stats.get("discovered", 0)
        analyzed   = run_stats.get("analyzed", 0)
        queued     = run_stats.get("queued", 0)
        run_status = run_stats.get("status", "completed")
    else:
        discovered = 0
        analyzed   = len([l for l in all_leads if l.get("analysis_date") == datetime.now().strftime("%Y-%m-%d")])
        queued     = len(queue_leads)
        run_status = "completed"

    status_color = "#27ae60" if run_status == "completed" else "#e74c3c"

    # ── Queue table rows ──────────────────────────────────────────────────────
    queue_rows_html = ""
    for row in queue_leads[:10]:
        queue_rows_html += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #eee">{row.get('Company Name','—')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee">{row.get('Lead Category','—')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee">{row.get('Lead Contact','—')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;font-family:monospace;font-size:12px">{row.get('Email','—')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;font-size:12px">{(row.get('Personalized Hook','') or '').split('|')[0].strip()[:80]}</td>
        </tr>"""

    if not queue_rows_html:
        queue_rows_html = '<tr><td colspan="5" style="padding:12px;color:#888;text-align:center">No queue generated today</td></tr>'

    # ── Top 5 prospect rows ───────────────────────────────────────────────────
    top5_rows_html = ""
    for lead in top5:
        sc    = lead.get("prospect_score", 0) or 0
        color = _score_color(sc)
        emoji = _grade_emoji(sc)
        dm    = lead.get("key_decision_maker", {})
        dm_name = dm.get("name", "—") if isinstance(dm, dict) else "—"
        top5_rows_html += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #eee">{emoji} {lead.get('company_name','—')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;color:{color};font-weight:bold">{int(sc)}/100</td>
            <td style="padding:8px;border-bottom:1px solid #eee">{lead.get('lead_category','—')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee">{dm_name}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;font-size:12px">{(lead.get('recommended_action') or '—')[:60]}</td>
        </tr>"""

    if not top5_rows_html:
        top5_rows_html = '<tr><td colspan="5" style="padding:12px;color:#888;text-align:center">No analyses yet</td></tr>'

    # ── Error rows ────────────────────────────────────────────────────────────
    error_rows_html = ""
    for lead in errors[:5]:
        error_rows_html += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #eee">{lead.get('company_name') or lead.get('url','—')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;color:#e74c3c;font-size:12px">{(lead.get('error_message') or '—')[:100]}</td>
        </tr>"""

    errors_section = ""
    if errors:
        errors_section = f"""
        <h2 style="color:#e74c3c;font-size:16px;margin:32px 0 12px">⚠️ Errors ({len(errors)})</h2>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px">
            <thead>
                <tr style="background:#fdf3f3">
                    <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Company</th>
                    <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Error</th>
                </tr>
            </thead>
            <tbody>{error_rows_html}</tbody>
        </table>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f8f9fa;margin:0;padding:0">
<div style="max-width:700px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)">

  <!-- Header -->
  <div style="background:#0f3460;padding:28px 32px;color:#fff">
    <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;opacity:.7">The SG Daily · Viral Asia</div>
    <h1 style="margin:8px 0 4px;font-size:22px;font-weight:700">Daily Pipeline Report</h1>
    <div style="opacity:.8;font-size:14px">{today}</div>
  </div>

  <!-- Run summary -->
  <div style="padding:24px 32px;border-bottom:1px solid #eee">
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      <div style="flex:1;min-width:100px;text-align:center;padding:16px;background:#f0f9f0;border-radius:6px">
        <div style="font-size:28px;font-weight:700;color:#27ae60">{discovered}</div>
        <div style="font-size:12px;color:#666;margin-top:2px">Discovered</div>
      </div>
      <div style="flex:1;min-width:100px;text-align:center;padding:16px;background:#f0f4f9;border-radius:6px">
        <div style="font-size:28px;font-weight:700;color:#2980b9">{analyzed}</div>
        <div style="font-size:12px;color:#666;margin-top:2px">Analyzed</div>
      </div>
      <div style="flex:1;min-width:100px;text-align:center;padding:16px;background:#fdf8f0;border-radius:6px">
        <div style="font-size:28px;font-weight:700;color:#f39c12">{queued}</div>
        <div style="font-size:12px;color:#666;margin-top:2px">Queued Today</div>
      </div>
      <div style="flex:1;min-width:100px;text-align:center;padding:16px;background:#f8f9fa;border-radius:6px">
        <div style="font-size:28px;font-weight:700;color:#555">{len(all_leads)}</div>
        <div style="font-size:12px;color:#666;margin-top:2px">Total Leads</div>
      </div>
    </div>
    <div style="margin-top:12px;font-size:12px;color:{status_color}">
      ● Pipeline run: <strong>{run_status}</strong>
      &nbsp;·&nbsp; {len(pending)} pending analysis
      &nbsp;·&nbsp; {len(errors)} errors
    </div>
  </div>

  <div style="padding:24px 32px">

    <!-- Today's queue -->
    <h2 style="color:#0f3460;font-size:16px;margin:0 0 12px">📬 Today's Outreach Queue</h2>
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#f8f9fa">
          <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Company</th>
          <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Category</th>
          <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Contact</th>
          <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Email</th>
          <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Hook</th>
        </tr>
      </thead>
      <tbody>{queue_rows_html}</tbody>
    </table>

    <!-- Top 5 prospects -->
    <h2 style="color:#0f3460;font-size:16px;margin:32px 0 12px">🎯 Top 5 Prospects</h2>
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#f8f9fa">
          <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Company</th>
          <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Score</th>
          <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Category</th>
          <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Key DM</th>
          <th style="padding:8px;text-align:left;border-bottom:2px solid #eee">Next Action</th>
        </tr>
      </thead>
      <tbody>{top5_rows_html}</tbody>
    </table>

    {errors_section}

  </div>

  <!-- Footer -->
  <div style="padding:16px 32px;background:#f8f9fa;font-size:11px;color:#999;text-align:center">
    Viral Asia · The SG Daily · AI Sales Pipeline &nbsp;|&nbsp; Auto-generated report
  </div>

</div>
</body>
</html>"""


# ── Send ──────────────────────────────────────────────────────────────────────

def send(run_stats: dict | None = None):
    to_addr   = os.environ.get("EMAIL_TO", "")
    from_addr = os.environ.get("EMAIL_FROM", "")
    smtp_user = os.environ.get("SMTP_USER", from_addr)
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not all([to_addr, from_addr, smtp_pass]):
        print("ERROR: EMAIL_TO, EMAIL_FROM, and SMTP_PASS must be set.")
        return False

    today   = datetime.now().strftime("%B %d, %Y")
    subject = f"SG Daily Pipeline Report — {today}"
    html    = build_html(run_stats)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr

    # HTML body
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    # Attach PDFs from today's queued leads
    date_str     = datetime.now().strftime("%Y-%m-%d")
    queue_entry  = db.get_queue(date_str)
    attached     = 0
    if queue_entry and queue_entry.get("queue_json"):
        import json
        queued_names = {r.get("Company Name", "").strip().lower()
                        for r in json.loads(queue_entry["queue_json"] or "[]")}
        for lead in db.get_analyses():
            if lead.get("company_name", "").strip().lower() not in queued_names:
                continue
            pdf_path = Path(lead.get("_pdf_path", "") or "")
            if not pdf_path.exists():
                continue
            try:
                with open(pdf_path, "rb") as f:
                    part = MIMEApplication(f.read(), _subtype="pdf")
                safe_name = (lead.get("company_name") or "prospect").replace(" ", "-")
                part.add_header("Content-Disposition", "attachment",
                                filename=f"{safe_name}-analysis.pdf")
                msg.attach(part)
                attached += 1
            except Exception as e:
                print(f"  Could not attach PDF for {lead.get('company_name')}: {e}")

    if attached:
        print(f"Attaching {attached} PDF report(s)")

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, to_addr, msg.as_string())
        print(f"Report sent to {to_addr} ({attached} PDFs attached)")
        return True
    except Exception as e:
        print(f"ERROR sending email: {e}")
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, default=None)
    args = parser.parse_args()

    run_stats = None
    if args.run_id:
        runs = db.get_pipeline_runs(limit=100)
        run_stats = next((r for r in runs if r["id"] == args.run_id), None)

    success = send(run_stats)
    sys.exit(0 if success else 1)
