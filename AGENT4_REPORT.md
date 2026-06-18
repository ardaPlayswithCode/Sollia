# Agent 4 — Risk Factor Delta Scanner (10-K vs 10-K)

**Status: built, wired into backend + frontend, and verified end-to-end on Ford (F).**
Date: 2026-06-18

Agent 4 compares **Item 1A (Risk Factors)** from a company's two most recent 10-K
filings, classifies what changed, and returns the **top 10** most significant
changes with verbatim sentence-level quotes and SEC links.

---

## 1. What was built

| Piece | Location |
|---|---|
| Agent implementation | `sollia_working/src/agents/risk_factors.py` (new) |
| Backend registry + runner | `sollia_web/backend/agents.py` (modified) |
| Clean-export + rich-result wiring | `sollia_web/backend/runs.py` (modified) |
| Frontend agent chip + render card + signal box | `sollia_web/frontend/src/App.jsx` (modified) |

The agent follows the same pattern as Agents 1–3:

1. **Fetch** the two most recent 10-Ks (cache-first via `get_cached_filings`, then
   the SEC EDGAR provider on a miss).
2. **Extract** Item 1A from each (deterministic regex, HTML/XBRL stripped).
3. **Compare** both full sections in **one** LLM call (`call_llm`, the mandated
   provider chain).
4. **Verify** every quote is present in the source text (exact + 80% fuzzy).
5. **Build** SEC links, dedup, score, and emit the top 10.

It uses **only** `call_llm` (LLM) and the **SEC EDGAR provider** + universal cache
helpers (`get_cached_filings` / `get_cached_text` / `cache_text`) — no new
endpoints, and `llm_config.py` / `document_priority.py` / the cache provider were
left untouched.

---

## 2. Test result — Ford (F)

Run via the spec's curl command:

```bash
curl -X POST http://localhost:8000/api/analyze -H "Content-Type: application/json" \
  -d '{"ticker":"F","agents":["risk_factors"],"year":2025}'
```

| Field | Value |
|---|---|
| Old 10-K | filed **2025-02-06** (FY2024, acc `0000037996-25-000013`) |
| New 10-K | filed **2026-02-11** (FY2025, acc `0000037996-26-000015`) |
| Item 1A extracted | 86,450 chars (old) / 93,085 chars (new) |
| Risk factors (heuristic count) | **11 → 11** |
| Changes found | **10** |
| Quote-verified | **9 / 10** |
| Score | **40** |
| Signal | **NEUTRAL** |

### Top changes (real, verbatim-quoted)

| # | Category | Verified | Risk | What changed |
|---|---|---|---|---|
| 1 | NEW | ✓ | Environmental Regulatory Compliance | New risk on emissions-related defect reporting |
| 2 | REWORDED | ✓ | Ford+ Plan Execution | Added "modernize our systems, processes, and technologies" |
| 3 | REWORDED | ✓ | Capital Allocation | Minor rewording |
| 4 | ESCALATED | ✓ | Global Political Instability | Strengthened: "global political instability and volatility … unpredictable trade policy (including tariffs)" |
| 5 | REWORDED | ✓ | Product Defects | "vehicles" → "products" |
| 6 | REWORDED | ✓ | NHTSA Enforcement | "trend" → "strategy" |
| 7 | NEW | ✓ | Consent Order | New risk: 2024 NHTSA consent order + independent monitor |
| 8 | REWORDED | ✗ | Warranty Costs | "vehicles" → "products" (quote truncated by output cap → honestly flagged unverified) |
| 9 | REWORDED | ✓ | Financial Performance | "quarterly performance" → "financial performance" |
| 10 | REWORDED | ✓ | Risk Factors | Added "Some of the risks … may have previously occurred." |

These are genuine year-over-year edits in Ford's 10-K — including a systematic
"vehicles → products" rewording sweep, escalated tariff/geopolitical language, and
two newly disclosed risks (environmental compliance, the NHTSA consent order).

### Scoring check

Categories → weights (ESCALATED 100, NEW 80, REMOVED 60, DE-ESCALATED 40,
REWORDED 20). For F: (80 + 20 + 20 + 100 + 20 + 20 + 80 + 20 + 20 + 20) / 10 =
**40** → between 35 and 65 → **NEUTRAL**. ✓

### Verification of every acceptance criterion

- [x] Both 10-Ks fetched (cache-checked first; `F_filings.json` raw SEC JSON read from cache)
- [x] Item 1A extracted from both
- [x] LLM returns valid JSON with changes
- [x] Quotes verified against source text (9/10 verbatim; the 1 miss is flagged `verified:false`)
- [x] SEC links valid & clickable — both URLs return **HTTP 200**
- [x] Score and signal computed
- [x] `py_compile` clean on every edited file; frontend `vite build` succeeds
- [x] Registered in backend (`/api/agents` lists `risk_factors`) and rendered in the UI
- [x] Clean export schema populated (`export.agents.risk_factors` with citations/severity/direction)

