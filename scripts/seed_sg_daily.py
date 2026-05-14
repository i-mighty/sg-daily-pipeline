#!/usr/bin/env python3
"""
One-time seed script: inserts the SG Daily mode into the modes table.

Run once on the Railway deployment after the multi-mode migration:
    railway run python scripts/seed_sg_daily.py

Safe to re-run — uses upsert so it won't duplicate.
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

import importlib.util
import db  # noqa: E402

# Load SG_DAILY_PROMPT from run_batch.py without requiring scripts/ to be a package
_spec = importlib.util.spec_from_file_location("run_batch", BASE_DIR / "scripts" / "run_batch.py")
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SG_DAILY_PROMPT = _mod.SG_DAILY_PROMPT

SG_DAILY_DISCOVERY = """You are a B2B lead scout for Viral Asia and "The SG Daily" — a premium media brand
targeting high-spending Singaporean travelers on regional weekend trips (Batam, Bintan, Johor Bahru,
Desaru, Penang).

Find {NUM_LEADS} qualified prospects split roughly evenly across two categories:

---

## CATEGORY A — MEDIA FEATURE LEADS
High-end lifestyle brands at/near Singapore targeting Singaporean visitors:
- Resorts, boutique hotels, spas, golf clubs, restaurants, experience operators
- Pricing: rooms >$120/night, spa treatments >$200, golf >$80/round
- Must specifically benefit from Singaporean day-trip or weekend traffic
- Exclusivity positioning (not budget/mass market)

Target industries: hospitality, leisure, golf, spa/wellness, fine dining, experience tourism
Geography: Batam, Bintan, Johor Bahru, Desaru, Penang, or Singapore-adjacent destinations

## CATEGORY B — INFLUENCER MANAGEMENT LEADS
Global/tech brands running OOH advertising in Singapore:
- Companies with confirmed or likely bus stop, MRT, or billboard presence in Singapore
- Scale to invest in mass content production and social proof amplification
- Tech, fintech, lifestyle, consumer app, or international brand categories
- Evidence of active Singapore marketing (hiring for SG roles, local campaigns, regional offices)

Target industries: fintech, consumer tech, e-commerce, lifestyle apps, travel platforms, F&B chains
Geography: brands with Singapore office/operations or active SG marketing spend

---

## RESEARCH PROCESS
1. Search for Category A: "luxury resort Batam Bintan Johor Bahru Singaporean tourists 2024 2025"
2. Search for Category A: "boutique hotel Penang Desaru Singapore visitors weekend getaway"
3. Search for Category A: "golf club Johor Bahru Singaporean members weekend"
4. Search for Category B: "OOH billboard MRT bus stop advertising Singapore 2024 2025 tech brand"
5. Search for Category B: "fintech lifestyle app Singapore marketing campaign outdoor advertising"
6. Search for Category B: "consumer brand Singapore launch campaign marketing spend"
7. Fetch 2-3 company websites from each category to verify they are active and qualify
8. Search for decision makers: "marketing director [company name] Singapore" or LinkedIn
9. Verify pricing signals (rooms/treatment/golf rates) for Category A leads
10. Verify Singapore OOH presence signals for Category B leads

---

## DISQUALIFICATION RULES
- Budget brands or mass-market accommodation (hostels, budget chains)
- No verifiable Singapore-relevant angle
- No accessible website
- Already a customer or in active pipeline

---

## OUTPUT
Return ONLY a valid JSON array (no other text before or after):

```json
[
  {
    "company_name": "Exact Company Name",
    "url": "https://www.website.com",
    "lead_category": "Media Feature Lead",
    "industry_hint": "Luxury Resort / Hospitality",
    "priority": "High",
    "sg_qualifier": "One specific sentence on why this company qualifies for SG Daily",
    "notes": "Key intelligence: pricing signals, SG USP, decision maker found, OOH evidence"
  }
]
```

Return exactly {NUM_LEADS} companies — roughly half Category A, half Category B.
Only include companies you verified during research.
"""

SG_DAILY_MODE = {
    "name":             "sg-daily",
    "label":            "SG Daily",
    "description":      "Viral Asia / The SG Daily — Batam Insider media features and Singapore OOH influencer management.",
    "analysis_prompt":  SG_DAILY_PROMPT,
    "discovery_prompt": SG_DAILY_DISCOVERY,
    "discover_count":   10,
    "queue_size":       8,
    "is_active":        1,
}


def main():
    db.init_db()

    existing = db.get_mode("sg-daily")
    if existing:
        try:
            answer = input("sg-daily mode already exists. Overwrite? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            print("sg-daily already exists — skipped.")
            return

    db.upsert_mode(SG_DAILY_MODE)
    mode = db.get_mode("sg-daily")
    print(f"✅ Seeded: {mode['label']} (name={mode['name']}, active={mode['is_active']}, "
          f"discover_count={mode['discover_count']}, queue_size={mode['queue_size']})")


if __name__ == "__main__":
    main()
