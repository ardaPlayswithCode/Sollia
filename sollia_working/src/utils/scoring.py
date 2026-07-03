"""Unified scoring for all four Sollia agents (PART 2 redesign).

WHY THIS MODULE EXISTS
----------------------
Before this redesign each agent invented its own scale and its own neutral
point, and several were not magnitude-aware (Agent 1 scored only its single
worst discrepancy, so 1 high-severity contradiction and 8 of them both produced
10/100). This module is the single, documented source of truth so that an
analyst can read ANY agent's number the same way.

SHARED CONVENTIONS (every agent obeys these)
--------------------------------------------
* Direction: higher = more bullish / healthier, lower = more bearish. ALWAYS.
* Range: clamped to [SCORE_FLOOR, SCORE_CEIL] = [10, 95]. Real companies are
  rarely perfectly clean (100) or maximally damning (0); the band keeps scores
  honest and comparable.
* Signal bands are IDENTICAL across agents so "65" means the same thing
  everywhere:
      BULLISH  : score >= 65
      NEUTRAL  : 36 .. 64
      BEARISH  : score <= 35
* Two families of question, one scale:
    - STATE agents (1 consistency, 2 credibility): the score describes a
      standing condition. Agent 1's healthy default is HIGH (clean disclosures
      = 90); Agent 2's no-track-record default is 50 (a ratio with no data).
    - TREND agents (3 legal, 4 risk): the score describes the DIRECTION of
      change since the prior filing. "Nothing changed" = a neutral ~50-55;
      improvements push up, deteriorations push down.
  Different neutral points are intentional and reflect each domain's default;
  the signal bands above make them comparable regardless.

Every function is PURE post-processing — no LLM calls, no I/O — so scoring is
deterministic and a run can be re-scored offline.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# ── Shared band constants ────────────────────────────────────────────────────
SCORE_FLOOR = 10.0
SCORE_CEIL = 95.0
NEUTRAL = 50.0
BULLISH_AT = 65.0   # score >= this  -> BULLISH
BEARISH_AT = 35.0   # score <= this  -> BEARISH


def _clamp(x: float, lo: float = SCORE_FLOOR, hi: float = SCORE_CEIL) -> float:
    return max(lo, min(hi, x))


def classify_signal(score: float) -> str:
    """Map any agent score to the shared 3-band signal."""
    if score >= BULLISH_AT:
        return "BULLISH"
    if score <= BEARISH_AT:
        return "BEARISH"
    return "NEUTRAL"


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 1 — CORPORATE DISCREPANCY  ("How consistent are disclosures?")
# ═════════════════════════════════════════════════════════════════════════════
# Healthy default is HIGH: no cross-document discrepancies = clean = 90 (not 100,
# because absence of found discrepancies is not proof none exist). Each
# discrepancy subtracts a penalty derived from its existing `final_score`
# (0-100, already a blend of severity x type-objectivity x doc-tier x
# confidence, with same-filing false-positives pre-discounted). Penalties use
# RANK DECAY so the worst discrepancy dominates but additional ones still lower
# the score (magnitude awareness) without letting a long tail of minor tensions
# floor it on noise.
A1_BASELINE = 90.0
A1_SINGLE_MAX = 45.0   # penalty of a hypothetical single final_score=100 HIGH discrepancy
A1_RANK_DECAY = 0.70   # contribution of the i-th worst discrepancy = DECAY**i
# Severity gates the penalty (2026-07-01 recalibration). final_score blends
# type-objectivity/doc-tier/confidence, but it let a MEDIUM land ~80/100 — six
# medium/low findings floored the score at 10, reading like catastrophic
# inconsistency when the docket was actually "0 high, 3 medium, 3 low". An
# analyst reads severity first; the multiplier makes the score do the same:
# only HIGH findings can produce a bearish floor, mediums pull toward neutral,
# lows barely register.
A1_SEV_MULT = {"high": 1.0, "medium": 0.5, "low": 0.2}


def score_corporate_discrepancy(top_discrepancies: List[Dict]) -> Tuple[float, str, str]:
    """PART 2 rolling-window update (2026-07-01): findings may carry a
    `recency_weight` (1.0 = current period, halving per fiscal year of age —
    set by run_discrepancy_agent). Each finding's penalty is scaled by it, so
    a 5-year window doesn't floor every mature company at 10: today's
    contradictions dominate, history informs. Findings without the field
    weigh 1.0 — single-year callers are unchanged."""
    discs = [d for d in (top_discrepancies or []) if isinstance(d, dict)]
    if not discs:
        score = A1_BASELINE
        return score, classify_signal(score), (
            "No cross-document discrepancies found — disclosures are internally "
            "consistent (clean baseline 90; capped below 100 since absence of "
            "found conflicts is not proof of none).")

    def _eff(d):
        sev_mult = A1_SEV_MULT.get(str(d.get("severity", "low")).lower(), 0.2)
        rec = float(d.get("recency_weight", 1.0))
        return (float(d.get("final_score", 0.0)) / 100.0) * sev_mult * rec

    # Rank by EFFECTIVE contribution (severity x recency x score) so the rank
    # decay applies to the effectively-worst finding first.
    ranked = sorted(discs, key=_eff, reverse=True)
    penalty = 0.0
    for i, d in enumerate(ranked):
        penalty += _eff(d) * A1_SINGLE_MAX * (A1_RANK_DECAY ** i)
    score = _clamp(A1_BASELINE - penalty)
    sev = {"high": 0, "medium": 0, "low": 0}
    for d in ranked:
        s = d.get("severity", "low")
        if s in sev:
            sev[s] += 1
    windowed = any("recency_weight" in d for d in ranked)
    rationale = (
        f"{len(ranked)} discrepancy(ies) "
        f"({sev['high']} high, {sev['medium']} medium, {sev['low']} low); "
        f"{'recency-weighted, ' if windowed else ''}rank-decayed penalty {penalty:.0f} "
        f"from clean baseline 90"
        f"{' (older fiscal years discounted 50%/yr)' if windowed else ''}.")
    return round(score, 1), classify_signal(score), rationale


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 2 — PROMISE AUDIT  ("Does management do what they say?")
# ═════════════════════════════════════════════════════════════════════════════
# Severity-and-recency-weighted fulfilment ratio (the prior design was sound).
# The fix is SAMPLE-SIZE SHRINKAGE: a 90 from 3 promises is far less trustworthy
# than a 90 from 90 verdicts, yet the old formula scored both alike and let one
# lone BROKEN promise read as 10/100. We shrink the ratio toward 0.5 with a
# neutral pseudo-prior of A2_PRIOR weighted-verdict mass, so thin records sit
# near neutral and only a real track record can move the needle. Large samples
# are essentially unaffected.
A2_PRIOR = 8.0  # weighted-verdict mass of a neutral prior (~2 average verdicts)


def score_promise_audit(weighted_kept: float, weighted_partial: float,
                        weighted_broken: float) -> Tuple[float, str, str]:
    wt = weighted_kept + weighted_partial + weighted_broken
    fulfilled = weighted_kept + 0.5 * weighted_partial
    # Bayesian shrinkage toward 0.5 (neutral) with pseudo-count A2_PRIOR.
    shrunk = (fulfilled + 0.5 * A2_PRIOR) / (wt + A2_PRIOR)
    score = _clamp(shrunk * 100.0)
    if wt <= 0:
        return NEUTRAL, classify_signal(NEUTRAL), (
            "No resolved promises yet (all pending/unresolved) — neutral 50, no "
            "track record to score.")
    raw = (fulfilled / wt) * 100.0
    rationale = (
        f"Fulfilment {fulfilled:.1f} of {wt:.1f} weighted verdicts = {raw:.0f}% "
        f"raw, shrunk to {score:.0f} toward neutral with a {A2_PRIOR:.0f}-mass "
        f"prior (thin records pulled to 50; large records barely move).")
    return round(score, 1), classify_signal(score), rationale


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 3 — LEGAL PROCEEDINGS  ("Are legal risks rising, stable, or falling?")
# ═════════════════════════════════════════════════════════════════════════════
# TREND score over every proceeding/event across 10-K, 10-Q and 8-K. Only the
# `status` is reliably available (the model does not emit a priority), so the
# direction of each status drives the score. QUIETLY_REMOVED is the most
# penalized: a matter that disappears WITHOUT a stated settlement/dismissal is a
# disclosure-quality red flag. Stable matters (UNCHANGED/NO_UPDATE) barely move
# it. Baseline 55: ongoing litigation is normal, and "no legal news" is mildly
# reassuring.
A3_BASELINE = 55.0
A3_STATUS_WEIGHT = {
    "QUIETLY_REMOVED": -16.0,
    "ESCALATED":       -13.0,
    "NEW":              -9.0,
    "AMENDED":          -3.0,   # 8-K: a material agreement amended (mild)
    "NO_UPDATE":         0.0,
    "UNCHANGED":        +1.0,   # explicitly unchanged is mildly reassuring
    "TERMINATED":       +5.0,   # 8-K: an obligation ended (mildly positive)
    "SETTLED":          +8.0,
    "DISMISSED":       +12.0,
}


def score_legal_proceedings(findings: List[Dict]) -> Tuple[float, str, str]:
    statuses: List[str] = []
    for f in (findings or []):
        if not isinstance(f, dict):
            continue
        for p in f.get("proceedings", []) or []:
            st = str(p.get("status", "")).upper().strip()
            if st:
                statuses.append(st)
    if not statuses:
        return A3_BASELINE, classify_signal(A3_BASELINE), (
            "No legal proceedings or changes disclosed across 10-K/10-Q/8-K — "
            "stable legal posture (neutral baseline 55).")
    delta = sum(A3_STATUS_WEIGHT.get(st, 0.0) for st in statuses)
    score = _clamp(A3_BASELINE + delta)
    from collections import Counter
    counts = Counter(statuses)
    moved = {k: v for k, v in counts.items() if A3_STATUS_WEIGHT.get(k, 0.0) != 0.0}
    rationale = (
        f"Legal trend across {len(statuses)} matter(s): {dict(counts)}; "
        f"net {delta:+.0f} from baseline 55"
        + (f" (driven by {moved})" if moved else " (all stable)") + ".")
    return round(score, 1), classify_signal(score), rationale


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 4 — RISK FACTORS  ("Are corporate risks rising, stable, or falling?")
# ═════════════════════════════════════════════════════════════════════════════
# TREND score over Item 1A changes. The prior category+rank model was the most
# mature scorer; we keep it (rank is a reasonable severity proxy since changes
# are ranked by importance) and only (a) unify the band to [10,95] + shared
# signals, (b) keep the 10-K + 1.5x10-Q recency blend, (c) emit a rationale.
# ESCALATED/NEW push bearish; REMOVED/DE-ESCALATED push bullish (but less, by
# design: a newly-disclosed risk is more actionable than a dropped one);
# REWORDED never moves it.
A4_BASELINE = 50.0
A4_IMPACT = {
    "ESCALATED":    lambda rank: -(12.0 - rank),       # rank1 -11 .. rank10 -2
    "NEW":          lambda rank: -(8.0 - rank * 0.6),  # rank1 -7.4 .. rank10 -2
    "REMOVED":      lambda rank: 6.0 - rank * 0.4,     # rank1 +5.6 .. rank10 +2
    "DE-ESCALATED": lambda rank: 8.0 - rank * 0.6,     # rank1 +7.4 .. rank10 +2
    "REWORDED":     lambda rank: 0.0,
}


def _score_risk_change_set(changes: List[Dict]) -> float:
    score = A4_BASELINE
    for i, ch in enumerate(changes or []):
        cat = str(ch.get("category", "")).upper().strip()
        impact = A4_IMPACT.get(cat)
        if impact:
            score += impact(i + 1)
    return _clamp(score)


def score_risk_factors(tenk_changes: List[Dict],
                       tenq_changes: Optional[List[Dict]] = None
                       ) -> Tuple[float, str, str]:
    tenk = _score_risk_change_set(tenk_changes or [])
    if tenq_changes:
        tenq = _score_risk_change_set(tenq_changes)
        final = _clamp((tenk + 1.5 * tenq) / 2.5)  # 10-Q weighted 1.5x (more recent)
        blend = f"10-K {tenk:.0f} blended with 10-Q {tenq:.0f} (10-Q x1.5)"
    else:
        final = tenk
        blend = f"10-K only ({tenk:.0f})"
    from collections import Counter
    cats = Counter(str(c.get("category", "")).upper().strip()
                   for c in (tenk_changes or []))
    rationale = (
        f"Risk-factor trend: {dict(cats) if cats else 'no Item 1A changes'}; "
        f"{blend}; baseline 50.")
    return round(final, 1), classify_signal(final), rationale


# ═════════════════════════════════════════════════════════════════════════════
# COMPOSITE  — simple, transparent mean of the four independent agent scores.
# ═════════════════════════════════════════════════════════════════════════════
# Each agent score is now independently meaningful on one scale with one
# direction, so the only defensible composite is their plain average. (The old
# Synthesizer's Gaussian-similarity blend pulled toward 50 by an opaque rule —
# "44.2 HOLD" that no analyst could reconstruct.)
def composite_score(agent_scores: Dict[str, float]) -> Tuple[float, str]:
    vals = [v for v in agent_scores.values() if isinstance(v, (int, float))]
    if not vals:
        return NEUTRAL, classify_signal(NEUTRAL)
    avg = sum(vals) / len(vals)
    return round(avg, 1), classify_signal(avg)
