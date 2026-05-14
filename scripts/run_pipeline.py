#!/usr/bin/env python3
"""
End-to-end pipeline orchestrator.

Reads active modes from the DB and for each mode runs:
  1. discover_leads   — find new leads using the mode's discovery_prompt
  2. run_batch        — analyze all pending leads for this mode
  3. daily_queue      — generate today's outreach queue for this mode
  4. send_outreach    — send personalised emails to each queued prospect
  5. send_report      — email HTML summary + PDF attachments
Then after all modes:
  6. cleanup          — delete results/ artefacts (MD, PDF, JSON files)

Usage:
    python scripts/run_pipeline.py                  # run all active modes
    python scripts/run_pipeline.py --mode sg-daily  # run one specific mode
    python scripts/run_pipeline.py --no-email        # skip outreach + reports
    python scripts/run_pipeline.py --no-cleanup      # keep result files
    python scripts/run_pipeline.py --dry-run         # discover only, no analysis/email

Railway cron:
    command = "python scripts/run_pipeline.py"
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


def run_mode(mode: dict, args: argparse.Namespace, python: str) -> dict:
    """Run the full pipeline for a single mode. Returns per-mode stats."""
    name  = mode["name"]
    label = mode["label"]
    stats = {"discovered": 0, "analyzed": 0, "queued": 0, "outreached": 0, "errors": []}

    print(f"\n{'#'*60}")
    print(f"# MODE: {label} ({name})")
    print(f"{'#'*60}")

    # ── Step 1: Discover ──────────────────────────────────────────────────────
    discover_count = mode.get("discover_count", 0)
    if discover_count > 0 and not args.dry_run:
        code, _ = run_step(
            f"[{name}] Discover {discover_count} leads",
            [python, "scripts/discover_leads.py", "--mode", name, "--count", str(discover_count)],
        )
        if code != 0:
            stats["errors"].append(f"discover_leads exited {code}")
        else:
            stats["discovered"] = discover_count
    elif discover_count > 0 and args.dry_run:
        code, _ = run_step(
            f"[{name}] Discover {discover_count} leads (dry-run)",
            [python, "scripts/discover_leads.py", "--mode", name, "--count", str(discover_count), "--dry-run"],
        )
        stats["discovered"] = discover_count

    if args.dry_run:
        return stats

    # ── Step 2: Analyze ───────────────────────────────────────────────────────
    before_count = len(db.get_analyses())
    code, _ = run_step(
        f"[{name}] Analyze pending leads",
        [python, "scripts/run_batch.py", "--mode", name,
         "--concurrency", str(args.concurrency)],
    )
    if code != 0:
        stats["errors"].append(f"run_batch exited {code}")
    stats["analyzed"] = max(0, len(db.get_analyses()) - before_count)

    # ── Step 3: Generate queue ────────────────────────────────────────────────
    code, _ = run_step(
        f"[{name}] Generate queue",
        [python, "scripts/daily_queue.py", "--mode", name],
    )
    if code != 0:
        stats["errors"].append(f"daily_queue exited {code}")
    else:
        date_str    = datetime.now().strftime("%Y-%m-%d")
        queue_entry = db.get_queue(date_str, mode=name)
        if queue_entry and queue_entry.get("queue_json"):
            stats["queued"] = len(json.loads(queue_entry["queue_json"]))

    # ── Step 4: Send outreach ─────────────────────────────────────────────────
    if not args.no_email:
        print(f"\n{'='*60}")
        print(f"STEP: [{name}] Send outreach emails")
        print(f"{'='*60}")
        sys.path.insert(0, str(BASE_DIR / "scripts"))
        from send_outreach import send_outreach
        results = send_outreach(datetime.now().strftime("%Y-%m-%d"), dry_run=False, mode=name)
        stats["outreached"] = sum(1 for r in results if r.get("status") == "sent")
        if any(r.get("status") == "error" for r in results):
            stats["errors"].append("some outreach emails failed")

    # ── Step 5: Send report ───────────────────────────────────────────────────
    if not args.no_email:
        print(f"\n{'='*60}")
        print(f"STEP: [{name}] Send pipeline report")
        print(f"{'='*60}")
        from send_report import send
        send(
            {
                "discovered": stats["discovered"],
                "analyzed":   stats["analyzed"],
                "queued":     stats["queued"],
                "status":     "completed" if not stats["errors"] else "completed_with_errors",
            },
            mode=name,
        )

    return stats


def main():
    parser = argparse.ArgumentParser(description="Multi-mode end-to-end pipeline")
    parser.add_argument("--mode",        default="",  help="Run only this mode (default: all active modes)")
    parser.add_argument("--concurrency", type=int, default=3, help="Analysis concurrency (default: 3)")
    parser.add_argument("--no-email",    action="store_true", help="Skip outreach + report emails")
    parser.add_argument("--no-cleanup",  action="store_true", help="Skip deleting result files")
    parser.add_argument("--dry-run",     action="store_true", help="Discover only, no analysis/email")
    args = parser.parse_args()

    run_id     = db.start_pipeline_run(run_type="cron" if not args.dry_run else "dry-run")
    started_at = datetime.now()
    print(f"\nPipeline run #{run_id} started at {started_at.strftime('%Y-%m-%d %H:%M:%S')}")

    python = sys.executable

    # Resolve which modes to run
    if args.mode:
        mode_config = db.get_mode(args.mode)
        if not mode_config:
            sys.exit(f"ERROR: Mode '{args.mode}' not found in DB.")
        modes = [mode_config]
    else:
        modes = db.get_modes(active_only=True)
        if not modes:
            sys.exit("No active modes in DB. Add a mode via the Modes page or enable an existing one.")

    print(f"Modes to run: {', '.join(m['name'] for m in modes)}\n")

    total_discovered = 0
    total_analyzed   = 0
    total_queued     = 0
    total_outreached = 0
    all_errors       = []

    for mode in modes:
        stats = run_mode(mode, args, python)
        total_discovered += stats["discovered"]
        total_analyzed   += stats["analyzed"]
        total_queued     += stats["queued"]
        total_outreached += stats["outreached"]
        all_errors.extend([f"[{mode['name']}] {e}" for e in stats["errors"]])

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if not args.no_cleanup and not args.dry_run:
        print(f"\n{'='*60}")
        print("STEP: Clean up result files")
        print(f"{'='*60}")
        n = cleanup_results()
        print(f"Deleted {n} result folder(s) and daily queue MD files")

    final_status = "completed" if not all_errors else "completed_with_errors"
    db.finish_pipeline_run(run_id, discovered=total_discovered, analyzed=total_analyzed,
                            queued=total_queued, status=final_status,
                            error_log="; ".join(all_errors))

    duration = (datetime.now() - started_at).seconds
    print(f"\n{'='*60}")
    print(f"Pipeline #{run_id} complete in {duration}s")
    print(f"  Discovered: {total_discovered}  |  Analyzed: {total_analyzed}  |  "
          f"Queued: {total_queued}  |  Outreach sent: {total_outreached}")
    if all_errors:
        print(f"  Errors: {'; '.join(all_errors)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
