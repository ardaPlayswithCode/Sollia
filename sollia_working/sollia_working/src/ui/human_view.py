"""Human view — PM-facing dashboard, no internal math, no Greek letters."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import streamlit as st

from src.schemas import SolliaAnalysisResult, SolliaDiscrepancySignal, SolliaSentimentSignal


def render_human_view(result: Optional[SolliaAnalysisResult]) -> None:
    if result is None:
        _render_welcome()
        return

    _render_header(result)
    _render_exec_summary(result)

    col1, col2 = st.columns(2)
    with col1:
        _render_discrepancy(result)
    with col2:
        _render_sentiment(result)

    _render_agent_log(result)
    _render_export(result)


# ── Section 1: Header ─────────────────────────────────────────────────────────

def _render_welcome() -> None:
    st.markdown(
        '<div style="display:flex;align-items:center;justify-content:center;min-height:62vh;">'
        '<div style="text-align:center;">'
        '<p style="font-size:52px;font-weight:900;color:#F8FAFC;margin:0 0 8px;'
        'letter-spacing:.06em;line-height:1.1;">SOLLIA</p>'
        '<p style="font-size:14px;color:#94A3B8;margin:0 0 28px;">Agentic Investment Analysis</p>'
        '<p style="font-size:13px;color:#4a5060;line-height:1.8;">'
        'Enter a ticker in the sidebar and click '
        '<b style="color:#94A3B8;">Run Analysis</b> to begin.'
        '</p></div></div>',
        unsafe_allow_html=True,
    )


def _render_header(result: SolliaAnalysisResult) -> None:
    score = int(round(result.s_final))
    color = result.signal_color
    ts = datetime.now().strftime("%B %d, %Y %I:%M %p")
    st.markdown(
        f'<div style="text-align:center;padding:36px 0 28px;">'
        f'<div style="font-size:88px;font-weight:900;color:{color};line-height:1;'
        f'text-shadow:0 0 40px {color}33;">'
        f'{score}%</div>'
        f'<div style="font-size:30px;font-weight:700;color:{color};margin-top:6px;'
        f'letter-spacing:.08em;">'
        f'{result.signal}</div>'
        f'<div style="font-size:17px;font-weight:600;color:#F8FAFC;margin-top:12px;">'
        f'{result.company_name} ({result.ticker})</div>'
        f'<div style="font-size:12px;color:#94A3B8;margin-top:4px;">{ts}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ── Section 2: Executive Summary ─────────────────────────────────────────────

def _render_exec_summary(result: SolliaAnalysisResult) -> None:
    if not result.executive_summary:
        return
    st.markdown(
        f'<div style="background:#1A1D2E;border-radius:10px;padding:20px 24px;'
        f'margin-bottom:28px;">'
        f'<p style="color:#F8FAFC;font-size:15px;line-height:1.65;margin:0;">'
        f'{result.executive_summary}'
        f'</p></div>',
        unsafe_allow_html=True,
    )


# ── Section 3: Discrepancy (Agent 1) ─────────────────────────────────────────

def _render_discrepancy(result: SolliaAnalysisResult) -> None:
    st.markdown(
        '<div style="font-size:19px;font-weight:700;color:#F8FAFC;margin-bottom:14px;">'
        'Discrepancy</div>',
        unsafe_allow_html=True,
    )
    if not result.forensic:
        st.markdown(
            '<p style="color:#94A3B8;font-size:13px;">Forensic Accountant was not run.</p>',
            unsafe_allow_html=True,
        )
        return

    f = result.forensic

    # Metric box
    contra_html = ""
    if f.direct_contradiction:
        contra_html = (
            f'<p style="color:#EF4444;font-size:13px;font-weight:600;margin:10px 0 0;">'
            f'Direct contradiction detected: {f.contradiction_detail}</p>'
        )
    fallback_html = ""
    if f.fallback_used:
        fallback_html = (
            f'<p style="color:#F59E0B;font-size:12px;margin:8px 0 0;">'
            f'Note: {f.fallback_detail}</p>'
        )

    st.markdown(
        f'<div style="background:#1A1D2E;border-radius:10px;padding:18px 20px;margin-bottom:18px;">'
        f'<p style="color:#94A3B8;font-size:11px;font-weight:600;margin:0 0 6px;'
        f'text-transform:uppercase;letter-spacing:.08em;">Tone vs Reality</p>'
        f'<p style="color:#F8FAFC;font-size:15px;font-weight:600;margin:0;">'
        f'{f.discrepancy_label}</p>'
        f'{contra_html}'
        f'{fallback_html}'
        f'</div>',
        unsafe_allow_html=True,
    )

    if f.top_signals:
        st.markdown(
            '<p style="color:#94A3B8;font-size:11px;font-weight:700;margin-bottom:10px;'
            'text-transform:uppercase;letter-spacing:.08em;">Top Discrepancy Signals</p>',
            unsafe_allow_html=True,
        )
        for sig in f.top_signals:
            _render_discrepancy_signal(sig)


def _render_discrepancy_signal(sig: SolliaDiscrepancySignal) -> None:
    link_html = ""
    if sig.htm_link:
        link_html = (
            f'<a href="{sig.htm_link}" target="_blank" '
            f'style="color:#60A5FA;font-size:11px;text-decoration:none;">'
            f'View filing ↗</a>'
        )
    st.markdown(
        f'<div style="background:#0F1117;border-left:3px solid {sig.severity_color};'
        f'padding:10px 14px;margin-bottom:10px;border-radius:0 6px 6px 0;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;'
        f'margin-bottom:4px;">'
        f'<span style="color:{sig.severity_color};font-size:11px;font-weight:700;">'
        f'{sig.severity}</span>'
        f'<span style="color:#94A3B8;font-size:11px;">{sig.fiscal_period}</span>'
        f'</div>'
        f'<p style="color:#F8FAFC;font-size:13px;margin:0 0 6px;">{sig.description}</p>'
        f'<p style="color:#94A3B8;font-size:11px;margin:0 0 2px;">'
        f'Transcript: {sig.transcript_citation}</p>'
        f'<p style="color:#94A3B8;font-size:11px;margin:0 0 6px;">'
        f'Filing: {sig.filing_citation}</p>'
        f'{link_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


# ── Section 4: Sentiment (Agent 2) ───────────────────────────────────────────

def _render_sentiment(result: SolliaAnalysisResult) -> None:
    st.markdown(
        '<div style="font-size:19px;font-weight:700;color:#F8FAFC;margin-bottom:14px;">'
        'Sentiment</div>',
        unsafe_allow_html=True,
    )
    if not result.narrative:
        st.markdown(
            '<p style="color:#94A3B8;font-size:13px;">Narrative Scout was not run.</p>',
            unsafe_allow_html=True,
        )
        return

    n = result.narrative

    st.markdown(
        f'<div style="background:#1A1D2E;border-radius:10px;padding:18px 20px;margin-bottom:18px;">'
        f'<p style="color:#94A3B8;font-size:11px;font-weight:600;margin:0 0 6px;'
        f'text-transform:uppercase;letter-spacing:.08em;">News vs Historical Average</p>'
        f'<p style="color:#F8FAFC;font-size:15px;font-weight:600;margin:0;">'
        f'{n.sentiment_label}</p>'
        f'<p style="color:#94A3B8;font-size:12px;margin:8px 0 0;">'
        f'{n.filtered_articles:,} articles analyzed'
        f'{"  |  providers: " + ", ".join(n.providers_used) if n.providers_used else ""}'
        f'</p>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if n.top_signals:
        st.markdown(
            '<p style="color:#94A3B8;font-size:11px;font-weight:700;margin-bottom:10px;'
            'text-transform:uppercase;letter-spacing:.08em;">Top Sentiment Signals</p>',
            unsafe_allow_html=True,
        )
        for sig in n.top_signals:
            _render_sentiment_signal(sig)


def _render_sentiment_signal(sig: SolliaSentimentSignal) -> None:
    if sig.url:
        hl_html = (
            f'<a href="{sig.url}" target="_blank" '
            f'style="color:#F8FAFC;text-decoration:none;font-weight:500;">'
            f'{sig.headline}</a>'
        )
    else:
        hl_html = f'<span style="color:#F8FAFC;font-weight:500;">{sig.headline}</span>'

    score_color = (
        "#22C55E" if sig.sentiment_score > 0.05
        else "#EF4444" if sig.sentiment_score < -0.05
        else "#94A3B8"
    )
    score_prefix = "+" if sig.sentiment_score > 0 else ""

    st.markdown(
        f'<div style="background:#0F1117;border-radius:6px;padding:11px 14px;margin-bottom:10px;">'
        f'<p style="font-size:13px;margin:0 0 6px;line-height:1.4;">{hl_html}</p>'
        f'<div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;">'
        f'<span style="color:#94A3B8;font-size:11px;">{sig.source}</span>'
        f'<span style="color:#94A3B8;font-size:11px;">{sig.date}</span>'
        f'<span style="color:{score_color};font-size:11px;font-weight:700;">'
        f'{score_prefix}{sig.sentiment_score:.2f}</span>'
        f'</div>'
        f'<p style="color:#94A3B8;font-size:11px;margin:5px 0 0;">{sig.relevance}</p>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ── Section 5: Agent Log ──────────────────────────────────────────────────────

def _render_agent_log(result: SolliaAnalysisResult) -> None:
    with st.expander("Agent Log", expanded=True):
        # Conclusions
        if result.forensic and result.forensic.conclusion:
            st.markdown(
                f'<p style="color:#F8FAFC;font-size:14px;font-weight:600;'
                f'margin-bottom:4px;line-height:1.5;">'
                f'Forensic Accountant: {result.forensic.conclusion}</p>',
                unsafe_allow_html=True,
            )
        if result.narrative and result.narrative.conclusion:
            st.markdown(
                f'<p style="color:#F8FAFC;font-size:14px;font-weight:600;'
                f'margin-bottom:14px;line-height:1.5;">'
                f'Narrative Scout: {result.narrative.conclusion}</p>',
                unsafe_allow_html=True,
            )

        # Collect all log messages (deduplicated)
        all_msgs = []
        seen = set()

        def _add(msgs):
            for m in msgs:
                key = f"{m.speaker}|{m.message}"
                if key not in seen:
                    seen.add(key)
                    all_msgs.append(m)

        if result.forensic:
            _add(result.forensic.log_messages)
        if result.narrative:
            _add(result.narrative.log_messages)
        _add(result.log_messages)

        lines = []
        for msg in all_msgs:
            ts = msg.timestamp.strftime("%H:%M:%S") if msg.timestamp else "--:--:--"
            lines.append(
                f'<div style="margin-bottom:3px;">'
                f'<span style="color:#64748B;">[{ts}]</span> '
                f'<span style="color:#60A5FA;font-weight:600;">{msg.speaker}</span>'
                f'<span style="color:#94A3B8;">: </span>'
                f'<span style="color:#E2E8F0;">{msg.message}</span>'
                f'</div>'
            )

        log_html = (
            '<div style="font-family:\'Menlo\',\'Consolas\',monospace;font-size:12px;'
            'line-height:1.6;">' + "".join(lines) + '</div>'
        )
        st.markdown(log_html, unsafe_allow_html=True)


# ── Section 6: Export JSON ────────────────────────────────────────────────────

def _render_export(result: SolliaAnalysisResult) -> None:
    if not result.export_data:
        return
    st.markdown('<div style="margin-top:12px;"></div>', unsafe_allow_html=True)
    try:
        json_str = json.dumps(result.export_data, indent=2, default=str)
    except Exception:
        json_str = "{}"
    fname = f"sollia_{result.ticker}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    st.download_button(
        label="⬇ Export full analysis as JSON",
        data=json_str,
        file_name=fname,
        mime="application/json",
    )
