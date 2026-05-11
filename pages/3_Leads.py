from pathlib import Path

import pandas as pd
import streamlit as st

from utils import (dm_email, dm_name, grade, grade_color, grade_emoji,
                   load_analyses, mark_outreach_sent, score_emoji)

st.set_page_config(page_title="Lead Browser", page_icon="🎯", layout="wide")

st.title("🎯 Lead Browser")
st.caption("Browse, filter, and deep-dive every analyzed lead.")

analyses = load_analyses()

if not analyses:
    st.info("No analyses yet. Go to **Discover** → **Analyze** to build your pipeline.")
    st.stop()

# ── Sidebar filters ───────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Filters")

    score_range = st.slider("Score range", 0, 100, (0, 100), step=5)

    all_cats = sorted({a.get("lead_category") or "Unknown" for a in analyses})
    sel_cats = st.multiselect("Category", all_cats, default=all_cats)

    all_grades = ["A+", "A", "B", "C", "D", "?"]
    sel_grades = st.multiselect("Grade", all_grades, default=all_grades)

    all_statuses = sorted({a.get("outreach_status") or "pending" for a in analyses})
    sel_statuses = st.multiselect("Outreach status", all_statuses, default=all_statuses)

    search = st.text_input("Search company / DM name")

# ── Apply filters ─────────────────────────────────────────────────────────────

def _passes(a: dict) -> bool:
    sc  = float(a.get("prospect_score") or 0)
    g   = grade(sc)
    cat = a.get("lead_category") or "Unknown"
    sts = a.get("outreach_status") or "pending"

    if not (score_range[0] <= sc <= score_range[1]):      return False
    if sel_cats and cat not in sel_cats:                   return False
    if sel_grades and g not in sel_grades:                 return False
    if sel_statuses and sts not in sel_statuses:           return False
    if search:
        q = search.lower()
        name = (a.get("company_name") or "").lower()
        dm   = dm_name(a).lower()
        if q not in name and q not in dm:                  return False
    return True

filtered = [a for a in analyses if _passes(a)]
st.caption(f"Showing {len(filtered)} of {len(analyses)} leads")

# ── Table ─────────────────────────────────────────────────────────────────────

if not filtered:
    st.warning("No leads match your filters.")
    st.stop()

rows = []
for a in filtered:
    sc = a.get("prospect_score", 0)
    g  = grade(sc)
    rows.append({
        " ":         grade_emoji(g),
        "Company":   a.get("company_name", "?"),
        "Score":     int(sc) if sc not in ("", None) else 0,
        "Grade":     g,
        "Category":  (a.get("lead_category") or "—")[:28],
        "Industry":  (a.get("industry") or "—")[:22],
        "Key DM":    dm_name(a),
        "Email":     dm_email(a),
        "Outreach":  a.get("outreach_status") or "pending",
        "Date":      a.get("analysis_date", "—"),
    })

df        = pd.DataFrame(rows)
selection = st.dataframe(
    df,
    hide_index=True,
    use_container_width=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "Score": st.column_config.ProgressColumn(
            "Score", min_value=0, max_value=100, format="%d"
        ),
    },
)

# ── Lead detail panel ─────────────────────────────────────────────────────────

sel_rows = selection.selection.rows
if not sel_rows:
    st.info("👆 Click any row to expand the full lead profile.")
    st.stop()

lead = filtered[sel_rows[0]]
sc   = lead.get("prospect_score", 0)
g    = grade(sc)

st.divider()

# Header
hcol1, hcol2, hcol3 = st.columns([3, 1, 1])
with hcol1:
    st.subheader(f"{score_emoji(sc)} {lead.get('company_name', '?')}")
    st.caption(f"{lead.get('url', '')} · {lead.get('industry', '')} · {lead.get('company_type', '')}")
with hcol2:
    color = grade_color(g)
    st.markdown(f"<h1 style='color:{color};margin:0'>{sc}</h1><p style='margin:0'>/ 100 · Grade {g}</p>",
                unsafe_allow_html=True)
with hcol3:
    cat = lead.get("lead_category", "")
    st.markdown(f"**Category**  \n{cat}")
    ooh = lead.get("ooh_presence", {})
    ooh_str = "✅ Confirmed" if (isinstance(ooh, dict) and ooh.get("detected")) else "Unknown"
    st.markdown(f"**OOH**  \n{ooh_str}")

# SG USP
sg_usp = lead.get("sg_usp", "")
if sg_usp:
    st.info(f"💡 **SG USP:** {sg_usp}")

tabs = st.tabs(["📧 Outreach", "📊 Scores", "👥 Contacts", "📋 Intel", "📄 Full Report"])

