#!/usr/bin/env python3
"""
Autonomous lead discovery for Viral Asia / The SG Daily.
Searches the web for brands matching the "SG Dollar Filter" and appends
qualified companies to prospects.csv as pending rows.

Usage:
    python scripts/discover_leads.py               # 10 leads (5 each category)
    python scripts/discover_leads.py --count 20    # 20 leads total
    python scripts/discover_leads.py --cat a       # only Media Feature leads
    python scripts/discover_leads.py --cat b       # only Influencer Management leads
    python scripts/discover_leads.py --dry-run     # preview without writing CSV

Requirements:
    ANTHROPIC_API_KEY env var must be set.
    source .venv/bin/activate
"""

import asyncio
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
import db  # noqa: E402

MODEL           = "claude-haiku-4-5-20251001"  # ~95% cheaper than Opus; plenty for search + JSON output
MAX_TOKENS      = 3000                          # just needs a JSON array, not a long report
MAX_TOOL_CALLS  = 15                            # hard cap per discovery run

# ── Web tools (same implementations as run_batch.py) ─────────────────────────

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
                headers={"Accept": "application/json", "X-Subscription-Token": os.environ["BRAVE_API_KEY"]},
                params={"q": query, "count": num_results}, timeout=10,
            )
            if r.status_code == 200:
                items = r.json().get("web", {}).get("results", [])
                result = "\n\n".join(f"Title: {i.get('title','')}\nURL: {i.get('url','')}\nSnippet: {i.get('description','')}" for i in items)
                if result:
                    return result
        except Exception:
            pass
        # Fall through to DuckDuckGo

    # Fallback: DuckDuckGo
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"}, timeout=10)
        soup = BeautifulSoup(r.content, "html.parser")
        results = []
        for div in soup.find_all("div", class_="result", limit=num_results):
            ta = div.find("a", class_="result__a")
            sa = div.find("a", class_="result__snippet")
            if not ta: continue
            href = ta.get("href", "")
            if "uddg=" in href:
                href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
            results.append(f"Title: {ta.get_text(strip=True)}\nURL: {href}\nSnippet: {sa.get_text(strip=True) if sa else ''}")
        return "\n\n".join(results) or "No results."
    except Exception as e:
        return f"[search error] {e}"


def _execute_tool(name: str, inputs: dict) -> str:
    if name == "fetch_url":   return _fetch_url(inputs["url"])
    if name == "web_search":  return _web_search(inputs["query"], inputs.get("num_results", 8))
    return f"[unknown tool] {name}"


