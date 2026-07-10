"""
Staged analysis pipeline — stage definitions, data contracts, and prompt assembly.

One prospect analysis is four stages:

    research  ->  dossier   (firmographics, signals, candidate contacts)
    contact   ->  contact   (decision maker + real email)   [needs dossier]
    score     ->  score     (prospect score, grade, BANT)   [needs dossier]
    outreach  ->  outreach  (email + recommended actions)   [needs dossier+contact+score]

Dependency graph:  research -> {contact, score} -> outreach

This module is PURE — no DB, no network. It builds the (system, user) prompt for a
given stage from a campaign's sectioned analysis_prompt plus the outputs of earlier
stages, and merges the four stage outputs into the final analysis record (same shape
the Prospect drawer already consumes).

Used by both analysis paths:
  - live  (scripts/run_batch.py): runs the stages in-process; research uses web tools.
  - batch (scripts/batch_submit.py): each stage is a batch item across multiple rounds;
    research is fed pre-fetched data instead of live tools.
"""

import json
import re

from analysis_scaffold import EMAIL_SYSTEM_PROMPT, split_sections

# Ordered stages. contact and score share a "round" (both depend only on dossier).
STAGES = ["research", "contact", "score", "outreach"]

# Which stages carry the email policy as their system prompt.
_EMAIL_STAGES = {"contact", "outreach"}

# Fallback for campaigns whose CONTACT section is empty (e.g. legacy prompts where
# contact-finding lived inside RESEARCH). Keeps Stage 2 meaningful.
DEFAULT_CONTACT_INSTRUCTION = """Identify the single best decision maker to contact at this company
for a B2B sales conversation (typically Head of Sales, VP Revenue, Commercial
Director, or a Founder who owns sales). Use the dossier's candidate contacts and any
email pattern found. Capture their name, title, LinkedIn if known, a specific
personalization anchor, and resolve their email address per the system policy:
verified sources first, include the source URL, and always record the best
role inbox from the official site as fallback_generic_email."""


# ── Stage output contracts (shapes shown to the model) ──────────────────────────

DOSSIER_SCHEMA = """{
  "company_name": "", "url": "", "industry": "", "company_type": "",
  "founded": "", "employees": "", "funding": "", "revenue_estimate": "",
  "hq_location": "", "tech_stack": [], "recent_developments": [],
  "buying_signals": [], "current_solutions": [], "competitive_gaps": [],
  "email_pattern": "",
  "candidate_contacts": [{"name": "", "title": "", "source_url": ""}],
  "sources": [],
  "lead_category": "",
  "sg_usp": "",
  "ooh_presence": {"detected": false, "evidence": "", "channels": []},
  "sg_dollar_filter": {"passes": true, "budget_signal": "", "rationale": ""}
}"""

CONTACT_SCHEMA = """{
  "name": "", "title": "", "linkedin": "",
  "email": "", "email_source": "found|generic|derived",
  "email_source_url": "",
  "email_confidence": "high|medium|low",
  "fallback_generic_email": "",
  "personalization_anchor": "",
  "buying_committee": [{"name": "", "title": "", "role": ""}]
}"""

SCORE_SCHEMA = """{
  "prospect_score": 0, "grade": "", "label": "", "confidence": "",
  "scores": {
    "company_fit":          {"score": 0, "key_finding": ""},
    "contact_access":       {"score": 0, "key_finding": ""},
    "opportunity_quality":  {"score": 0, "key_finding": ""},
    "competitive_position": {"score": 0, "key_finding": ""},
    "outreach_readiness":   {"score": 0, "key_finding": ""}
  },
  "bant": {
    "budget":    {"score": 0, "evidence": ""},
    "authority": {"score": 0, "evidence": ""},
    "need":      {"score": 0, "evidence": ""},
    "timeline":  {"score": 0, "evidence": ""},
    "total": 0
  },
  "red_flags": []
}"""

OUTREACH_SCHEMA = """{
  "outreach_email": {
    "to_name": "", "to_title": "", "to_email": "",
    "subject_a": "", "subject_b": "", "body": "", "cta": ""
  },
  "hook_ideas": [],
  "recommended_action": "",
  "immediate_actions": []
}"""

