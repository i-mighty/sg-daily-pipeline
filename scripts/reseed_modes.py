#!/usr/bin/env python3
"""
Re-seed the shipped default modes into the live DB.

db._seed_default_modes only inserts when the modes table is empty, so an existing
deployment never picks up prompt changes (e.g. the new === RESEARCH/CONTACT/SCORING/
OUTPUT === section markers the staged analysis pipeline relies on). This pushes the
current DEFAULT_MODES prompts in via upsert_mode (ON CONFLICT DO UPDATE).

Only modes present in default_modes.py are touched (i.e. `generic`). Customer modes
created via the Modes UI are stored in the DB and are NOT affected — re-section those
through the UI to get the staged-pipeline quality boost (un-sectioned prompts still
run, falling back to today's single-pass behavior).

Usage:
    python scripts/reseed_modes.py            # show what would change
    python scripts/reseed_modes.py --apply    # write the changes
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from analysis_scaffold import has_sections  # noqa: E402
from default_modes import DEFAULT_MODES  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Re-seed shipped default modes into the DB.")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is dry-run).")
    args = parser.parse_args()

    db.init_db()
    existing = {m["name"]: m for m in db.get_modes()}

    for m in DEFAULT_MODES:
        name = m["name"]
        cur = existing.get(name)
        sectioned = has_sections(m["analysis_prompt"])
        if cur is None:
            action = "INSERT (new)"
        elif cur.get("analysis_prompt") == m["analysis_prompt"]:
            print(f"  = {name}: unchanged, skipping")
            continue
        else:
            was = "sectioned" if has_sections(cur.get("analysis_prompt", "")) else "legacy/whole-prompt"
            action = f"UPDATE ({was} -> {'sectioned' if sectioned else 'whole-prompt'})"

        if args.apply:
            db.upsert_mode(m)
            print(f"  ✓ {name}: {action}")
        else:
            print(f"  [dry-run] {name}: {action}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
