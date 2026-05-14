#!/usr/bin/env python3
"""
Generate the daily outreach queue for Viral Asia / The SG Daily.
Picks 5-10 high-intent targets from completed analyses and formats
them in the SG Daily insider style.

Outputs:
  - DAILY-QUEUE-{date}.md          (human-readable, ready to action)
  - results/queue-{date}.csv       (copy-paste CSV for CRM/sheets)

Usage:
    python scripts/daily_queue.py
    python scripts/daily_queue.py --count 10
    python scripts/daily_queue.py --category "Media Feature Lead"
"""

import csv
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR  = Path(__file__).parent.parent
RESULTS   = BASE_DIR / "results"
QUEUE_DIR = BASE_DIR  # queue MDs go in root

sys.path.insert(0, str(BASE_DIR))
import db  # noqa: E402


def _score(d: dict) -> float:
    try:
        return float(d.get("prospect_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _dm(d: dict) -> dict:
    dm = d.get("key_decision_maker", {})
    return dm if isinstance(dm, dict) else {}


def _email(d: dict) -> dict:
    e = d.get("outreach_email", {})
    return e if isinstance(e, dict) else {}


def load_queue_candidates(mode: str, category_filter: str | None = None) -> list[dict]:
    """Load done analyses for the given mode, excluding already-sent outreach."""
    import json as _json
    candidates = []

    for lead in db.get_analyses():
        if lead.get("mode", "sg-daily") != mode:
            continue

        if (lead.get("outreach_status") or "").lower() in {"sent", "replied", "converted"}:
            continue

        if category_filter and lead.get("lead_category", "") != category_filter:
            continue

        # Restore nested dicts from JSON blob if they were flattened
        if "analysis_json" in lead and lead["analysis_json"]:
            try:
                blob = _json.loads(lead["analysis_json"])
                for key in ("key_decision_maker", "outreach_email", "ooh_presence", "hook_ideas"):
                    if key not in lead or not lead[key]:
                        lead[key] = blob.get(key)
            except Exception:
                pass

        candidates.append(lead)

    candidates.sort(key=_score, reverse=True)
    return candidates


def _grade_label(score) -> str:
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "?"
    if s >= 90: return "A+ 🔥"
    if s >= 75: return "A ✅"
    if s >= 60: return "B 🔵"
    if s >= 40: return "C 🟡"
    return "D 🔴"


def _format_hook_ideas(d: dict) -> str:
    """Return formatted bullet hook ideas from JSON or outreach_email."""
    hooks = d.get("hook_ideas") or []

    # Fall back to email body bullets if hook_ideas not populated
    if not hooks:
        email_body = _email(d).get("body", "")
        bullets = [line.strip().lstrip("•-* ") for line in email_body.splitlines() if line.strip().startswith(("•", "-", "*"))]
        hooks = bullets[:3]

    if not hooks:
        return "_No hook ideas generated — re-run analysis in sg-daily mode._"

    return "\n".join(f"  • {h}" for h in hooks[:3])


def _format_entry(i: int, d: dict) -> str:
    company   = d.get("company_name", "Unknown")
    cat       = d.get("lead_category", "—")
    score     = d.get("prospect_score", "?")
    grade     = _grade_label(score)
    sg_usp    = d.get("sg_usp") or "—"
    ooh       = d.get("ooh_presence", {})
    ooh_str   = "Yes" if (isinstance(ooh, dict) and ooh.get("detected")) else ("Yes" if ooh is True else "Unknown")

    dm    = _dm(d)
    email = _email(d)

    dm_name  = dm.get("name",          "—")
    dm_title = dm.get("title",         "—")
    dm_email = email.get("to_email") or dm.get("email_pattern", "—")

    subject_a = email.get("subject_a") or email.get("subject", "—")
    subject_b = email.get("subject_b", "")
    body      = (email.get("body") or "").strip()
    cta       = email.get("cta", "Are you free for a 10-minute call?")

    hooks_str = _format_hook_ideas(d)

    entry = f"""---

### {i}. {company}
**Category:** {cat}  |  **Score:** {score}/100 ({grade})  |  **OOH Presence:** {ooh_str}

**SG USP:** {sg_usp}

**Send to:**
- Name: {dm_name}
- Title: {dm_title}
- Email: `{dm_email}`

**Subject A:** {subject_a}"""

    if subject_b:
        entry += f"\n**Subject B:** {subject_b}"

    entry += f"""

**Hook Ideas:**
{hooks_str}

**Email Body:**
```
{body}
```

**CTA:** {cta}
"""
    return entry


def build_queue_md(targets: list[dict], count: int, date_str: str) -> str:
    total = len(targets)
    cats  = {}
    for t in targets:
        c = t.get("lead_category", "Unknown")
        cats[c] = cats.get(c, 0) + 1

    cat_summary = "  |  ".join(f"{v} {k}" for k, v in cats.items())

    lines = [
        f"# Daily Outreach Queue — {date_str}",
        f"**Targets:** {total}  |  {cat_summary}",
        "",
        "> Tone: peer-to-peer, strategic, insider. No 'collaboration'. No numerical distances.",
        "> Framework: Batam Insider / Regional Discovery series for Media Feature leads.",
        "> Big brands: lead with mass curation + social proof loops.",
        "",
    ]

    for i, target in enumerate(targets, 1):
        lines.append(_format_entry(i, target))

    lines += [
        "---",
        "",
        f"*Queue generated: {date_str} | Viral Asia / The SG Daily*",
    ]

    return "\n".join(lines)


CSV_QUEUE_FIELDS = [
    "Company Name", "Lead Contact", "Role", "Email",
    "Lead Category", "Score", "OOH Presence", "SG USP",
    "Personalized Hook", "Subject A", "Subject B", "CTA",
]


def build_queue_csv(targets: list[dict]) -> list[dict]:
    rows = []
    for d in targets:
        dm    = _dm(d)
        email = _email(d)
        hooks = d.get("hook_ideas") or []

        if not hooks:
            # Extract from email body bullets
            body = email.get("body", "")
            hooks = [l.strip().lstrip("•-* ") for l in body.splitlines() if l.strip().startswith(("•", "-", "*"))]

        ooh = d.get("ooh_presence", {})

        rows.append({
            "Company Name":    d.get("company_name", ""),
            "Lead Contact":    dm.get("name",  email.get("to_name", "")),
            "Role":            dm.get("title", email.get("to_title", "")),
            "Email":           email.get("to_email") or dm.get("email_pattern", ""),
            "Lead Category":   d.get("lead_category", ""),
            "Score":           d.get("prospect_score", ""),
            "OOH Presence":    "Yes" if (isinstance(ooh, dict) and ooh.get("detected")) else "Unknown",
            "SG USP":          d.get("sg_usp", ""),
            "Personalized Hook": " | ".join(hooks[:3]),
            "Subject A":       email.get("subject_a") or email.get("subject", ""),
            "Subject B":       email.get("subject_b", ""),
            "CTA":             email.get("cta", "Are you free for a 10-minute call?"),
        })
    return rows


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Generate daily outreach queue")
    parser.add_argument("--mode",     default="sg-daily", help="Mode to queue for (default: sg-daily)")
    parser.add_argument("--count",    type=int, default=None,
                        help="Number of targets (default: mode's queue_size)")
    parser.add_argument("--category", type=str, default=None,
                        help="Optional lead_category filter")
    args = parser.parse_args()

    # Resolve count: arg > mode's queue_size > 8
    if args.count is None:
        mode_config = db.get_mode(args.mode)
        args.count  = mode_config["queue_size"] if mode_config else 8

    candidates = load_queue_candidates(args.mode, args.category)

    if not candidates:
        print(f"No completed analyses found for mode '{args.mode}'.")
        print(f"Run: python scripts/discover_leads.py --mode {args.mode}")
        print(f"Then: python scripts/run_batch.py --mode {args.mode}")
        sys.exit(0)

    targets   = candidates[:args.count]
    date_str  = datetime.now().strftime("%Y-%m-%d")
    queue_md  = QUEUE_DIR / f"DAILY-QUEUE-{args.mode}-{date_str}.md"
    queue_csv = RESULTS / f"queue-{args.mode}-{date_str}.csv"

    # Write markdown
    md_content = build_queue_md(targets, args.count, date_str)
    queue_md.write_text(md_content, encoding="utf-8")
    print(f"Queue MD:  {queue_md}")

    # Write CSV
    RESULTS.mkdir(exist_ok=True)
    rows = build_queue_csv(targets)
    with open(queue_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_QUEUE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Queue CSV: {queue_csv}")

    # Persist to DB
    db.save_queue(date_str, rows, md_content, mode=args.mode)

    # Summary
    cats = {}
    for t in targets:
        c = t.get("lead_category", "Unknown")
        cats[c] = cats.get(c, 0) + 1

    print(f"\n{len(targets)} targets queued:")
    for cat, n in cats.items():
        print(f"  {n}x {cat}")

    top = targets[0] if targets else None
    if top:
        dm = _dm(top)
        print(f"\nTop target: {top.get('company_name')} ({top.get('prospect_score')}/100) → {dm.get('name','?')}")


if __name__ == "__main__":
    main()
