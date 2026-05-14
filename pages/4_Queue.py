from datetime import datetime
from pathlib import Path

import streamlit as st

import db
from utils import (dm_email, dm_name, grade_emoji, load_analyses,
                   load_queue_files, mark_outreach_sent, score_emoji,
                   stream_script)

st.set_page_config(page_title="Daily Queue", page_icon="📬", layout="wide")

st.title("📬 Daily Outreach Queue")
st.caption("Your highest-priority targets for today. Copy, send, mark done.")

# ── Mode selector ─────────────────────────────────────────────────────────────

modes      = db.get_modes()
mode_names = [m["name"]  for m in modes] or ["sg-daily"]
mode_labels= [m["label"] for m in modes] or ["SG Daily"]

sel_idx    = st.selectbox("Mode", range(len(mode_names)),
                           format_func=lambda i: f"{mode_labels[i]} ({mode_names[i]})",
                           label_visibility="collapsed")
sel_mode   = mode_names[sel_idx] if mode_names else "sg-daily"

# ── Generate queue ─────────────────────────────────────────────────────────────

col_gen, col_count, col_cat = st.columns([2, 1, 1])

with col_gen:
    generate = st.button("🔄 Generate Today's Queue", type="primary", use_container_width=True)

with col_count:
    count = st.number_input("Targets", min_value=3, max_value=20, value=8, step=1)

with col_cat:
    cat_filter = st.selectbox("Filter category", ["All", "Media Feature Lead", "Influencer Management Lead"])

if generate:
    args = ["scripts/daily_queue.py", "--mode", sel_mode, "--count", str(count)]
    if cat_filter != "All":
        args += ["--category", cat_filter]

    with st.status("Generating queue...", expanded=False) as status:
        lines = []
        for line in stream_script(args):
            if not line.startswith("__EXIT_CODE__"):
                lines.append(line)
        status.update(label="✅ Queue generated!", state="complete")

    for l in lines:
        st.write(l)
    st.rerun()

# ── Load and display queue ────────────────────────────────────────────────────

analyses  = load_analyses()
queue_mds = load_queue_files()

# Tab 1: Live queue from JSON (interactive)
# Tab 2: Saved markdown files
tab_live, tab_saved = st.tabs(["🎯 Live Queue", "📁 Saved Queue Files"])

with tab_live:
    # Build live queue: done analyses for this mode, not sent, sorted by score, top N
    candidates = [
        a for a in analyses
        if a.get("outreach_status", "").lower() not in {"sent", "replied", "converted"}
        and a.get("mode", "sg-daily") == sel_mode
    ]

    if cat_filter != "All":
        candidates = [a for a in candidates if a.get("lead_category", "") == cat_filter]

    queue = candidates[:count]

    if not queue:
        st.info("No leads in queue. Discover and analyze leads first, or all outreach is already sent.")
    else:
        today = datetime.now().strftime("%B %d, %Y")
        st.markdown(f"### Queue for {today} — {len(queue)} targets")
        st.divider()

        for i, lead in enumerate(queue, 1):
            sc   = lead.get("prospect_score", 0)
            name = lead.get("company_name", "Unknown")
            cat  = lead.get("lead_category", "")
            sg_usp = lead.get("sg_usp", "")

            email = lead.get("outreach_email", {}) or {}
            hooks = lead.get("hook_ideas") or email.get("hook_ideas", [])

            to_name  = email.get("to_name")  or dm_name(lead)
            to_email = email.get("to_email") or dm_email(lead)
            to_title = email.get("to_title", "")
            subj_a   = email.get("subject_a") or email.get("subject", "")
            subj_b   = email.get("subject_b", "")
            body     = email.get("body", "")
            cta      = email.get("cta", "Are you free for a 10-minute call?")
            sts      = lead.get("outreach_status", "pending")

            with st.container(border=True):
                h1, h2, h3 = st.columns([3, 1, 1])
                with h1:
                    st.markdown(f"### {i}. {score_emoji(sc)} {name}")
                    st.caption(f"{cat} · Score {sc}/100")
                with h2:
                    st.markdown(f"**Send to**  \n{to_name}  \n_{to_title}_")
                with h3:
                    st.markdown(f"**Email**  \n`{to_email}`")
                    if sts == "sent":
                        st.success("✅ Sent")
                    else:
                        if st.button("Mark Sent ✅", key=f"q_sent_{i}_{lead.get('url','')}"):
                            mark_outreach_sent(lead.get("url", ""))
                            st.rerun()

                if sg_usp:
                    st.info(f"💡 SG USP: {sg_usp}")

                if hooks:
                    st.markdown("**Hook Ideas:**")
                    for h in hooks[:3]:
                        st.markdown(f"• {h}")

                ea, eb, ebody = st.columns([2, 2, 3])
                ea.markdown(f"**Subject A**  \n{subj_a}")
                if subj_b:
                    eb.markdown(f"**Subject B**  \n{subj_b}")

                if body:
                    with st.expander("📋 Copy email body"):
                        st.code(body, language=None)
                        st.caption(f"_{cta}_")

                st.divider()

with tab_saved:
    if not queue_mds:
        st.info("No saved queue files yet. Click 'Generate Today's Queue' above.")
    else:
        chosen = st.selectbox(
            "Select queue file",
            [p.name for p in queue_mds],
        )
        path = next(p for p in queue_mds if p.name == chosen)

        col_view, col_dl = st.columns([3, 1])
        with col_view:
            st.markdown(path.read_text(encoding="utf-8"))
        with col_dl:
            # Check for matching CSV
            csv_name = chosen.replace("DAILY-QUEUE-", "queue-").replace(".md", ".csv")
            csv_path = Path("results") / csv_name
            if csv_path.exists():
                with open(csv_path, "rb") as f:
                    st.download_button(
                        "⬇️ Download CSV",
                        data=f,
                        file_name=csv_name,
                        mime="text/csv",
                        use_container_width=True,
                    )
