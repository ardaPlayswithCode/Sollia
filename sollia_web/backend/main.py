"""FastAPI backend — serves agent pipeline."""
import sys
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, '/Users/ardaturgut/Desktop/Desktop - Arda\u2019s Mac Studio/Sollia/sollia_working')
from src.agents.synthesizer import Synthesizer
from backend.agents import get_agents_meta, run_selected_agents, AGENTS

app = FastAPI(title="Sollia API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174", "http://localhost:5175", "http://localhost:5176", "http://localhost:5177", "http://localhost:5178"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnalysisRequest(BaseModel):
    ticker: str
    agents: list[str] = ["corporate_discrepancy", "promise_audit"]
    year: int = 2023

@app.get("/api/agents")
async def list_agents():
    return {"agents": get_agents_meta()}

@app.post("/api/analyze")
async def run_analysis(req: AnalysisRequest):
    ticker = req.ticker.upper()
    selected = [a for a in req.agents if a in AGENTS]
    
    results, weights, logs = run_selected_agents(ticker, selected, req.year)

    if not results:
        return {
            "ticker": ticker, "company": ticker,
            "score": 50, "signal": "HOLD", "signal_color": "#F59E0B",
            "summary": "No agents produced results.",
            "agents_run": [], "log": []
        }

    synth = Synthesizer()
    merged = synth.run(results, weights, ticker, ticker)

    # Strip embedding arrays from response to avoid bloat
    def strip_embeddings(obj):
        if isinstance(obj, dict):
            return {k: strip_embeddings(v) for k, v in obj.items() if k != "embedding"}
        elif isinstance(obj, list):
            return [strip_embeddings(i) for i in obj]
        return obj
    
    return {
        "ticker": ticker,
        "company": ticker,
        "score": merged["score"],
        "signal": merged["signal"],
        "signal_color": merged["signal_color"],
        "summary": merged["summary"],
        "agents_run": merged["agents_run"],
        "similarity": merged.get("similarity", 0),
        "agent_weights": merged.get("agent_weights", {}),
        "agent_scores": merged.get("agent_scores", {}),
        "corporate_discrepancy": strip_embeddings(results.get("corporate_discrepancy")),
        "promise_audit": strip_embeddings(results.get("promise_audit")),
        "legal_proceedings": strip_embeddings(results.get("legal_proceedings")),
        "log": [{"speaker": "Sollia", "message": l} for l in merged.get("log", [])],
    }

frontend_dist = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'dist')
if os.path.exists(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
