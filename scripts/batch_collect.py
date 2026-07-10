#!/usr/bin/env python3
"""
Collect completed OpenAI Batch API results and drive the staged pipeline forward.

Polls every in-flight batch. When a stage's batch completes it:
  1. downloads the results and persists each lead's stage output,
  2. marks the batch 'collected',
  3. submits the next round:
        research  -> contact + score
        contact/score (both collected) -> outreach
        outreach  -> merge all stages -> final analysis_json (lead 'done')

Designed to be run repeatedly (e.g. a 15-minute cron). Idempotent: a no-op when
nothing is pending; a failed/expired batch resets its leads to 'pending' so the
next discovery/analysis pass (live or batch) retries them.

Usage:
    python scripts/batch_collect.py            # advance everything that's ready
    python scripts/batch_collect.py --status   # show batch statuses, take no action
    python scripts/batch_collect.py --batch-id batch_xxx
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_DIR = Path(__file__).parent.parent
SCRIPTS  = Path(__file__).parent
RESULTS  = BASE_DIR / "results"
sys.path.insert(0, str(BASE_DIR))
import db  # noqa: E402
import email_verify  # noqa: E402
import research_tools  # noqa: E402
from analysis_pipeline import merge_outputs, render_report_md  # noqa: E402

OPENAI_BASE_URL = "https://api.openai.com/v1"


# ── OpenAI REST (via requests) ─────────────────────────────────────────────────

def _headers() -> dict:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        sys.exit("ERROR: OPENAI_API_KEY is not set.")
    return {"Authorization": f"Bearer {key}"}


def _get_batch(batch_id: str) -> dict:
    resp = requests.get(f"{OPENAI_BASE_URL}/batches/{batch_id}", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _download_file(file_id: str) -> list[dict]:
    resp = requests.get(f"{OPENAI_BASE_URL}/files/{file_id}/content", headers=_headers(), timeout=120)
    resp.raise_for_status()
    return [json.loads(line) for line in resp.text.strip().splitlines() if line.strip()]


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    if not text:
        return {}
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        m2 = re.search(r"\{.*\}", raw, re.DOTALL)
        if m2:
            try:
                return json.loads(m2.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _parse_custom_id(custom_id: str) -> tuple[int | None, str | None]:
    """custom_id is "lead_id:stage" (or bare "lead_id" for a legacy single-shot)."""
    parts = str(custom_id).split(":")
    lead_id = int(parts[0]) if parts and parts[0].isdigit() else None
    stage   = parts[1] if len(parts) >= 2 else None
    return lead_id, stage


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "unknown"


# ── Result persistence ─────────────────────────────────────────────────────────

def _save_result(lead: dict, md_content: str, json_data: dict, mode: str) -> None:
    """Write MD/JSON/PDF locally and update the lead row. Mirrors run_batch._save."""
    url          = lead.get("url", "")
    company_name = lead.get("company_name", "")
    slug         = _slugify(json_data.get("company_name") or company_name or "unknown")
    out_dir      = RESULTS / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "PROSPECT-ANALYSIS.md").write_text(md_content, encoding="utf-8")
    (out_dir / "prospect-data.json").write_text(json.dumps(json_data, indent=2), encoding="utf-8")

    try:
        sys.path.insert(0, str(SCRIPTS))
        from generate_pdf import generate_pdf
        generate_pdf(md_content, json_data, str(out_dir / "prospect-analysis.pdf"))
    except Exception as e:
        print(f"    [warn] PDF generation failed: {e}")

    dm = json_data.get("key_decision_maker", {})
    db.upsert_lead({
        "url":                url,
        "mode":               mode or "sg-daily",
        "status":             "done",
        "company_name":       json_data.get("company_name", company_name),
        "prospect_score":     json_data.get("prospect_score"),
        "grade":              json_data.get("grade", ""),
        "label":              json_data.get("label", ""),
        "lead_category":      json_data.get("lead_category", lead.get("lead_category", "")),
        "key_decision_maker": dm.get("name", "") if isinstance(dm, dict) else "",
        "recommended_action": json_data.get("recommended_action", ""),
        "outreach_status":    json_data.get("outreach_status", "pending"),
        "analysis_date":      datetime.now().strftime("%Y-%m-%d"),
        "output_folder":      str(out_dir),
        "error_message":      "",
        "analysis_json":      json.dumps(json_data),
    })


def _finalize_lead(lead: dict, outreach_json: dict, mode: str) -> None:
    """Merge all stage outputs into the final record and save it."""
    prior = db.get_stage_outputs(int(lead["id"]))
    final = merge_outputs(
        lead,
        prior.get("dossier", {}),
        prior.get("contact", {}),
        prior.get("score", {}),
        outreach_json,
    )
    final.setdefault("analysis_date", datetime.now().strftime("%Y-%m-%d"))
    final.setdefault("mode", mode)
    _save_result(lead, render_report_md(final), final, mode)


# ── Round advancement ──────────────────────────────────────────────────────────

def _advance_round(completed_stage: str, run_id: str, mode: str, leads: list[dict]) -> None:
    """Submit the next round once a stage's batch is collected."""
    if not run_id:
        return
    from batch_submit import submit_stage_batch
    from research_tools import enrich_contact

    if completed_stage == "research":
        prior = {str(l["id"]): db.get_stage_outputs(int(l["id"])) for l in leads}
        # Tool work BETWEEN rounds: targeted decision-maker research per lead, fed to
        # the contact stage (off-site sources beat the company site for real emails).
        contact_research = {}
        for l in leads:
            dossier = prior.get(str(l["id"]), {}).get("dossier", {})
            try:
                cr = enrich_contact(l.get("company_name") or l.get("url", ""), l.get("url", ""), dossier)
            except Exception as e:
                print(f"  [warn] contact enrich failed for {l.get('company_name')}: {e}")
                cr = ""
            contact_research[str(l["id"])] = cr or ""
        submit_stage_batch("contact", leads, run_id, mode, prior, contact_research)
        submit_stage_batch("score",   leads, run_id, mode, prior)

    elif completed_stage in ("contact", "score"):
        if db.siblings_collected(run_id, ["contact", "score"]):
            prior = {str(l["id"]): db.get_stage_outputs(int(l["id"])) for l in leads}
            submit_stage_batch("outreach", leads, run_id, mode, prior)
    # outreach is terminal — leads were finalized during processing


