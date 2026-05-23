"""Synthesizer — two-track combination of Forensic Accountant and Narrative Scout."""
from __future__ import annotations
import os

import math
import re
import logging
from typing import List, Optional, Tuple

import requests

from src.schemas import (
    AgentLogMessage,
    SolliaAnalysisResult,
    SolliaForensicResult,
    SolliaNarrativeResult,
)

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"


def _add_log(msgs: List[AgentLogMessage], text: str) -> None:
    msgs.append(AgentLogMessage(speaker="Final Analysis", message=text))
    logger.info("[SYN] %s", text)


def _ask_ollama(prompt: str) -> Optional[str]:
    try:
        api_key = os.environ.get("GROQ_API_KEY", "")
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": prompt}], "temperature": 0.0},
            timeout=25,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        pass
    return None


def _rate_dimensions(sentence: str) -> List[float]:
    """Rate sentence on 2 dimensions (Optimism, Specificity) via Ollama."""
    if not sentence:
        return [0.0, 0.0]
    prompt = (
        "Rate this sentence on 2 dimensions each from -1.0 to +1.0:\n"
        "1. Optimism/Pessimism (-1=very pessimistic, +1=very optimistic)\n"
        "2. Specificity/Vagueness (-1=very vague, +1=very specific)\n\n"
        "Sentence: \"" + sentence[:400] + "\"\n\n"
        "Reply with ONLY 2 numbers separated by commas, e.g.: 0.8, 0.5"
    )
    resp = _ask_ollama(prompt)
    if resp:
        nums = re.findall(r'-?\d+\.?\d*', resp)
        if len(nums) >= 4:
            return [max(-1.0, min(1.0, float(n))) for n in nums[:4]]
    return [0.0, 0.0]


def _cca_score(a_vecs: List[List[float]], b_vecs: List[List[float]]) -> float:
    """CCA correlation between two sets of 4D vectors. Falls back to Pearson if sklearn absent."""
    try:
        import numpy as np
        from sklearn.cross_decomposition import CCA

        n = min(len(a_vecs), len(b_vecs))
        if n < 3:
            return 0.5
        A = np.array(a_vecs[:n])
        B = np.array(b_vecs[:n])
        if A.shape[0] <= A.shape[1]:
            return 0.5

        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', category=UserWarning)
            warnings.filterwarnings('ignore', category=RuntimeWarning)
            old = np.seterr(divide='ignore', invalid='ignore')
            try:
                cca = CCA(n_components=1)
                cca.fit(A, B)
                Ac, Bc = cca.transform(A, B)
                corr = float(np.corrcoef(Ac.flatten(), Bc.flatten())[0, 1])
            finally:
                np.seterr(**old)
        return max(0.0, min(1.0, abs(corr)))
    except Exception:
        pass

    # Fallback: mean pairwise Pearson on first component
    try:
        import statistics
        n = min(len(a_vecs), len(b_vecs))
        if n < 2:
            return 0.5
        a0 = [v[0] for v in a_vecs[:n]]
        b0 = [v[0] for v in b_vecs[:n]]
        ma = statistics.mean(a0)
        mb = statistics.mean(b0)
        sa = statistics.stdev(a0) if len(a0) > 1 else 1.0
        sb = statistics.stdev(b0) if len(b0) > 1 else 1.0
        num = sum((a0[i] - ma) * (b0[i] - mb) for i in range(n))
        denom = (n - 1) * max(sa, 1e-9) * max(sb, 1e-9)
        return max(0.0, min(1.0, abs(num / denom)))
    except Exception:
        return 0.5


