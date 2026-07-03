# Sollia — Security Hardening

_Summary of the security posture after the 2026-07 final bulletproofing pass
(Part 3 + Part 6). Every control below is implemented and tested; test entry
points are named so each can be re-verified._

The design goal for scale: **the only thing that should slow down at 50+
customers is queue time from API rate limits — never cost, crashes, or a
breach.**

---

## Threat model

Sollia ingests attacker-influenceable input from three surfaces:
1. **User request** (ticker, agent list) — a public HTTP endpoint.
2. **SEC filing / transcript text** — anyone can file with the SEC, and 8-K
   EX-99 exhibits carry press releases; this text flows into LLM prompts.
3. **The LLM providers themselves** — untrusted third parties returning text
   that the pipeline parses.

The controls are organized by which surface they defend.

---

## 1. Prompt injection (filing text → LLM)

**Structural defenses (primary).** The agents never trust model output as
control flow:
- Every agent parses a **strict JSON schema** and discards anything off-schema.
  An injected `{"events":[…],"pwned":true}` cannot change an agent's output
  shape — verified: schema held on all 15 injection payloads.
- **Quote grounding** — Agents 1/3/4/6 verify every quote is present in the
  source filing text; a hallucinated or injected quote is dropped.
- **Agent 5 uses an allowlist** — `_match_sector()` can only return one of 14
  taxonomy sectors, `UNSUPPORTED`, or `None`. No input can make it emit
  anything else (proven exhaustively in `tools/injection_test.py`).

**Defense-in-depth (output hygiene).** `src/utils/prompt_safety.py`
`neutralize_injections()` defangs unambiguous injection directives ("ignore all
previous instructions", `SYSTEM:` line leads, `[[system]]`/`<system>` tags,
"reveal your prompt", "you are now an unrestricted assistant") in the UNTRUSTED
document **before** it reaches any prompt or is surfaced as a quote. It is a
verified **no-op on genuine filings** (0 hits across 948 real cached docs from
36 tickers), so it neither corrupts legitimate quotes nor busts the embedding /
LLM caches. Wired into `_strip_html` (Agents 1/3/6), `extract_risk_factors`
(Agent 4), and Agent 3's legal-section intake.

**Test.** `python3.11 tools/injection_test.py` (run with `SOLLIA_LLM_NO_CACHE=1`
to force live calls) — 5 payloads × Agents 3/4/5. Latest result: **15/15 pass,
0 instruction-following breaches, 0 system-prompt leaks.** Agents 1/2/6 were
covered in the prior audit.

---

## 2. Cost / financial abuse (the "don't burn the key" controls)

| Control | Limit (default) | Env override | Where |
|---|---|---|---|
| Hard per-run **cost cap** | $2.00 billed | `SOLLIA_MAX_COST_PER_RUN` | `llm_config.call_llm` |
| Per-run **LLM call budget** | 300 live calls | `SOLLIA_MAX_LLM_CALLS_PER_RUN` | `llm_config._budget_precheck` |
| Per-run **SEC fetch budget** | 200 fetches | `SOLLIA_MAX_SEC_FETCHES_PER_RUN` | `fetch_budget` |
| **Circuit breaker** | 5 consecutive chain failures | `SOLLIA_LLM_CIRCUIT_THRESHOLD` | `llm_config` |
| **Cache-bust rate limit** | 5 `NO_CACHE` runs/hour | `SOLLIA_MAX_NO_CACHE_RUNS_PER_HOUR` | `llm_config.reset_run_budget` |
| Per-IP **request rate** | 10/min | `SOLLIA_RATE_LIMIT_PER_MIN` | `backend/main.py` |
| Per-IP **concurrent runs** | 2 | `SOLLIA_MAX_CONCURRENT_RUNS_PER_IP` | `backend/main.py` |
| Per-IP **daily runs** | 20/day | `SOLLIA_MAX_RUNS_PER_DAY_PER_IP` | `backend/main.py` |

**The cost cap cannot be bypassed by code path.** The check lives inside
`call_llm`'s provider loop: once cumulative *billed* spend (tier `billed:True` =
Fable 5) crosses the cap, the paid tier is skipped and every remaining call runs
free-tier only. A single run cannot exceed the cap plus one in-flight call.
Adaptive routing (`select_tier_for_prompt`) additionally keeps simple prompts
off the paid tier entirely, and never promotes a cheap agent onto it — so an
attacker cannot force paid calls with an oversized document.

