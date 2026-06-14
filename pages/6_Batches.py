"""Batches — OpenAI Batch API pipeline monitor + collector trigger."""

import streamlit as st

import ui
from utils import stream_script

ui.inject_css()

head_l, head_r = st.columns([5, 1])
with head_l:
    st.title("🛰️ OpenAI Batch Runs")
    st.caption("Staged analysis over the OpenAI Batch API — research → contact ∥ score → outreach.")
with head_r:
    ui.refresh_button("refresh_batches")

batch_rows = ui.batches()

# ── Summary ────────────────────────────────────────────────────────────────────

active = [b for b in batch_rows
          if b.get("status") not in ("collected", "failed", "expired", "cancelled")]
runs = {b.get("parent_batch_id") for b in batch_rows}
c1, c2, c3 = st.columns(3)
c1.metric("Total runs", len([r for r in runs if r]))
c2.metric("Active rounds", len(active))
c3.metric("Batches recorded", len(batch_rows))

st.divider()

# ── Controls ───────────────────────────────────────────────────────────────────

st.subheader("Collector")
st.caption(
    "The collector polls in-flight batches, saves each completed round, and submits "
    "the next. In production this runs every 15 minutes via the `cron_collect` Railway "
    "service — run it manually here to advance immediately."
)
col_run, col_status = st.columns(2)
with col_run:
    if st.button("▶ Run collector now", type="primary"):
        with st.status("Running batch_collect.py…", expanded=True) as status:
            code = None
            for line in stream_script(["scripts/batch_collect.py"]):
                if line.startswith("__EXIT_CODE__"):
                    code = int(line.replace("__EXIT_CODE__", ""))
                else:
                    st.write(line)
            status.update(label=f"Collector finished (exit {code})",
                          state="complete" if code == 0 else "error")
        ui.clear_caches()
with col_status:
    if st.button("🔍 Show batch statuses"):
        with st.status("Polling OpenAI…", expanded=True) as status:
            for line in stream_script(["scripts/batch_collect.py", "--status"]):
                if not line.startswith("__EXIT_CODE__"):
                    st.write(line)
            status.update(label="Done", state="complete")

st.divider()

# ── Runs ───────────────────────────────────────────────────────────────────────

st.subheader("Runs")
ui.batches_panel(batch_rows)
