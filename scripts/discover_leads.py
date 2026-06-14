#!/usr/bin/env python3
"""
Autonomous lead discovery. Loads the discovery prompt from the modes table in DB,
runs an agentic web-search loop, and appends qualified companies as pending leads.

Usage:
    python scripts/discover_leads.py --mode sg-daily --count 10
    python scripts/discover_leads.py --mode sg-daily --count 20 --dry-run
    python scripts/discover_leads.py --list-modes
"""

import asyncio
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
import db  # noqa: E402
from providers import resolve_model, run_agentic_loop, require_api_key  # noqa: E402

MODEL_TIER     = "fast"   # mapped to the active provider's model (see providers/)
MAX_TOKENS     = 3000
MAX_TOOL_CALLS = 15

# ── Web tools ─────────────────────────────────────────────────────────────────

def _fetch_url(url: str, max_chars: int = 8000) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "iframe", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return re.sub(r'\n{3,}', '\n\n', text)[:max_chars]
    except Exception as e:
        return f"[fetch error] {e}"


def _web_search(query: str, num_results: int = 8) -> str:
    if os.environ.get("BRAVE_API_KEY"):
        try:
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json",
                         "X-Subscription-Token": os.environ["BRAVE_API_KEY"]},
                params={"q": query, "count": num_results}, timeout=10,
            )
            if r.status_code == 200:
                items = r.json().get("web", {}).get("results", [])
                result = "\n\n".join(
                    f"Title: {i.get('title','')}\nURL: {i.get('url','')}\nSnippet: {i.get('description','')}"
                    for i in items
                )
                if result:
                    return result
        except Exception:
            pass

    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0",
                                        "Accept-Language": "en-US"}, timeout=10)
        soup = BeautifulSoup(r.content, "html.parser")
        results = []
        for div in soup.find_all("div", class_="result", limit=num_results):
            ta = div.find("a", class_="result__a")
            sa = div.find("a", class_="result__snippet")
            if not ta:
                continue
            href = ta.get("href", "")
            if "uddg=" in href:
                href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
            results.append(
                f"Title: {ta.get_text(strip=True)}\nURL: {href}\n"
                f"Snippet: {sa.get_text(strip=True) if sa else ''}"
            )
        return "\n\n".join(results) or "No results."
    except Exception as e:
        return f"[search error] {e}"


def _execute_tool(name: str, inputs: dict) -> str:
    if name == "fetch_url":  return _fetch_url(inputs["url"])
    if name == "web_search": return _web_search(inputs["query"], inputs.get("num_results", 8))
    return f"[unknown tool] {name}"