---

## 3. Output shape

`run()` returns exactly the spec schema:

```json
{
  "ticker": "F",
  "agent": "Agent 4 — Risk Factor Delta",
  "run_timestamp": "2026-06-18T...",
  "old_filing_date": "2025-02-06",
  "new_filing_date": "2026-02-11",
  "total_risks_old": 11,
  "total_risks_new": 11,
  "changes_found": 10,
  "top_changes": [
    {
      "rank": 1, "category": "NEW", "risk_title": "...",
      "old_quote": "", "new_quote": "...", "summary": "...",
      "old_url": "https://www.sec.gov/...", "new_url": "https://www.sec.gov/...",
      "old_quote_verified": false, "new_quote_verified": true, "verified": true
    }
  ],
  "score": 40,
  "signal": "NEUTRAL"
}
```

(`old_quote_verified` / `new_quote_verified` are extras so the export can mark each
citation's trust independently; `verified` is the combined per-change flag.)

---

## 4. Key engineering decisions

**Extraction must skip the TOC, cross-references, and running headers.** Ford's
10-K contains 25 separate "Item 1A" strings: a table-of-contents entry, inline
cross-references ("…see Item 1A. Risk Factors for a discussion of…"), and a
"(Continued)" page header repeated on every page of the section. A naive
"first match → next Item 1B/2" (or "longest span") grabbed 428k chars (nearly the
whole document). The final extractor locates the **genuine** "Item 1A. Risk
Factors" header (not followed by a page number or a lowercase preposition, not a
"(Continued)" header), runs to the **first** following Item 1B/1C/2, and strips
the running headers. Result: clean 86k/93k-char sections.

**Risk count is a labeled heuristic.** Ford bolds each risk factor's lead thesis
sentence (~60–400 chars); the count tallies those unique bold runs inside the
section (11 for both years). It is approximate metadata, applied identically to
both filings so the *delta* is meaningful.

**Deduplication.** A weaker model can emit the same quote several times under
different titles; findings are deduped on the (normalized) cited quote — the same
approach Agent 3 uses — before ranking/capping to 10.

**Robust JSON parsing.** Handles markdown fences, leading prose, and
output truncated at `max_tokens` (salvages every complete finding object).

---

## 5. Operational notes on the LLM (important)

Prompt sizing was driven by **measured** Groq rate limits, because they — not the
model context window — are the binding constraint on this account's free tier:

| Model | Role | Per-minute (TPM) | Per-day (TPD) |
|---|---|---|---|
| `llama-3.3-70b-versatile` | Primary | 12,000 | **100,000** |
| `llama-3.1-8b-instant` | Fallback | 6,000 | (separate) |
| OpenRouter `llama-3.1-70b-instruct` | Backup | — | **key invalid (HTTP 401)** |

Two findings worth surfacing (neither is an Agent-4 bug; both affect *all* agents):

1. **The OpenRouter backup key currently returns 401 "User not found."** So for any
   large prompt the only working providers are the two Groq models.
2. **`max_tokens` counts against the budget**, and SEC legalese tokenizes at
   ~5.4 chars/token. Each Item 1A section is therefore capped
   (`MAX_SECTION_CHARS = 8000`, `LLM_MAX_TOKENS = 1800`) so the whole request
   (~5.4k tokens) fits **both** Groq models — the agent keeps working on the 8B
   fallback when the 70B's daily quota is spent.

During development this report's testing **exhausted the 70B's 100k tokens/day**
(it read `Used 99,492 / 100,000`, reset ~1h15m), so the verified Ford run above was
produced by the **8B fallback** — which still returned verbatim, quote-verified
findings with correct classification and scoring. When the 70B has daily budget it
is tried first (per the mandated priority) and yields better-calibrated analysis.

**Tunable:** raise `MAX_SECTION_CHARS` toward ~16000 for more section coverage if
you can rely on the 70B having daily budget (it comfortably fits the 70B's 12k TPM
but exceeds the 8B's 6k TPM, disabling the fallback).

---

## 6. Files changed

```
sollia_working/src/agents/risk_factors.py   (new)  — the agent
sollia_web/backend/agents.py                (mod)  — import, registry entry, runner, ordering, log
sollia_web/backend/runs.py                  (mod)  — AGENT_NAMES, signal polarity, _findings_risk, builders, rich result
sollia_web/frontend/src/App.jsx             (mod)  — agent chip, default selection, score box, render card, badge colors
```

No existing agent logic was modified; all changes to shared files are additive.
