import streamlit as st
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

st.set_page_config(page_title="Sollia Test", layout="centered")
st.title("Sollia — Pipeline Test")

if st.button("Run OKLO Analysis"):
    with st.spinner("Running full pipeline for OKLO..."):
        from src.pipeline.orchestrator import run_analysis
        from src.schemas import SolliaAnalysisResult
        
        for event in run_analysis("OKLO", run_forensic=True, run_narrative=True):
            if isinstance(event, SolliaAnalysisResult):
                score = int(round(event.s_final))
                signal = event.signal
                color = event.signal_color
                st.markdown(f"<h1 style='font-size:80px;color:{color};text-align:center;'>{score}%</h1>", unsafe_allow_html=True)
                st.markdown(f"<h2 style='text-align:center;color:{color};'>{signal}</h2>", unsafe_allow_html=True)
                st.markdown(f"<p style='text-align:center;color:#94A3B8;'>{event.executive_summary}</p>", unsafe_allow_html=True)
            elif hasattr(event, 'message'):
                st.text(f"[{event.speaker}] {event.message}")
