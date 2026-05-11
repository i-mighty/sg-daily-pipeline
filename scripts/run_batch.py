#!/usr/bin/env python3
"""
Batch prospect analyzer.
Reads prospects.csv, runs full AI analysis for each pending company,
saves MD + JSON + PDF to results/{company-slug}/.

Usage:
    python scripts/run_batch.py                         # all pending rows (generic mode)
    python scripts/run_batch.py --mode sg-daily         # SG Daily / Viral Asia mode
    python scripts/run_batch.py --limit 10              # up to 10 (highest priority first)
    python scripts/run_batch.py --concurrency 3         # parallel analyses (default: 3)
    python scripts/run_batch.py --retry-errors          # also re-run error rows

Modes:
    generic   — General B2B prospect analysis (default)
    sg-daily  — Viral Asia / The SG Daily: SG Dollar Filter, OOH research,
                Batam Insider narrative, insider-tone outreach with 3 hook ideas

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
from datetime import datetime
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
RESULTS  = BASE_DIR / "results"
SCRIPTS  = Path(__file__).parent

sys.path.insert(0, str(BASE_DIR))
import db  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────

MODEL           = "claude-sonnet-4-6"   # ~80% cheaper than Opus; fully capable for research + writing
MAX_TOKENS      = 8000                  # full report fits comfortably; Opus-level output not needed
MAX_TOOL_CALLS  = 20                    # hard cap — prevents runaway search loops
DEFAULT_CONC    = 3

# ── Web tool implementations ──────────────────────────────────────────────────

def _fetch_url(url: str, max_chars: int = 12000) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "iframe", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text[:max_chars]
    except Exception as e:
        return f"[fetch error] {e}"


def _web_search(query: str, num_results: int = 8) -> str:
    # Primary: DuckDuckGo HTML (no API key needed)
    # If BRAVE_API_KEY is set, uses Brave Search API instead (more reliable)
    if os.environ.get("BRAVE_API_KEY"):
        return _brave_search(query, num_results)
    return _ddg_search(query, num_results)


def _ddg_search(query: str, num_results: int) -> str:
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.content, "html.parser")
        results = []
        for div in soup.find_all("div", class_="result", limit=num_results):
            title_el   = div.find("a", class_="result__a")
            snippet_el = div.find("a", class_="result__snippet")
            if not title_el:
                continue
            href = title_el.get("href", "")
            if "uddg=" in href:
                href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            results.append(f"Title: {title_el.get_text(strip=True)}\nURL: {href}\nSnippet: {snippet}")
        return "\n\n".join(results) if results else "No results found."
    except Exception as e:
        return f"[search error] {e}"


def _brave_search(query: str, num_results: int) -> str:
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": os.environ["BRAVE_API_KEY"],
            },
            params={"q": query, "count": num_results},
            timeout=10,
        )
        if r.status_code != 200:
            raise ValueError(f"Brave API error {r.status_code}")
        data = r.json()
        results = []
        for item in data.get("web", {}).get("results", []):
            results.append(f"Title: {item.get('title', '')}\nURL: {item.get('url', '')}\nSnippet: {item.get('description', '')}")
        if not results:
            raise ValueError("Brave returned no results")
        return "\n\n".join(results)
    except Exception:
        return _ddg_search(query, num_results)


def _execute_tool(name: str, inputs: dict) -> str:
    if name == "fetch_url":
        return _fetch_url(inputs["url"])
    if name == "web_search":
        return _web_search(inputs["query"], inputs.get("num_results", 8))
    return f"[unknown tool] {name}"


# ── Tool schema for Anthropic API ─────────────────────────────────────────────

TOOLS = [
    {
        "name": "fetch_url",
        "description": (
            "Fetch the text content of any web page. Use for: company homepages, about/team/pricing/careers pages, "
            "news articles, LinkedIn profiles, job boards, Crunchbase, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web. Use for: finding news, funding announcements, job postings, tech stack signals, "
            "executives, reviews, competitor mentions, and general company intelligence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string"},
                "num_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
]

# ── Analysis prompt ───────────────────────────────────────────────────────────

# Uses simple {PLACEHOLDER} style — no .format() curly-brace escaping needed
ANALYSIS_PROMPT = """You are a world-class B2B sales intelligence analyst. Perform a comprehensive prospect analysis.

TARGET: {URL}
COMPANY NAME HINT: {COMPANY_NAME}
INDUSTRY HINT: {INDUSTRY_HINT}
ADDITIONAL CONTEXT: {NOTES}

