#!/usr/bin/env python3
"""
End-to-end pipeline orchestrator for The SG Daily.

Steps (in order):
  1. discover_leads   — find new brands (optional, --discover N)
  2. run_batch        — analyze all pending leads
  3. daily_queue      — generate today's outreach queue
  4. send_outreach    — send personalised emails to each prospect in the queue
  5. send_report      — email HTML summary + PDF attachments to EMAIL_TO
  6. cleanup          — delete results/ artefacts (MD, PDF, JSON files)

Usage:
    python scripts/run_pipeline.py                    # analyze + queue + outreach + report + cleanup
    python scripts/run_pipeline.py --discover 10      # also discover 10 new leads first
    python scripts/run_pipeline.py --queue 10         # queue 10 targets (default: 8)
    python scripts/run_pipeline.py --no-email         # skip outreach + report emails
    python scripts/run_pipeline.py --dry-run          # discover only, no analysis/email/cleanup

Railway cron:
    command = "python scripts/run_pipeline.py --discover 10 --queue 8"
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

import db  # noqa: E402


def run_step(label: str, cmd: list[str]) -> tuple[int, int]:
    print(f"\n{'='*60}")
    print(f"STEP: {label}")
    print(f"{'='*60}")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(BASE_DIR),
    )
    lines = 0
    for line in proc.stdout:
        print(line, end="")
        lines += 1
    proc.wait()
    print(f"[{label}] exit code: {proc.returncode}")
    return proc.returncode, lines


def cleanup_results() -> int:
    """Delete company result folders and daily queue MD files. Returns count deleted."""
    results_dir = BASE_DIR / "results"
    deleted = 0
    if results_dir.exists():
        for item in results_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
                deleted += 1
    for md in BASE_DIR.glob("DAILY-QUEUE-*.md"):
        md.unlink()
    return deleted


def main():
    parser = argparse.ArgumentParser(description="SG Daily end-to-end pipeline")
    _discover_default = int(os.environ.get("DAILY_DISCOVER_COUNT", "0"))
    parser.add_argument("--discover",    type=int, default=_discover_default,  help="New leads to discover (0=skip, or set DAILY_DISCOVER_COUNT env var)")
    parser.add_argument("--queue",       type=int, default=8,  help="Queue size (default: 8)")
    parser.add_argument("--concurrency", type=int, default=3,  help="Analysis concurrency (default: 3)")
    parser.add_argument("--no-email",    action="store_true",  help="Skip outreach + report emails")
    parser.add_argument("--no-cleanup",  action="store_true",  help="Skip deleting result files")
    parser.add_argument("--dry-run",     action="store_true",  help="Discover only, no analysis/email")
    args = parser.parse_args()

    run_id     = db.start_pipeline_run(run_type="cron" if not args.dry_run else "dry-run")
    started_at = datetime.now()
    print(f"\nPipeline run #{run_id} started at {started_at.strftime('%Y-%m-%d %H:%M:%S')}")

    discovered = 0
    analyzed   = 0
    queued     = 0
    outreached = 0
    errors     = []
    python     = sys.executable

    # ── Step 1: Discover ──────────────────────────────────────────────────────
    if args.discover > 0:
        code, _ = run_step(
            f"Discover {args.discover} leads",
            [python, "scripts/discover_leads.py", "--count", str(args.discover)],
        )
        if code != 0:
            errors.append(f"discover_leads exited {code}")
        else:
            discovered = args.discover

    if args.dry_run:
        db.finish_pipeline_run(run_id, discovered=discovered, analyzed=0,
                                queued=0, status="completed")
        print("\n[dry-run] Stopping after discovery.")
        return

    # ── Step 2: Analyze ───────────────────────────────────────────────────────
    before_count = len(db.get_analyses())
    code, _ = run_step(
        "Analyze pending leads",
        [python, "scripts/run_batch.py", "--mode", "sg-daily",
         "--concurrency", str(args.concurrency)],
    )
    if code != 0:
        errors.append(f"run_batch exited {code}")
    analyzed = max(0, len(db.get_analyses()) - before_count)

    # ── Step 3: Generate queue ────────────────────────────────────────────────
    code, _ = run_step(
        f"Generate queue ({args.queue} targets)",
        [python, "scripts/daily_queue.py", "--count", str(args.queue)],
    )
    if code != 0:
        errors.append(f"daily_queue exited {code}")
    else:
        date_str    = datetime.now().strftime("%Y-%m-%d")
        queue_entry = db.get_queue(date_str)
        if queue_entry and queue_entry.get("queue_json"):
            queued = len(json.loads(queue_entry["queue_json"]))

    # ── Step 4: Send outreach emails ──────────────────────────────────────────
    if not args.no_email:
        print(f"\n{'='*60}")
        print("STEP: Send outreach emails to prospects")
        print(f"{'='*60}")
        sys.path.insert(0, str(BASE_DIR / "scripts"))
        from send_outreach import send_outreach
        results = send_outreach(datetime.now().strftime("%Y-%m-%d"), dry_run=False)
        outreached = sum(1 for r in results if r.get("status") == "sent")
        if any(r.get("status") == "error" for r in results):
            errors.append("some outreach emails failed to send")

    # ── Step 5: Send admin report (with PDF attachments) ─────────────────────
    final_status = "completed" if not errors else "completed_with_errors"
    db.finish_pipeline_run(run_id, discovered=discovered, analyzed=analyzed,
                            queued=queued, status=final_status,
                            error_log="; ".join(errors))

    if not args.no_email:
        print(f"\n{'='*60}")
        print("STEP: Send admin report (with PDF attachments)")
        print(f"{'='*60}")
        from send_report import send
        send({
            "discovered": discovered,
            "analyzed":   analyzed,
            "queued":     queued,
            "status":     final_status,
        })

    # ── Step 6: Cleanup result files ──────────────────────────────────────────
    if not args.no_cleanup:
        print(f"\n{'='*60}")
        print("STEP: Clean up result files")
        print(f"{'='*60}")
        n = cleanup_results()
        print(f"Deleted {n} result folder(s) and daily queue MD files")

    duration = (datetime.now() - started_at).seconds
    print(f"\n{'='*60}")
    print(f"Pipeline #{run_id} complete in {duration}s")
    print(f"  Discovered: {discovered}  |  Analyzed: {analyzed}  |  "
          f"Queued: {queued}  |  Outreach sent: {outreached}")
    if errors:
        print(f"  Errors: {'; '.join(errors)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
