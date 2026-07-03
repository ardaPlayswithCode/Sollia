"""FastAPI backend — serves the agent pipeline with live streaming (WS5) and
parallel execution (WS6).

Hardened 2026-07-01 (final bulletproofing pass):
  * No year selector — Agent 1 analyzes a rolling 5-year window (Part 2).
    `year` is still accepted in the request body for old clients but ignored.
  * Per-IP rate limiting (10 analysis requests/min), max 2 concurrent runs per
    IP, and a daily run cap (20/day/IP) — Part 3c/6a financial-abuse guards.
  * Request bodies over 10 KB are rejected (413) before JSON parsing.
  * Tickers are validated against the SEC's company_tickers.json (cached 24 h)
    BEFORE any fetch, so junk tickers can't burn the SEC fetch budget.
"""
import sys
import os
import re
import json
import time
import asyncio
import threading
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOLLIA_WEB = os.path.dirname(_HERE)
sys.path.insert(0, '/Users/ardaturgut/Desktop/Desktop - Arda’s Mac Studio/Sollia/sollia_working')
sys.path.insert(0, _SOLLIA_WEB)  # ensure the 'backend' package resolves

from backend import runs
from backend.agents import get_agents_meta, AGENTS

app = FastAPI(title="Sollia API", version="2.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev default — restrict to the app origin in production
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_BODY_BYTES = int(os.environ.get("SOLLIA_MAX_BODY_BYTES", str(10 * 1024)))          # 10 KB
RATE_LIMIT_PER_MIN = int(os.environ.get("SOLLIA_RATE_LIMIT_PER_MIN", "10"))            # analysis starts
MAX_CONCURRENT_RUNS_PER_IP = int(os.environ.get("SOLLIA_MAX_CONCURRENT_RUNS_PER_IP", "2"))
MAX_RUNS_PER_DAY_PER_IP = int(os.environ.get("SOLLIA_MAX_RUNS_PER_DAY_PER_IP", "20"))


# ── abuse-guard state (in-memory; keyed by client IP — swap the key for the
#    API key once authenticated accounts exist) ──────────────────────────────
_guard_lock = threading.Lock()
_req_times: dict = {}       # ip -> [timestamps of analysis requests, last 60s]
_daily_runs: dict = {}      # ip -> [timestamps of started runs, last 24h]
_active_runs: dict = {}     # ip -> currently running pipeline count


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "unknown")


def _check_rate_limits(ip: str) -> None:
    """Sliding-window per-IP limits. Raises HTTPException 429 when exceeded."""
    now = time.time()
    with _guard_lock:
        window = [t for t in _req_times.get(ip, []) if now - t < 60.0]
        if len(window) >= RATE_LIMIT_PER_MIN:
            raise HTTPException(status_code=429, detail=(
                "Rate limit exceeded: max %d analysis requests per minute." % RATE_LIMIT_PER_MIN))
        window.append(now)
        _req_times[ip] = window

        day = [t for t in _daily_runs.get(ip, []) if now - t < 86400.0]
        if len(day) >= MAX_RUNS_PER_DAY_PER_IP:
            raise HTTPException(status_code=429, detail=(
                "Daily run limit exceeded: max %d analysis runs per day." % MAX_RUNS_PER_DAY_PER_IP))

        if _active_runs.get(ip, 0) >= MAX_CONCURRENT_RUNS_PER_IP:
            raise HTTPException(status_code=429, detail=(
                "Too many concurrent runs: max %d at a time. Wait for a run to "
                "finish." % MAX_CONCURRENT_RUNS_PER_IP))
        day.append(now)
        _daily_runs[ip] = day
        _active_runs[ip] = _active_runs.get(ip, 0) + 1


def _release_run_slot(ip: str) -> None:
    with _guard_lock:
        _active_runs[ip] = max(0, _active_runs.get(ip, 0) - 1)


@app.middleware("http")
async def _body_size_limit(request: Request, call_next):
    """Reject oversized POST bodies before they reach JSON parsing (Part 6a:
    cost-amplification guard — a 500 KB 'ticker' never gets parsed)."""
    if request.method == "POST":
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > MAX_BODY_BYTES:
                    return JSONResponse(status_code=413, content={
                        "detail": "Request body too large (max %d bytes)." % MAX_BODY_BYTES})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Bad Content-Length."})
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={
                "detail": "Request body too large (max %d bytes)." % MAX_BODY_BYTES})
    return await call_next(request)


class AnalysisRequest(BaseModel):
    ticker: str
    agents: list[str] = ["corporate_discrepancy", "promise_audit"]
    # DEPRECATED (Part 2): kept so old clients don't 422; the pipeline ignores
    # it — Agent 1 analyzes the rolling 5-year window automatically.
    year: int | None = None


