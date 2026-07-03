"""Prompt-injection defanging for UNTRUSTED document text (Part 3a, 2026-07-02).

SEC filing / transcript text is attacker-influenceable (anyone can file, and
8-K exhibits carry press releases). The agents already resist injection
structurally — strict JSON-schema parsing, quote grounding against source
sentences, and Agent 5's sector allowlist — so the 2026-07-02 injection suite
found ZERO instruction-following breaches across Agents 1-6. The one imperfect
result was OUTPUT HYGIENE: Agent 4 quoted an attacker's "SYSTEM: override…"
sentence verbatim as a risk-factor change. This module closes that gap.

neutralize_injections() replaces unambiguous injection DIRECTIVES with a short
marker BEFORE the text is interpolated into a prompt. Design constraints:
  * Only fires on phrases that do not occur in genuine SEC prose ("ignore all
    previous instructions", "SYSTEM:" as a line lead, "[[system]]", role tags),
    so legitimate filing text — and quote grounding against it — is untouched.
  * Applied to the UNTRUSTED document only, never to an agent's own prompt
    template (whose instructions are legitimate and must survive).
  * Idempotent and cheap (a handful of regexes); never raises.
"""
from __future__ import annotations

import re
from typing import Tuple

_MARK = "[filtered: non-filing directive]"

# High-signal injection directives. Each pattern targets language that does not
# appear in real 10-K/10-Q/8-K/DEF-14A prose, so false positives on genuine
# filings are effectively nil.
_PATTERNS = [
    # "ignore/disregard/forget (all) (previous/prior/above) instructions/context"
    re.compile(r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}?"
               r"\b(?:previous|prior|above|earlier|all)\b[^.\n]{0,30}?"
               r"\b(?:instruction|instructions|context|constraints|rules|prompt)\b",
               re.IGNORECASE),
    # role / control tokens: [[system]], <system>, "### system", chat role tags
    re.compile(r"\[\[?\s*/?\s*(?:system|assistant|user|instruction)s?\s*\]?\]", re.IGNORECASE),
    re.compile(r"<\s*/?\s*(?:system|assistant|user|instruction)s?\s*>", re.IGNORECASE),
    # a line that LEADS with a role directive: "SYSTEM: do X", "ASSISTANT: ..."
    re.compile(r"(?m)^\s*(?:system|assistant|developer)\s*:\s+(?=\S)", re.IGNORECASE),
    # "you are now (a/an) ... assistant" jailbreak framing
    re.compile(r"\byou are now\b[^.\n]{0,60}?\b(?:assistant|model|ai|dan|unrestricted)\b",
               re.IGNORECASE),
    # explicit "reveal/print/output your (system) prompt/instructions"
    re.compile(r"\b(?:reveal|print|output|repeat|show)\b[^.\n]{0,30}?"
               r"\b(?:system\s+)?(?:prompt|instructions)\b", re.IGNORECASE),
]


def neutralize_injections(text: str, max_len: int = None) -> str:
    """Return `text` with injection directives replaced by a neutral marker.
    Optionally truncates to `max_len` chars first (callers often clip anyway)."""
    if not text:
        return text
    if max_len is not None and len(text) > max_len:
        text = text[:max_len]
    out = text
    for pat in _PATTERNS:
        out = pat.sub(_MARK, out)
    return out


def scan_injections(text: str) -> Tuple[bool, list]:
    """Diagnostic: return (found, [matched snippets]) without modifying text."""
    hits = []
    for pat in _PATTERNS:
        for m in pat.finditer(text or ""):
            hits.append(m.group(0)[:80])
    return (bool(hits), hits)
