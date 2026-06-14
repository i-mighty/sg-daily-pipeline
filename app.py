"""Entry point / router for the SG Daily control dashboard.

Uses st.navigation so we control exactly which pages appear in the sidebar.
Queue and Prospect are routable but hidden (visibility="hidden"): Prospect opens
from a lead's "Open ↗" link, and the outreach/send view lives on that page.
"""

import streamlit as st

import ui

st.set_page_config(
    page_title="SG Daily · Sales Pipeline",
    page_icon="🎯",
    layout="wide",
)
ui.inject_css()

nav = st.navigation([
    st.Page("home.py",             title="Home",     icon="🎯", url_path="home", default=True),
    st.Page("pages/1_Discover.py", title="Discover", icon="🔍", url_path="discover"),
    st.Page("pages/2_Analyze.py",  title="Analyze",  icon="⚡", url_path="analyze"),
    st.Page("pages/3_Leads.py",    title="Leads",    icon="📇", url_path="leads"),
    st.Page("pages/5_Modes.py",    title="Modes",    icon="⚙️", url_path="modes"),
    st.Page("pages/6_Batches.py",  title="Batches",  icon="🛰️", url_path="batches"),
    # Hidden from the sidebar but still routable:
    st.Page("pages/7_Prospect.py", title="Prospect", icon="📄",
            url_path="prospect", visibility="hidden"),
    st.Page("pages/4_Queue.py",    title="Queue",    icon="📬",
            url_path="queue", visibility="hidden"),
])
nav.run()
