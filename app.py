import pandas as pd
import plotly.express as px
import streamlit as st

from utils import grade, grade_color, grade_emoji, load_analyses, load_prospects, score_emoji

st.set_page_config(
    page_title="SG Daily · Sales Pipeline",
    page_icon="🎯",
    layout="wide",
)

st.title("🎯 SG Daily Sales Pipeline")
st.caption("Viral Asia · Automated lead intelligence · Use the sidebar to navigate")

analyses  = load_analyses()
prospects = load_prospects()

# ── Metric cards ──────────────────────────────────────────────────────────────

scores   = [float(a.get("prospect_score") or 0) for a in analyses]
avg      = round(sum(scores) / len(scores), 1) if scores else 0
hot      = sum(1 for s in scores if s >= 75)
pending  = sum(1 for p in prospects if p.get("status", "pending").lower() in {"pending", ""})
sent     = sum(1 for a in analyses if a.get("outreach_status", "") == "sent")
errors   = sum(1 for p in prospects if p.get("status", "").lower() == "error")

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Total Analyzed",   len(analyses))
c2.metric("🔥 Hot Leads ≥75", hot)
c3.metric("⏳ Pending",        pending)
c4.metric("📬 Outreach Sent",  sent)
c5.metric("Avg Score",        f"{avg}/100")
c6.metric("⚠️ Errors",         errors, delta=f"-{errors}" if errors else None,
          delta_color="inverse")

st.divider()

# ── Charts ────────────────────────────────────────────────────────────────────

if analyses:
    col_chart, col_pie = st.columns(2)

    with col_chart:
        grade_counts = {"A+": 0, "A": 0, "B": 0, "C": 0, "D": 0}
        for a in analyses:
            g = grade(a.get("prospect_score"))
            if g in grade_counts:
                grade_counts[g] += 1
        fig = px.bar(
            x=list(grade_counts.keys()),
            y=list(grade_counts.values()),
            color=list(grade_counts.keys()),
            color_discrete_map={
                "A+": "#27ae60", "A": "#2ecc71",
                "B": "#2980b9", "C": "#f39c12", "D": "#e74c3c",
            },
            labels={"x": "Grade", "y": "Leads"},
            title="Score Distribution",
        )
        fig.update_layout(showlegend=False, height=260,
                          margin=dict(t=40, b=0, l=0, r=0))
        st.plotly_chart(fig, use_container_width=True)

    with col_pie:
        cats: dict[str, int] = {}
        for a in analyses:
            c = a.get("lead_category") or "Generic / Unknown"
            cats[c] = cats.get(c, 0) + 1
        fig2 = px.pie(
            names=list(cats.keys()),
            values=list(cats.values()),
            title="Lead Category Split",
            color_discrete_sequence=["#0f3460", "#2980b9", "#27ae60", "#f39c12"],
            hole=0.4,
        )
        fig2.update_layout(height=260, margin=dict(t=40, b=0, l=0, r=0))
        st.plotly_chart(fig2, use_container_width=True)

    st.divider()

# ── Recent analyses table ─────────────────────────────────────────────────────

st.subheader("All Analyzed Leads")

if not analyses:
    st.info("No analyses yet. Go to **Discover** → **Analyze** to build your pipeline.")
else:
    rows = []
    for a in analyses:
        sc = a.get("prospect_score", "?")
        g  = grade(sc)
        rows.append({
            " ":          grade_emoji(g),
            "Company":    a.get("company_name", "?"),
            "Score":      int(sc) if sc not in ("?", "", None) else "?",
            "Grade":      g,
            "Category":   (a.get("lead_category") or "—")[:30],
            "Industry":   (a.get("industry") or "—")[:25],
            "Key DM":     (a.get("key_decision_maker") or {}).get("name", "—") if isinstance(a.get("key_decision_maker"), dict) else "—",
            "Outreach":   a.get("outreach_status", "pending"),
            "Analyzed":   a.get("analysis_date", "—"),
        })

    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%d"
            ),
        },
    )

# ── Pipeline stage funnel ─────────────────────────────────────────────────────

if prospects:
    st.divider()
    st.subheader("Pipeline Funnel")

    stage_counts = {
        "Discovered":     len(prospects),
        "Analyzed":       len(analyses),
        "Hot (≥75)":      hot,
        "Outreach Sent":  sent,
    }
    fig3 = px.funnel(
        x=list(stage_counts.values()),
        y=list(stage_counts.keys()),
        color_discrete_sequence=["#0f3460"],
    )
    fig3.update_layout(height=220, margin=dict(t=10, b=0, l=0, r=0))
    st.plotly_chart(fig3, use_container_width=True)
