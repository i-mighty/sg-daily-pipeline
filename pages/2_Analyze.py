import pandas as pd
import streamlit as st
from utils import load_prospects, stream_script

st.set_page_config(page_title="Analyze Leads", page_icon="⚡", layout="wide")

st.title("⚡ Analyze Leads")
st.caption("Runs the full AI analysis on pending leads — scores, decision makers, outreach emails.")

# ── Pending leads table ───────────────────────────────────────────────────────

prospects = load_prospects()
pending   = [p for p in prospects
             if p.get("status", "pending").lower() in {"pending", "", "error"}]

if not pending:
    st.success("No pending leads. Go to **Discover** to find new ones.")
    st.stop()

st.subheader(f"{len(pending)} leads ready to analyze")

rows = []
for p in pending:
    rows.append({
        "Company":    p.get("company_name") or p.get("url"),
        "Category":   p.get("lead_category", "—"),
        "Priority":   p.get("priority", "—"),
        "Mode":       p.get("mode", "generic"),
        "Status":     p.get("status", "pending"),
        "URL":        p.get("url", ""),
    })

df       = pd.DataFrame(rows)
selected = st.dataframe(
    df,
    hide_index=True,
    use_container_width=True,
    on_select="rerun",
    selection_mode="multi-row",
)

selected_idx = selected.selection.rows
n_selected   = len(selected_idx)

# ── Run options ───────────────────────────────────────────────────────────────

st.divider()
c1, c2, c3, c4 = st.columns(4)

run_all    = c1.toggle("Run all pending", value=True)
mode       = c2.selectbox("Mode", ["sg-daily", "generic"], index=0)
limit      = c3.number_input("Limit", min_value=1, max_value=50,
                              value=min(5, len(pending)), step=1,
                              help="Max companies per run (cost control)")
concurrency = c4.slider("Concurrency", min_value=1, max_value=5, value=1,
                         help="Parallel analyses. Keep at 1-2 to manage API costs.")

if not run_all and n_selected == 0:
    st.warning("Select rows above or enable 'Run all pending'.")
    st.stop()

label = f"🚀 Analyze {'all' if run_all else n_selected} lead(s)"
run   = st.button(label, type="primary", use_container_width=True)

# ── Estimated cost callout ────────────────────────────────────────────────────

n_to_run = min(limit, len(pending)) if run_all else n_selected
with st.expander("💰 Estimated cost"):
    low  = round(n_to_run * 0.30, 2)
    high = round(n_to_run * 0.80, 2)
    st.write(f"**{n_to_run} companies** × ~$0.30–0.80 each = **~${low}–${high} USD**")
    st.caption("Using claude-sonnet-4-6 with 20 tool call cap. Actual cost depends on data richness.")

# ── Run analysis ──────────────────────────────────────────────────────────────

if run:
    st.divider()
    args = [
        "scripts/run_batch.py",
        "--mode", mode,
        "--limit", str(limit),
        "--concurrency", str(concurrency),
    ]

    lines     = []
    exit_code = 0

    with st.status(f"⚡ Analyzing {n_to_run} lead(s)...", expanded=True) as status:
        output_box = st.empty()
        for line in stream_script(args):
            if line.startswith("__EXIT_CODE__"):
                exit_code = int(line.replace("__EXIT_CODE__", ""))
            else:
                lines.append(line)
                # Highlight key lines
                display = "\n".join(lines[-50:])
                output_box.code(display, language=None)

        if exit_code == 0:
            status.update(label="✅ Analysis complete!", state="complete")
        else:
            status.update(label="❌ Analysis failed — check output above", state="error")

    if exit_code == 0:
        done  = sum(1 for l in lines if l.startswith("[DONE]"))
        error = sum(1 for l in lines if l.startswith("[ERROR]"))
        col1, col2 = st.columns(2)
        col1.metric("Completed", done)
        col2.metric("Errors", error)
        st.success("Go to **Lead Browser** to view results or **Daily Queue** for outreach.")
