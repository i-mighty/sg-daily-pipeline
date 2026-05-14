import streamlit as st

import db
from utils import stream_script

st.set_page_config(page_title="Modes", page_icon="⚙️", layout="wide")

st.title("⚙️ Pipeline Modes")
st.caption("Each mode has its own discovery prompt, analysis prompt, and pipeline settings. "
           "Active modes run automatically in the daily cron.")

# ── Session state ─────────────────────────────────────────────────────────────

if "editing_mode" not in st.session_state:
    st.session_state.editing_mode = None   # name of mode being edited
if "adding_mode" not in st.session_state:
    st.session_state.adding_mode = False
if "confirm_delete" not in st.session_state:
    st.session_state.confirm_delete = None
if "running_mode" not in st.session_state:
    st.session_state.running_mode = None

# ── Load modes ────────────────────────────────────────────────────────────────

modes = db.get_modes()

# ── Mode cards ────────────────────────────────────────────────────────────────

if not modes:
    st.info("No modes found. Add your first mode below.")
else:
    for mode in modes:
        name       = mode["name"]
        is_editing = st.session_state.editing_mode == name
        is_running = st.session_state.running_mode == name

        with st.container(border=True):
            # Header row
            h1, h2, h3, h4, h5 = st.columns([3, 1, 1, 1, 3])
            with h1:
                active_badge = "🟢 Active" if mode["is_active"] else "⚫ Inactive"
                st.markdown(f"**{mode['label']}** &nbsp; `{name}` &nbsp; {active_badge}")
                if mode.get("description"):
                    st.caption(mode["description"])
            h2.metric("Discover / run", mode["discover_count"])
            h3.metric("Queue size",     mode["queue_size"])

            leads_count = len([l for l in db.get_leads(mode=name)])
            h4.metric("Leads in DB",    leads_count)

            with h5:
                btn1, btn2, btn3 = st.columns(3)
                if btn1.button("✏️ Edit",   key=f"edit_{name}",   use_container_width=True):
                    st.session_state.editing_mode = name if not is_editing else None
                    st.rerun()
                if btn2.button("▶ Run",    key=f"run_{name}",    use_container_width=True):
                    st.session_state.running_mode = name
                    st.rerun()
                if btn3.button("🗑 Delete", key=f"delete_{name}", use_container_width=True):
                    st.session_state.confirm_delete = name
                    st.rerun()

            # ── Delete confirmation ───────────────────────────────────────────
            if st.session_state.confirm_delete == name:
                st.warning(f"Delete mode **{name}**? This cannot be undone. Leads tagged with this mode remain in DB.")
                dc1, dc2, _ = st.columns([1, 1, 4])
                if dc1.button("Yes, delete", key=f"confirm_del_{name}", type="primary"):
                    db.delete_mode(name)
                    st.session_state.confirm_delete = None
                    st.rerun()
                if dc2.button("Cancel", key=f"cancel_del_{name}"):
                    st.session_state.confirm_delete = None
                    st.rerun()

            # ── Inline run panel ──────────────────────────────────────────────
            if is_running:
                st.divider()
                st.markdown(f"#### ▶ Running pipeline for `{name}`")
                run_opts = st.columns(3)
                no_email  = run_opts[0].toggle("Skip emails",  key=f"noemail_{name}")
                no_clean  = run_opts[1].toggle("Keep files",   key=f"noclean_{name}")
                dry_run   = run_opts[2].toggle("Dry run only", key=f"dryrun_{name}")

                if st.button(f"🚀 Start run for {name}", key=f"start_{name}", type="primary"):
                    args = ["scripts/run_pipeline.py", "--mode", name]
                    if no_email:  args.append("--no-email")
                    if no_clean:  args.append("--no-cleanup")
                    if dry_run:   args.append("--dry-run")

                    lines     = []
                    exit_code = 0
                    with st.status(f"Running pipeline for {name}...", expanded=True) as status:
                        output_box = st.empty()
                        for line in stream_script(args):
                            if line.startswith("__EXIT_CODE__"):
                                exit_code = int(line.replace("__EXIT_CODE__", ""))
                            else:
                                lines.append(line)
                                output_box.code("\n".join(lines[-60:]), language=None)
                        if exit_code == 0:
                            status.update(label=f"✅ Pipeline for {name} complete!", state="complete")
                        else:
                            status.update(label=f"❌ Pipeline failed — check output", state="error")

                    st.session_state.running_mode = None

                if st.button("✕ Cancel", key=f"cancel_run_{name}"):
                    st.session_state.running_mode = None
                    st.rerun()

            # ── Edit form ─────────────────────────────────────────────────────
            if is_editing:
                st.divider()
                _render_edit_form(mode)


# ── Edit form renderer ────────────────────────────────────────────────────────

