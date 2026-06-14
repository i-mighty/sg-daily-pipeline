"""
Shared web research tools — fetch, search, and multi-surface enrichment.

Used by both analysis paths:
  - live  (scripts/run_batch.py): the agentic loop calls these as tools; the
    contact stage uses enrich_contact() for targeted decision-maker research.
  - batch (scripts/batch_submit.py): deep_enrich() / enrich_contact() do the tool
    work the OpenAI Batch API can't do inside a request, between rounds.

Search (Brave when BRAVE_API_KEY is set, else DuckDuckGo) is the reliable signal;
direct page fetches are best-effort because many sites block scrapers. For contact
data especially, off-site sources (LinkedIn, contact databases, news) beat the
company's own website.
"""

import os
import re
import urllib.parse

import requests
from bs4 import BeautifulSoup

MAX_PAGE_CHARS   = 6000
MAX_SEARCH_CHARS = 2000
MAX_SEARCH_HITS  = 5

# Subpages worth pulling for firmographics + decision makers + email pattern.
ENRICH_PATHS = [
    "/about", "/about-us", "/company", "/team", "/our-team", "/leadership",
    "/people", "/management", "/contact", "/contact-us", "/careers", "/pricing",
]
MAX_ENRICH_PAGES = 7
MAX_ENRICH_CHARS = 38000


def fetch_url(url: str, max_chars: int = MAX_PAGE_CHARS) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "iframe", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return re.sub(r"\n{3,}", "\n\n", text)[:max_chars]
    except Exception as e:
        return f"[fetch error: {e}]"


def web_search(query: str, count: int = MAX_SEARCH_HITS, max_chars: int = MAX_SEARCH_CHARS) -> str:
    if os.environ.get("BRAVE_API_KEY"):
        try:
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json",
                         "X-Subscription-Token": os.environ["BRAVE_API_KEY"]},
                params={"q": query, "count": count},
                timeout=10,
            )
            if r.status_code == 200:
                items = r.json().get("web", {}).get("results", [])
                return "\n\n".join(
                    f"Title: {i.get('title','')}\nURL: {i.get('url','')}\n{i.get('description','')}"
                    for i in items
                )[:max_chars]
        except Exception:
            pass
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"}, timeout=10)
        soup = BeautifulSoup(r.content, "html.parser")
        results = []
        for div in soup.find_all("div", class_="result", limit=count):
            ta = div.find("a", class_="result__a")
            sa = div.find("a", class_="result__snippet")
            if not ta:
                continue
            results.append(f"Title: {ta.get_text(strip=True)}\n{sa.get_text(strip=True) if sa else ''}")
        return "\n\n".join(results)[:max_chars] or "[no results]"
    except Exception as e:
        return f"[search error: {e}]"


def multi_search(queries: list[str], count: int = 6, per_chars: int = 1800) -> list[str]:
    """Run several searches; return labelled, non-empty result blocks."""
    blocks = []
    for q in queries:
        res = web_search(q, count=count, max_chars=per_chars)
        if res and not res.startswith("[no results") and not res.startswith("[search error"):
            blocks.append(f"### Search: {q}\n{res}")
    return blocks


def base_url(url: str) -> str:
    p = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    return f"{p.scheme}://{p.netloc}"


def deep_enrich(company_name: str, url: str) -> str:
    """
    Build a rich research corpus: best-effort company pages + broad firmographic
    search surfaces (funding, headcount, news, customers, competitors, tech,
    hiring, Crunchbase/Tracxn). Search is the reliable part; fetches may fail.
    """
    base  = base_url(url) if url else ""
    parts: list[str] = []

    home = fetch_url(url or base, max_chars=MAX_PAGE_CHARS)
    if home and not home.startswith("[fetch error"):
        parts.append(f"### Homepage ({url or base})\n{home}")

    seen, fetched = {url, base}, 0
    for path in ENRICH_PATHS:
        if fetched >= MAX_ENRICH_PAGES or not base:
            break
        page_url = base + path
        if page_url in seen:
            continue
        seen.add(page_url)
        txt = fetch_url(page_url, max_chars=4500)
        if txt and not txt.startswith("[fetch error") and len(txt) > 200:
            parts.append(f"### {path} ({page_url})\n{txt}")
            fetched += 1

    parts += multi_search([
        f"{company_name} funding round investors valuation",
        f"{company_name} revenue employees headcount company size",
        f"{company_name} news announcement 2024 2025",
        f"{company_name} customers clients case study",
        f"{company_name} competitors alternatives market",
        f"{company_name} technology stack tools software",
        f"{company_name} hiring sales jobs openings",
        f"{company_name} crunchbase OR tracxn profile",
    ])

    corpus = "\n\n".join(parts).strip()
    return corpus[:MAX_ENRICH_CHARS] or "[no data retrieved]"


def enrich_contact(company_name: str, url: str, dossier: dict) -> str:
    """
    Targeted decision-maker research: team pages (best-effort) plus off-site
    searches across LinkedIn, contact databases, and news — where contact info
    actually lives. Searches whoever the research stage already surfaced by name.
    """
    base  = base_url(url) if url else ""
    parts: list[str] = []

    for path in ("/team", "/leadership", "/our-team", "/people", "/contact"):
        if not base:
            break
        txt = fetch_url(base + path, max_chars=3500)
        if txt and not txt.startswith("[fetch error") and len(txt) > 200:
            parts.append(f"### {path}\n{txt}")

    pattern  = (dossier or {}).get("email_pattern", "")
    surfaces = [
        f'{company_name} "head of sales" OR "VP sales" OR "VP revenue" OR "commercial director"',
        f'site:linkedin.com/in {company_name} sales OR revenue OR growth OR business development',
        f'{company_name} CEO OR founder OR "chief revenue officer" linkedin',
        f'{company_name} leadership team executives management',
        f'{company_name} email format firstname lastname rocketreach OR zoominfo OR apollo OR lusha',
        f'{company_name} appoints OR hires OR promotes sales OR revenue leader',
    ]
    for cand in ((dossier or {}).get("candidate_contacts") or [])[:3]:
        name = (cand.get("name") or "").strip()
        if name:
            surfaces.append(f'"{name}" {company_name} linkedin email')

    parts += multi_search(surfaces, count=6, per_chars=1800)
    if pattern:
        parts.append(f"### Known email pattern\n{pattern}")

    return "\n\n".join(parts).strip()[:MAX_ENRICH_CHARS]