## Phase 1 — Research (use tools extensively, at least 12-15 calls)

1. Fetch the company homepage
2. Fetch About, Team/Leadership, Pricing, Careers, Blog pages if they exist
3. Search: "{COMPANY_NAME} funding news 2024 2025"
4. Search: "{COMPANY_NAME} CEO CTO CPO leadership team"
5. Search: "{COMPANY_NAME} technology stack software tools"
6. Search: "{COMPANY_NAME} competitors alternatives"
7. Search: "{COMPANY_NAME} job postings hiring" (reveals stack + growth)
8. Search executives on LinkedIn (names + company)
9. Search: "{COMPANY_NAME} customer case study testimonials"
10. Fetch any relevant news articles or press releases found

## Phase 2 — Analysis

Score the company across 5 dimensions (each 0-100):

| Dimension | Weight | What to score |
|-----------|--------|---------------|
| Company Fit | 25% | Size fit, industry match, growth trajectory, tech sophistication, budget signals |
| Contact Access | 20% | Decision makers found, email patterns confirmed, personalization anchors, warm paths |
| Opportunity Quality | 20% | BANT: Budget evidence, Authority clarity, Need strength, Timeline urgency |
| Competitive Position | 15% | Current solutions identified, switching cost, competitive gaps we can exploit |
| Outreach Readiness | 20% | Personalization depth, trigger events found, channel strategy clarity |

Prospect Score = Company_Fit*0.25 + Contact_Access*0.20 + Opportunity_Quality*0.20 + Competitive_Position*0.15 + Outreach_Readiness*0.20

Grades: 90-100=A+(Hot Lead) | 75-89=A(Strong Prospect) | 60-74=B(Qualified Lead) | 40-59=C(Lukewarm) | 0-39=D(Poor Fit)

## Phase 3 — Output

Produce TWO sections in your response:

---
### SECTION 1: MARKDOWN REPORT

Write a complete PROSPECT-ANALYSIS.md with these sections exactly:

# Prospect Analysis: [Company Name]
**URL:** [url]  **Date:** {TODAY}  **Company Type:** [type]  **Industry:** [industry]
**Prospect Score: [X]/100 (Grade: [letter] — [label])**  **Confidence:** [High/Medium/Low]

## Executive Summary
[3-5 paragraphs. Lead with score and grade. Biggest opportunity, biggest risk, recommended approach, top DM to target, timing. End with clear go/no-go recommendation.]

## Prospect Snapshot
[Table: Company, Website, Industry, Company Type, Founded, Employees, Funding, Revenue Est., HQ, Key Decision Maker, Prospect Score, Recommended Action]

## Score Breakdown
[Table: Category | Score | Weight | Weighted | Key Finding — for all 5 dimensions + TOTAL row]

## Company Profile
[Overview, business model, product/tech, funding history, market position, recent developments]

## Decision Maker Map
[Buying committee table: Name | Title | Buying Role | Personalization Anchor | Approach Strategy]
[ASCII org chart]
[Top 3 priority contacts with full profiles: name, title, LinkedIn summary, personalization anchors, first message suggestion]

## Opportunity Assessment
[BANT scorecard table: Dimension | Score | Evidence | Confidence]
[MEDDIC table: Element | Finding | Evidence | Confidence]
[Buying Signals — bullet list]
[Red Flags — bullet list]

## Competitive Landscape
[Current solutions table]
[Switching cost analysis]
[Competitive gaps we can exploit]
[Positioning angles]

## Outreach Strategy
[Framework selection and why]
[Channel strategy]
[Personalization hooks — numbered list]
[Trigger events detected — with dates]
[Objection prep — top 3]

## Prioritized Action Plan
### Immediate (Next 24-48 Hours)
[5 specific actions]
### Short-Term (Next 1-2 Weeks)
[5 specific actions]
### Long-Term (Next 1-3 Months)
[3 specific actions]

## Ready-to-Send Email
[Complete email — NOT a template. Real names, real data.]
To: [Name] <[email]> — [Title]
Subject A: [subject]
Subject B: [subject]
---
[Email body — under 100 words, specific, personalized, clear CTA]
---

---
### SECTION 2: JSON DATA

After the markdown, output a JSON block with this EXACT structure:

```json
{
  "company_name": "",
  "url": "",
  "analysis_date": "{TODAY}",
  "company_type": "",
  "industry": "",
  "founded": "",
  "employees": "",
  "funding": "",
  "revenue_estimate": "",
  "hq_location": "",
  "prospect_score": 0,
  "grade": "",
  "label": "",
  "confidence": "",
  "scores": {
    "company_fit":           {"score": 0, "key_finding": ""},
    "contact_access":        {"score": 0, "key_finding": ""},
    "opportunity_quality":   {"score": 0, "key_finding": ""},
    "competitive_position":  {"score": 0, "key_finding": ""},
    "outreach_readiness":    {"score": 0, "key_finding": ""}
  },
  "bant": {
    "budget":    {"score": 0, "evidence": ""},
    "authority": {"score": 0, "evidence": ""},
    "need":      {"score": 0, "evidence": ""},
    "timeline":  {"score": 0, "evidence": ""},
    "total": 0
  },
  "key_decision_maker": {
    "name": "", "title": "", "email_pattern": "", "linkedin": ""
  },
  "buying_committee": [
    {"name": "", "title": "", "role": "", "personalization_anchor": ""}
  ],
  "buying_signals": [],
  "red_flags": [],
  "current_solutions": [],
  "competitive_gaps": [],
  "outreach_email": {
    "to_name": "", "to_title": "", "to_email": "",
    "subject_a": "", "subject_b": "",
    "body": ""
  },
  "immediate_actions": [],
  "recommended_action": ""
}
```

Be specific and actionable throughout. Generic observations are useless — cite real data found during research.
"""


# ── SG Daily analysis prompt ──────────────────────────────────────────────────

SG_DAILY_PROMPT = """You are a strategic business development analyst for Viral Asia and "The SG Daily" —
a premium media brand targeting high-spending Singaporean travelers on regional weekend trips
(Batam, Bintan, Johor Bahru, Desaru, Penang).

TARGET COMPANY: {URL}
COMPANY NAME: {COMPANY_NAME}
LEAD CATEGORY HINT: {LEAD_CATEGORY}
INDUSTRY HINT: {INDUSTRY_HINT}
CONTEXT: {NOTES}

## ABOUT VIRAL ASIA / THE SG DAILY
Two revenue streams:
1. **Media Feature Leads** — $1,000 SGD: Brand gets an editorial feature in "Batam Insider" or
   "Regional Discovery" series, positioned as a must-visit for Singaporean travelers.
2. **Influencer Management Leads** — Larger engagement: Brands running OOH ads in Singapore get
   "digital surround sound" — mass content production, social proof loops, influencer amplification.

## RESEARCH PHASE (8-12 targeted tool calls — quality over quantity)

1. Fetch company homepage + About/Contact pages
2. Fetch pricing page or look for pricing signals
3. Fetch any Singapore-specific landing page or promotions page
4. Search: "{COMPANY_NAME} Singapore tourists marketing 2024 2025"
5. Search: "{COMPANY_NAME} OOH billboard bus stop MRT advertising Singapore"
6. Search: "{COMPANY_NAME} director sales marketing manager email contact"
7. Search for decision makers on LinkedIn (marketing/sales/partnerships roles)
8. Search: "{COMPANY_NAME} Singapore promotion deal package Singaporean"
9. Search: "{COMPANY_NAME} reviews Singapore visitors experience"
10. For Category B: Search for OOH industry news mentioning this brand in Singapore
11. Fetch their social media or news page for recent Singapore-market activity
12. Look for press releases or news articles confirming OOH spend or Singapore marketing push
13. Search: "{COMPANY_NAME} CEO founder marketing director contact"
14. Verify email format from press releases, LinkedIn, or contact pages

## SG DOLLAR FILTER — CLASSIFY THIS BRAND

**Media Feature Lead**: High-end lifestyle brand at/near Singapore. Confirm:
- Pricing signals supporting $1,000 SGD editorial partnership (rooms > $120/night, treatments > $200, golf > $80/round)
- Specifically targets or benefits from Singaporean visitors
- Exclusivity positioning (not budget/mass market)

**Influencer Management Lead**: Global/tech brand advertising in Singapore. Confirm:
- Evidence of current OOH (bus stop, MRT, billboard) or heavy digital ads in Singapore
- Would benefit from mass content production and social proof amplification
- Budget scale suggests investment in digital alongside OOH

**Disqualified**: Budget brand, no Singapore relevance, or no verifiable web presence.

## RESEARCH PRIORITIES

### OOH Presence (Category B focus)
Rate: Confirmed / Likely / Unlikely / Unknown
Evidence: specific ad sightings, industry news, job postings for Singapore marketing roles

