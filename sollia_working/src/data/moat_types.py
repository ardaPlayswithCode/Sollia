"""The 8 moat types audited by Agent 6 (Moat Auditor) — PART 1 of the spec.

Each moat carries three deterministic assets:
  * ``definition``         — what the moat means (for the human-readable output).
  * ``claim_indicators``   — keywords that mark a COMPANY claiming this moat in its
                             own 10-K / earnings calls (drives candidate extraction
                             in PART 3 Step 1).
  * ``challenge_keywords`` — keywords that mark a COMPETITOR challenging this moat
                             in their filings (drives the challenge search in
                             PART 4 Step 2).

These keyword libraries are RECALL instruments: a generous match-set surfaces
candidate sentences, and an LLM selection pass (temperature 0) then picks the
single clearest claim / strongest challenge. So a few broad keywords are fine —
precision comes from the LLM step, not from the keyword list. The libraries are
meant to be tuned empirically (PART 8c); `matches`/`moats_for_text` are the only
surface callers use, so tuning a list never changes the call sites.

Matching is case-insensitive and word-boundary aware (so "win" never fires inside
"winning"); multi-word phrases match as contiguous substrings. The agent also
augments the per-moat challenge set at runtime with company-name patterns (e.g.
"migrated from {company}") — see moat_auditor._dynamic_challenge_terms.
"""
from __future__ import annotations

import re
from typing import Dict, List, Set