# ── Tab 1: Outreach ───────────────────────────────────────────────────────────
with tabs[0]:
    email = lead.get("outreach_email", {}) or {}
    hooks = lead.get("hook_ideas") or email.get("hook_ideas", [])

    c1, c2 = st.columns([2, 1])
    with c1:
        st.markdown(f"**To:** {email.get('to_name', dm_name(lead))} — {email.get('to_title', '')}")
        st.markdown(f"**Email:** `{email.get('to_email', dm_email(lead))}`")
        st.markdown(f"**Subject A:** {email.get('subject_a', email.get('subject', '—'))}")
        if email.get("subject_b"):
            st.markdown(f"**Subject B:** {email.get('subject_b')}")
    with c2:
        sts = lead.get("outreach_status", "pending")
        st.markdown(f"**Status:** `{sts}`")
        if sts != "sent":
            if st.button("✅ Mark as Sent", key=f"sent_{lead.get('url')}"):
                mark_outreach_sent(lead.get("url", ""))
                st.success("Marked as sent!")
                st.rerun()

    if hooks:
        st.markdown("**Hook Ideas:**")
        for h in hooks[:3]:
            st.markdown(f"• {h}")

    body = email.get("body", "")
    if body:
        st.markdown("**Email body:**")
        st.code(body, language=None)
        cta = email.get("cta", "Are you free for a 10-minute call?")
        st.caption(f"CTA: _{cta}_")

# ── Tab 2: Scores ────────────────────────────────────────────────────────────
with tabs[1]:
    scores_d = lead.get("scores", {}) or {}
    bant     = lead.get("bant", {}) or {}

    score_rows = []
    weights = [
        ("company_fit",          "Company Fit",          0.25),
        ("contact_access",       "Contact Access",       0.20),
        ("opportunity_quality",  "Opportunity Quality",  0.20),
        ("competitive_position", "Competitive Position", 0.15),
        ("outreach_readiness",   "Outreach Readiness",   0.20),
    ]
    for key, label, w in weights:
        entry = scores_d.get(key, {}) or {}
        s     = entry.get("score", 0) or 0
        score_rows.append({
            "Category":    label,
            "Score":       int(s),
            "Weight":      f"{int(w*100)}%",
            "Weighted":    round(s * w, 1),
            "Key Finding": entry.get("key_finding", "—"),
        })

    st.dataframe(
        pd.DataFrame(score_rows),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d"),
        },
    )

    st.markdown("**BANT Scorecard**")
    bant_rows = []
    for dim in ["budget", "authority", "need", "timeline"]:
        entry = bant.get(dim, {}) or {}
        bant_rows.append({
            "Dimension": dim.capitalize(),
            "Score /25":  entry.get("score", 0),
            "Evidence":   entry.get("evidence", "—"),
        })
    bant_rows.append({"Dimension": "TOTAL", "Score /25": bant.get("total", 0), "Evidence": ""})
    st.dataframe(pd.DataFrame(bant_rows), hide_index=True, use_container_width=True)

# ── Tab 3: Contacts ───────────────────────────────────────────────────────────
with tabs[2]:
    committee = lead.get("buying_committee", []) or []
    if committee:
        comm_rows = []
        for p in committee:
            comm_rows.append({
                "Name":                p.get("name", ""),
                "Title":               p.get("title", ""),
                "Role":                p.get("role", ""),
                "Personalization Anchor": p.get("personalization_anchor", ""),
            })
        st.dataframe(pd.DataFrame(comm_rows), hide_index=True, use_container_width=True)
    else:
        dm = lead.get("key_decision_maker", {}) or {}
        if isinstance(dm, dict) and dm.get("name"):
            st.markdown(f"**{dm.get('name')}** — {dm.get('title', '')}")
            st.markdown(f"Email: `{dm.get('email_pattern', '—')}`")
            if dm.get("linkedin"):
                st.markdown(f"LinkedIn: {dm.get('linkedin')}")
            if dm.get("personalization_anchor"):
                st.info(f"💡 {dm.get('personalization_anchor')}")
        else:
            st.info("No contact data. Re-analyze in sg-daily mode.")

# ── Tab 4: Intel ──────────────────────────────────────────────────────────────
with tabs[3]:
    col_l, col_r = st.columns(2)

    signals   = lead.get("buying_signals", []) or []
    red_flags = lead.get("red_flags", []) or []
    solutions = lead.get("current_solutions", []) or []
    gaps      = lead.get("competitive_gaps", []) or []

    with col_l:
        if signals:
            st.markdown("**✅ Buying Signals**")
            for s in signals:
                st.markdown(f"• {s}")
        if gaps:
            st.markdown("**🎯 Competitive Gaps**")
            for g in gaps:
                st.markdown(f"• {g}")

    with col_r:
        if red_flags:
            st.markdown("**⚠️ Red Flags**")
            for f in red_flags:
                st.markdown(f"• {f}")
        if solutions:
            st.markdown("**🔧 Current Solutions**")
            for s in solutions:
                st.markdown(f"• {s}")

    actions = lead.get("immediate_actions", []) or []
    if actions:
        st.markdown("**⚡ Immediate Actions**")
        for a in actions:
            st.markdown(f"1. {a}")

# ── Tab 5: Full Report ────────────────────────────────────────────────────────
with tabs[4]:
    md_path  = Path(lead.get("_md_path", "") or "")
    pdf_path = Path(lead.get("_pdf_path", "") or "")

    if md_path.exists():
        with st.expander("View full markdown report", expanded=False):
            st.markdown(md_path.read_text(encoding="utf-8"))
    else:
        st.caption("Markdown report cleaned up after pipeline run.")

    if pdf_path.exists():
        with open(pdf_path, "rb") as f:
            st.download_button(
                "⬇️ Download PDF Report",
                data=f,
                file_name=f"{lead.get('company_name', 'prospect')}-analysis.pdf",
                mime="application/pdf",
            )
    else:
        st.info("PDF was emailed to you after the last pipeline run and cleaned up from disk.")