_STAGE_SCHEMA = {
    "research": DOSSIER_SCHEMA,
    "contact":  CONTACT_SCHEMA,
    "score":    SCORE_SCHEMA,
    "outreach": OUTREACH_SCHEMA,
}


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _unescape(s):
    """Turn over-escaped sequences (literal \\n, \\r\\n, \\t) into real whitespace."""
    if not isinstance(s, str):
        return s
    return s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "  ").strip()


def _dedash(s):
    """Enforce the no-em-dash email rule deterministically (the model sometimes
    slips one in despite the system prompt). An em/en dash used as punctuation
    becomes a comma: "scale — especially" -> "scale, especially"."""
    if not isinstance(s, str):
        return s
    s = re.sub(r"\s*[—–]\s*", ", ", s)
    return re.sub(r"[ \t]{2,}", " ", s)


def _clean_email(em: dict) -> dict:
    """Normalise whitespace escapes + strip em dashes so display + send are clean."""
    if not isinstance(em, dict):
        return em
    return {k: (_dedash(_unescape(v)) if k in ("body", "subject_a", "subject_b", "cta") else v)
            for k, v in em.items()}


def _fill(text: str, lead: dict, today: str) -> str:
    """Substitute the campaign placeholders in a section body."""
    return (text
        .replace("{URL}",           lead.get("url", "") or "")
        .replace("{COMPANY_NAME}",  lead.get("company_name", "") or lead.get("url", "") or "")
        .replace("{INDUSTRY_HINT}", lead.get("industry_hint", "") or lead.get("industry", "") or "Unknown")
        .replace("{LEAD_CATEGORY}", lead.get("lead_category", "") or "Unknown")
        .replace("{NOTES}",         lead.get("notes", "") or "None")
        .replace("{TODAY}",         today))


def _target_block(lead: dict) -> str:
    return (
        f"TARGET: {lead.get('url', '')}\n"
        f"COMPANY NAME: {lead.get('company_name', '') or lead.get('url', '')}\n"
        f"INDUSTRY HINT: {lead.get('industry_hint', '') or lead.get('industry', '') or 'Unknown'}\n"
        f"CATEGORY: {lead.get('lead_category', '') or 'Unknown'}\n"
        f"PRIOR NOTES: {lead.get('notes', '') or 'None'}"
    )


def _json_block(label: str, obj) -> str:
    return f"{label}:\n```json\n{json.dumps(obj, indent=2, ensure_ascii=False)}\n```"


# ── Prompt assembly ─────────────────────────────────────────────────────────────

