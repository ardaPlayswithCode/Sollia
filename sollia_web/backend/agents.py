"""Agent registry — pluggable, extensible for future agents."""
from typing import Dict, List, Optional
from dataclasses import dataclass
from pydantic import BaseModel
from typing import Any

# Import agents
import sys
sys.path.insert(0, '/Users/ardaturgut/Desktop/Sollia/sollia_working')
from src.agents.corporate_discrepancy import run_discrepancy_agent as run_corporate_discrepancy
from src.agents.forensic_accountant import ForensicAccountant

from src.agents.follow_through import FollowThroughAgent as PromiseAuditAgent
from src.schemas import AgentLogMessage

@dataclass
class AgentDefinition:
    id: str
    name: str
    description: str
    runner: object
    weight: float = 1.0

# Registry
AGENTS: Dict[str, AgentDefinition] = {
    "corporate_discrepancy": AgentDefinition(
        id="corporate_discrepancy",
        name="Corporate Discrepancy",
        description="Cross-document contradiction finder across 5 SEC filings",
        runner=ForensicAccountant(),
        weight=1.0,
    ),
    "promise_audit": AgentDefinition(
        id="promise_audit",
        name="Promise Audit",
        description="Tracks management promises and verifies follow-through",
        runner=FollowThroughAgent(),
        weight=0.5,  # Half weight: 1/(2N) where N=2 → 0.25 normalized to 0.33
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
):
    """Run selected agents and return results."""
    forensic = None
    narrative = None
    ft_result = None
    all_logs: List[AgentLogMessage] = []

    if "forensic" in selected:
        agent = AGENTS["forensic"]
        result = agent.runner.run(ticker)
        if getattr(result, 'quarterly_pairs', None) and len(result.quarterly_pairs) > 0:
            forensic = result
        all_logs.extend(getattr(result, 'log_messages', []))

    if "narrative" in selected:
        agent = AGENTS["narrative"]
        hist_start = getattr(forensic, 'historical_start', "2020-01-01") if forensic else "2020-01-01"
        hist_end = getattr(forensic, 'historical_end', "2026-01-01") if forensic else "2026-01-01"
        result = agent.runner.run(ticker, hist_start, hist_end)
        narrative = result
        all_logs.extend(getattr(result, 'log_messages', []))

    if "follow_through" in selected:
        agent = AGENTS["follow_through"]
        raw_result = agent.runner.run(ticker)
        
        if isinstance(raw_result, dict):
            # Add conclusion for frontend
            if "conclusion" not in raw_result or not raw_result["conclusion"]:
                score = raw_result.get("score", 50)
                if score >= 75:
                    conclusion = "Strong follow-through on key strategic promises."
                elif score >= 55:
                    conclusion = "Mixed execution. Some important promises delivered, others remain unproven."
                else:
                    conclusion = "Concerning gaps in management follow-through detected."
                raw_result["conclusion"] = conclusion
            
            try:
                ft_result = SolliaFollowThroughResult(**raw_result)
            except Exception as e:
                print(f"Schema validation warning: {e}")
                ft_result = raw_result  # fallback
        
        all_logs.append(AgentLogMessage(
            speaker="Follow-Through", 
            message=f"Analysis complete. Score: {raw_result.get('score', 50):.1f}%"
        ))

    return forensic, narrative, all_logs, ft_result
