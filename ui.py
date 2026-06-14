"""
Shared UI layer for the Streamlit control dashboard.

Centralises the things that make the app feel fast and consistent:
  - cached data loaders (DB reads are cached for `CACHE_TTL`s, with a manual
    Refresh that clears them) so a click doesn't re-query Postgres every time,
  - a small design system: injected CSS, metric cards, score/status pills,
  - a rich prospect detail view shared by every page,
  - a batches panel for the OpenAI Batch pipeline.

Pages import from here instead of re-implementing layout. Keep business logic in
db.py / scripts; keep presentation here.
"""

from __future__ import annotations

import json

import streamlit as st

import db
from analysis_pipeline import render_report_md
from utils import grade, grade_color, grade_emoji

CACHE_TTL = 60  # seconds — DB reads refresh at least this often; Refresh clears now.


# ── Cached data loaders ────────────────────────────────────────────────────────

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def analyses() -> list[dict]:
    return db.get_analyses()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def prospects() -> list[dict]:
    return db.get_leads()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def pipeline_runs(limit: int = 30) -> list[dict]:
    return db.get_pipeline_runs(limit=limit)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def batches(limit: int = 100) -> list[dict]:
    try:
        return db.get_batches(limit=limit)
    except Exception:
        return []  # batches table may not exist on a not-yet-migrated DB


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def analysis_by_url(url: str) -> dict | None:
    return db.get_analysis_by_url(url)


def clear_caches() -> None:
    st.cache_data.clear()


def refresh_button(key: str = "refresh") -> None:
    """A right-aligned Refresh control that drops the cached DB reads."""
    if st.button("↻ Refresh", key=key, help="Reload data from the database"):
        clear_caches()
        st.rerun()


# ── Design system ──────────────────────────────────────────────────────────────

