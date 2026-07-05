# LLM Routing Report — Multi-Key Wiring, Rate-Limit Research, JSON Robustness, Speed

**Date:** 2026-07-03 · **Branch:** optimization-pass
**Scope:** Part 1 (multi-key + rate limits), Part 2 (JSON robustness) and Part 3
(speed) of the speed/UX/robustness pass.

---

## 1. Key Verification (Part 1b)

Method: every Groq key got a 1-token ping to `llama-3.1-8b-instant`; every
OpenRouter key got `GET /auth/key` + `GET /credits`. Script:
`scratchpad/verify_keys.py`, run 2026-07-03.

| Key | Status | Notes |
|---|---|---|
| `GROQ_API_KEY` (gsk_6Lq…8h1w, pre-existing) | ✅ 200 | kept as primary |
| Task's "existing mac studio" gsk_8dWK…ttOv | ❌ **401 Invalid API Key** | **NOT wired** — dead |
| `GROQ_API_KEY_2` … `GROQ_API_KEY_11` (10 new) | ✅ 200 all | wired |
| `OPENROUTER_API_KEY` (sk-or-v1-5b98…056) | ✅ 200 | funded acct: $20.00 purchased, $13.67 used, **$6.33 remaining** |
| `OPENROUTER_API_KEY_2…_6, _8, _9` (7 new; no `_7` in the task list) | ✅ 200 all | **same account** as the funded key |

**Bottom line: 11/12 Groq keys live, 8/8 OpenRouter keys live.** All are wired
into `LLM_PROVIDERS` via `key_envs` (`_GROQ_KEYS` / `_OPENROUTER_KEYS` in
`llm_config.py`); `.env` backup at `.env.bak.2026-07-03`.

---

## 2. Rate-Limit Research (Part 1c)

### 2.1 Groq — documented (console.groq.com/docs/rate-limits) and confirmed by headers

| Model | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `llama-3.3-70b-versatile` | 30 | 1,000 | 12,000 | 100,000 |
| `llama-3.1-8b-instant` | 30 | 14,400 | 6,000 | 500,000 |

- Limits are enforced **per ORGANIZATION, not per key** (Groq docs state this
  explicitly; empirically confirmed below).
- Per-**model** buckets are separate: 70B and 8B don't share TPM, so the two
  Groq tiers together give ~18K TPM.
- No separate concurrent-request cap was observed below the RPM ceiling
  (sequential hammering at ~7 req/s was accepted until RPM tripped).

### 2.2 Groq — empirical multi-key test (`scratchpad/groq_hammer.py`)

- **1 key hammered:** exactly **30 × 200, then 429 at request #31** (6.1s).
  RPM=30 confirmed to the request.
- **3 keys round-robin:** only **14 × 200 combined** before a 429 — and at that
  instant the other two keys **429'd on immediate probe**.
- The initial 11-key ping also showed one shared daily counter decrementing
  across all keys (14,399 → 14,389 in ping order).

**Verdict: all 11 Groq keys live in ONE organization → one shared 30 RPM /
12K-TPM (70B) / 6K-TPM (8B) bucket. Total throughput with 11 keys = 1×, not
11×.** The task's premise ("effective TPM ~144K with 12 keys") does not hold
for these keys. To actually multiply Groq throughput, keys must come from
separate Groq **accounts** (each its own org). The wiring is in place and will
scale automatically the day such keys are dropped into `.env`.

### 2.3 OpenRouter — documented + empirical

- All 8 keys report **identical** account state (`GET /credits`: $20.00
  purchased / $13.67 used / $6.33 balance) → **one account, 8 keys**.
  OpenRouter enforces limits **per account**, not per key.
- `:free` models: **20 req/min** and — because the account has purchased ≥$10 —
  **1,000 req/day** account-wide (`free-models-per-day-high-balance`).
- Empirical 4-key test (`scratchpad/or_free_test.py`): the very first `:free`
  request returned 429 with `X-RateLimit-Limit: 1000, Remaining: 0` — today's
  shared daily pool was already consumed (by earlier pipeline fallbacks);
  probes on two other keys 429'd identically. **Shared pool confirmed; keys
  429 together, not independently.** Resets midnight UTC.
- **Paid** models (Fable 5): no daily cap; throughput scales with credit
  balance (~account-balance requests/sec — far above pipeline needs). Every
  one of the 8 keys can bill the single funded balance, so Fable-5 key
  rotation can never land on an "unfunded" key — but a runaway would also
  drain the balance regardless of which key is used. The **$1.00/run hard cap**
  (Part 1f, `MAX_COST_PER_RUN_USD`) is the guard.