TOOLS = [
    {
        "name": "fetch_url",
        "description": "Fetch text content from a URL. Use for company websites, resort pages, news articles, review sites.",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "web_search",
        "description": "Search the web. Use for finding brands, OOH ad evidence, resort listings, fintech campaigns.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
]

# ── Discovery prompt ──────────────────────────────────────────────────────────

DISCOVERY_PROMPT = """You are a lead scout for Viral Asia and "The SG Daily" — a premium media brand
targeting high-spending Singaporean travelers who take regular weekend and short-break trips to regional
destinations (Batam, Bintan, Johor Bahru, Desaru, Penang, Langkawi).

## YOUR MISSION
Find {NUM_LEADS} qualified brands for our "SG Dollar Filter." Use 10-15 focused searches — be precise, not exhaustive.
searches and page fetches. Quality over speed: only include brands you have actually verified exist and qualify.

---

## CATEGORY A — MEDIA FEATURE LEADS (target: {MEDIA_COUNT})
High-end lifestyle brands at or near Singapore's weekend destinations.
We pitch them a $1,000 SGD editorial feature in our "Batam Insider" or "Regional Discovery" content series.

Target sub-categories:
- **Luxury resorts / boutique hotels** in Batam, Bintan, Johor Bahru, Desaru (rooms > $120 SGD/night)
- **Fine dining** in Johor Bahru or Batam targeting Singaporean diners
- **Elite golf clubs** in Johor or Bintan (green fees > $80 SGD)
- **Aesthetic clinics / medical tourism** providers in Malaysia or Indonesia marketing to Singaporeans
- **Unique experience operators** (yacht charters, private islands, luxury spas, helicopter tours)

Qualification gate: Pricing signals that margins support a $1,000 SGD editorial partnership.

---

## CATEGORY B — INFLUENCER MANAGEMENT LEADS (target: {INFLUENCER_COUNT})
Global or tech-forward brands actively advertising to Singaporeans via Out-of-Home (OOH).
We pitch them mass content production and "digital surround sound" to complement their OOH spend.

Target sub-categories:
- **Fintech / crypto / trading apps** with Singapore bus stop or MRT station ads
- **Travel booking platforms** running Singapore billboard or transit advertising
- **FMCG premium brands** with active Singapore OOH campaigns
- **Airlines or ferry operators** with heavy Singapore market advertising
- **Property or investment platforms** running Singapore OOH campaigns

Qualification gate: Evidence of current or recent OOH advertising in Singapore.

---

## RESEARCH PROCESS — EXECUTE ALL OF THESE

**Category A searches:**
1. Search: "luxury resort Batam Indonesia Singapore tourists 2024 2025"
2. Search: "boutique hotel Bintan Singapore weekend 2025 best"
3. Search: "best fine dining Johor Bahru Singapore tourists 2025"
4. Search: "golf club Johor Bahru Singapore members visitors"
5. Search: "aesthetic clinic Johor Bahru medical tourism Singaporeans 2025"
6. Search: "private villa Batam Bintan weekend Singapore"
7. Search: "Desaru resort Singapore weekend getaway luxury"
8. Fetch the top 2-3 most promising resort or hotel websites to verify pricing and quality

**Category B searches:**
9. Search: "OOH advertising Singapore bus stop brands 2024 2025"
10. Search: "fintech billboard MRT station Singapore advertising 2025"
11. Search: "Singapore outdoor advertising campaign brands 2025"
12. Search: "FMCG brand OOH campaign Singapore 2025"
13. Search: "travel app advertising Singapore bus stop billboard"
14. Search: "ferry Batam Singapore OOH advertising campaign"
15. Fetch 2-3 OOH news/industry pages to find specific brand names running Singapore campaigns

**Verification:**
16. For each shortlisted brand, fetch their website to confirm: active, real web presence, correct category
17. Look for pricing pages, contact pages, or press mentions to confirm qualification

---

## DISQUALIFICATION RULES
- Generic/budget brands (hostels, fast food, mass market)
- Brands with no accessible website (Facebook-only is insufficient)
- Category A brands with no pricing signals above the threshold
- Category B brands with no evidence of OOH advertising
- Brands already in the Singapore domestic market only (we need regional/destination brands for Cat A)

---

## OUTPUT
After all your research, return ONLY a valid JSON array (no other text before or after):

```json
[
  {
    "company_name": "Exact Brand Name",
    "url": "https://www.website.com",
    "lead_category": "Media Feature Lead",
    "industry_hint": "Luxury Resort / Fintech / etc.",
    "priority": "High",
    "sg_qualifier": "One specific sentence on why this passes the SG Dollar Filter",
    "notes": "Key intelligence: pricing signals, OOH evidence, location, target market, unique angle"
  }
]
```

Return exactly {NUM_LEADS} brands. Only include brands you verified during research.
"""

# ── Core discovery ────────────────────────────────────────────────────────────

async def discover(num_leads: int, media_count: int, influencer_count: int) -> list[dict]:
    prompt = (DISCOVERY_PROMPT
        .replace("{NUM_LEADS}", str(num_leads))
        .replace("{MEDIA_COUNT}", str(media_count))
        .replace("{INFLUENCER_COUNT}", str(influencer_count))
    )

    client   = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [{"role": "user", "content": prompt}]
    full_text = ""

    print(f"Scouting {num_leads} leads ({media_count} Media Feature + {influencer_count} Influencer Mgmt)...")
    print(f"Model: {MODEL}  |  Tool call cap: {MAX_TOOL_CALLS}")

    tool_call_n = 0

    while True:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            tools=TOOLS,
            messages=messages,
        )

        for block in response.content:
            if hasattr(block, "text"):
                full_text += block.text

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_call_n += 1
                print(f"  [{tool_call_n}/{MAX_TOOL_CALLS}] [{block.name}] {str(block.input)[:80]}")
                result = _execute_tool(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})

        # Hard cap — force output with data gathered so far
        if tool_call_n >= MAX_TOOL_CALLS:
            messages.append({"role": "user", "content": tool_results + [{
                "type": "text",
                "text": "You've done enough research. Output the JSON array now with the brands you've found."
            }]})
            final = await client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS, messages=messages
            )
            for block in final.content:
                if hasattr(block, "text"):
                    full_text += block.text
            break

        messages.append({"role": "user", "content": tool_results})

    # Parse JSON from response
    m = re.search(r'```json\s*(\[.*?\])\s*```', full_text, re.DOTALL)
    if not m:
        # Try to find a raw JSON array
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

    parser = argparse.ArgumentParser(description="Autonomous lead discovery for The SG Daily")
    parser.add_argument("--count",   type=int, default=10, help="Total leads to find (default: 10)")
    parser.add_argument("--cat",     choices=["a", "b", "both"], default="both", help="a=Media Feature, b=Influencer Mgmt")
    parser.add_argument("--dry-run", action="store_true", help="Preview leads without writing to CSV")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set.")

    if args.cat == "a":
        media_count, influencer_count = args.count, 0
    elif args.cat == "b":
        media_count, influencer_count = 0, args.count
    else:
        media_count      = args.count // 2
        influencer_count = args.count - media_count

    leads = await discover(args.count, media_count, influencer_count)

    if not leads:
        print("No leads found. Check your API key and try again.")
        return

    # Deduplicate against DB
    existing_urls = db.get_existing_urls()
    new_leads     = [l for l in leads if l.get("url", "").strip().lower() not in existing_urls]
    dupes         = len(leads) - len(new_leads)

    print(f"\nDiscovered {len(leads)} leads | {dupes} already in DB | {len(new_leads)} new\n")

    for lead in new_leads:
        cat = lead.get("lead_category", "")
        print(f"  [{cat[:1].upper()}] {lead.get('company_name','?')} — {lead.get('url','')}")
        print(f"       {lead.get('sg_qualifier','')}")

    if args.dry_run:
        print("\n[dry-run] Not written to DB.")
        return

    if not new_leads:
        print("Nothing new to add.")
        return

    for lead in new_leads:
        lead.setdefault("status", "pending")
        lead.setdefault("mode", "sg-daily")
        lead.setdefault("outreach_status", "pending")
        db.upsert_lead(lead)

    print(f"\nAdded {len(new_leads)} new leads to DB ({db.DB_PATH})")
    print("Run 'python scripts/run_batch.py --mode sg-daily' to analyze them.")


if __name__ == "__main__":
    asyncio.run(main())
