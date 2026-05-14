import streamlit as st

import db
from utils import load_prospects, stream_script

st.set_page_config(page_title="Discover Leads", page_icon="🔍", layout="wide")

st.title("🔍 Discover Leads")
st.caption("Autonomously finds companies matching a mode's discovery prompt and adds them to your pipeline.")

# ── Load modes ────────────────────────────────────────────────────────────────

modes = db.get_modes()
if not modes:
    st.warning("No modes configured. Go to **Modes** to add one.")
    st.stop()

mode_names  = [m["name"]  for m in modes]
mode_labels = [m["label"] for m in modes]

# ── Config form ───────────────────────────────────────────────────────────────

with st.form("discover_form"):
    c1, c2, c3 = st.columns(3)

    sel_idx  = c1.selectbox("Mode", range(len(modes)),
                             format_func=lambda i: f"{mode_labels[i]} ({mode_names[i]})")
    sel_mode = modes[sel_idx]

    default_count = int(sel_mode.get("discover_count") or 5)
    count   = c2.number_input("Leads to find", min_value=1, max_value=50,
                               value=default_count, step=1,
                               help="Overrides the mode's default discover count for this run.")
    dry_run = c3.toggle("Dry run (preview only, don't add to DB)", value=False)

    submitted = st.form_submit_button("🚀 Run Discovery", type="primary", use_container_width=True)

# ── Pipeline preview ──────────────────────────────────────────────────────────

prospects = load_prospects()
pending   = [p for p in prospects
             if p.get("status", "pending").lower() in {"pending", ""}
             and p.get("mode") == sel_mode["name"]]

col_a, col_b = st.columns(2)
col_a.metric("Total leads in DB", len(prospects))
col_b.metric(f"Pending ({sel_mode['name']})", len(pending))

if pending:
    with st.expander(f"View {len(pending)} pending leads for {sel_mode['label']}"):
        for p in pending:
            cat_tag = p.get("lead_category", "")
            tag = f"[{cat_tag[:1].upper()}]" if cat_tag else ""
            st.write(f"**{tag} {p.get('company_name') or p.get('url')}** — {p.get('url')}")

# ── Run discovery ─────────────────────────────────────────────────────────────

if submitted:
    st.divider()
    args = ["scripts/discover_leads.py", "--mode", sel_mode["name"], "--count", str(count)]
    if dry_run:
        args.append("--dry-run")

    st.subheader("Discovery Output")
    lines     = []
    exit_code = 0

    with st.status("🔍 Scanning for leads...", expanded=True) as status:
        output_box = st.empty()
        for line in stream_script(args):
            if line.startswith("__EXIT_CODE__"):
                exit_code = int(line.replace("__EXIT_CODE__", ""))
            else:
                lines.append(line)
                output_box.code("\n".join(lines[-40:]), language=None)

        if exit_code == 0:
            status.update(label="✅ Discovery complete!", state="complete")
        else:
            status.update(label="❌ Discovery failed — check output above", state="error")

    if exit_code == 0 and not dry_run:
        st.success(f"Leads added to DB. Go to **Analyze** to score them.")
        st.rerun()