def inject_css() -> None:
    st.markdown(
        """
        <style>
          .block-container { padding-top: 2.2rem; }
          /* Metric cards */
          div[data-testid="stMetric"] {
              background: #ffffff; border: 1px solid #e8ebf0; border-radius: 12px;
              padding: 14px 16px; box-shadow: 0 1px 2px rgba(16,24,40,.04);
          }
          div[data-testid="stMetricLabel"] { opacity: .75; font-weight: 600; }
          /* Pills */
          .pill { display:inline-block; padding:2px 10px; border-radius:999px;
                  font-size:.78rem; font-weight:700; color:#fff; }
          .pill-soft { display:inline-block; padding:2px 10px; border-radius:999px;
                  font-size:.78rem; font-weight:600; border:1px solid #d6dae1; color:#374151; }
          .anchor { background:#f3f4f6; border-radius:8px; padding:10px 12px; font-size:.9rem; }
          .email-box { background:#0f3460; color:#eaf0fb; border-radius:12px; padding:16px 18px;
                  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.9rem;
                  white-space: pre-wrap; }
          h1, h2, h3 { letter-spacing: -0.01em; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def score_pill(score) -> str:
    g = grade(score)
    try:
        s = int(score)
    except (TypeError, ValueError):
        s = "?"
    return f'<span class="pill" style="background:{grade_color(g)}">{grade_emoji(g)} {s} · {g}</span>'


_STATUS_COLORS = {
    "done": "#27ae60", "pending": "#f39c12", "batching": "#2980b9",
    "running": "#2980b9", "error": "#e74c3c", "sent": "#27ae60",
    "collected": "#27ae60", "in_progress": "#2980b9", "completed": "#27ae60",
    "failed": "#e74c3c", "expired": "#e74c3c", "cancelled": "#9aa0a6",
}


def status_badge(status: str) -> str:
    s = (status or "").lower()
    color = _STATUS_COLORS.get(s, "#9aa0a6")
    return f'<span class="pill" style="background:{color}">{s or "—"}</span>'


# ── Prospect detail ────────────────────────────────────────────────────────────

def render_prospect_detail(a: dict) -> None:
    """Rich, read-only view of one merged analysis record."""
    # These can arrive as a flattened string (e.g. key_decision_maker = name) when an
    # older record's columns shadowed the JSON; coerce to dict so .get() is always safe.
    dm = a.get("key_decision_maker") or {}
    em = a.get("outreach_email") or {}
    if not isinstance(dm, dict):
        dm = {"name": str(dm)}
    if not isinstance(em, dict):
        em = {}

    st.markdown(
        f"### {a.get('company_name','?')} &nbsp; {score_pill(a.get('prospect_score'))}",
        unsafe_allow_html=True,
    )
    meta = " · ".join(
        x for x in [
            a.get("url", ""), a.get("industry", ""), a.get("company_type", ""),
            f"{a.get('employees','')} emp" if a.get("employees") else "",
            a.get("hq_location", ""),
        ] if x
    )
    if meta:
        st.caption(meta)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Score", f"{a.get('prospect_score','?')}/100")
    c2.metric("Grade", grade(a.get("prospect_score")))
    c3.metric("Funding", a.get("funding") or "—")
    c4.metric("Confidence", a.get("confidence") or "—")

    # Decision maker + email
    left, right = st.columns([1, 1])
    with left:
        st.markdown("#### Decision maker")
        src = dm.get("email_source", "")
        src_badge = f' <span class="pill-soft">{src}</span>' if src else ""
        st.markdown(
            f"**{dm.get('name','—')}** — {dm.get('title','—')}<br>"
            f"✉️ {dm.get('email','—')}{src_badge}<br>"
            + (f"🔗 {dm.get('linkedin')}<br>" if dm.get("linkedin") else ""),
            unsafe_allow_html=True,
        )
        if dm.get("personalization_anchor"):
            st.markdown(f'<div class="anchor">🎯 {dm["personalization_anchor"]}</div>',
                        unsafe_allow_html=True)
    with right:
        st.markdown("#### BANT")
        bant = a.get("bant", {}) or {}
        for dim in ("budget", "authority", "need", "timeline"):
            d = bant.get(dim, {}) or {}
            sc = d.get("score", 0) or 0
            st.progress(min(int(sc), 100) / 100, text=f"{dim.title()} · {sc}/100")

    # Signals
    sig = a.get("buying_signals") or []
    gaps = a.get("competitive_gaps") or []
    if sig or gaps:
        s1, s2 = st.columns(2)
        with s1:
            if sig:
                st.markdown("#### Buying signals")
                for x in sig[:6]:
                    st.markdown(f"- {x}")
        with s2:
            if gaps:
                st.markdown("#### Competitive gaps")
                for x in gaps[:6]:
                    st.markdown(f"- {x}")

    # Ready-to-send email
    if em.get("body"):
        st.markdown("#### Ready-to-send email")
        st.caption(
            f"To: {em.get('to_name','')} <{em.get('to_email','')}> — {em.get('to_title','')}"
        )
        if em.get("subject_a"):
            st.markdown(f"**Subject A:** {em['subject_a']}")
        if em.get("subject_b"):
            st.markdown(f"**Subject B:** {em['subject_b']}")
        st.markdown(f'<div class="email-box">{em.get("body","")}</div>', unsafe_allow_html=True)
        st.code(em.get("body", ""), language=None)  # easy copy

    with st.expander("Full report (markdown)"):
        st.markdown(render_report_md(a))
    with st.expander("Raw analysis JSON"):
        st.json(a)


def prospect_picker(analyses_list: list[dict], key: str = "picker") -> None:
    """A selectbox that renders the chosen prospect's detail inline."""
    if not analyses_list:
        st.info("No analyses yet.")
        return
    labels = {
        f"{grade_emoji(grade(a.get('prospect_score')))} {a.get('company_name','?')} "
        f"({a.get('prospect_score','?')})": a
        for a in analyses_list
    }
    choice = st.selectbox("Inspect a prospect", list(labels.keys()), key=key)
    if choice:
        a = labels[choice]
        if st.button("📄 Open full detail page →", key=f"{key}_open"):
            st.query_params["url"] = a.get("url", "")
            st.switch_page("pages/7_Prospect.py")
        render_prospect_detail(a)


# ── Batches panel ──────────────────────────────────────────────────────────────

def batches_panel(rows: list[dict] | None = None) -> None:
    """Show OpenAI batch runs grouped by run (parent_batch_id), round by round."""
    rows = rows if rows is not None else batches()
    if not rows:
        st.info("No OpenAI batches yet. Submit one with "
                "`python scripts/batch_submit.py` (or run the pipeline with "
                "`--analysis-path batch`).")
        return

    runs: dict[str, list[dict]] = {}
    for r in rows:
        runs.setdefault(r.get("parent_batch_id") or "—", []).append(r)

    stage_order = {"research": 0, "contact": 1, "score": 2, "outreach": 3}
    for run_id, batch_list in runs.items():
        batch_list.sort(key=lambda b: stage_order.get(b.get("stage"), 9))
        mode = next((b.get("mode") for b in batch_list if b.get("mode")), "—")
        active = any(b.get("status") not in ("collected", "failed", "expired", "cancelled")
                     for b in batch_list)
        head = "🟢 active" if active else "✓ complete"
        with st.expander(f"Run {run_id[:8]} · mode={mode} · {head} · {len(batch_list)} round(s)",
                         expanded=active):
            for b in batch_list:
                try:
                    n = len(json.loads(b.get("lead_ids") or "[]"))
                except (TypeError, ValueError):
                    n = "?"
                st.markdown(
                    f"{status_badge(b.get('status'))} &nbsp; **{b.get('stage','?')}** "
                    f"· {n} lead(s) · `{b.get('batch_id','')}`",
                    unsafe_allow_html=True,
                )
