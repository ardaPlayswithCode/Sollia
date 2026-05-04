"""CSS fixes for Sollia dashboard."""
from __future__ import annotations

import streamlit as st


def inject_styles() -> None:
    st.markdown("""
<style>
/* ── Base background ── */
.stApp, .main, [data-testid="stAppViewContainer"] {
    background-color: #0F1117 !important;
}

/* ── Sidebar ── */
[data-testid="stSidebar"] {
    background-color: #1A1D2E !important;
}
[data-testid="stSidebar"] .stMarkdown,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] p {
    color: #F8FAFC !important;
}

/* ── Hide sidebar keyboard shortcut tooltip ── */
[data-testid="stSidebar"] [data-testid="stTooltipHoverTarget"] {
    display: none !important;
}

/* ── Global text ── */
.stMarkdown, p, span, label, div {
    color: #F8FAFC;
}

/* ── Expander margin ── */
.stExpander {
    margin-top: 24px !important;
    margin-bottom: 24px !important;
}

/* ── Input fields ── */
.stTextInput input {
    background-color: #0F1117 !important;
    color: #F8FAFC !important;
    border: 1px solid #2D3148 !important;
    border-radius: 6px !important;
}
.stTextInput input:focus {
    border-color: #3B82F6 !important;
    box-shadow: 0 0 0 2px rgba(59,130,246,0.2) !important;
}

/* ── Checkboxes ── */
.stCheckbox label {
    color: #94A3B8 !important;
}

/* ── Buttons ── */
.stButton > button[kind="primary"] {
    background-color: #3B82F6 !important;
    color: #fff !important;
    border: none !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
}
.stButton > button[kind="primary"]:hover {
    background-color: #2563EB !important;
}
.stButton > button {
    border-radius: 6px !important;
    background-color: #1A1D2E !important;
    color: #94A3B8 !important;
    border: 1px solid #2D3148 !important;
}
.stButton > button:hover {
    background-color: #232640 !important;
    color: #F8FAFC !important;
}

/* ── Download button ── */
.stDownloadButton > button {
    background-color: #1A1D2E !important;
    color: #60A5FA !important;
    border: 1px solid #3B82F6 !important;
    border-radius: 6px !important;
}

/* ── Expander header ── */
.stExpander details summary {
    background-color: #1A1D2E !important;
    color: #F8FAFC !important;
    border-radius: 8px !important;
}
.stExpander details[open] summary {
    border-radius: 8px 8px 0 0 !important;
}
.stExpander details .stExpanderContent {
    background-color: #1A1D2E !important;
    border-radius: 0 0 8px 8px !important;
    padding: 16px !important;
}

/* ── Spinner ── */
.stSpinner > div {
    border-top-color: #3B82F6 !important;
}

/* ── Columns ── */
[data-testid="stColumns"] {
    gap: 16px !important;
}
</style>
""", unsafe_allow_html=True)
