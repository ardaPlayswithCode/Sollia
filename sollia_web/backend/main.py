"""FastAPI backend — serves the agent pipeline with live streaming (WS5) and
parallel execution (WS6)."""
import sys
import os
import json
import asyncio
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOLLIA_WEB = os.path.dirname(_HERE)
sys.path.insert(0, '/Users/ardaturgut/Desktop/Desktop - Arda’s Mac Studio/Sollia/sollia_working')
sys.path.insert(0, _SOLLIA_WEB)  # ensure the 'backend' package resolves

from backend import runs
from backend.agents import get_agents_meta, AGENTS

app = FastAPI(title="Sollia API", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalysisRequest(BaseModel):
    ticker: str
    agents: list[str] = ["corporate_discrepancy", "promise_audit"]
    year: int = 2023


def _parse_request(req: AnalysisRequest):
    return req.ticker.upper(), [a for a in req.agents if a in AGENTS]


@app.get("/api/agents")
async def list_agents():
    return {"agents": get_agents_meta()}


@app.post("/api/analyze")
async def run_analysis(req: AnalysisRequest):
    """Blocking analysis. Runs the pipeline in a worker thread (so the event loop
    stays free for concurrent streams) and returns the rich UI result plus the
    clean export. Kept for backward compatibility / non-streaming clients."""
    ticker, selected = _parse_request(req)
    run_id = runs.new_run(ticker, selected, req.year)
    rich, export = await asyncio.to_thread(runs.run_pipeline, run_id, ticker, selected, req.year)
    out = dict(rich)
    out["export"] = export
    out["run_id"] = run_id
    return out


@app.post("/api/analyze/start")
async def start_analysis(req: AnalysisRequest):
    """Start an analysis in the background and return a run_id to stream from."""
    ticker, selected = _parse_request(req)
    run_id = runs.new_run(ticker, selected, req.year)
    threading.Thread(
        target=runs.run_pipeline,
        args=(run_id, ticker, selected, req.year),
        daemon=True,
    ).start()
    return {"run_id": run_id, "ticker": ticker, "year": req.year, "agents": selected}


@app.get("/api/analyze/stream/{run_id}")
async def stream_analysis(run_id: str):
    """Server-sent events: live per-agent milestones, then a final 'complete'
    event carrying the rich result + clean export."""
    async def event_gen():
        since = 0
        ticks = 0
        yield "retry: 2000\n\n"
        while True:
            snap = runs.snapshot_since(run_id, since)
            if snap is None:
                yield "data: %s\n\n" % json.dumps({"type": "error", "message": "Unknown run id"})
                return
            since = snap["max_seq"]
            yield "data: %s\n\n" % json.dumps({"type": "progress", "agents": snap["agents"]})
            if snap["status"] == "complete":
                yield "data: %s\n\n" % json.dumps({
                    "type": "complete", "result": snap.get("result"), "export": snap.get("export")})
                return
            if snap["status"] == "error":
                yield "data: %s\n\n" % json.dumps({
                    "type": "error", "message": snap.get("error"),
                    "result": snap.get("result"), "export": snap.get("export")})
                return
            ticks += 1
            if ticks > 2400:  # ~12 min safety cap
                yield "data: %s\n\n" % json.dumps({"type": "error", "message": "Stream timed out"})
                return
            await asyncio.sleep(0.3)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """Polling fallback — full current state of a run (all log lines)."""
    snap = runs.snapshot_since(run_id, 0)
    if snap is None:
        return {"status": "error", "error": "Unknown run id"}
    return snap


frontend_dist = os.path.join(_HERE, '..', 'frontend', 'dist')
if os.path.exists(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