### SG-Specific USP
Identify the single strongest hook for a Singaporean customer. Use time-based metrics ONLY.
Good examples:
- "Accessible from Singapore in under 40 mins by direct ferry — no crowds, no queues"
- "Private golf course that Singaporean members access for a fraction of local club fees"
- "Fee-free currency exchange that thousands of Singaporeans already use weekly"
BAD: "Located 54km from Singapore" (NEVER use numerical distances)

### Decision Maker Research
Find: Marketing Director, Director of Sales, Regional Growth Lead, Head of Partnerships, Marketing Manager
NOT: info@, reservations@, or generic contact emails
Check: LinkedIn, press releases, team pages, news articles, company blog bylines

## OUTPUT FORMAT

### SECTION 1: MARKDOWN REPORT

# SG Daily Prospect: [Company Name]
**URL:** [url]  **Date:** {TODAY}  **Lead Category:** [Media Feature Lead / Influencer Management Lead]
**Prospect Score: [X]/100 (Grade: [letter])**  **OOH Presence:** [Confirmed/Likely/Unknown]

## Executive Summary
[3 paragraphs: what this brand is, why it's a fit for The SG Daily, the specific pitch angle.
Recommend Media Feature OR Influencer Management. Go/no-go on the $1,000 SGD outreach.]

## Company Overview
[Business description, location, target market, key offerings, Singapore relevance, recent news]

## SG Dollar Filter Assessment
| Criterion | Status | Evidence |
|-----------|--------|----------|
| Lead Category | [Media Feature / Influencer Mgmt] | [specific evidence] |
| SG USP | [the hook] | [source] |
| OOH Ad Presence | [Confirmed/Likely/Unknown] | [specific evidence or "not detected"] |
| Budget Signal | [passes/fails $1,000 SGD threshold] | [pricing evidence] |
| Decision Maker Found | [name or "not found"] | [source] |

## Decision Maker
**Primary Contact:**
- Name: [full name]
- Title: [exact title]
- Email: [email or best guess with format]
- LinkedIn: [url if found]
- Personalization Anchor: [specific detail — recent post, company milestone, Singapore campaign]

**Backup Contact:**
- Name: [if found]
- Title: [title]
- Email: [email]

## Three Hook Ideas
[Three specific, punchy content angles we'd pitch — tailored to this brand's Singapore audience]

1. **[Hook Name]** — [1-2 sentence description of the content angle and why it works]
2. **[Hook Name]** — [1-2 sentence description]
3. **[Hook Name]** — [1-2 sentence description]

For Influencer Management leads: frame around mass curation, social proof loops, and digital surround sound.
For Media Feature leads: frame around the Batam Insider / Regional Discovery editorial series.

## Outreach Email

To: [Name] <[email]> — [Title]
Subject: [Direct subject line — no spam words, no "collaboration"]
---
[Email body — peer-to-peer, insider tone. 3 bulleted hook ideas. Under 120 words total.
NEVER use "collaboration", "hidden gems", or numerical distances.
End with: "Are you free for a 10-minute call?"]
---

## Action Plan
**Send within 48 hours:**
- [Specific action 1]
- [Specific action 2]
- [Specific action 3]

---
### SECTION 2: JSON DATA

```json
{
  "company_name": "",
  "url": "",
  "analysis_date": "{TODAY}",
  "mode": "sg-daily",
  "lead_category": "Media Feature Lead | Influencer Management Lead",
  "company_type": "",
  "industry": "",
  "founded": "",
  "employees": "",
  "hq_location": "",
  "prospect_score": 0,
  "grade": "",
  "label": "",
  "confidence": "",
  "sg_dollar_filter": {
    "passes": true,
    "budget_signal": "",
    "rationale": ""
  },
  "ooh_presence": {
    "detected": false,
    "evidence": "",
    "channels": []
  },
  "sg_usp": "",
  "hook_ideas": ["", "", ""],
  "scores": {
    "company_fit":           {"score": 0, "key_finding": ""},
    "contact_access":        {"score": 0, "key_finding": ""},
    "opportunity_quality":   {"score": 0, "key_finding": ""},
    "competitive_position":  {"score": 0, "key_finding": ""},
    "outreach_readiness":    {"score": 0, "key_finding": ""}
  },
  "bant": {
    "budget":    {"score": 0, "evidence": ""},
    "authority": {"score": 0, "evidence": ""},
    "need":      {"score": 0, "evidence": ""},
    "timeline":  {"score": 0, "evidence": ""},
    "total": 0
  },
  "key_decision_maker": {
    "name": "", "title": "", "email_pattern": "", "linkedin": "",
    "personalization_anchor": ""
  },
  "buying_committee": [
    {"name": "", "title": "", "role": "", "personalization_anchor": ""}
  ],
  "buying_signals": [],
  "red_flags": [],
  "outreach_email": {
    "to_name": "",
    "to_title": "",
    "to_email": "",
    "subject_a": "",
    "subject_b": "",
    "hook_ideas": ["", "", ""],
    "body": "",
    "cta": "Are you free for a 10-minute call?"
  },
  "immediate_actions": [],
  "recommended_action": "",
  "outreach_status": "pending"
}
```

