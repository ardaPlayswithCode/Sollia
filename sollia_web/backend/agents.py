"""Agent registry — pluggable, extensible for future agents."""
from typing import Dict, List, Optional
from dataclasses import dataclass
from pydantic import BaseModel
from typing import Any

# Import agents
import sys
sys.path.insert(0, '/Users/ardaturgut/Desktop/Desktop - Arda\u2019s Mac Studio/Sollia/sollia_working')
from src.agents.corporate_discrepancy import run_discrepancy_agent as run_corporate_discrepancy
from src.agents.promise_audit import PromiseAuditAgent
from src.agents.legal_proceedings import track_legal
from src.agents.risk_factors import RiskFactorAgent
from src.agents.constraint_auditor import ConstraintAuditor
from src.agents.moat_auditor import MoatAuditor
from src.schemas import AgentLogMessage

@dataclass
class AgentDefinition:
    id: str
    name: str
    description: str
    runner: object
    weight: float = 1.0

# Registry — all 3 MVP agents
AGENTS: Dict[str, AgentDefinition] = {
    "corporate_discrepancy": AgentDefinition(
        id="corporate_discrepancy",
        name="Corporate Discrepancy",
        description="Cross-document contradiction finder across 5 SEC filings",
        runner=run_corporate_discrepancy,
        weight=1.0,
    ),
    "promise_audit": AgentDefinition(
        id="promise_audit",
        name="Promise Audit",
        description="Tracks management promises and verifies follow-through",
        runner=PromiseAuditAgent(),
        weight=0.5,
    ),
    "legal_proceedings": AgentDefinition(
        id="legal_proceedings",
        name="Legal Proceedings",
        description="Tracks legal proceeding changes across 10-K, 10-Q, and 8-K filings",
        runner=track_legal,
        weight=0.5,
    ),
    "risk_factors": AgentDefinition(
        id="risk_factors",
        name="Risk Factors",
        description="Tracks changes in Item 1A Risk Factors across 10-K filings",
        runner=RiskFactorAgent(),
        weight=0.5,
    ),
    "constraint_auditor": AgentDefinition(
        id="constraint_auditor",
        name="Constraint Auditor",
        description="Audits whether a sector's projected growth is physically possible "
                    "given raw-material supply constraints",
        # Audit, not a bull/bear score — weight 0 so it never moves the composite (PART 6b).
        runner=ConstraintAuditor(),
        weight=0.0,
    ),
    "moat_auditor": AgentDefinition(
        id="moat_auditor",
        name="Moat Auditor",
        description="Pressure-tests a company's claimed competitive moats against "
                    "competitor SEC filings, citing every claim and challenge",
        # Audit, not a bull/bear score — weight 0 so it never moves the composite (PART 6b).
        runner=MoatAuditor(),
        weight=0.0,
    ),
}

def get_agents_meta() -> List[dict]:
    return [
        {"id": a.id, "name": a.name, "description": a.description}
        for a in AGENTS.values()
    ]

