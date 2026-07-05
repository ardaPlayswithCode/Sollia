# Quality & Speed Pass — 2026-07-05

Comprehensive quality/speed fix pass driven by full-pipeline runs on NVDA, OKLO,
and TSLA, then a cross-ticker audit (AMD, JPM, GE, MSTR, RIVN, COIN, SPOT).
Every issue below was root-caused, fixed deterministically where possible, and
re-verified against live runs. Companion memory: session notes; predecessor
reports: `AGENT7_AUDIT_FIXES.md`, `AGENT8_FINAL_HARDENING.md`,
`PRODUCTION_POLISH_REPORT.md`.

---

## Part 0 — Speed: the three Groq orgs were wired but SERIALIZED

**Root cause.** `GROQ_ORG_KEYS` (mac_studio×11, berkeley×3, heysollia×3) was
already loaded and org-interleaved — but `llm_config` still gated every live
call behind ONE global semaphore(3), ONE global 0.3 s inter-call spacing, and a
GLOBAL 30 s cooldown after any 429. One dry bucket paused all three orgs; the
3× capacity was never usable. Two more compounding bugs:

* **Groq-8B served strict-JSON classify on 429 fallthrough** — measured on
  2026-07-04: 950 Agent-1 classify calls answered with PROSE (88 % truncated at
  the 400-token cap), every one parsed to `[]` → silently dropped findings.
* **No paid rescue**: the chain ran `effective_idx..end`, so tier-0 (Fable 5)
  was unreachable — under a storm a call burned 3 full chain passes
  (backoffs + 30 s cooldowns) and FAILED (114 such failures that night; OKLO's
  998 s run produced **0 findings**).

**Fixes** (`src/config/llm_config.py`):
* Per-`(provider-family, org)` **bucket state**: `_bucket_sem` semaphores
  (`LLM_MAX_CONCURRENCY` per org), `_rate_gate(family, org)` reservation-style
  spacing (sleeps outside the lock), `_trigger_bucket_cooldown` per org.
  The global cooldown now fires only when an entire chain pass 429s.
* `Groq-8B` marked `json_weak` → strict-JSON calls skip it in the chain.
* **Paid last-resort**: when `effective_idx > 0`, `requires_strict_json`, and
  the prompt is < 2,500 words (the shape Fable 5 measures clean on), tier 0 is
  appended to the chain order — still guarded by the per-run cost cap.
* Per-call serving log: `[LLM] Groq-70B org=berkeley key=GROQ_API_KEY_BERKELEY_2
  957ms …` + `org`/`key_env` recorded in the history index; startup prints the
  org wiring (`Groq orgs wired: mac_studio×11, berkeley×3, heysollia×3`).