# Input validation (Part 8c + Part 3c). Everything the pipeline touches (SEC
# URLs, cache paths, LLM prompts) receives the ticker, so it is validated ONCE
# here: shape first (cheap), then EXISTENCE against the SEC's official ticker
# directory — junk tickers are rejected before a single SEC fetch is spent.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_TICKER_DIR_CACHE = os.path.join(
    '/Users/ardaturgut/Desktop/Desktop - Arda’s Mac Studio/Sollia/sollia_working',
    '.cache', 'company_tickers.json')
_TICKER_DIR_TTL = 24 * 3600.0
_ticker_dir_lock = threading.Lock()
_valid_tickers: set = set()
_ticker_dir_loaded_at = 0.0


def _load_valid_tickers() -> set:
    """The SEC ticker directory, cached to disk for 24 h. On any failure the
    previous in-memory set is kept; if nothing was ever loaded, validation
    degrades to shape-only (never blocks operation on SEC downtime)."""
    global _valid_tickers, _ticker_dir_loaded_at
    with _ticker_dir_lock:
        if _valid_tickers and time.time() - _ticker_dir_loaded_at < _TICKER_DIR_TTL:
            return _valid_tickers
        data = None
        try:
            st = os.stat(_TICKER_DIR_CACHE)
            if time.time() - st.st_mtime < _TICKER_DIR_TTL:
                with open(_TICKER_DIR_CACHE) as f:
                    data = json.load(f)
        except Exception:
            data = None
        if data is None:
            try:
                import requests as _rq
                r = _rq.get("https://www.sec.gov/files/company_tickers.json",
                            headers={"User-Agent": "Sollia contact@sollia.app"}, timeout=15)
                r.raise_for_status()
                data = r.json()
                os.makedirs(os.path.dirname(_TICKER_DIR_CACHE), exist_ok=True)
                with open(_TICKER_DIR_CACHE, "w") as f:
                    json.dump(data, f)
            except Exception:
                data = None
        if data:
            _valid_tickers = {str(v.get("ticker", "")).upper() for v in data.values()}
            _ticker_dir_loaded_at = time.time()
        return _valid_tickers


def _parse_request(req: AnalysisRequest):
    ticker = (req.ticker or "").strip().upper()
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=422, detail=(
            "Invalid ticker: expected 1-10 characters (letters, digits, '.', '-'), "
            "starting with a letter."))
    valid = _load_valid_tickers()
    if valid:
        # SEC directory stores class shares with '-' (BRK-B); users type either.
        if ticker not in valid and ticker.replace(".", "-") not in valid:
            raise HTTPException(status_code=422, detail=(
                "Unknown ticker %r: not in the SEC's company directory." % ticker))
    selected = [a for a in req.agents if a in AGENTS]
    if not selected:
        raise HTTPException(status_code=422, detail=(
            "No valid agents selected. Valid agents: %s" % ", ".join(AGENTS)))
    return ticker, selected


@app.get("/api/agents")
async def list_agents():
    return {"agents": get_agents_meta()}


@app.post("/api/analyze")
async def run_analysis(req: AnalysisRequest, request: Request):
    """Blocking analysis. Runs the pipeline in a worker thread (so the event loop
    stays free for concurrent streams) and returns the rich UI result plus the
    clean export. Kept for backward compatibility / non-streaming clients."""
    ticker, selected = _parse_request(req)
    ip = _client_ip(request)
    _check_rate_limits(ip)
    try:
        run_id = runs.new_run(ticker, selected)
        rich, export = await asyncio.to_thread(runs.run_pipeline, run_id, ticker, selected)
    finally:
        _release_run_slot(ip)
    out = dict(rich)
    out["export"] = export
    out["run_id"] = run_id
    return out


@app.post("/api/analyze/start")
async def start_analysis(req: AnalysisRequest, request: Request):
    """Start an analysis in the background and return a run_id to stream from."""
    ticker, selected = _parse_request(req)
    ip = _client_ip(request)
    _check_rate_limits(ip)

    run_id = runs.new_run(ticker, selected)

    def _run_and_release():
        try:
            runs.run_pipeline(run_id, ticker, selected)
        finally:
            _release_run_slot(ip)

    threading.Thread(target=_run_and_release, daemon=True).start()
    return {"run_id": run_id, "ticker": ticker, "agents": selected,
            "window": "last 5 fiscal years + current partial year"}


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
            _progress = {"type": "progress", "agents": snap["agents"]}
            if snap.get("new_results"):
                # per-agent results stream LIVE as each agent finishes (Part 7d)
                _progress["new_results"] = snap["new_results"]
            yield "data: %s\n\n" % json.dumps(_progress)
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
            if ticks > 6000:  # ~30 min safety cap (5-year cold window runs long)
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