def run_selected_agents(
    ticker: str,
    selected: List[str],
    year: int = 2023,
    emit=None,
):
    """Run selected agents CONCURRENTLY and return (results, weights, logs).

    WS6: the agents are independent, so the I/O-bound work in Promise Audit and
    Legal Proceedings overlaps Corporate Discrepancy's embedding/compute instead of
    waiting for it — wall-clock time drops to roughly max(agent) instead of the sum.
    emit(agent_id, message, kind) is an optional live-progress callback (WS5)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _progress_for(agent_id):
        if emit is None:
            return None
        return lambda message, kind="info": emit(agent_id, message, kind)

    runners = {
        "corporate_discrepancy": lambda: AGENTS["corporate_discrepancy"].runner(
            ticker, year, progress=_progress_for("corporate_discrepancy")),
        "promise_audit": lambda: AGENTS["promise_audit"].runner.run(
            ticker, progress=_progress_for("promise_audit")),
        "legal_proceedings": lambda: AGENTS["legal_proceedings"].runner(
            ticker, progress=_progress_for("legal_proceedings")),
        "risk_factors": lambda: AGENTS["risk_factors"].runner.run(
            ticker, progress=_progress_for("risk_factors")),
        "constraint_auditor": lambda: AGENTS["constraint_auditor"].runner.run(
            ticker, progress=_progress_for("constraint_auditor"), year=year),
        "moat_auditor": lambda: AGENTS["moat_auditor"].runner.run(
            ticker, progress=_progress_for("moat_auditor"), year=year),
    }
    to_run = {aid: fn for aid, fn in runners.items() if aid in selected and aid in AGENTS}

    raw: Dict[str, dict] = {}
    if to_run:
        with ThreadPoolExecutor(max_workers=len(to_run)) as ex:
            futures = {ex.submit(fn): aid for aid, fn in to_run.items()}
            for fut in as_completed(futures):
                aid = futures[fut]
                try:
                    raw[aid] = fut.result()
                except Exception as e:
                    raw[aid] = {"error": str(e)}
                    if emit:
                        emit(aid, "Run failed: %s" % str(e)[:160], "flag")

    results: Dict[str, dict] = {}
    weights: Dict[str, float] = {}
    all_logs: List[AgentLogMessage] = []
    # Deterministic ordering regardless of which agent finished first.
    for aid in ["corporate_discrepancy", "promise_audit", "legal_proceedings", "risk_factors",
                "constraint_auditor", "moat_auditor"]:
        result = raw.get(aid)
        if not result:
            continue
        agent = AGENTS[aid]
        if "error" in result:
            if emit:
                emit(aid, "Could not complete: %s" % str(result.get("error"))[:160], "flag")
            continue
        results[aid] = result
        weights[aid] = agent.weight
        if aid == "corporate_discrepancy":
            for log_entry in result.get("agent_log", []):
                all_logs.append(AgentLogMessage(speaker="Corporate Discrepancy", message=log_entry.get("detail", "")))
        elif aid == "promise_audit":
            all_logs.append(AgentLogMessage(speaker="Promise Audit", message=f"Analysis complete. Score: {result.get('score', 50):.1f}%"))
        elif aid == "legal_proceedings":
            all_logs.append(AgentLogMessage(speaker="Legal Proceedings", message=f"Legal analysis complete. Score: {result.get('agent3_score', 50):.1f}%"))
        elif aid == "risk_factors":
            all_logs.append(AgentLogMessage(speaker="Risk Factors", message=f"Risk factor delta complete. Score: {result.get('score', 50):.1f}%"))
        elif aid == "constraint_auditor":
            if result.get("status") == "unsupported":
                all_logs.append(AgentLogMessage(speaker="Constraint Auditor", message="Sector not in the constraint library — audit skipped."))
            else:
                _n = sum(1 for c in result.get("constraints", []) if c.get("status") == "success")
                all_logs.append(AgentLogMessage(speaker="Constraint Auditor", message=f"Constraint audit complete ({result.get('sector','?')}): {_n} material constraint(s) evaluated."))
        elif aid == "moat_auditor":
            _m = result.get("moats_audited", []) or []
            _dt = sum(1 for x in _m if x.get("verdict") == "DIRECT THREAT")
            all_logs.append(AgentLogMessage(speaker="Moat Auditor", message=f"Moat audit complete: {len(_m)} moat(s) audited across {len(result.get('competitors_analyzed', []))} competitors, {_dt} direct threat(s)."))

    return results, weights, all_logs

# Import shared LLM config for agents to use
import sys
sys.path.insert(0, '/Users/ardaturgut/Desktop/Desktop - Arda\u2019s Mac Studio/Sollia/sollia_working/src')
from config.llm_config import LLM_PROVIDERS, call_llm, get_primary_model