* Agent-1 classify: `max_tokens` 400→700 (15 % of Groq-70B responses had been
  truncated mid-array), executor 4→8 workers, explicit agent/stage attribution
  (worker threads don't inherit the thread-local context).

**Measured.** Org distribution is balanced (13-15 calls per org per run);
FAILED calls this session: 4 (all >2,500-word prompts under total saturation)
vs 114 the night before. TSLA A1 **warm = 102 s** (target < 3 min ✓). Fully
cold runs on 2026-07-05 were bounded by **Groq token-per-day physics** — the
previous night's storms drained the 70B TPD (100k/org/day, rolling); even so,
TSLA A1 cold completed all 151 batches (2,351 s, zero dropped batches) and
OKLO A1 cold went from *998 s / 0 findings* to *619 s / 2 verified findings*.
On a refilled day the 3-org throughput supports the < 3 min cold target; the
infra no longer wastes any of it.

## Part 1 — Agent 4 quote verification: zero unverified

Three distinct root causes behind TSLA's 5 unverified quotes:
1. **Inline page-break junk** in the extracted section (“…subject 14 Table of
   Contents to a number…”) — the quote was right, the source was polluted.
   Stripped in `extract_risk_factors` AND in `_norm` (both sides).
2. **LLM stitching/paraphrase** — quotes verbatim for 180-310 chars, then the
   model jumps to a non-adjacent sentence or rewrites a clause.
   `_reground_quote` re-anchors on the quote's opening words and returns the
   SOURCE's own span (verbatim by construction); a finding whose substantive
   quote still can't be grounded is DROPPED (never shown), with one
   `bypass_cache` retry per comparison.
3. **Update-only 10-Q referral stubs** (TSLA 327 chars, OKLO 318 chars) —
   comparing them forced hallucinated ESCALATEDs whose “new” quotes were 10-K
   text. `_is_referral_stub` short-circuits with an honest
   `no_material_changes` result and zero LLM calls.

Also: SEC throttle pages are HTTP-200 HTML — `_is_sec_error_page` keeps them
out of the text cache (the OKLO “Item 1A could not be located” transient), and
an empty extraction refetches once past the cache.

**Verified:** TSLA ×3 (9/9), NVDA (10/10), OKLO (10/10), RIVN (9+10), AMD
(8+2), GE (9), MSTR (6) — **zero unverified quotes everywhere**.

## Part 2 — CAGR extraction

A qualifying percentage now requires: a forward TIME HORIZON in the sentence,
growth context AROUND the percentage itself, not "X % of <something>", no
safe-harbor/litigation boilerplate, no past-tense-only framing; selection
prefers the CAGR-anchored percentage and never the largest number. The
“Source” is a real URL or absent (`_tr_url` no longer fabricates
"SEC 8-K EX-99.1").

**Verified:** OKLO 95 % (nuclear-fuel energy content) → unavailable; AMD
2.125 % (safe-harbor) → unavailable; NVDA/TSLA stay unavailable; genuine
"30 % CAGR through 2035" sentences extract.

## Part 3 — Promise audit noise elimination

Stage-1 exclusions added: quarterly-guidance signature (also never
auto-BROKEN — PENDING if < 120-day horizon), accounting estimates (future
amortization, “we account for”, effective-tax-rate policy, ASU adoption,
ASC-606 “expect to recognize $X in the next 12 months”), press-release
logistics (webcast/advisory/What:/contact), EX-99.1/FOR-IMMEDIATE-RELEASE
header artifacts, stock-split trading mechanics, estimated tax payments,
risk hedges (“there is no assurance…”, “we may not be able to…”), necessity
language (“we will need to…”), and vague filler (continue-to verbs with no
metric once year/quarter tokens are ignored). Near-duplicate promises dedup
via word-set containment.

Stage-2: keyword overlap can NEVER yield KEPT — `_evidence_confirms`
(achievement verbs) is required, else honest UNRESOLVED “Same disclosure found
in later filing”; near-identical evidence (≥ 0.8 similarity) is treated as a
restatement; evidence-candidate ranking boosts completion-reporting sentences;
the verdict JSON is `requires_strict_json` so a drought can never hand
adjudication to a json_weak tier.

**Verified:** NVDA 87 → 50 surfaced (zero amortization BROKENs, zero guidance
BROKENs, zero press-release KEPTs, zero “strong keyword overlap”); TSLA’s four
earnings-call-date KEPTs gone; KEPT examples carry real confirmations
(“shipping production systems of Blackwell began in Q4 FY2025”).

## Part 4 — Agent 1 false positives

Deterministic guards in `_validate_llm_response`:
* `_is_period_clone` — same numeric-masked sentence skeleton across filings
  with no shared year anchor (checked in the quote AND its located sentence —
  models quote bare fragments) → different reporting windows, dropped. The
  genuine capex revision ($20B vs $25B, both anchored “in 2026”) survives and
  ranks #1 HIGH.
* `_is_pure_relabel` — near-identical boilerplate whose wording diff carries no
  quantifier/modal/negation/direction/temporal signal (kills “digital twin” →
  “3D internet”; keeps “some” → “a substantial number”).
* Subset-clarification, table-ROW quotes, table-CELL quotes (both sides < 4
  words — “Cybertruck / Active” vs “Cybertruck / Tooling”), “refer to Note N”
  numbering plumbing, and omissions whose ABSENT side is the 10-Q while the
  10-K holds the detail (structural quarterly brevity; the reverse direction
  is kept as new information).

**Verified live (TSLA):** 30 → 20 findings; no 28,819-vs-21,767, no identical
excerpts, no segment ordering; capex finding present at #1.

## Part 5 — Moat auditor competitor hallucination

**Root cause:** the sector classify call sent ONLY the company name; Groq-8B
did not know OKLO (2024 de-SPAC) and answered “Cloud Infrastructure &
Platform” → the competitor step coherently returned AMZN/MSFT/GOOGL/… and the
result was cached. Fixes: classification runs AFTER the 10-K fetch and is
grounded in the Item-1 text (cache schema v2 auto-invalidates old entries);
the competitor cache is valid only when its recorded sector matches; the
competitor prompt carries the business excerpt, ranks by directness of
competition, and returns `[]` rather than guessing; the cross-sector escape
lost the always-true “compete” keyword and is capped at 3; empty lists render
“No direct public competitors identified in SEC filings.”

**Verified:** OKLO → sector “Utilities & Power Generation” (the 55-taxonomy
has no nuclear sector), competitors **SMR (NuScale — direct SMR peer)**, EXC,
NEE, DUK; verdicts reasoned and consistent (real-but-peripheral → NO THREAT).
TSLA regression: LCID/RIVN/NIO/XPEV/GM.

## Part 6 — Item 1A for SPAC / recent-IPO / integrated-report filers

OKLO extracts correctly (the earlier failure was the SEC-throttle-page
transient, now guarded). Added: bare “RISK FACTORS” heading fallback,
tiny-section fall-through (GE’s primary doc indexes “Item 1A … 24-31” and
heads the body “RISK FACTORS. The following discussion…” — the old path
returned a 27-char stub and reported a false “no material changes”; now 63k
chars extract and GE yields 9 verified changes), honest messages for
fewer-than-two-10-K filers (SPOT/20-F).

## Part 7 — UI

Completion messages show severity counts (no more “Score 24 (BEARISH)” toast);
`Quote` renders the Source label as a hyperlink only for real http(s) URLs;
the provisioning wall-of-text is a one-line `<details>` collapsible; unverified
findings are filtered in `RiskCard` AND suppressed in `runs.py`
(`_risk_finding` returns None on any unverified citation) — with Part 1, the
“⚠ quote not verified verbatim” state is unreachable.

## Bonus root-causes found by the cross-ticker audit

* **COIN legal “no proceedings” was two stacked extraction bugs**: the Item-3
  regex matched the TOC row on raw HTML (headers are split by inline markup —
  patterns retry on tag-stripped text), and the real Item 3 defers to “Note 21.
  Commitments and Contingencies” — `_extract_referenced_note` follows the
  pointer (legal subsection preferred). COIN now reports its state-AG and
  SEC/regulator subpoena matters with verbatim quotes; the current-year quote
  visibly drops the SEC mention (the dismissed case).
* JPM/COIN A4 comparisons remained capacity-blocked on the drained day
  (> 2,500-word strict-JSON prompts with every eligible tier saturated) —
  honest “comparison failed” rather than fabricated output; they succeed when
  quotas refill.

## Provider physics (measured, for future passes)

Groq free tier: 70B = 100k tokens/day per org (ROLLING window) — one cold
Agent-1 run ≈ 350k tokens exceeds the 3-org daily budget, so back-to-back cold
runs WILL spill to Gemini → OR :free → Fable 5 last-resort. That spillover is
now graceful (zero dropped work), and warm runs are unaffected (TSLA A1 102 s).
OpenRouter funded balance after this pass: ≈ $3.8 of $20.
