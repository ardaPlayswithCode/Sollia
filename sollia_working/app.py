"""Sollia — Agentic Investment Analysis Dashboard."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import streamlit as st

st.set_page_config(
    page_title="SOLLIA",
    layout="wide",
    initial_sidebar_state="expanded",
)

from config.settings import settings  # noqa: F401 — ensures settings are importable
from src.ui.state import init_state
from src.ui.human_view import render_human_view
from src.ui.styles import inject_styles

inject_styles()
init_state()

# ── Keyboard shortcuts (Enter = Run, Escape = Stop) ────────────────────────
try:
    import streamlit.components.v1 as _components
    _components.html("""
<script>
(function() {
    function clickBtn(label) {
        var btns = parent.document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            if (btns[i].innerText.trim() === label) {
                btns[i].click();
                return;
            }
        }
    }
    parent.document.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey &&
            parent.document.activeElement &&
            parent.document.activeElement.tagName !== 'INPUT' &&
            parent.document.activeElement.tagName !== 'TEXTAREA') {
            clickBtn('Run Analysis');
        }
        if (e.key === 'Escape') {
            clickBtn('Stop');
        }
    }, false);
})();
</script>
""", height=0)
except Exception:
    pass  # Components unavailable — keyboard shortcuts silently disabled


# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        '<p style="font-size:24px;font-weight:900;color:#F8FAFC;margin:0 0 2px;">'
        'SOLLIA</p>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<p style="font-size:11px;color:#94A3B8;margin-bottom:24px;">'
        'Agentic Investment Analysis</p>',
        unsafe_allow_html=True,
    )

    ticker_input = st.text_input(
        "Ticker",
        value=st.session_state.get("current_ticker", ""),
        placeholder="e.g. NVDA",
        key="ticker_input_field",
        label_visibility="visible",
    )

    st.markdown(
        '<p style="color:#94A3B8;font-size:12px;font-weight:600;margin-top:16px;'
        'margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em;">Agents</p>',
        unsafe_allow_html=True,
    )
    run_fa = st.checkbox(
        "Forensic Accountant",
        value=st.session_state.get("run_fa", True),
        key="run_fa_cb",
    )
    run_ns = st.checkbox(
        "Narrative Scout",
        value=st.session_state.get("run_ns", True),
        key="run_ns_cb",
    )

    st.markdown('<div style="margin-top:16px;"></div>', unsafe_allow_html=True)
    col_r, col_s = st.columns(2)
    with col_r:
        run_clicked = st.button("Run Analysis", type="primary", use_container_width=True)
    with col_s:
        stop_clicked = st.button("Stop", use_container_width=True)

    if stop_clicked:
        st.session_state.stop_requested = True
        st.session_state.analyzing = False

    # Home button — only shown when a result is displayed
    if st.session_state.get("result") is not None:
        st.markdown('<div style="margin-top:8px;"></div>', unsafe_allow_html=True)
        if st.button("← Home", use_container_width=True):
            st.session_state.result = None
            st.session_state.analysis_error = None
            st.session_state.current_ticker = ""
            st.rerun()

    # Error display
    if st.session_state.get("analysis_error"):
        st.markdown('<div style="margin-top:8px;"></div>', unsafe_allow_html=True)
        st.error(st.session_state.analysis_error)

    # Info / keyboard hint
    st.markdown(
        '<p style="color:#4a5060;font-size:11px;margin-top:24px;">'
        'Enter = Run Analysis &nbsp;|&nbsp; Esc = Stop</p>',
        unsafe_allow_html=True,
    )


# ── Ticker validation ──────────────────────────────────────────────────────
def _validate_ticker(ticker: str):
    """Returns (valid: bool, suggestion: str or None)."""
    import requests
    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "Sollia Analysis contact@sollia.app"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        ticker_up = ticker.upper()
        for item in data.values():
            if str(item.get("ticker", "")).upper() == ticker_up:
                return True, None
        # Fuzzy suggestion
        candidates = [
            str(v.get("ticker", "")).upper()
            for v in data.values()
            if str(v.get("ticker", "")).upper().startswith(ticker_up[:2])
        ]
        suggestion = candidates[0] if candidates else None
        return False, suggestion
    except Exception:
        return True, None  # If validation fails, proceed anyway


# ── Handle Run click ───────────────────────────────────────────────────────
if run_clicked and ticker_input.strip():
    raw_ticker = ticker_input.strip().upper()

    if not run_fa and not run_ns:
        st.session_state.analysis_error = "Please select at least one agent to run."
    else:
        valid, suggestion = _validate_ticker(raw_ticker)
        if not valid:
            if suggestion:
                st.session_state.analysis_error = (
                    f"Ticker '{raw_ticker}' not found in SEC EDGAR. "
                    f"Did you mean: {suggestion}?"
                )
            else:
                st.session_state.analysis_error = (
                    f"Ticker '{raw_ticker}' not found in SEC EDGAR. "
                    f"Please verify the symbol."
                )
        else:
            st.session_state.analysis_error = None
            st.session_state.result = None
            st.session_state.analyzing = True
            st.session_state.stop_requested = False
            st.session_state.current_ticker = raw_ticker
            st.session_state.run_fa = run_fa
            st.session_state.run_ns = run_ns
            st.rerun()


# ── Analysis execution ─────────────────────────────────────────────────────
if st.session_state.get("analyzing") and st.session_state.get("result") is None:
    ticker = st.session_state.current_ticker
    do_fa = st.session_state.get("run_fa", True)
    do_ns = st.session_state.get("run_ns", True)

    # Observatory animation placeholder
    anim_slot = st.empty()
    log_slot = st.empty()

    anim_slot.markdown(
        f'<div style="text-align:center;padding:40px 0 20px;">'
        f'<p style="font-size:20px;font-weight:700;color:#F8FAFC;margin-bottom:6px;">'
        f'Analysing {ticker}...</p>'
        f'<p style="font-size:13px;color:#94A3B8;">Running agentic pipeline — this may take a minute.</p>'
        f'</div>',
        unsafe_allow_html=True,
    )

    from src.pipeline.orchestrator import run_analysis
    from src.schemas import SolliaAnalysisResult, AgentLogMessage

    log_lines = []
    final_result = None
    error_msg = None

    try:
        for event in run_analysis(ticker, run_forensic=do_fa, run_narrative=do_ns):
            if st.session_state.get("stop_requested"):
                break
            if isinstance(event, SolliaAnalysisResult):
                final_result = event
            elif isinstance(event, AgentLogMessage):
                log_lines.append(f"[{event.speaker}] {event.message}")
                tail = log_lines[-18:]
                body = "".join(
                    f'<div>[{ln.split("]")[0][1:]}] '
                    f'{"] ".join(ln.split("]")[1:])}</div>'
                    for ln in tail
                )
                log_slot.markdown(
                    f'<div style="background:#1A1D2E;border-radius:8px;padding:14px 16px;'
                    f'font-family:monospace;font-size:12px;color:#94A3B8;'
                    f'max-height:220px;overflow:auto;">{body}</div>',
                    unsafe_allow_html=True,
                )
    except Exception as exc:
        error_msg = str(exc)

    anim_slot.empty()
    log_slot.empty()

    if error_msg:
        st.session_state.analysis_error = f"Analysis failed: {error_msg}"
    elif final_result:
        st.session_state.result = final_result
        st.session_state.analysis_error = None
    else:
        st.session_state.analysis_error = "Analysis did not produce a result. Please try again."

    st.session_state.analyzing = False
    st.rerun()


# ── Render result ──────────────────────────────────────────────────────────
render_human_view(st.session_state.get("result"))
