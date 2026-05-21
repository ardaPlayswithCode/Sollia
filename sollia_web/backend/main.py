"""FastAPI backend — serves agent pipeline."""
import sys
import os
import json
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, '/Users/ardaturgut/Desktop/SMALL SOLLIA TEST FOLDER ,LAYER BY LAYER/sollia_working')
from src.agents.synthesizer import Synthesizer
from backend.agents import get_agents_meta, run_selected_agents, AGENTS
import backend.agents as _agents

app = FastAPI(title="Sollia API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalysisRequest(BaseModel):
    ticker: str
    agents: list[str] = ["forensic", "narrative"]


@app.get("/api/agents")
async def list_agents():
    return {"agents": get_agents_meta()}


@app.post("/api/analyze")
async def run_analysis(req: AnalysisRequest):
    ticker = req.ticker.upper()
    selected = [a for a in req.agents if a in AGENTS]

    forensic, narrative, logs = run_selected_agents(ticker, selected)

    # Determine company name
    company_name = ticker
    if forensic:
        company_name = getattr(forensic, 'ceo_name', None) or ticker
        if company_name == ticker and hasattr(forensic, 'company_name'):
            company_name = forensic.company_name or ticker

    # Synthesize
    synth = Synthesizer()
    result = synth.run(forensic, narrative, ticker, company_name)
    normalized_score = round((result.s_final - 20.0) / 60.0 * 100.0, 1)

    # Merge all log messages: forensic -> narrative -> synthesizer
    all_logs = []
    if forensic:
        all_logs.extend(forensic.log_messages)
    if narrative:
        all_logs.extend(narrative.log_messages)
    all_logs.extend(result.log_messages)

    return {
        "ticker": result.ticker,
        "company": result.company_name,
        "score": normalized_score,
        "signal": result.signal,
        "signal_color": result.signal_color,
        "summary": result.executive_summary,
        "agents_run": result.agents_run,
        "track1_score": result.track1_score,
        "corroboration": result.corroboration,
        "forensic": forensic.model_dump() if forensic else None,
        "narrative": narrative.model_dump() if narrative else None,
        "follow_through": ft_result if ft_result else None,
        "log": [{"speaker": l.speaker, "message": l.message} for l in all_logs],
    }


# Serve React build in production
frontend_dist = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'dist')
if os.path.exists(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