def _process_batch(batch_row: dict) -> int:
    """Download a completed batch, persist its stage outputs, advance the round."""
    batch_id   = batch_row["batch_id"]
    stage      = batch_row.get("stage")
    run_id     = batch_row.get("parent_batch_id")
    batch_mode = batch_row.get("mode") or "sg-daily"
    lead_ids   = batch_row["lead_ids"] if isinstance(batch_row["lead_ids"], list) else json.loads(batch_row["lead_ids"] or "[]")

    leads_by_id = db.get_leads_by_ids([int(i) for i in lead_ids])
    print(f"\nProcessing batch {batch_id}  stage={stage}  ({len(lead_ids)} requests)")

    output_fid = batch_row.get("output_file_id") or ""
    error_fid  = batch_row.get("error_file_id") or ""
    saved = 0

    if output_fid:
        for item in _download_file(output_fid):
            lead_id, item_stage = _parse_custom_id(item.get("custom_id", ""))
            item_stage = item_stage or stage or "outreach"
            lead    = leads_by_id.get(lead_id, {})
            url     = lead.get("url", str(lead_id))
            company = lead.get("company_name", url)
            mode    = lead.get("mode", batch_mode)

            if item.get("error"):
                db.upsert_lead({"url": url, "mode": mode, "status": "error", "error_message": str(item["error"])})
                print(f"  [ERROR] {company} ({item_stage}): {item['error']}")
                continue
            try:
                text = item["response"]["body"]["choices"][0]["message"]["content"] or ""
                data = _extract_json(text)
                if item_stage in ("research", "contact", "score"):
                    if item_stage == "contact":
                        # Deterministic verification before the outreach round reads
                        # this: MX/verifier-check the model's pick plus site-scraped
                        # inboxes; may blank the email if nothing survives.
                        try:
                            dossier     = db.get_stage_outputs(int(lead["id"])).get("dossier", {})
                            site_emails = research_tools.scrape_site_emails(lead.get("url", ""))
                            data = email_verify.finalize_contact_email(
                                data, dossier, lead.get("url", ""), site_emails)
                            print(f"  [email] {company}: {data.get('email') or 'none'} "
                                  f"({data.get('email_status')}, {data.get('email_confidence')}/100)")
                        except Exception as e:
                            print(f"  [warn] email verification failed for {company}: {e}")
                    db.save_stage_output(int(lead["id"]), item_stage, data)
                    print(f"  [{item_stage}] {company} — saved")
                else:  # outreach -> finalize
                    _finalize_lead(lead, data, mode)
                    print(f"  [DONE]  {company}")
                saved += 1
            except Exception as e:
                db.upsert_lead({"url": url, "mode": mode, "status": "error", "error_message": str(e)})
                print(f"  [ERROR] {company} ({item_stage}): {e}")

    if error_fid:
        try:
            for item in _download_file(error_fid):
                lid, _ = _parse_custom_id(item.get("custom_id", ""))
                err_lead = leads_by_id.get(lid, {})
                db.upsert_lead({"url": err_lead.get("url", str(lid)), "mode": err_lead.get("mode", batch_mode),
                                "status": "error", "error_message": str(item)})
        except Exception:
            pass

    db.update_batch_status(batch_id, "collected")

    if stage in ("research", "contact", "score"):
        try:
            _advance_round(stage, run_id, batch_mode, list(leads_by_id.values()))
        except Exception as e:
            print(f"  [WARN] round advance after {stage} failed: {e}")

    return saved


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Collect OpenAI Batch API results")
    parser.add_argument("--status",   action="store_true", help="Show batch statuses and exit")
    parser.add_argument("--batch-id", default=None, help="Process a specific batch ID only")
    args = parser.parse_args()

    if args.batch_id:
        row = db._one("SELECT * FROM batches WHERE batch_id=%s", (args.batch_id,))
        batch_rows = [row] if row else []
    else:
        batch_rows = db.get_active_batches()

    if not batch_rows:
        print("No active batches.")
        return

    total_saved = 0
    for row in batch_rows:
        batch_id = row["batch_id"]
        live     = _get_batch(batch_id)
        status   = live["status"]
        counts   = live.get("request_counts", {})
        print(f"Batch {batch_id}:  {status}  (total={counts.get('total',0)}, "
              f"completed={counts.get('completed',0)}, failed={counts.get('failed',0)})  stage={row.get('stage')}")

        if args.status:
            continue

        if status == "completed":
            row["output_file_id"] = live.get("output_file_id")
            row["error_file_id"]  = live.get("error_file_id")
            db.update_batch_status(batch_id, "completed",
                                   output_file_id=live.get("output_file_id") or "",
                                   error_file_id=live.get("error_file_id") or "")
            total_saved += _process_batch(row)

        elif status in ("failed", "expired", "cancelled"):
            print(f"  → Batch {status}. Resetting leads to pending.")
            stored = row["lead_ids"] if isinstance(row["lead_ids"], list) else json.loads(row["lead_ids"] or "[]")
            db.set_lead_status([int(i) for i in stored], "pending")
            db.update_batch_status(batch_id, status)
        else:
            print("  → Still processing. Check again later.")

    if not args.status:
        print(f"\nTotal saved: {total_saved} lead-stage result(s).")


if __name__ == "__main__":
    main()
