"""Agent registry — pluggable, extensible for future agents."""
from typing import Dict, List, Optional
from dataclasses import dataclass

# Import your existing agents
import sys
sys.path.insert(0, '/Users/ardaturgut/Desktop/SMALL SOLLIA TEST FOLDER ,LAYER BY LAYER/sollia_working')
from src.agents.forensic_accountant import ForensicAccountant
from src.agents.narrative_scout import NarrativeScout
from src.agents.follow_through import FollowThroughAgent
from src.schemas import SolliaForensicResult, SolliaNarrativeResult, AgentLogMessage


@dataclass
class AgentDefinition:
    id: str
    name: str
    description: str
    runner: object  # Agent instance with .run() method


# Registry — add new agents here, frontend auto-discovers them
AGENTS: Dict[str, AgentDefinition] = {
    "forensic": AgentDefinition(
        id="forensic",
        name="CEO vs 10-K",
        description="Measures gaps between CEO transcript tone and 10-K financial reality",
        runner=ForensicAccountant(),
    ),
    "narrative": AgentDefinition(
        id="narrative",
        name="News Sentiment",
        description="Analyzes financial news flow against historical sentiment baselines",
        runner=NarrativeScout(),
    ),
    # Future agents:
    # "supplier": AgentDefinition(
    #     id="supplier",
    #     name="Supply Chain Monitor",
    #     description="Tracks supplier 10-K mentions of target ticker",
    #     runner=SupplyChainAgent(),
    # ),
    "follow_through": AgentDefinition(
        id="follow_through",
        name="Management Follow-Through",
        description="Tracks CEO promises across filings and checks if they were kept",
        runner=FollowThroughAgent(),
    ),
}


def get_agents_meta() -> List[dict]:
    """Return agent metadata for frontend (no runner instances)."""
    return [
        {"id": a.id, "name": a.name, "description": a.description}
        for a in AGENTS.values()
    ]


def run_selected_agents(
    ticker: str,
    selected: List[str],
) -> tuple[Optional[SolliaForensicResult], Optional[SolliaNarrativeResult], List[AgentLogMessage]]:
    """Run only selected agents, collect results and logs."""
    forensic = None
    narrative = None
    all_logs: List[AgentLogMessage] = []

    if "forensic" in selected:
        agent = AGENTS["forensic"]
        result = agent.runner.run(ticker)
        # Only use forensic result if it has actual data
        if result.quarterly_pairs and len(result.quarterly_pairs) > 0:
            forensic = result
        all_logs.extend(result.log_messages)

    if "narrative" in selected:
        agent = AGENTS["narrative"]
        hist_start = forensic.historical_start if forensic else "2020-01-01"
        hist_end = forensic.historical_end if forensic else "2026-01-01"
        result = agent.runner.run(ticker, hist_start, hist_end)
        narrative = result
        all_logs.extend(result.log_messages)

    if "follow_through" in selected:
        agent = AGENTS["follow_through"]
        result = agent.runner.run(ticker)
        all_logs.append(AgentLogMessage(speaker="Follow-Through", message=f"Analysis complete. Score: {result.get('score', 50):.1f}%"))
        # Store follow_through result - we'll handle it in main.py
        if not hasattr(run_selected_agents, '_ft_result'):
            run_selected_agents._ft_result = None
        run_selected_agents._ft_result = result

    return forensic, narrative, all_logs
