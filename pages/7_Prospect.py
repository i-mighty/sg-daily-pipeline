"""Prospect detail — full discovery + analysis for one prospect.

Reachable from the Lead Browser (and Home) via the "Open full detail page" button,
which deep-links with ?url=<prospect url>. Also usable standalone: pick any prospect
from the selector. Works for un-analyzed leads too (shows discovery only).
"""

import streamlit as st

import ui
from utils import grade, grade_emoji

st.set_page_config(page_title="Prospect", page_icon="📄", layout="wide")
ui.inject_css()

prospects = ui.prospects()
if not prospects:
    st.info("No prospects yet. Go to **Discover** to find leads.")
    st.stop()

by_url = {p.get("url", ""): p for p in prospects if p.get("url")}
urls   = list(by_url.keys())


def _label(u: str) -> str:
    p = by_url.get(u, {})
    sc = p.get("prospect_score")
    badge = f" · {int(sc)}" if isinstance(sc, (int, float)) and sc else ""
    return f"{grade_emoji(grade(sc))} {p.get('company_name') or u}{badge}"


# ── Selector (synced with the ?url= deep link) ─────────────────────────────────

top_l, top_r = st.columns([5, 1])
with top_r:
    ui.refresh_button("refresh_prospect")
with top_l:
    st.page_link("pages/3_Leads.py", label="← Back to Lead Browser")

qp_url      = st.query_params.get("url", "")
default_idx = urls.index(qp_url) if qp_url in by_url else 0
sel = st.selectbox("Prospect", urls, index=default_idx, format_func=_label, key="prospect_sel")
if sel != qp_url:
    st.query_params["url"] = sel  # keep the URL shareable / refresh-safe

rec = ui.analysis_by_url(sel)
if not rec:
    st.error("Prospect not found.")
    st.stop()

# ── Discovery ──────────────────────────────────────────────────────────────────

st.markdown(f"## {rec.get('company_name') or rec.get('url')}")
url  = rec.get("url", "")
link = f"[{url}]({url})" if url else ""
st.markdown(f"{ui.status_badge(rec.get('status'))} &nbsp; {link}", unsafe_allow_html=True)

with st.container(border=True):
    st.markdown("#### 🔍 Discovery")
    d1, d2, d3 = st.columns(3)
    d1.markdown(f"**Category**  \n{rec.get('lead_category') or '—'}")
    d1.markdown(f"**Industry hint**  \n{rec.get('industry_hint') or rec.get('industry') or '—'}")
    d2.markdown(f"**Priority**  \n{rec.get('priority') or '—'}")
    d2.markdown(f"**Mode**  \n{rec.get('mode') or '—'}")
    d3.markdown(f"**Discovered**  \n{(rec.get('created_at') or '—')[:10]}")
    d3.markdown(f"**Last updated**  \n{(rec.get('updated_at') or '—')[:19]}")
    if rec.get("notes"):
        st.markdown(f"**Discovery notes**  \n{rec['notes']}")
    if rec.get("error_message"):
        st.error(f"Last error: {rec['error_message']}")

st.divider()

# ── Analysis ───────────────────────────────────────────────────────────────────

if rec.get("analysis_json") or rec.get("status") == "done":
    ui.render_prospect_detail(rec)
else:
    status = (rec.get("status") or "pending").lower()
    if status == "batching":
        st.info("🛰️ This lead is being analysed in an OpenAI batch. Check the **Batches** page; "
                "results land here once the run completes.")
    else:
        st.info("Not analysed yet. Run the **Analyze** step (or the pipeline) to generate the "
                "full prospect analysis.")