**Tests.** Cost cap + cache-bust limiter: mock-provider suite in the Part-1
verification (skip-paid-tier on cap, `NO_CACHE` suppression after 5/hour).
Backend limits: `TestClient` suite (422 bad ticker / 413 oversized / 429 burst).

---

## 3. Input validation (request → pipeline)

- **Ticker shape**: `^[A-Z][A-Z0-9.\-]{0,9}$` (accepts `BRK.A`, `BF-B`;
  rejects junk and injection strings).
- **Ticker existence**: validated against the SEC's official
  `company_tickers.json` (cached 24 h) **before any fetch** — junk tickers are
  rejected up front and can't spend the SEC fetch budget. Degrades to
  shape-only if the SEC directory is unreachable (never blocks on SEC downtime).
- **Request body size**: POST bodies > 10 KB rejected with 413 *before* JSON
  parsing (cost-amplification guard — a 500 KB "ticker" is never parsed).
- **Year selector removed** (Part 2): Agent 1 uses a fixed rolling 5-year
  window, eliminating the year as an input-abuse vector entirely.

---

## 4. Data integrity (external data → pipeline)

- **TLS everywhere**: all SEC/USGS/FRED/LLM traffic is HTTPS; no `verify=False`
  anywhere in the codebase (grep-verified). MITM injection of filing data is
  therefore not possible without breaking TLS.
- **USGS PDF provenance**: every extract records the `pdf_sha256` of the exact
  bytes parsed, so any supply/demand number is traceable to — and
  re-verifiable against — the source PDF.
- **EDGAR outage handling**: `fetch_budget` retries transient 5xx once and, after
  5 consecutive sec.gov failures, fails fast for 60 s instead of burning
  timeouts. Caches are checked **before** the network, so cached tickers keep
  working through a full EDGAR outage.
- **Stalled-connection hang guard**: a real hang was found in this pass — an
  ESTABLISHED connection to a provider's Cloudflare front stalled for 13+
  minutes despite a socket timeout (`requests`' timeout is inactivity-between-
  bytes; a trickling/keepalive server defeats it). Fixed two ways: (1) every LLM
  POST now runs on a **daemon thread with a hard wall-clock deadline**
  (`_post_with_wallclock`, ceiling = provider timeout + 15 s); on breach the
  call is abandoned and the chain falls through to the next provider — a stalled
  provider can never block a run or delay process exit. (2) The remaining
  un-timeouted SEC requests in Agent 3 were given `(connect, read)` timeouts.
  Verified: a 300 s-hung provider is abandoned in ~deadline and the run
  completes on a healthy tier.

---

## 5. Secret handling

- **Keys only from the environment** — `.env` (gitignored) or the shell; never
  hardcoded. `check_api_keys()` flags placeholder/short keys at startup.
- **Pre-commit hook** (`.git/hooks/pre-commit`) blocks any commit whose staged
  diff adds an API-key-shaped string (OpenRouter/Anthropic/Groq/Google/OpenAI
  patterns) and refuses to commit a `.env` file. Tested: blocks a planted key,
  passes clean files.
- **History clean**: `git log --all` for `*.env`/`*.pem`/`*key*` and a
  full-history regex scan for key patterns → **0 real keys** in tree or history
  (the only match is a vendored `certifi` CA bundle, not a secret).
- **`.env.example`** documents every key and ops override with placeholders, so
  a new developer configures the system without ever seeing a real secret.

---

## 6. Operational resilience (disk / memory / CPU exhaustion)

- **Disk**: `cache_janitor.enforce_cache_limit()` caps `.cache` + `.llm_cache`
  at 10 GB (`SOLLIA_CACHE_MAX_GB`) with LRU eviction, run at pipeline start. Run
  archives + index logs are protected from eviction.
- **Memory / runaway work**: the per-run SEC fetch budget (200) and LLM call
  budget (300) bound how much a single run can pull, so no run can fetch "a
  million filings".
- **Provider resilience**: 8-tier chain across 3 independent providers (Groq /
  Gemini / OpenRouter). Tested with Groq dead, OpenRouter dead (Fable 5
  unreachable → falls to Groq), and all-dead (clean typed error, no hang).

---

## Re-verification checklist

```bash
cd sollia_working
python3.11 tools/health_check.py                 # keys, SEC, caches, imports, budgets
SOLLIA_LLM_NO_CACHE=1 python3.11 tools/injection_test.py   # 15/15 injection tests
python3.11 -m pip_audit -r requirements.txt      # dependency CVEs (0 known)
git commit ...                                   # pre-commit hook blocks planted keys
```