class Synthesizer:
    """Combines Agent 1 and Agent 2 into a single bullishness score (20-80)."""

    def run(
        self,
        forensic: Optional[SolliaForensicResult],
        narrative: Optional[SolliaNarrativeResult],
        ticker: str,
        company_name: str,
    ) -> SolliaAnalysisResult:
        logs: List[AgentLogMessage] = []
        _add_log(logs, "Starting synthesis...")

        s1 = forensic.s1 if forensic else 50.0
        s2 = narrative.s2 if narrative else 50.0
        agents_run = []
        if forensic:
            agents_run.append("ForensicAccountant")
        if narrative:
            agents_run.append("NarrativeScout")

        if not forensic and not narrative:
            s_final = 50.0
            track1 = 50.0
            corroboration = 0.5
        elif not forensic:
            s_final = s2
            track1 = s2
            corroboration = 0.5
        elif not narrative:
            s_final = s1
            track1 = s1
            corroboration = 0.5
        else:
            track1, corroboration = self._two_track(s1, s2, forensic, narrative, logs)
            s_raw = 50.0 + (track1 - 50.0) * (1.0 - 0.2 * abs(corroboration))
            s_final = max(20.0, min(80.0, s_raw))

        _add_log(logs, f"Synthesis result: final score {s_final:.0f}/100 ({'BUY' if s_final >= 62 else 'HOLD' if s_final >= 39 else 'SELL'})")

        signal, color = self._classify(s_final)
        _add_log(logs, f"Signal: {signal}")

        exec_summary = self._executive_summary(forensic, narrative, signal, ticker)

        result = SolliaAnalysisResult(
            ticker=ticker,
            company_name=company_name,
            s_final=round(s_final, 2),
            signal=signal,
            signal_color=color,
            agents_run=agents_run,
            forensic=forensic,
            narrative=narrative,
            executive_summary=exec_summary,
            track1_score=round(track1, 2),
            corroboration=round(corroboration, 4),
            log_messages=logs,
        )

        # Build export data without circular reference
        try:
            raw = result.model_dump(exclude={"export_data"})
        except AttributeError:
            raw = result.dict()
            raw.pop("export_data", None)
        result.export_data = raw
        return result

    # ── Private ───────────────────────────────────────────────────────────

    def _two_track(
        self,
        s1: float,
        s2: float,
        forensic: SolliaForensicResult,
        narrative: SolliaNarrativeResult,
        logs: List[AgentLogMessage],
    ) -> Tuple[float, float]:
        _add_log(logs, f"Track 1 — Comparing agent signals for alignment...")
        _add_log(logs, "  Measuring cross-agent signal convergence")

        w1 = abs(s1 - 50.0) / 30.0
        w2 = abs(s2 - 50.0) / 30.0
        similarity = math.exp(-((s1 - s2) ** 2) / (2.0 * 225.0))

        if w1 + w2 < 1e-6:
            weighted_avg = 50.0
        else:
            weighted_avg = (s1 * w1 + s2 * w2) / (w1 + w2)

        s_track1 = similarity * weighted_avg + (1.0 - similarity) * 50.0
        _add_log(logs, f"  Agent signals are {'strongly aligned' if similarity > 0.7 else 'moderately aligned' if similarity > 0.3 else 'diverging'}, combined signal strength: {s_track1:.0f}/100")
        _add_log(logs, "  Signal alignment computed — proceeding to historical validation")

        _add_log(logs, "Track 2 — Historical Corroboration via CCA...")
        _add_log(logs, "  Analyzing paired agent history across all available quarters")
        corroboration = self._corroboration(forensic, narrative, logs)
        _add_log(logs, f"  Historical corroboration: {'high' if corroboration > 0.7 else 'moderate' if corroboration > 0.4 else 'low'}")
        _add_log(logs, "  Corroboration adjustment applied to final score weighting")

        return s_track1, corroboration

    def _corroboration(
        self,
        forensic: SolliaForensicResult,
        narrative: SolliaNarrativeResult,
        logs: List[AgentLogMessage],
    ) -> float:
        fa_vecs: List[List[float]] = []
        ns_vecs: List[List[float]] = []

        for pair in forensic.quarterly_pairs[:6]:
            ctx_text = pair.reality.context_snippet if pair.reality and pair.reality.context_snippet else "No context"
            tone_desc = (
                f"CEO tone gap: {pair.gap:.2f} (negative=CEO more optimistic than 10-K). "
                f"10-K excerpt: \"{ctx_text[:200]}\". "
                f"Period: {pair.fiscal_period}"
            )
            summary = _ask_ollama(f"Summarize this quarterly earnings context in one sentence: {tone_desc}")
            if summary:
                fa_vecs.append(_rate_dimensions(summary))
            else:
                fa_vecs.append([
                    max(-1.0, min(1.0, -pair.gap * 2.0)),  # Optimism
                    0.5,  # Specificity (default)
                ])

        for sig in narrative.top_signals[:6]:
            summary = _ask_ollama(f"Summarize this news tone in one sentence: {sig.headline}")
            if summary:
                ns_vecs.append(_rate_dimensions(summary))
            else:
                ns_vecs.append([
                    max(-1.0, min(1.0, sig.sentiment_score)),  # Optimism
                    0.5,  # Specificity (default)
                ])

        if len(fa_vecs) < 5 or len(ns_vecs) < 5:
            _add_log(logs, f"  Not enough data for CCA (A1={len(fa_vecs)}, A2={len(ns_vecs)}) — using default 0.5")
            return 0.5

        return _cca_score(fa_vecs, ns_vecs)

    @staticmethod
    def _classify(score: float) -> Tuple[str, str]:
        if score <= 38:
            return "SELL", "#EF4444"
        if score <= 61:
            return "HOLD", "#F59E0B"
        return "BUY", "#22C55E"

    @staticmethod
    def _executive_summary(
        forensic: Optional[SolliaForensicResult],
        narrative: Optional[SolliaNarrativeResult],
        signal: str,
        ticker: str,
    ) -> str:
        parts = []
        if forensic and forensic.conclusion:
            parts.append(forensic.conclusion.rstrip("."))
        if narrative and narrative.conclusion:
            parts.append(narrative.conclusion.rstrip("."))
        if not parts:
            return f"{ticker} analysis is inconclusive due to limited data available."
        return "; ".join(parts) + "."
