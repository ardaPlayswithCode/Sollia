import streamlit as st
import json

st.set_page_config(page_title="Sollia", layout="centered")

# Dark background
st.markdown("""
<style>
body { background: #0F1117; }
.stApp { background: #0F1117; }
</style>
""", unsafe_allow_html=True)

st.markdown("<h1 style='text-align:center;color:#F8FAFC;'>Sollia</h1>", unsafe_allow_html=True)

try:
    with open('/tmp/sollia_result.json') as f:
        data = json.load(f)
    
    score = int(round(data['score']))
    signal = data['signal']
    color = data['color']
    
    st.markdown(f"<h1 style='font-size:100px;color:{color};text-align:center;margin:0;'>{score}%</h1>", unsafe_allow_html=True)
    st.markdown(f"<h2 style='text-align:center;color:{color};margin-top:0;'>{signal}</h2>", unsafe_allow_html=True)
    st.markdown(f"<p style='text-align:center;color:#94A3B8;'>NVIDIA CORP</p>", unsafe_allow_html=True)
    st.markdown(f"<p style='text-align:center;color:#94A3B8;font-size:14px;max-width:600px;margin:auto;'>{data['summary']}</p>", unsafe_allow_html=True)
except FileNotFoundError:
    st.markdown("<p style='text-align:center;color:#94A3B8;'>No analysis yet. Run pipeline first.</p>", unsafe_allow_html=True)
