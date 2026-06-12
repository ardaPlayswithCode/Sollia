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
):
    """Run selected agents and return results with weights."""
    results = {}
    all_logs: List[AgentLogMessage] = []
    weights = {}

    if "corporate_discrepancy" in selected:
        agent = AGENTS["corporate_discrepancy"]
        result = agent.runner(ticker, year)
        if "error" not in result:
            results["corporate_discrepancy"] = result
            weights["corporate_discrepancy"] = agent.weight
            for log_entry in result.get("agent_log", []):
                all_logs.append(AgentLogMessage(
                    speaker="Corporate Discrepancy",
                    message=log_entry.get("detail", "")
                ))

    if "promise_audit" in selected:
        agent = AGENTS["promise_audit"]
        result = agent.runner.run(ticker)
        if "error" not in result:
            results["promise_audit"] = result
            weights["promise_audit"] = agent.weight
            all_logs.append(AgentLogMessage(
                speaker="Promise Audit",
                message=f"Analysis complete. Score: {result.get('score', 50):.1f}%"
            ))

    if "legal_proceedings" in selected:
        agent = AGENTS["legal_proceedings"]
        result = agent.runner(ticker)
        if "error" not in result:
            results["legal_proceedings"] = result
            weights["legal_proceedings"] = agent.weight
            agent3_score = result.get("agent3_score", 50)
            all_logs.append(AgentLogMessage(
                speaker="Legal Proceedings",
                message=f"Legal analysis complete. Score: {agent3_score:.1f}%"
            ))

    return results, weights, all_logs

# Import shared LLM config for agents to use
import sys
sys.path.insert(0, '/Users/ardaturgut/Desktop/Desktop - Arda\u2019s Mac Studio/Sollia/sollia_working/src')
from config.llm_config import LLM_PROVIDERS, call_llm, get_primary_model