### 2.4 What this means for the chain (implemented)

1. **All keys wired anyway** — rotation costs nothing, survives a key
   revocation, and gives automatic N× scaling if keys are ever re-issued from
   separate accounts.
2. **`_KEY_429_CUTOFF = 2`:** with a shared bucket, a second consecutive 429
   proves the bucket is dry for every remaining key; the old behavior would
   have burned ~11 doomed round-trips (~5s) per chain pass. Auth errors
   (401/402/403) still advance to the next key so one dead key can't kill a
   tier.
3. **Real speed levers** (since key-count is not one): byte-identical LLM
   cache, per-model TPM split (8B for high-volume stage-1, 70B for the rest),
   the Gemini account as a genuinely independent 429 refuge, semaphore(3) to
   stay inside TPM windows, and cache-first SEC fetches.

---

## 3. Routing Policy (Part 1e — implemented in `llm_config.py`)

- **Fable 5** is reachable ONLY via: (a) `pin_tier=True` — Agent 1's severity
  audit, ~1 call/run, ~$0.037; (b) `requires_long_context` prompts **>90K
  words** (beyond Groq's 128K-token context). `corporate_discrepancy`'s
  nominal tier moved 0 → 1 to close the third (unintended) path where an
  unflagged ≥2,000-word prompt started at tier 0. Agent 4 stays off Fable 5
  (measured 0/3 clean JSON on its long strict-JSON shape vs Groq-70B's 3/3).
- **Groq-70B:** all `requires_strict_json` tasks and all medium/long prompts
  (>1,500 words).
- **Groq-8B:** high-volume simple classification (Agent 2 Stage 1, Agent 5
  sector selection).
- **Gemini + OpenRouter `:free` tiers:** 429-fallthrough only.
- **Cost cap (Part 1f):** `MAX_COST_PER_RUN_USD` **$2.00 → $1.00**. Expected
  actual spend ≈ $0.04/run.

---

## 4. JSON Format Research (Part 2a) — what each model ACTUALLY returns

Method: the REAL Agent-4 risk prompt (benchmark fixture `test4`, 1,967 words,
max_tokens 2500) sent to each chain model over direct HTTP (no chain
fallthrough — every sample is genuinely from the target model), raw response
recorded before any parsing. Tool: `tools/json_format_research.py` (resumable);
raw samples in `.test_results/json_format_research/`.

| Model | Clean JSON | Fenced | Preamble | Trailing comma | Single quotes | Comments | Truncated | Other |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|---|
| Fable5 | 3/3 | 0 | 0 | 0 | 0 | 0 | 0 | — |
| Groq-70B | 4/4 | 0 | 0 | 0 | 0 | 0 | 0 | byte-identical responses |
| Groq-8B | 5/5 | 0 | 0 | 0 | 0 | 0 | 0 | byte-identical responses |
| Gemini-2.5-flash | 0/3 | 2/3 | 0 | 0 | 0 | 0 | **3/3** | before the fix — see below |
| OR-Gemma4-31B | 2/2 | 0 | 0 | 0 | 0 | 0 | 0 | — |
| OR-Gemma4-26B | — | — | — | — | — | — | — | 0 samples: `:free` slug "temporarily rate-limited **upstream**" all day |
| OR-Nemotron3-Super | 3/5 | 2/5 | 0 | 0 | 0 | 0 | 0 | 2× NO-JSON (prose); "clean" ones were `[]` |
| OR-Llama33-70B | 1/1 | 0 | 0 | 0 | 0 | 0 | 0 | plus one transient 402 |

Sample-count notes: Fable5 capped at 3 fresh samples (paid ~$0.13/call; adds to
the 3 prior routing-benchmark samples of the longer 2.8k-word shape). Gemini
capped at 3 by its daily quota. Groq-70B at 4 (samples byte-identical; the 5th
was TPM-starved and adds no information). Gemma-26B returned upstream-capacity
429s for hours — a **third** `:free` rate-limit layer (provider capacity shared
across ALL OpenRouter users) independent of the account's 20 RPM / 1000-per-day.

**Headline findings:**

1. **Gemini's truncation class is thinking-starvation, now fixed at the request
   layer.** 2.5-flash thinks by default on the OpenAI-compat endpoint and
   thinking bills against `max_tokens` → every long-JSON call ended
   `finish=length` at ~400 chars, 2/3 additionally inside an UNCLOSED ```json
   fence. Fix: `payload_extra: {"reasoning_effort": "none"}` on the Gemini
   provider. **Verified live** in the Part-2d rerun: 5/5 Gemini A4 responses
   clean, complete, ~4.4s.
2. **Fable 5 returned 3/3 clean JSON on this 1,967-word shape** — its measured
   0/3 failure is specific to the LONGER (~2.8k-word) variant (fenced / empty /
   truncated, routing_benchmark 2026-07-03). Routing unchanged: A4 stays on
   Groq-70B.
3. **Nemotron cannot do the A4 strict-JSON shape** (prose, truncation, or empty
   arrays; 0/5 useful across sweep + burst test) → `json_weak: True`, and the
   chain now skips json_weak providers for `requires_strict_json` calls.

## 5. Extractor Hardening + Pre-Parse Validation (Part 2b/2c)

`src/utils/json_extractor.py` additions, each covered in
`tools/json_extractor_test.py` (**45 tests, 45 pass**):

- **Truncation completion** (`_complete_truncated`): a response cut mid-array /
  mid-string / mid-key (the Gemini class; any thinking model can produce it)
  is completed by dropping the incomplete trailing element and closing the
  root. Never invents content — the result is a strict prefix of the model's
  output plus closers. Applied ONLY to the first unterminated region; a
  truncated array can no longer "win" as its first inner balanced object
  (which previously returned a dict where the agent expected a list).
- **Unclosed-fence handling**: fenced-but-truncated payloads recover through
  the same path (the balanced-region scan sees through the missing closing
  fence).
- **Pre-parse validation** (`preparse_validate`, Part 2c): classifies a raw
  response BEFORE extraction — `no_json` (no bracket anywhere), `error_response`
  (429/rate-limit/error prose), `provider_error_body` (`{"error": …}` envelopes,
  which are valid JSON but never data), `long_response_no_array` (>10k chars
  without `[`). Wired INSIDE `extract_json` so all agents get it with zero
  call-site changes; also exported for direct use. Typed ValueErrors feed the
  agents' existing parse-failure retry (`bypass_cache=True`) paths.
- Real-sample validation: 12/15 sweep samples parse (the 3 rejects are Gemini
  pre-fix responses truncated inside their FIRST element — nothing to salvage,
  correctly refused rather than fabricated).

## 6. Parse-Failure Verification (Part 2d) — 0% across all three shapes

`tools/parse_failure_verification.py`: each agent's REAL recorded prompt
replayed 10× through the production chain (`call_llm(bypass_cache=True)` — all
live responses, exactly what agents receive), every response through
`extract_json` + schema check. This is a deliberate BURST (10 jumbo calls
back-to-back), denser than any real pipeline run, so it stress-tests 429
fallthrough into the backup tiers as well.

| Shape | Result |
|---|---|
| Agent 1 discrepancy classify (NVDA-class) | **10/10, 0% parse failures** |
| Agent 2 Stage-1 promise extraction (AMD-class) | **10/10, 0% parse failures** |
| Agent 4 risk delta (TSLA-class strict JSON) | first run 8/10 — both failures from Nemotron after deep 429 fallthrough → `json_weak` skip added → **rerun 10/10, 0% parse failures** |

The first A4 run was the productive failure: under total Groq saturation the
chain fell through to tier #7 (Nemotron), whose responses on that shape are
prose or `[]`. `preparse_validate` classified the prose correctly (`no_json`),
and the `json_weak` routing rule now keeps strict-JSON tasks off that model
entirely. The rerun's fallthrough landed on Gemini (fixed) and Gemma-31B
instead: zero failures under the same saturation.

## 7. Network-Condition Testing — router calibration under stress

Conditions 2–5 are CHAIN-level behaviors (all 8 providers share one request
path: `_post_with_wallclock` → key cycling → fallthrough), so they were
exercised against the REAL chain code with local mock providers
(`tools/network_condition_test.py`) — deterministic, free, and repeatable
without burning the day's provider quotas. Real-provider stress evidence was
captured live earlier in the pass and is cited per row. **9/9 scenarios pass**
(`.test_results/network_condition_results.json`).

| Condition | Served by | Attempts | Wall | Verdict |
|---|---|---|---|---|
| NORMAL (control) | primary | 1 | 0.21s | baseline |
| RATE-LIMITED (429 on every key) | fallback | 3 | 0.21s | key cycling fires; `_KEY_429_CUTOFF=2` stops doomed cycling; clean fallthrough. REAL evidence: Groq org-storm (hammer test — 3 keys 429 together), OR account + upstream 429s, Part-2d saturation burst |
| PROVIDER DOWN (connection refused) | fallback | 2 | 0.21s | instant advance |
| PROVIDER 404 (bad endpoint) | fallback | 2 | 0.21s | recorded + advance. REAL evidence: transient OR 402 — key cycling now advances on 401/402/403 |
| HIGH LATENCY (5s < read timeout) | primary | 1 | 5.0s | served; wall-clock guard correctly did NOT fire |
| READ TIMEOUT (5s > 2s timeout) | fallback | 2 | 2.21s | socket timeout advances |
| SLOW TRICKLE (Cloudflare-hang class: headers + 1 byte/2s forever) | fallback | 2 | **timeout+slack exactly** (8.21s at test slack 5; production = timeout+15s) | read timeout NEVER fires — only the wall-clock guard ends it, and it did |
| HANG MEMO | — | — | — | a wall-clock breach is remembered 10 min: that provider's next read timeout clamps to 30s |
| HANG REROUTE | — | — | — | `select_best_tier` stops promoting >90k-word calls to a hung Fable5 (Groq-70B serves) |

**Response quality under degradation:** quality follows the tier that ends up
serving — measured in the §4 format table and §6 burst test. Under total Groq
saturation the chain now lands on Gemini (fixed, clean) or Gemma-31B (clean)
instead of Nemotron (prose/empty), because strict-JSON calls skip `json_weak`
providers. That is the router change that turned the A4 burst from 8/10 into
10/10.

**`select_best_tier` now weights reliability under stress, not just clean-JSON
rate** (implemented + unit-verified):

1. **Hang memo** (`_note_hard_timeout` / `_recently_hung`): a provider that
   breached the wall-clock guard loses its >90k-word promotion for 10 minutes
   and gets a clamped 30s read timeout — a repeat Cloudflare-class stall costs
   ~35s, not 180+15s. Fable 5 is the canonical case: 3/3 clean JSON on the
   1,967-word shape, but its transport produced the 2026-07-02 13-minute
   production hang — "99% clean that can stall" loses to "95% clean that
   always answers".
2. **`json_weak` skip**: models measured incapable of a strict-JSON shape are
   never handed one, including on 429 fallthrough (Nemotron).
3. **Fallback ORDER embodies stress data**: Groq-70B/8B (never observed
   hanging, worst case ~4s, org 429s recover in one TPM window) stay primary;
   Gemini (independent account, truncation fixed) beats the `:free` tiers,
   whose availability is hostage to a third, provider-side capacity limit.

## 8. Speed Tests (Part 3)

_See section appended after the speed-test phase below._

---

# 9. Bulletproofing Pass (2026-07-03, evening) — 3 Groq Orgs + Network-Aware Router

## 9.1 Official rate limits re-verified from PRIMARY sources (Part 4a)

**Groq** (console.groq.com/docs/rate-limits, fetched live):

| Model | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `llama-3.3-70b-versatile` | 30 | 1,000 | 12,000 | 100,000 |
| `llama-3.1-8b-instant` | 30 | 14,400 | 6,000 | 500,000 |

- **"Rate limits apply at the organization level, not individual users."** (verbatim)
- No concurrent-request cap documented; none observed below the RPM ceiling.
- Free plan = base limits; Developer plan raises them for select workloads.

**OpenRouter** (openrouter.ai/docs/api/reference/limits, fetched live):
- `:free` models: **20 requests/min**; **1,000 requests/day** with ≥$10 purchased
  (50/day below $10).
- **"Making additional accounts or API keys will not affect your rate limits,
  as we govern capacity globally."** (verbatim) — per-ACCOUNT enforcement.
- Paid models: no documented cap; Cloudflare DDoS layer; 402 on negative balance.

## 9.2 THREE Groq orgs wired and empirically verified (Part 5)

`.env` now carries 17 Groq keys tagged by org in `GROQ_ORG_KEYS`:
`mac_studio` (11 keys), `berkeley` (3), `heysollia` (3).

**Org-fingerprint proof** (1-token pings, x-ratelimit-remaining-requests):
berkeley keys decremented ONE shared daily counter (14399→14398→14397),
heysollia decremented its OWN (14399→14398→14397), mac_studio untouched →
**three independent buckets**. Effective Groq capacity is now genuinely 3×:
90 RPM, 36k TPM (70B), 18k TPM (8B), 3k RPD (70B).

**Live 429-storm rescue** (`scratchpad/org_storm_test.py`):
1. mac_studio org hammered on 8B → first 429 at request **#31** (RPM=30
   confirmed to the request, matching the docs).
2. Second mac_studio key mid-storm → **429** (shared bucket re-confirmed).
3. berkeley key mid-storm → **200 in 303ms**; heysollia → **200 in 228ms**.
4. PRODUCTION chain call mid-storm → served by **Groq-8B in 0.5s,
   attempts=2** — the org-hop (mac 429 → berkeley 200). Before this pass the
   chain would have burned same-org keys and fallen through to Gemini.

## 9.3 Key cycling is now ORG-aware

- `_GROQ_KEYS` is org-INTERLEAVED (`_interleave_org_keys`): consecutive keys
  belong to different orgs, so "next key" = "next rate-limit bucket".
- On a 429, every remaining key of THAT org is skipped for the pass; the
  provider is abandoned only when ALL its orgs have 429'd. Single-account
  providers (OpenRouter, Gemini) abandon on the first 429 — measured, their
  keys 429 together. `_KEY_429_CUTOFF` is deprecated (kept for env compat).
- `_rotated()` sinks keys of congested/down orgs to the back of the attempt
  order (healthy-first), preserving interleave within each health group.

## 9.4 NetworkMonitor — live network-state detector (Part 4e)

`NETWORK_MONITOR` tracks per rate-limit DOMAIN — ("groq", org) /
("openrouter", "account") / ("gemini", "account"):

| State | Trigger | Expiry |
|---|---|---|
| congested | ≥3 consecutive 429s (or huge Retry-After) | 75s (one RPM window + slack), up to 1h on daily-pool 429s |
| degraded | ≥3 consecutive 200s slower than 10s | 300s or one fast success |
| down | ≥2 transport errors, or ONE wall-clock hang | 120s |

Fed from inside the chain's key loop (every 200/429/exception/hard-timeout);
consumed by `select_best_tier`, `_rotated`, and a chain-level fast-skip of
DOWN providers (the last tier is always tried — fail-open).

## 9.5 select_best_tier — the six-dimension decision engine (Part 4d)

Priority order (all thresholds empirical; 28/28 unit checks pass):
1. OpenRouter down → never start at Fable 5 / OR-* (same transport).
2. All 3 Groq orgs congested → start at Gemini (the only real refuge
   measured under total Groq saturation; strict-JSON safe post-fix).
3. >90k words + long-context → Fable 5 (200k ctx) UNLESS hung/down/
   congested/cost-gated → then **Gemini (1M ctx)** — it actually FITS the
   prompt, unlike Groq's 128k (this upgraded the old Groq-70B reroute).
4. Strict JSON → Groq-70B, never below (8B measured 0/3 on indices).
5. >1500 words → Groq-70B; else Groq-8B.
6. COST GATE (Part 4f): run spend ≥80% of the $1.00 cap → paid promotion
   off (pinned severity audit exempt until the hard cap).

Chain-level suite (mock providers, REAL chain code): **9/9 scenarios pass**
— normal, 429-storm, provider-down, 404, high-latency, read-timeout,
slow-trickle (wall-clock guard at timeout+slack), hang-memo, hang-reroute.

## 9.6 SEC backup research is documented in SEC_BACKUP_SOURCES.md

## 9.7 Field fix from live stress (2026-07-04): congestion-memo TTL cap

Observed live during the edge-case re-runs: a transient Groq RPD dip (rolling
24h window, heavy test day) returned an hours-scale Retry-After; the monitor
stamped all Groq orgs "congested" for its full 3600s cap, so every call
STARTED at Gemini (daily pool dry until 07:00 UTC) and crawled down to
OR-`:free` (39s Gemma-26B calls, one full-chain FAIL) — while direct pings
showed all 3 Groq orgs serving 200s with full buckets again.

Fix: `CONGESTED_TTL_MAX = 300s` — a Retry-After-driven memo now expires
within 5 minutes and Groq gets re-probed. A wrong probe costs one ~200ms
429; the old behavior cost up to an hour of 40s-per-call fallback crawl.
Verified: unit test (10800s Retry-After → 300s memo) + the live batch
recovered onto Groq-8B the minute its in-process memo expired.