MOAT_DEFINITIONS: List[Dict[str, object]] = [
    {
        "name": "Switching Costs",
        "definition": (
            "Expensive, disruptive, or psychologically painful for customers to "
            "leave — financial (contract penalties, migration), operational "
            "(retraining, reintegration), and psychological (sunk cost from loyalty "
            "programs, ecosystem investment)."),
        "claim_indicators": [
            "deeply integrated", "standardized on", "cost of switching", "switching cost",
            "long-term contract", "long term contract", "multi-year contract", "ecosystem",
            "retention rate", "net retention", "churn", "migration cost", "sticky",
            "entrenched", "embedded in", "mission-critical", "lock-in", "lock in",
        ],
        "challenge_keywords": [
            "migration", "migrate", "migrated", "compatible with", "transition from",
            "interoperable", "interoperability", "easy switch", "seamless transition",
            "seamless migration", "onboarding from", "displaced", "drop-in replacement",
            "minimal friction", "without rewriting", "switch from", "rip and replace",
        ],
    },
    {
        "name": "Network Effects",
        "definition": (
            "The product becomes more valuable as more participants join — "
            "marketplace networks (buyers+sellers), platform networks "
            "(developers+users), social networks, and data networks (more usage -> "
            "better product -> more usage)."),
        "claim_indicators": [
            "network effect", "network effects", "marketplace", "platform", "two-sided",
            "users", "members", "participants", "liquidity", "scale of our network",
            "largest network", "ecosystem of partners", "developer community",
            "more valuable as", "flywheel", "active users", "active members",
        ],
        "challenge_keywords": [
            "users", "members", "liquidity", "multi-tenant", "cross-platform",
            "multi-platform", "customer count", "subscribers", "market share",
            "fastest growing", "fastest-growing", "overtook", "surpassed",
            "largest network", "developer community", "two-sided",
        ],
    },
    {
        "name": "Brand",
        "definition": (
            "Brand allows premium pricing, customer preference, or reduced marketing "
            "cost. Distinct from IP — this is consumer perception, trust, and "
            "willingness to pay more."),
        "claim_indicators": [
            "brand", "premium", "pricing power", "loyalty", "most recognized", "trusted",
            "iconic", "customer preference", "nps", "net promoter", "brand equity",
            "brand recognition", "reputation", "command a premium",
        ],
        "challenge_keywords": [
            "market share", "growing faster than", "fastest growing brand",
            "fastest-growing brand", "#1 brand", "number one brand", "customer preference",
            "pricing pressure", "value over brand", "customers increasingly",
            "brand perception", "considered alongside", "premium alternative",
            "gaining share", "share gains",
        ],
    },
    {
        "name": "IP & Patents",
        "definition": (
            "Patents, trade secrets, proprietary technology, and regulatory "
            "exclusivity (FDA market exclusivity, spectrum licenses) — legal barriers "
            "that prevent direct copying."),
        "claim_indicators": [
            "patent", "proprietary", "trade secret", "intellectual property",
            "exclusivity", "fda approval", "regulatory protection", "patent-protected",
            "patent portfolio", "our technology is protected", "patented", "proprietary technology",
            "orange book", "market exclusivity",
        ],
        "challenge_keywords": [
            "patent", "ipr", "inter partes review", "does not infringe", "non-infringing",
            "novel approach", "alternative to", "workaround", "work around", "bypass",
            "generic", "biosimilar", "developed independently", "open-source alternative",
            "open source alternative", "design around", "royalty-free",
        ],
    },
    {
        "name": "Economies of Scale",
        "definition": (
            "Lower per-unit cost due to size — manufacturing scale, purchasing power, "
            "distribution density, and fixed-cost amortization over larger volume."),
        "claim_indicators": [
            "economies of scale", "economy of scale", "purchasing power", "cost advantage",
            "lowest cost", "lowest-cost", "scale", "volume", "efficiency", "capacity",
            "fixed cost leverage", "operating leverage", "cost leadership", "unit cost",
            "scale advantage",
        ],
        "challenge_keywords": [
            "capacity expansion", "new facility", "cost parity", "achieved cost",
            "efficiency gains", "lower cost", "lower-cost", "matching their",
            "comparable scale", "scaled to", "ramped production", "ramping production",
            "new capacity", "gigafactory", "fab", "economies of scale",
        ],
    },
    {
        "name": "Data Moat",
        "definition": (
            "Proprietary data that competitors cannot replicate and that improves the "
            "product — training data for AI, datasets from customer usage, unique "
            "sensor/data collection, and accumulated historical data."),
        "claim_indicators": [
            "proprietary data", "unique dataset", "unique data set", "data advantage",
            "training data", "years of data", "data assets", "data moat", "our data",
            "data we have collected", "data set", "billions of data points", "first-party data",
            "data network", "proprietary dataset",
        ],
        "challenge_keywords": [
            "dataset", "data set", "data partnership", "data agreement", "synthetic data",
            "open-source data", "open source data", "public data", "data access",
            "data sharing", "trained on", "data acquisition", "purchased data", "data lake",
            "licensed data", "first-party data",
        ],
    },
    {
        "name": "Distribution Lock-in",
        "definition": (
            "Exclusive or deeply-entrenched distribution competitors cannot easily "
            "access — bottling networks, dealer networks, shelf space, installed base "
            "of channel partners, platform bundling, and direct-sales-force density."),
        "claim_indicators": [
            "distribution", "distribution network", "channel", "dealer network",
            "shelf space", "stores", "locations", "partners", "resellers",
            "direct sales force", "route to market", "last mile", "installed base",
            "channel partners", "go-to-market", "go to market",
        ],
        "challenge_keywords": [
            # NOTE: bare "dtc" was removed — it collided with "DTC" (Depository
            # Trust Company) in debt-offering 8-K note boilerplate. Spell it out.
            "distribution", "channel", "partnership", "direct-to-consumer",
            "bypass", "online channel", "e-commerce", "ecommerce", "new distribution",
            "expanded distribution", "direct sales", "own stores", "owned stores",
            "marketplace listing", "third-party logistics",
        ],
    },
    {
        "name": "Cornered Resources",
        "definition": (
            "Exclusive or preferential access to a critical input — raw materials, "
            "talent, suppliers, or physical infrastructure. Connects to Agent 5 "
            "(Constraint Auditor): a cornered constrained material is a double signal."),
        "claim_indicators": [
            "supply agreement", "secured access", "long-term contract", "long term contract",
            "exclusive rights", "reserves", "deposits", "strategic supply",
            "vertical integration", "vertically integrated", "self-sufficient", "in-house supply",
            "offtake", "secured supply", "exclusive access",
        ],
        "challenge_keywords": [
            "supply agreement", "secured access", "long-term contract", "long term contract",
            "vertical integration", "vertically integrated", "new source", "new mine",
            "new supplier", "alternative supply", "diversified supply", "in-house production",
            "backward integration", "offtake", "secured supply", "domestic supply",
        ],
    },
]