TONE RULES (never break these):
- Peer-to-peer, strategic, insider. Never corporate or grovelling.
- Never use: "collaboration", "hidden gems", "synergy", numerical distances (km/miles)
- Always use: time-based proximity ("40 mins away"), specific numbers, insider language
- CTAs must be low-friction: "Are you free for a 10-minute call?" not "Please consider our proposal"
"""


def slugify(text: str) -> str:
    text = re.sub(r'[^\w\s-]', '', text.lower().strip())
    return re.sub(r'[\s_-]+', '-', text)[:50].strip('-')


# ── Core analysis ─────────────────────────────────────────────────────────────

def _build_prompt(url: str, company_name: str, industry_hint: str, notes: str,
                  mode: str = "generic", lead_category: str = "") -> str:
    today    = datetime.now().strftime("%Y-%m-%d")
    template = SG_DAILY_PROMPT if mode == "sg-daily" else ANALYSIS_PROMPT
    return (template
        .replace("{URL}", url)
        .replace("{COMPANY_NAME}", company_name or url)
        .replace("{INDUSTRY_HINT}", industry_hint or "Unknown")
        .replace("{NOTES}", notes or "None")
        .replace("{LEAD_CATEGORY}", lead_category or "Unknown")
        .replace("{TODAY}", today)
    )


def _parse_response(text: str, url: str, company_name: str) -> tuple[str, dict]:
    """Split full response text into (markdown_content, json_dict)."""
    json_data: dict = {}

    # Extract JSON block
    m = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            json_data = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Extract markdown (before JSON block or before SECTION 2)
    md = text[:m.start()].strip() if m else text.strip()

    # Strip SECTION 1 header if present
    if "SECTION 1:" in md:
        md = md.split("SECTION 1:", 1)[1]
        if "SECTION 2:" in md:
            md = md.split("SECTION 2:", 1)[0]
        md = md.strip()

    # Fallbacks
    json_data.setdefault("url", url)
    json_data.setdefault("company_name", company_name)
    json_data.setdefault("analysis_date", datetime.now().strftime("%Y-%m-%d"))

    return md, json_data


async def _run_agentic_loop(client: anthropic.AsyncAnthropic, prompt: str,
                            model: str = MODEL, max_tool_calls: int = MAX_TOOL_CALLS) -> str:
    """Run multi-turn conversation until the model stops calling tools or hits the cap."""
    messages     = [{"role": "user", "content": prompt}]
    full_text    = ""
    tool_call_n  = 0

    while True:
        response = await client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            tools=TOOLS,
            messages=messages,
        )

        for block in response.content:
            if hasattr(block, "text"):
                full_text += block.text

        if response.stop_reason != "tool_use":
            break

        # Execute tool calls
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_call_n += 1
                result = _execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})

        # Hard cap: force the model to write its output with what it has
        if tool_call_n >= max_tool_calls:
            messages.append({"role": "user", "content": tool_results + [{
                "type": "text",
                "text": "You've completed enough research. Now write your full output using the data gathered."
            }]})
            final = await client.messages.create(
                model=model, max_tokens=MAX_TOKENS, messages=messages
            )
            for block in final.content:
                if hasattr(block, "text"):
                    full_text += block.text
            break

        messages.append({"role": "user", "content": tool_results})

    return full_text


async def analyze_company(row: dict, semaphore: asyncio.Semaphore,
                          mode_override: str = "") -> dict:
    """Full analysis for one company. Mutates and returns the row."""
    async with semaphore:
        url           = row["url"].strip()
        company_name  = row.get("company_name", "").strip()
        industry      = row.get("industry_hint", "")
        notes         = row.get("notes", "")
        lead_category = row.get("lead_category", "")

        # Mode: explicit override > row's mode column > "generic"
        mode = mode_override or row.get("mode", "generic").strip().lower() or "generic"

        label = company_name or url
        print(f"[START] {label}  [{mode}]")

        row["status"]        = "running"
        row["analysis_date"] = datetime.now().strftime("%Y-%m-%d")
        row["mode"]          = mode

        try:
            client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            prompt = _build_prompt(url, company_name, industry, notes, mode, lead_category)
            full_text = await _run_agentic_loop(client, prompt)

            md_content, json_data = _parse_response(full_text, url, company_name)

            # Write output files
            slug    = slugify(json_data.get("company_name") or company_name or "unknown")
            out_dir = RESULTS / slug
            out_dir.mkdir(parents=True, exist_ok=True)

            (out_dir / "PROSPECT-ANALYSIS.md").write_text(md_content, encoding="utf-8")
            (out_dir / "prospect-data.json").write_text(json.dumps(json_data, indent=2), encoding="utf-8")

            # PDF
            sys.path.insert(0, str(SCRIPTS))
            from generate_pdf import generate_pdf
            generate_pdf(md_content, json_data, str(out_dir / "prospect-analysis.pdf"))

            # Update DB
            dm  = json_data.get("key_decision_maker", {})
            ooh = json_data.get("ooh_presence", {})
            updates = {
                "url":               url,
                "status":            "done",
                "company_name":      json_data.get("company_name", company_name),
                "prospect_score":    json_data.get("prospect_score"),
                "grade":             json_data.get("grade", ""),
                "label":             json_data.get("label", ""),
                "lead_category":     json_data.get("lead_category", lead_category),
                "ooh_presence":      "Yes" if (isinstance(ooh, dict) and ooh.get("detected")) else "",
                "sg_usp":            json_data.get("sg_usp", ""),
                "key_decision_maker": dm.get("name", "") if isinstance(dm, dict) else "",
                "recommended_action": json_data.get("recommended_action", ""),
                "outreach_status":   json_data.get("outreach_status", "pending"),
                "analysis_date":     row["analysis_date"],
                "output_folder":     str(out_dir),
                "error_message":     "",
                "analysis_json":     json.dumps(json_data),
            }
            db.upsert_lead(updates)
            row.update(updates)

            print(f"[DONE]  {label} — {row['prospect_score']}/100 ({row['grade']})")

        except Exception as e:
            db.upsert_lead({"url": url, "status": "error", "error_message": str(e)})
            row["status"]        = "error"
            row["error_message"] = str(e)
            print(f"[ERROR] {label}: {e}", file=sys.stderr)

        return row


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Batch prospect analyzer")
    parser.add_argument("--mode",         default="",  help="Analysis mode: generic (default) or sg-daily")
    parser.add_argument("--limit",        type=int,  default=None, help="Max companies to process")
    parser.add_argument("--concurrency",  type=int,  default=DEFAULT_CONC)
    parser.add_argument("--retry-errors", action="store_true", help="Re-run error rows")
    args = parser.parse_args()

    mode_override = args.mode.strip().lower() if args.mode else ""

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set.\nSet it with: export ANTHROPIC_API_KEY=sk-ant-...")

    RESULTS.mkdir(exist_ok=True)

    valid_statuses = ["pending"]
    if args.retry_errors:
        valid_statuses.append("error")

    all_pending = []
    for s in valid_statuses:
        all_pending.extend(db.get_leads(status=s))

    to_process = all_pending

    if mode_override:
        to_process = [r for r in to_process
                      if not r.get("mode") or r.get("mode", "").lower() == mode_override]

    if args.limit:
        order = {"high": 0, "medium": 1, "low": 2, "": 3}
        to_process.sort(key=lambda r: order.get((r.get("priority") or "").lower(), 3))
        to_process = to_process[:args.limit]

    if not to_process:
        print("No pending companies in DB. Run discover_leads.py first.")
        return

    mode_label = mode_override or "generic (per-row)"
    print(f"\nAnalyzing {len(to_process)} companies  |  mode={mode_label}  |  concurrency={args.concurrency}  |  model={MODEL}\n")

    semaphore = asyncio.Semaphore(args.concurrency)
    updated   = await asyncio.gather(*[analyze_company(r, semaphore, mode_override) for r in to_process])

    done  = sum(1 for r in updated if r.get("status") == "done")
    error = sum(1 for r in updated if r.get("status") == "error")
    print(f"\nComplete: {done} done, {error} errors.")


if __name__ == "__main__":
    asyncio.run(main())
