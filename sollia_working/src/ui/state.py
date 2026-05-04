"""Streamlit session state initialization."""
import streamlit as st


def init_state() -> None:
    defaults = {
        "result": None,
        "analyzing": False,
        "stop_requested": False,
        "agent_log": [],
        "current_ticker": "",
        "analysis_error": None,
        "result_from_cache": False,
        "run_fa": True,
        "run_ns": True,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val