# Canonical ordered names + name->record lookups.
MOAT_NAMES: List[str] = [m["name"] for m in MOAT_DEFINITIONS]
MOATS: Dict[str, Dict[str, object]] = {m["name"]: m for m in MOAT_DEFINITIONS}


def _compile(keywords) -> "re.Pattern":
    """One case-insensitive alternation with word boundaries around each keyword.
    Boundaries stop short keywords ('win', 'fab') firing inside larger words
    ('winning', 'fabric'); multi-word phrases match because the spaces are literal.
    A trailing optional 's' absorbs the common plural so 'switching cost' also
    catches 'switching costs' and 'user' catches 'users' (longest keyword wins via
    length-sorted alternation)."""
    parts = sorted({re.escape(k.strip()) for k in keywords if k and k.strip()},
                   key=len, reverse=True)
    if not parts:
        return re.compile(r"(?!x)x")  # never matches
    return re.compile(r"(?<![A-Za-z])(?:%s)s?(?![A-Za-z])" % "|".join(parts), re.IGNORECASE)


_CLAIM_RE: Dict[str, "re.Pattern"] = {
    m["name"]: _compile(m["claim_indicators"]) for m in MOAT_DEFINITIONS}
_CHALLENGE_RE: Dict[str, "re.Pattern"] = {
    m["name"]: _compile(m["challenge_keywords"]) for m in MOAT_DEFINITIONS}


def claim_moats_for_sentence(sentence: str) -> Set[str]:
    """Return the set of moat names whose CLAIM indicators appear in the sentence
    (a sentence can indicate multiple moats — that is expected and preserved)."""
    if not sentence:
        return set()
    return {name for name, rx in _CLAIM_RE.items() if rx.search(sentence)}


def sentence_challenges_moat(sentence: str, moat_name: str,
                             extra_terms: List[str] = None) -> bool:
    """True if a sentence contains any CHALLENGE keyword for the given moat, or any
    of the runtime-supplied `extra_terms` (company-name patterns the agent adds)."""
    if not sentence or moat_name not in _CHALLENGE_RE:
        return False
    if _CHALLENGE_RE[moat_name].search(sentence):
        return True
    if extra_terms:
        low = sentence.lower()
        return any(t and t.lower() in low for t in extra_terms)
    return False


def moat_list_text(bullet_numbered: bool = True) -> str:
    """Render the 8 moats + one-line definitions for an LLM classification prompt."""
    lines = []
    for i, m in enumerate(MOAT_DEFINITIONS, 1):
        # First clause of the definition keeps the prompt compact + deterministic.
        short = str(m["definition"]).split(" — ")[-1].split(". ")[0]
        prefix = ("%d. " % i) if bullet_numbered else "* "
        lines.append("%s%s — %s" % (prefix, m["name"], short))
    return "\n".join(lines)


if __name__ == "__main__":  # quick self-check / keyword smoke test
    print("%d moat types" % len(MOAT_DEFINITIONS))
    print(moat_list_text())
    tests = [
        ("Our CUDA platform has created significant switching costs for millions of developers.",
         "Switching Costs"),
        ("Our ROCm platform now supports automated migration of CUDA code with minimal friction.",
         None),  # challenge sentence, not a claim
        ("We benefit from economies of scale across our global manufacturing capacity.",
         "Economies of Scale"),
    ]
    print("\nclaim detection:")
    for sent, expect in tests:
        got = claim_moats_for_sentence(sent)
        print("  %-20s in %s  | %s" % (expect, sorted(got), sent[:55]))
    print("\nchallenge detection (Switching Costs):")
    print("  ", sentence_challenges_moat(
        "Customers can migrate workloads with minimal friction.", "Switching Costs"))
