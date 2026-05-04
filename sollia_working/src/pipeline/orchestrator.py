"""Pipeline orchestrator — runs selected agents and yields log events + final result."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Generator, Optional, Union

from src.schemas import AgentLogMessage, SolliaAnalysisResult, SolliaForensicResult, SolliaNarrativeResult

logger = logging.getLogger(__name__)


def run_analysis(
    ticker: str,
    run_forensic: bool = True,
    run_narrative: bool = True,
) -> Generator[Union[AgentLogMessage, SolliaAnalysisResult], None, None]:
    """
    Generator that yields AgentLogMessage events during analysis,
    then yields one SolliaAnalysisResult as the final item.
    """
    from src.providers.filings.sec_edgar import SECEdgarProvider
    from src.agents.forensic_accountant import ForensicAccountant
    from src.agents.narrative_scout import NarrativeScout
    from src.agents.synthesizer import Synthesizer

    ticker = ticker.upper()

    def _emit(msg: str, speaker: str = "Sollia") -> AgentLogMessage:
        m = AgentLogMessage(speaker=speaker, message=msg)
        logger.info("[ORCH] %s", msg)
        return m

    edgar = SECEdgarProvider()
    cik = edgar.get_cik(ticker)
    if not cik:
        yield _emit(f"Ticker '{ticker}' not found in SEC EDGAR.")
        yield SolliaAnalysisResult(
            ticker=ticker,
            company_name=ticker,
            s_final=50.0,
            signal="HOLD",
            signal_color="#F59E0B",
            executive_summary=f"Could not find SEC filings for {ticker}. Please verify the ticker symbol.",
        )
        return

    company_name = edgar.get_company_name(ticker)
    yield _emit(f"Analysis started for {ticker} ({company_name})")

    forensic: Optional[SolliaForensicResult] = None
    narrative: Optional[SolliaNarrativeResult] = None

    # Forensic Accountant
    if run_forensic:
        yield _emit("Running CEO vs 10-K Discrepancy...")
        try:
            fa = ForensicAccountant()
            forensic = fa.run(ticker)
            for msg in forensic.log_messages:
                yield msg
            yield _emit(f"Forensic Accountant complete. Score signal: {forensic.s1:.1f}")
        except Exception as exc:
            yield _emit(f"Forensic Accountant error: {exc}")

    # Narrative Scout — inherit historical dates from forensic if available
    if run_narrative:
        yield _emit("Running News Sentiment...")
        if forensic and forensic.historical_start:
            hist_start = forensic.historical_start
            hist_end = forensic.historical_end
        else:
            hist_end = datetime.now().strftime("%Y-%m-%d")
            hist_start = (datetime.now() - timedelta(days=6 * 365)).strftime("%Y-%m-%d")
        try:
            ns = NarrativeScout()
            narrative = ns.run(ticker, hist_start, hist_end)
            for msg in narrative.log_messages:
                yield msg
            yield _emit(f"Narrative Scout complete. Score signal: {narrative.s2:.1f}")
        except Exception as exc:
            yield _emit(f"Narrative Scout error: {exc}")

    # Synthesizer
    yield _emit("Running Synthesizer...")
    try:
        synth = Synthesizer()
        result = synth.run(forensic, narrative, ticker, company_name)
        for msg in result.log_messages:
            yield msg
        yield _emit(f"Synthesis complete. Final score={result.s_final:.1f} ({result.signal})")
        yield result
    except Exception as exc:
        yield _emit(f"Synthesizer error: {exc}")
        yield SolliaAnalysisResult(
            ticker=ticker,
            company_name=company_name,
            s_final=50.0,
            signal="HOLD",
            signal_color="#F59E0B",
            forensic=forensic,
            narrative=narrative,
            executive_summary=f"Synthesis failed: {exc}",
        )