def build_stage_prompt(
    stage: str,
    campaign_prompt: str,
    lead: dict,
    today: str,
    prior: dict | None = None,
    research_data: str | None = None,
) -> tuple[str | None, str]:
    """
    Build (system_prompt, user_prompt) for one pipeline stage.

    campaign_prompt : the campaign's sectioned analysis_prompt
    lead            : lead row (url, company_name, industry_hint, notes, lead_category)
    today           : YYYY-MM-DD
    prior           : {"dossier": {...}, "contact": {...}, "score": {...}} as available
    research_data   : for the research stage in batch mode, the pre-fetched text to use
                      instead of live tools. None (live) means the model uses its tools.

    Returns (system, user). system is None for non-email stages.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown stage: {stage}")

    prior    = prior or {}
    sections = split_sections(campaign_prompt)
    preamble = sections.get("preamble", "").strip()
    schema   = _STAGE_SCHEMA[stage]
    system   = EMAIL_SYSTEM_PROMPT if stage in _EMAIL_STAGES else None

    parts: list[str] = []
    if preamble:
        parts.append(_fill(preamble, lead, today))
    parts.append(_target_block(lead))

    if stage == "research":
        if research_data is not None:
            # Batch: no tools. Use the pre-fetched data only — do NOT include the
            # campaign's live-research steps (they instruct tool use the model lacks).
            parts.append("## Pre-Fetched Research Data\n\nYou have NO web access. Use ONLY the data below to build the dossier.\n\n" + research_data)
        else:
            # Live: include the campaign's research instructions; the model has tools.
            body = sections.get("research", "").strip()
            if body:
                parts.append(_fill(body, lead, today))
        parts.append(
            f"Produce ONLY a JSON object — the research dossier — with this shape:\n```json\n{schema}\n```\n"
            "The fields lead_category, sg_usp, ooh_presence, and sg_dollar_filter are "
            "campaign-optional: populate them ONLY if the campaign instructions above ask "
            "for that classification/USP/OOH/budget analysis; otherwise leave them at their "
            "empty defaults."
        )

    elif stage == "contact":
        parts.append(_json_block("Research dossier", prior.get("dossier", {})))
        # research_data here = fresh, targeted decision-maker research the orchestrator
        # gathered between rounds (team pages, LinkedIn/email searches for the named DM).
        if research_data:
            parts.append("## Targeted decision-maker research\n\n" + research_data)
        body = sections.get("contact", "").strip() or DEFAULT_CONTACT_INSTRUCTION
        parts.append(_fill(body, lead, today))
        parts.append(f"Produce ONLY a JSON object — the contact record — with this shape:\n```json\n{schema}\n```")

    elif stage == "score":
        parts.append(_json_block("Research dossier", prior.get("dossier", {})))
        body = sections.get("scoring", "").strip()
        if body:
            parts.append(_fill(body, lead, today))
        parts.append(f"Produce ONLY a JSON object — the score — with this shape:\n```json\n{schema}\n```")

    elif stage == "outreach":
        # NOTE: the campaign's OUTPUT section holds the legacy monolithic JSON schema,
        # which competes with this stage's OUTREACH_SCHEMA. It is intentionally NOT
        # injected here — the email rules come from the system prompt, the shape from
        # OUTREACH_SCHEMA below.
        parts.append(_json_block("Research dossier", prior.get("dossier", {})))
        parts.append(_json_block("Decision maker", prior.get("contact", {})))
        parts.append(_json_block("Score", prior.get("score", {})))
        # The campaign's OUTPUT section (legacy monolithic JSON schema) is intentionally
        # NOT injected — it competes with OUTREACH_SCHEMA. But an optional OUTREACH section
        # carries campaign STYLE/TONE (voice, banned words, hook instructions, CTA) with no
        # competing schema, so inject it here on top of the platform email rules.
        outreach_body = sections.get("outreach", "").strip()
        if outreach_body:
            parts.append("## Campaign outreach style\n\n" + _fill(outreach_body, lead, today))
        parts.append(
            "Using the dossier, decision maker, and score above, write the outreach "
            "email following the system email-writing rules"
            + (" and the campaign outreach style above" if outreach_body else "")
            + ". Then produce ONLY a JSON object with this shape (no markdown report, no "
            f"other text):\n```json\n{schema}\n```"
        )

    return system, "\n\n".join(parts)


def render_report_md(j: dict) -> str:
    """Render a markdown report from a merged analysis record (for PDF/storage)."""
    dm = j.get("key_decision_maker", {}) or {}
    em = j.get("outreach_email", {}) or {}
    lines = [
        f"# Prospect Analysis: {j.get('company_name', '')}",
        f"**URL:** {j.get('url','')}  **Industry:** {j.get('industry','')}  "
        f"**Score:** {j.get('prospect_score',0)}/100 ({j.get('grade','')} — {j.get('label','')})",
        "",
        "## Snapshot",
        f"- Type: {j.get('company_type','')}",
        f"- Employees: {j.get('employees','')}  Funding: {j.get('funding','')}  HQ: {j.get('hq_location','')}",
        "",
        "## Decision Maker",
        f"- {dm.get('name','')} — {dm.get('title','')}",
        f"- Email: {dm.get('email','') or 'not resolved'} ({dm.get('email_source','')}"
        + (f", {dm.get('email_status')}, confidence {dm.get('email_confidence')}/100" if dm.get("email_status") else "")
        + ")",
        f"- Anchor: {dm.get('personalization_anchor','')}",
        "",
        "## Buying Signals",
        *[f"- {s}" for s in (j.get("buying_signals") or [])],
        "",
        "## Recommended Action",
        j.get("recommended_action", ""),
        "",
        "## Ready-to-Send Email",
        f"To: {em.get('to_name','')} <{em.get('to_email','')}>",
        f"Subject A: {em.get('subject_a','')}",
        f"Subject B: {em.get('subject_b','')}",
        "",
        em.get("body", ""),
    ]
    return "\n".join(lines)


# ── Merge ───────────────────────────────────────────────────────────────────────

def _normalize_bant(bant: dict) -> dict:
    """Composite BANT score is the AVERAGE of the four dimensions (each /100), not
    their sum — the model sometimes returns the sum (e.g. 335), which reads as 335/100."""
    bant = dict(bant or {})
    scores: list[float] = []
    for dim in ("budget", "authority", "need", "timeline"):
        s = (bant.get(dim) or {}).get("score")
        try:
            scores.append(float(s))
        except (TypeError, ValueError):
            pass
    bant["total"] = round(sum(scores) / len(scores)) if scores else 0
    return bant


def merge_outputs(
    lead: dict,
    dossier: dict,
    contact: dict,
    score: dict,
    outreach: dict,
) -> dict:
    """
    Merge the four stage outputs into the final analysis record — the same shape
    the Prospect drawer and downstream code already expect from analysis_json.
    """
    dossier  = dossier or {}
    contact  = contact or {}
    score    = score or {}
    outreach = outreach or {}

    outreach_email = _clean_email(outreach.get("outreach_email", {}))
    # The verified contact email is authoritative: when the finalizer ran
    # (email_status present), the outreach stage may not re-guess the recipient —
    # including forcing it blank when no valid address survived verification.
    if isinstance(outreach_email, dict) and "email_status" in contact:
        outreach_email["to_email"] = contact.get("email", "")

    return {
        "company_name":     dossier.get("company_name") or lead.get("company_name", ""),
        "url":              lead.get("url", ""),
        "company_type":     dossier.get("company_type", ""),
        "industry":         dossier.get("industry") or lead.get("industry_hint", ""),
        "founded":          dossier.get("founded", ""),
        "employees":        dossier.get("employees", ""),
        "funding":          dossier.get("funding", ""),
        "revenue_estimate": dossier.get("revenue_estimate", ""),
        "hq_location":      dossier.get("hq_location", ""),

        "prospect_score":   score.get("prospect_score", 0),
        "grade":            score.get("grade", ""),
        "label":            score.get("label", ""),
        "confidence":       score.get("confidence", ""),
        "scores":           score.get("scores", {}),
        "bant":             _normalize_bant(score.get("bant", {})),
        "red_flags":        score.get("red_flags", []),

        "key_decision_maker": {
            "name":                   contact.get("name", ""),
            "title":                  contact.get("title", ""),
            "email":                  contact.get("email", ""),
            "email_source":           contact.get("email_source", ""),
            "email_source_url":       contact.get("email_source_url", ""),
            "email_status":           contact.get("email_status", ""),
            "email_confidence":       contact.get("email_confidence", ""),
            "fallback_generic_email": contact.get("fallback_generic_email", ""),
            "email_pattern":          dossier.get("email_pattern", ""),
            "linkedin":               contact.get("linkedin", ""),
            "personalization_anchor": contact.get("personalization_anchor", ""),
        },
        "buying_committee":  contact.get("buying_committee", []),

        "buying_signals":    dossier.get("buying_signals", []),
        "current_solutions": dossier.get("current_solutions", []),
        "competitive_gaps":  dossier.get("competitive_gaps", []),

        # Campaign-optional fields (e.g. SG Daily): pass through from the dossier so
        # downstream (dashboard ooh/usp columns, outreach) keeps them. Empty for
        # campaigns that don't ask for them.
        "lead_category":     dossier.get("lead_category") or lead.get("lead_category", ""),
        "sg_usp":            dossier.get("sg_usp", ""),
        "ooh_presence":      dossier.get("ooh_presence", {}),
        "sg_dollar_filter":  dossier.get("sg_dollar_filter", {}),
        "hook_ideas":        outreach.get("hook_ideas", []),

        "outreach_email":     outreach_email,
        "recommended_action": outreach.get("recommended_action", ""),
        "immediate_actions":  outreach.get("immediate_actions", []),

        "outreach_status":   "pending",
    }
