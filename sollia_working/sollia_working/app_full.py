import streamlit as st
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

st.set_page_config(page_title="Sollia", layout="wide")

st.markdown("""
<style>
.stApp { background: #0F1117; }
h1,h2,h3,p,span,div { color: #F8FAFC; }
</style>
""", unsafe_allow_html=True)

st.markdown("<h1 style='text-align:center;'>Sollia</h1>", unsafe_allow_html=True)

if st.button("Run NVDA Analysis", type="primary"):
    from src.pipeline.orchestrator import run_analysis
    from src.schemas import SolliaAnalysisResult, AgentLogMessage
    
    result = None
    logs = []
    status = st.empty()
    
    for event in run_analysis('NVDA', run_forensic=True, run_narrative=True):
        if isinstance(event, AgentLogMessage):
            logs.append(event)
            status.text(f"[{event.speaker}] {event.message}")
        elif isinstance(event, SolliaAnalysisResult):
            result = event
    
    status.empty()
    
    if result:
        col1, col2, col3 = st.columns([1,2,1])
        with col2:
            st.markdown(f"<h1 style='font-size:80px;text-align:center;color:{result.signal_color};margin:0;'>{int(round(result.s_final))}%</h1>", unsafe_allow_html=True)
            st.markdown(f"<h2 style='text-align:center;color:{result.signal_color};'>{result.signal}</h2>", unsafe_allow_html=True)
            st.markdown(f"<p style='text-align:center;color:#94A3B8;'>{result.company_name}</p>", unsafe_allow_html=True)
            st.markdown(f"<p style='text-align:center;color:#94A3B8;font-size:14px;'>{result.executive_summary}</p>", unsafe_allow_html=True)
            
            # Discrepancy
            if result.forensic:
                st.markdown("---")
                st.markdown(f"### Discrepancy")
                st.markdown(f"**{result.forensic.discrepancy_label}**")
                if result.forensic.top_signals:
                    for s in result.forensic.top_signals:
                        st.markdown(f"- [{s.severity}] **{s.quarter}**: {s.description} | Transcript: {s.transcript_citation[:80]}... | [Filing]({s.sec_link})")
            
            # Sentiment
            if result.narrative:
                st.markdown("---")
                st.markdown(f"### Sentiment")
                st.markdown(f"**{result.narrative.sentiment_label}**")
                if result.narrative.top_signals:
                    for s in result.narrative.top_signals:
                        st.markdown(f"- [{s.source}] [{s.headline[:80]}]({s.url}) — {s.relevance}")
            
            # Log
            st.markdown("---")
            with st.expander("Agent Log", expanded=True):
                for log in logs:
                    st.text(f"[{log.speaker}] {log.message}")