def _render_edit_form(mode: dict):
    name = mode["name"]
    with st.form(key=f"form_edit_{name}"):
        st.markdown("#### Edit Mode")

        fc1, fc2 = st.columns(2)
        new_label = fc1.text_input("Label (display name)", value=mode["label"])
        new_desc  = fc2.text_input("Description", value=mode.get("description", ""))

        fs1, fs2, fs3 = st.columns(3)
        new_discover = fs1.number_input("Discover count / run", min_value=0, max_value=100,
                                         value=int(mode["discover_count"]))
        new_queue    = fs2.number_input("Queue size",            min_value=1, max_value=50,
                                         value=int(mode["queue_size"]))
        new_active   = fs3.toggle("Active (runs in cron)", value=bool(mode["is_active"]))

        st.markdown("**Discovery Prompt** — uses `{NUM_LEADS}` as the only placeholder")
        new_discovery = st.text_area(
            "discovery_prompt", value=mode.get("discovery_prompt", ""),
            height=300, label_visibility="collapsed",
            help="Instructs the AI what kinds of companies to find. Use {NUM_LEADS} for the count.",
        )

        st.markdown("**Analysis Prompt** — placeholders: `{URL}` `{COMPANY_NAME}` `{INDUSTRY_HINT}` `{NOTES}` `{LEAD_CATEGORY}` `{TODAY}`")
        new_analysis = st.text_area(
            "analysis_prompt", value=mode.get("analysis_prompt", ""),
            height=500, label_visibility="collapsed",
            help="Full system prompt for the analysis agent.",
        )

        save, cancel = st.columns([1, 5])
        submitted = save.form_submit_button("💾 Save", type="primary", use_container_width=True)
        if cancel.form_submit_button("Cancel", use_container_width=True):
            st.session_state.editing_mode = None
            st.rerun()

        if submitted:
            db.upsert_mode({
                "name":             name,
                "label":            new_label,
                "description":      new_desc,
                "discover_count":   new_discover,
                "queue_size":       new_queue,
                "is_active":        1 if new_active else 0,
                "discovery_prompt": new_discovery,
                "analysis_prompt":  new_analysis,
            })
            st.session_state.editing_mode = None
            st.success(f"Mode '{name}' saved.")
            st.rerun()


# Streamlit requires functions to be defined before use in forms inside loops;
# re-render edit forms here now that the function is defined.
# (The calls inside the loop above work because Python resolves the name at call time.)


st.divider()

# ── Add new mode ──────────────────────────────────────────────────────────────

if st.button("＋ Add New Mode", type="secondary"):
    st.session_state.adding_mode = True

if st.session_state.adding_mode:
    st.markdown("### New Mode")

    with st.form("form_add_mode"):
        nc1, nc2 = st.columns(2)
        new_name  = nc1.text_input("Name (slug)", placeholder="e.g. us-saas",
                                    help="Lowercase, no spaces. Used in code and file names.")
        new_label = nc2.text_input("Label", placeholder="e.g. US SaaS Outbound")
        new_desc  = st.text_input("Description", placeholder="One-line description of what this mode targets")

        ns1, ns2, ns3 = st.columns(3)
        new_discover = ns1.number_input("Discover count / run", min_value=0, max_value=100, value=5)
        new_queue    = ns2.number_input("Queue size",            min_value=1, max_value=50,  value=8)
        new_active   = ns3.toggle("Active (runs in cron)", value=False)

        st.markdown("**Discovery Prompt** — use `{NUM_LEADS}` as the only placeholder")
        new_discovery = st.text_area(
            "discovery_prompt_new", height=250, label_visibility="collapsed",
            placeholder="You are a lead scout. Find {NUM_LEADS} companies that match...",
        )

        st.markdown("**Analysis Prompt** — placeholders: `{URL}` `{COMPANY_NAME}` `{INDUSTRY_HINT}` `{NOTES}` `{LEAD_CATEGORY}` `{TODAY}`")
        new_analysis = st.text_area(
            "analysis_prompt_new", height=400, label_visibility="collapsed",
            placeholder="You are a sales intelligence analyst. Analyze the following company...",
        )

        add_col, cancel_col = st.columns([1, 5])
        add_submitted = add_col.form_submit_button("Add Mode", type="primary", use_container_width=True)
        if cancel_col.form_submit_button("Cancel", use_container_width=True):
            st.session_state.adding_mode = False
            st.rerun()

        if add_submitted:
            slug = new_name.strip().lower().replace(" ", "-")
            if not slug:
                st.error("Name is required.")
            elif db.get_mode(slug):
                st.error(f"Mode '{slug}' already exists.")
            else:
                db.upsert_mode({
                    "name":             slug,
                    "label":            new_label or slug,
                    "description":      new_desc,
                    "discover_count":   new_discover,
                    "queue_size":       new_queue,
                    "is_active":        1 if new_active else 0,
                    "discovery_prompt": new_discovery,
                    "analysis_prompt":  new_analysis,
                })
                st.session_state.adding_mode = False
                st.success(f"Mode '{slug}' created.")
                st.rerun()
