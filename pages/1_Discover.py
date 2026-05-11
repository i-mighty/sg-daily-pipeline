import streamlit as st
from utils import load_prospects, stream_script

st.set_page_config(page_title="Discover Leads", page_icon="🔍", layout="wide")

st.title("🔍 Discover Leads")
st.caption("Autonomously finds brands matching the SG Dollar Filter and adds them to your pipeline.")

# ── Config form ───────────────────────────────────────────────────────────────

with st.form("discover_form"):
    c1, c2, c3 = st.columns(3)

    count = c1.number_input("Leads to find", min_value=1, max_value=50, value=5, step=1)

    cat_label = c2.selectbox(
        "Category",
        ["Both", "A — Media Feature Leads only", "B — Influencer Management only"],
    )
    cat_map = {"Both": "both", "A — Media Feature Leads only": "a",
               "B — Influencer Management only": "b"}
    cat = cat_map[cat_label]

    dry_run = c3.toggle("Dry run (preview only, don't add to CSV)", value=False)

    submitted = st.form_submit_button("🚀 Run Discovery", type="primary", use_container_width=True)

# ── Existing pipeline preview ─────────────────────────────────────────────────

prospects = load_prospects()
pending   = [p for p in prospects if p.get("status", "pending").lower() in {"pending", ""}]

col_a, col_b = st.columns(2)
col_a.metric("Total in CSV", len(prospects))
col_b.metric("Pending Analysis", len(pending))

if pending:
    with st.expander(f"View {len(pending)} pending leads"):
        for p in pending:
            cat_tag = p.get("lead_category", "")
            tag = f"[{cat_tag[:1].upper()}]" if cat_tag else ""
            st.write(f"**{tag} {p.get('company_name') or p.get('url')}** — {p.get('url')}")

# ── Run discovery ─────────────────────────────────────────────────────────────

if submitted:
    st.divider()
    args = ["scripts/discover_leads.py", "--count", str(count), "--cat", cat]
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
        st.success("Leads added to prospects.csv. Go to **Analyze** to score them.")
        st.rerun()