TOOLS = [
    {
        "name": "fetch_url",
        "description": "Fetch text content from a URL.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "web_search",
        "description": "Search the web for companies, news, and intelligence.",
        "parameters": {
            "type": "object",
            "properties": {
                "query":       {"type": "string"},
                "num_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
]

# ── Core discovery ─────────────────────────────────────────────────────────────

async def discover(num_leads: int, discovery_prompt: str, mode_name: str,
                   exclude: list[str] | None = None) -> list[dict]:
    prompt = discovery_prompt.replace("{NUM_LEADS}", str(num_leads))

    if exclude:
        prompt += (
            "\n\nALREADY IN THE DATABASE — do not suggest these companies/domains "
            "again; find different ones:\n"
            + "\n".join(f"- {d}" for d in exclude)
        )

    print(f"Scouting {num_leads} leads for mode '{mode_name}'...")
    print(f"Model: {resolve_model(MODEL_TIER)}  |  Tool call cap: {MAX_TOOL_CALLS}")

    def _log_tool(name: str, inputs: dict, n: int) -> None:
        print(f"  [{n}/{MAX_TOOL_CALLS}] [{name}] {str(inputs)[:80]}")

    full_text = await run_agentic_loop(
        prompt, TOOLS, _execute_tool,
        tier=MODEL_TIER, max_tokens=MAX_TOKENS, max_tool_calls=MAX_TOOL_CALLS,
        finish_instruction="You've done enough research. Output the JSON array now with the brands you've found.",
        on_tool=_log_tool,
    )

    m = re.search(r'```json\s*(\[.*?\])\s*```', full_text, re.DOTALL)
    if not m:
        m = re.search(r'(\[.*?\])', full_text, re.DOTALL)
    if not m:
        print("ERROR: Could not parse JSON from discovery response.")
        print("Raw response:", full_text[:500])
        return []

    try:
        leads = json.loads(m.group(1))
        return leads if isinstance(leads, list) else []
    except json.JSONDecodeError as e:
        print(f"ERROR: JSON parse failed: {e}")
        return []


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Autonomous lead discovery")
    parser.add_argument("--mode",       default="sg-daily", help="Mode name (must exist in DB)")
    parser.add_argument("--count",      type=int, default=None,
                        help="Total leads to find (overrides mode's discover_count)")
    parser.add_argument("--dry-run",    action="store_true", help="Preview without writing to DB")
    parser.add_argument("--list-modes", action="store_true", help="List available modes and exit")
    args = parser.parse_args()

    if args.list_modes:
        modes = db.get_modes()
        if not modes:
            print("No modes in DB.")
        for m in modes:
            status = "active" if m["is_active"] else "inactive"
            print(f"  {m['name']:20s}  {m['label']}  [{status}]  discover={m['discover_count']}")
        return

    require_api_key()

    mode_config = db.get_mode(args.mode)
    if not mode_config:
        sys.exit(f"ERROR: Mode '{args.mode}' not found in DB. Run --list-modes to see available modes.")

    num_leads = args.count if args.count is not None else mode_config["discover_count"]
    if num_leads <= 0:
        print(f"Mode '{args.mode}' has discover_count=0. Pass --count N to override.")
        return

    discovery_prompt = mode_config["discovery_prompt"]
    if not discovery_prompt.strip():
        sys.exit(f"ERROR: Mode '{args.mode}' has no discovery_prompt set.")

    # Over-fetch with retries: dedup eats into every batch (the model gravitates to
    # well-known names), so ask for double the shortfall, name the existing companies
    # as exclusions in the prompt, and go back for more until the quota of NEW leads is
    # met. Dedup is on canonical URL — against the DB and within the batch itself — so
    # www / slash / scheme variants collapse to one.
    seen        = db.get_existing_urls(mode=args.mode)
    new_leads:  list[dict] = []
    total_found = 0
    MAX_ATTEMPTS = 3

    for attempt in range(1, MAX_ATTEMPTS + 1):
        shortfall = num_leads - len(new_leads)
        if shortfall <= 0:
            break
        if attempt > 1:
            print(f"\n[attempt {attempt}/{MAX_ATTEMPTS}] {len(new_leads)}/{num_leads} new so far — scouting again...")
        leads = await discover(shortfall * 2, discovery_prompt, args.mode,
                               exclude=sorted(seen)[:120])
        total_found += len(leads)
        if not leads:
            break
        for l in leads:
            if len(new_leads) >= num_leads:
                break
            canon = db.canonicalize_url(l.get("url", ""))
            if not canon or canon in seen:
                continue
            seen.add(canon)
            new_leads.append(l)

    if total_found == 0:
        print("No leads found.")
        return

    dupes = total_found - len(new_leads)
    print(f"\nDiscovered {total_found} candidates | {dupes} already in DB / duplicate | {len(new_leads)} new\n")

    for lead in new_leads:
        cat = lead.get("lead_category", "")
        print(f"  [{cat[:1].upper() if cat else '?'}] {lead.get('company_name','?')} — {lead.get('url','')}")
        print(f"       {lead.get('sg_qualifier', lead.get('notes', ''))[:100]}")

    if args.dry_run:
        print("\n[dry-run] Not written to DB.")
        return

    if not new_leads:
        print("Nothing new to add.")
        return

    for lead in new_leads:
        lead["status"]         = "pending"
        lead["mode"]           = args.mode
        lead["outreach_status"] = "pending"
        db.upsert_lead(lead)

    print(f"\nAdded {len(new_leads)} new leads to DB ({db.DB_PATH})")
    print(f"Run: python scripts/run_batch.py --mode {args.mode}")


if __name__ == "__main__":
    asyncio.run(main())
