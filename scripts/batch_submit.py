#!/usr/bin/env python3
"""
Submit pending leads to the OpenAI Batch API for staged, overnight analysis.

~50% cheaper than the real-time API. One analysis run is multiple rounds (stages):

    research  ->  contact + score  ->  outreach

Each round is one OpenAI batch job. This script submits the FIRST round (research)
for all pending leads; scripts/batch_collect.py polls for completion and submits the
next round automatically. Leads are marked 'batching' until the run finalises.

Usage:
    python scripts/batch_submit.py                  # all pending leads (research round)
    python scripts/batch_submit.py --mode generic   # only one mode
    python scripts/batch_submit.py --limit 50       # cap leads
    python scripts/batch_submit.py --dry-run        # preview, no submit

Models (Batch API has its own allowlist):
    research round  -> OPENAI_BATCH_QUALITY_MODEL (default gpt-4.1)
    other rounds    -> OPENAI_BATCH_MODEL         (default gpt-4o-mini)
"""

import argparse
import io
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
import db  # noqa: E402
from analysis_pipeline import build_stage_prompt  # noqa: E402
from analysis_scaffold import has_sections  # noqa: E402
from research_tools import deep_enrich  # noqa: E402

OPENAI_BASE_URL = "https://api.openai.com/v1"


# ── Models ────────────────────────────────────────────────────────────────────

def _research_model() -> str:
    return (os.environ.get("OPENAI_BATCH_QUALITY_MODEL")
            or os.environ.get("OPENAI_QUALITY_MODEL") or "gpt-4.1")


def _small_model() -> str:
    return os.environ.get("OPENAI_BATCH_MODEL", "gpt-4o-mini")


def _model_for(stage: str) -> str:
    return _research_model() if stage == "research" else _small_model()


# ── OpenAI Batch API (via requests — already a dependency) ─────────────────────

def _auth_header() -> dict:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        sys.exit("ERROR: OPENAI_API_KEY is not set.")
    return {"Authorization": f"Bearer {key}"}


def _upload_jsonl(jsonl_bytes: bytes) -> str:
    """Upload a .jsonl file to the OpenAI Files API. Returns file_id."""
    resp = requests.post(
        f"{OPENAI_BASE_URL}/files",
        headers=_auth_header(),
        files={"file": ("batch_requests.jsonl", io.BytesIO(jsonl_bytes), "application/jsonl")},
        data={"purpose": "batch"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _create_batch(file_id: str) -> dict:
    resp = requests.post(
        f"{OPENAI_BASE_URL}/batches",
        headers={**_auth_header(), "Content-Type": "application/json"},
        json={
            "input_file_id":     file_id,
            "endpoint":          "/v1/chat/completions",
            "completion_window": "24h",
            "metadata":          {"source": "ai-sales"},
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ── Prompt assembly ────────────────────────────────────────────────────────────

def _campaign_prompt(mode: str) -> str:
    mc = db.get_mode(mode)
    return (mc or {}).get("analysis_prompt", "").strip()


# ── Staged batch submission ────────────────────────────────────────────────────

def submit_stage_batch(stage: str, leads: list[dict], run_id: str, mode: str,
                       prior_by_lead: dict | None = None,
                       extra_research_by_lead: dict | None = None,
                       *, dry_run: bool = False) -> str | None:
    """
    Build + submit ONE OpenAI batch for `stage` over `leads`. Returns the batch_id.

      research : deep pre-fetch per lead (no tools in batch) -> dossier
      contact  : reads prior_by_lead[lead_id]['dossier'] (+ targeted DM research)
      score    : reads prior_by_lead[lead_id]['dossier']
      outreach : reads dossier + contact + score

    All rounds of one run share `run_id` (parent_batch_id). custom_id is
    "lead_id:stage" so the collector can route results and advance rounds.
    """
    model         = _model_for(stage)
    max_tok       = int(os.environ.get("OPENAI_MAX_TOKENS", 4000))
    today         = datetime.now().strftime("%Y-%m-%d")
    prior_by_lead = prior_by_lead or {}

    requests_jsonl: list[str] = []
    custom_ids: list[str] = []
    lead_ids:   list[int] = []

    for lead in leads:
        campaign = _campaign_prompt(lead.get("mode", mode) or mode)
        if not campaign:
            print(f"  [skip] lead {lead.get('id')} — mode '{lead.get('mode')}' has no analysis_prompt")
            continue

        if stage == "research":
            company = lead.get("company_name") or lead.get("url", "?")
            # Batch has no live tools — pre-fetch a deep research corpus now and
            # feed it to the research stage as research_data.
            corpus = deep_enrich(company, lead.get("url", ""))
            system, user = build_stage_prompt("research", campaign, lead, today, research_data=corpus)
        else:
            prior = prior_by_lead.get(str(lead["id"]), {})
            extra = (extra_research_by_lead or {}).get(str(lead["id"])) if stage == "contact" else None
            system, user = build_stage_prompt(stage, campaign, lead, today, prior=prior, research_data=extra)

        cid  = f"{lead['id']}:{stage}"
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": user}]
        requests_jsonl.append(json.dumps({
            "custom_id": cid, "method": "POST", "url": "/v1/chat/completions",
            "body": {"model": model, "max_tokens": max_tok, "messages": msgs},
        }))
        custom_ids.append(cid)
        lead_ids.append(int(lead["id"]))

    if not requests_jsonl:
        print(f"  [{stage}] nothing to submit")
        return None

    if dry_run:
        print(f"  [dry-run] {stage}: {len(requests_jsonl)} request(s), model={model}")
        print("  sample custom_ids:", custom_ids[:3])
        return None

    file_id  = _upload_jsonl("\n".join(requests_jsonl).encode("utf-8"))
    batch    = _create_batch(file_id)
    batch_id = batch["id"]

    db.record_batch(batch_id, stage, run_id, mode, lead_ids, custom_ids, input_file_id=file_id)
    db.set_lead_status(lead_ids, "batching")
    print(f"  [{stage}] batch {batch_id} submitted — {len(custom_ids)} request(s), model={model}")
    return batch_id


# ── Main: kick off the research round ──────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Submit leads to OpenAI Batch API (research round)")
    parser.add_argument("--limit",   type=int, default=None)
    parser.add_argument("--mode",    default="", help="Analysis mode (generic / sg-daily)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without submitting")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("ERROR: OPENAI_API_KEY is not set.")

    mode = args.mode.strip().lower()
    leads = db.get_leads(status="pending", mode=mode or None)
    if args.limit:
        leads = leads[:args.limit]

    if not leads:
        print("No pending leads to submit. Run discover_leads.py first.")
        return

    # Warn (don't block) on un-sectioned prompts — research stage still runs, but the
    # campaign's research instructions won't be staged. Re-section via the Modes UI.
    sample_mode = mode or (leads[0].get("mode") or "sg-daily")
    if not has_sections(_campaign_prompt(sample_mode)):
        print(f"  [warn] mode '{sample_mode}' prompt has no === sections === — "
              "staged batch works best with sectioned prompts (see scripts/reseed_modes.py)")

    run_id = uuid.uuid4().hex
    print(f"Submitting research round for {len(leads)} lead(s) — run {run_id}, "
          f"model={_research_model()}\n")
    submit_stage_batch("research", leads, run_id, mode or sample_mode, dry_run=args.dry_run)

    if not args.dry_run:
        print("\nResearch round submitted. The collector advances rounds:")
        print("  python scripts/batch_collect.py --status")


if __name__ == "__main__":
    main()
