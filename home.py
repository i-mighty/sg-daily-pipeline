"""Home — pipeline overview. Rendered by the st.navigation router in app.py."""

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import db
import ui
from utils import grade, grade_emoji

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
from send_outreach import build_outreach_email, send_one  # noqa: E402

head_l, head_r = st.columns([5, 1])
with head_l:
    st.title("🎯 SG Daily Sales Pipeline")
    st.caption("Viral Asia · Automated lead intelligence · Use the sidebar to navigate")
with head_r:
    ui.refresh_button("refresh_home")

analyses   = ui.analyses()
prospects  = ui.prospects()
batch_rows = ui.batches()

# ── Metric cards ──────────────────────────────────────────────────────────────

scores   = [float(a.get("prospect_score") or 0) for a in analyses]
hot      = sum(1 for s in scores if s >= 75)
pending  = sum(1 for p in prospects if p.get("status", "pending").lower() in {"pending", ""})
batching = sum(1 for p in prospects if p.get("status", "").lower() == "batching")
sent     = sum(1 for a in analyses if a.get("outreach_status", "") == "sent")
errors   = sum(1 for p in prospects if p.get("status", "").lower() == "error")

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Total Analyzed",   len(analyses))
c2.metric("🔥 Hot Leads ≥75", hot)
c3.metric("⏳ Pending",        pending)
c4.metric("🛰️ In Batch",       batching)
c5.metric("📬 Outreach Sent",  sent)
c6.metric("⚠️ Errors",         errors, delta=f"-{errors}" if errors else None,
          delta_color="inverse")

# ── Needs attention ───────────────────────────────────────────────────────────

alerts = []
if errors:
    alerts.append(f"❌ **{errors}** lead(s) errored — check the Leads page.")
if batching:
    active_batches = sum(1 for b in batch_rows
                         if b.get("status") not in ("collected", "failed", "expired", "cancelled"))
    alerts.append(f"🛰️ **{batching}** lead(s) in flight across **{active_batches}** active batch round(s).")
if pending > 0 and not batching:
    alerts.append(f"⏳ **{pending}** pending lead(s) awaiting analysis.")
if alerts:
    st.warning("  \n".join(alerts))

# ── Email controls ────────────────────────────────────────────────────────────
# Toggle gates the daily cron's outreach step (settings.auto_outreach_enabled);
# the manual sender fires one prospect's email on demand via send_one().

with st.container(border=True):
    ec_l, ec_r = st.columns([1, 2])

    with ec_l:
        st.markdown("#### 📧 Email Controls")
        enabled = db.outreach_enabled()
        new_val = st.toggle(
            "Auto outreach emails",
            value=enabled,
            help="When off, the daily pipeline skips sending outreach emails to "
                 "prospects (the pipeline report email still sends).",
        )
        if new_val != enabled:
            db.set_setting("auto_outreach_enabled", "true" if new_val else "false")
            st.toast("Auto outreach enabled ✅" if new_val else "Auto outreach disabled ⛔")
            st.rerun()
        st.caption("Cron will **send** outreach emails." if enabled
                   else "Cron will **skip** outreach emails.")

    with ec_r:
        st.markdown("#### ✉️ Send One Manually")
        sendable = [
            a for a in analyses
            if (a.get("outreach_status") or "pending").lower()
            not in {"sent", "replied", "converted"}
            and build_outreach_email(a)
        ]
        if not sendable:
            st.caption("No prospects with a ready, unsent outreach email.")
        else:
            options = {
                f"{a.get('company_name') or a.get('url')} · "
                f"{int(a.get('prospect_score') or 0)}/100": a
                for a in sendable
            }
            choice  = st.selectbox("Prospect", list(options.keys()),
                                   label_visibility="collapsed", key="manual_send_pick")
            lead    = options[choice]
            preview = build_outreach_email(lead)
            st.caption(f"To: {preview['to_name']} <{preview['to_email']}> · "
                       f"Subject: {preview['subject']}")
            if st.button("✉️ Send this email now", type="primary", key="manual_send_btn"):
                with st.spinner("Sending…"):
                    res = send_one(lead)
                if res.get("status") == "sent":
                    st.toast(f"Sent to {res.get('to_email')} ✅")
                    ui.clear_caches()
                    st.rerun()
                elif res.get("status") == "skipped":
                    st.warning(res.get("reason", "Skipped"))
                else:
                    st.error(res.get("error", "Send failed"))

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
            c = a.get("lead_category") or "Unknown"
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

# ── Analyzed leads table (each row links to its detail page) ───────────────────

st.subheader("All Analyzed Leads")

if not analyses:
    st.info("No analyses yet. Go to **Discover** → **Analyze** to build your pipeline.")
else:
    rows = []
    for a in analyses:
        sc = a.get("prospect_score", "?")
        g  = grade(sc)
        dm = a.get("key_decision_maker")
        rows.append({
            " ":        grade_emoji(g),
            "Company":  a.get("company_name", "?"),
            "Score":    int(sc) if sc not in ("?", "", None) else 0,
            "Grade":    g,
            "Category": (a.get("lead_category") or "—")[:30],
            "Key DM":   dm.get("name", "—") if isinstance(dm, dict) else (dm or "—"),
            "Outreach": a.get("outreach_status", "pending"),
            "Analyzed": a.get("analysis_date", "—"),
            "View":     ui.detail_link(a.get("url", "")),
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
            "View": st.column_config.LinkColumn(
                "View", display_text="Open ↗", help="Open the full prospect detail"
            ),
        },
    )

# ── Pipeline run history ──────────────────────────────────────────────────────

runs = ui.pipeline_runs(limit=30)
if runs:
    st.divider()
    st.subheader("Pipeline Run History")
    hist = pd.DataFrame([
        {
            "Run":        r.get("started_at", "")[:16],
            "Discovered": r.get("discovered", 0) or 0,
            "Analyzed":   r.get("analyzed", 0) or 0,
            "Queued":     r.get("queued", 0) or 0,
        }
        for r in reversed(runs)
    ])
    fig4 = px.bar(
        hist, x="Run", y=["Discovered", "Analyzed", "Queued"],
        barmode="group",
        color_discrete_sequence=["#2563eb", "#27ae60", "#f39c12"],
    )
    fig4.update_layout(height=260, margin=dict(t=10, b=0, l=0, r=0),
                       legend_title_text="", xaxis_title="")
    st.plotly_chart(fig4, use_container_width=True)

# ── OpenAI batches ────────────────────────────────────────────────────────────

if batch_rows:
    st.divider()
    st.subheader("🛰️ OpenAI Batch Runs")
    ui.batches_panel(batch_rows)
