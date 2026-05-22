# Sollia — Complete Codebase Reference

> Auto-generated 2026-05-21. Intended as a full ground-truth reference for AI agents, reviewers, or test writers. Describes every file, every function, every data model, every external call, and every edge-case behavior.

---

## 1. Project Overview

**Sollia** is an agentic investment analysis platform. Given a US-listed ticker symbol, it runs two or three independent AI agents, combines their signals into a single bullishness score (20–80), and classifies the result as BUY / HOLD / SELL.

### Dual UI

| Surface | Framework | Entry point |
|---|---|---|
| Streamlit dashboard | Python / Streamlit | `sollia_working/app.py` |
| Web app (React + FastAPI) | Vite 8 / React 19 / FastAPI | `sollia_web/backend/main.py` + `sollia_web/frontend/` |

### Three Agents

| ID | Name | Score key | What it measures |
|---|---|---|---|
| `forensic` | ForensicAccountant | `s1` (20–80) | Gap between CEO earnings-call tone and 10-K financial reality |
| `narrative` | NarrativeScout | `s2` (20–80) | Recency-weighted news sentiment vs historical baseline |
| `follow_through` | FollowThroughAgent | `score` (0–100) | Whether specific CEO promises in filings/transcripts were later kept |

### Score Composition (Synthesizer)

```
track1  = similarity-weighted average of s1 and s2
s_final = 50 + (track1 - 50) × (1 - 0.2 × |corroboration|)
s_final clamped to [20, 80]

BUY   → s_final >= 62
HOLD  → 39 <= s_final < 62
SELL  → s_final < 39
```

---

## 2. Full File Tree

```
/Users/ardaturgut/Desktop/Sollia/
│
├── .git/
├── .gitignore
├── gen_ui.py                          # standalone HTML report generator
│
├── sollia_working/                    # PRIMARY BACKEND (Streamlit app)
│   ├── app.py                         # Streamlit entry point
│   ├── app_simple.py                  # alternate simplified entry
│   ├── app_minimal.py                 # alternate minimal entry
│   ├── app_full.py                    # alternate full entry
│   │
│   ├── config/
│   │   └── settings.py                # pydantic-settings config, API keys
│   │
│   └── src/
│       ├── __init__.py
│       ├── schemas.py                 # ALL Pydantic models
│       │
│       ├── agents/
│       │   ├── __init__.py
│       │   ├── forensic_accountant.py # Agent 1 — CEO tone vs 10-K reality
│       │   ├── narrative_scout.py     # Agent 2 — news sentiment
│       │   ├── follow_through.py      # Agent 3 — promise tracking (modified)
│       │   └── synthesizer.py         # Final score combiner
│       │
│       ├── pipeline/
│       │   ├── __init__.py
│       │   └── orchestrator.py        # Generator pipeline, streams events
│       │
│       ├── providers/
│       │   ├── __init__.py
│       │   ├── filings/
│       │   │   ├── __init__.py
│       │   │   └── sec_edgar.py       # SEC EDGAR 10-K fetcher + MD&A parser
│       │   ├── news/
│       │   │   ├── __init__.py
│       │   │   └── finnhub.py         # News: Google News → Finnhub → Yahoo RSS
│       │   └── transcripts/
│       │       ├── __init__.py
│       │       └── fmp.py             # Transcripts: SEC 8-K → StockAnalysis → Nasdaq → Yahoo
│       │
│       ├── ui/
│       │   ├── __init__.py
│       │   ├── human_view.py          # Streamlit result renderer
│       │   ├── state.py               # Session state initializer
│       │   └── styles.py              # Injected CSS
│       │
│       ├── .filing_cache/             # disk cache for 10-K/10-Q/8-K URLs
│       └── .transcript_cache/         # disk cache for earnings transcripts
│
└── sollia_web/                        # WEB STACK (FastAPI + React)
    ├── backend/
    │   ├── main.py                    # FastAPI server
    │   └── agents.py                  # Agent registry + runner
    └── frontend/
        ├── package.json               # React 19, Vite 8, TailwindCSS 4, Three.js
        ├── src/
        │   ├── components/
        │   └── assets/
        └── dist/                      # Built React bundle (git-ignored)
```

---

## 3. Configuration — `config/settings.py`

```python
class Settings(BaseSettings):
    fmp_api_key: str = "LWZY8x04bdD4qd0KIhkmvL87TGWjB5hT"
    finnhub_api_key: str = "d7lgkphr01qm7o0bpebgd7lgkphr01qm7o0bpec0"
    openai_api_key: str = ""          # unused — Ollama used instead
    cache_dir: str = "/tmp/sollia_cache"
    newsapi_key: str = ""             # unused
    alphavantage_key: str = ""        # unused

settings = Settings()
```

**Import pattern:** `from config.settings import settings` then `settings.finnhub_api_key`.

---

## 4. Data Models — `src/schemas.py`

All models use `pydantic.BaseModel`. All fields have defaults (no required fields), making them safe to instantiate empty.

### `AgentLogMessage`
```
speaker: str = ""
timestamp: datetime = Field(default_factory=datetime.now)
message: str = ""
```
Used for real-time streaming in Streamlit and collecting logs.

### `TranscriptResult`
```
ticker, company_name, fiscal_period, fiscal_period_end, date: str
content: str           # raw transcript text (up to 50 000 chars)
source: str            # "SEC-8K" | "StockAnalysis" | "Nasdaq" | "Yahoo Finance"
is_proxy: bool = False
speaker_sections: List[Dict[str, str]]  # [{speaker, text}, ...]
source_url: str
```

### `FilingResult`
```
ticker, company_name, cik: str
filing_type: str = "10-K"
period_of_report, filing_date, accession_number, htm_url: str
mda_text: str      # extracted MD&A section (up to 30 000 chars)
full_text: str     # first 80 000 chars of raw HTML → stripped text
ceo_name: str      # extracted from filing signature
```

### `SolliaToneMetrics`
```
hedging_density: float          # hedging words per 1000 words
confidence_ratio: float         # (we will + we are) / max(we expect + we hope, 1)
sentence_length_mean: float
sentence_length_variance: float
forward_looking_density: float  # forward-looking words per 1000 words
overall_sentiment: float        # -1.0 to +1.0 (Ollama or fallback)
raw_score: float                # composite: 0.45*confidence - 0.25*hedging + 0.30*sentiment
```

### `SolliaRealityMetrics`
```
revenue_direction: int      # -1, 0, +1
revenue_magnitude: float    # percentage growth detected (0 if not found)
raw_score: float            # direction * max(magnitude/20, 0.2)
context_snippet: str        # first matched revenue sentence from MD&A
```

### `SolliaQuarterlyPair`
```
fiscal_period, transcript_date, filing_date: str
tone: Optional[SolliaToneMetrics]
reality: Optional[SolliaRealityMetrics]
gap: float          # reality.raw_score - tone.raw_score  (positive = CEO underpromising = bullish)
rolling_sigma: float
```

### `SolliaDiscrepancySignal`
```
rank: int
fiscal_period: str
severity: str           # "RED" | "AMBER" | "GREEN"
severity_color: str     # hex color
description: str        # human-readable gap description
transcript_citation: str    # first 5 words of transcript sentence
filing_citation: str        # first 5 words of filing sentence
htm_link: str           # SEC EDGAR filing URL
transcript_url: str
```

### `SolliaSentimentSignal`
```
rank: int
headline, url, source, date: str
sentiment_score: float          # -1.0 to +1.0
weighted_contribution: float    # |sentiment * recency_weight|
relevance: str                  # "Strong bullish signal with recency weight X.XXX"
```

### `SolliaForensicResult`
```
ticker, ceo_name, ceo_start_date: str
historical_start, historical_end: str
quarterly_pairs: List[SolliaQuarterlyPair]
baseline_mean, baseline_stddev: float
current_gap, sigma_1: float
s1: float = 50.0                # final forensic score 20-80
direct_contradiction: bool
contradiction_detail: str
top_signals: List[SolliaDiscrepancySignal]   # up to 5
discrepancy_metric: float
discrepancy_label: str          # human-readable, e.g. "+0.18 [bullish | CEO underpromising]"
conclusion: str
log_messages: List[AgentLogMessage]
fallback_used: bool             # True if MD&A used instead of transcript
fallback_detail: str
```

### `SolliaNarrativeResult`
```
ticker, historical_start, historical_end: str
total_articles, filtered_articles: int
baseline_mean, baseline_stddev: float
test_weighted_sentiment: float
sigma_2: float
s2: float = 50.0
top_signals: List[SolliaSentimentSignal]    # up to 5
sentiment_metric: float         # test_ws - base_mean
sentiment_label: str            # human-readable
conclusion: str
log_messages: List[AgentLogMessage]
providers_used: List[str]       # e.g. ["GoogleNews", "Finnhub"]
```

### `Headline`
```
headline, url, source, date: str
sentiment: float = 0.0
weight: float = 1.0
```

### `SolliaAnalysisResult` (final output)
```
ticker, company_name: str
timestamp: datetime
s_final: float = 50.0
signal: str = "HOLD"            # "BUY" | "HOLD" | "SELL"
signal_color: str = "#F59E0B"   # green | amber | red
agents_run: List[str]           # e.g. ["ForensicAccountant", "NarrativeScout"]
forensic: Optional[SolliaForensicResult]
narrative: Optional[SolliaNarrativeResult]
executive_summary: str
track1_score: float = 50.0
corroboration: float = 0.0
track1_detail, track2_detail: Optional[Dict[str, Any]]
log_messages: List[AgentLogMessage]
export_data: Optional[Dict[str, Any]]    # model_dump() for JSON export
```

---

## 5. Agent 1 — `ForensicAccountant` (`src/agents/forensic_accountant.py`)

### Purpose
Detects gaps between what the CEO says in earnings calls and what the 10-K filing actually reports.

### External dependencies
- Local Ollama (llama3.2) at `http://localhost:11434/api/generate` — for sentiment scoring and tone attribution
- SEC EDGAR (via `SECEdgarProvider`)
- FMP earnings transcripts (via `FMPTranscriptProvider`)
- Python stdlib: `re`, `math`, `statistics`, `concurrent.futures`

### Module-level constants
```python
HEDGING_WORDS = ["would", "could", "may", "subject to", "uncertainties", "believe"]
FORWARD_LOOKING_WORDS = ["expect", "anticipate", "outlook", "guidance", "forecast", "will", "plan", "intend"]
BULLISH_REVENUE = [5 regex patterns for revenue increase language]
BEARISH_REVENUE = [2 regex patterns for revenue decline language]
CONTRADICTION_PAIRS = [5 (positive_phrase, negative_regex) tuples]
```

### Module-level functions

#### `_add_log(msgs, text)` → None
Appends `AgentLogMessage(speaker="CEO vs 10-K", message=text)` to list and calls `logger.info`.

#### `_ask_ollama(prompt, timeout=25)` → Optional[str]
POST to `http://localhost:11434/api/generate` with `model=llama3.2, stream=False`. Returns `response` field or `None` on error.

#### `_extract_tone(text, use_ollama=True)` → SolliaToneMetrics
1. Counts hedging/forward-looking words per 1000 words.
2. Computes `confidence_ratio = (we will + we are) / max(we expect + we hope, 1)`.
3. Splits text into sentences, computes length mean/variance.
4. If `use_ollama`: asks Ollama for sentiment (-1.0 to +1.0) with fallback to confidence_ratio.
5. `raw_score = 0.45*confidence - min(hedging/60, 0.3)*0.25 + 0.30*sentiment`, clamped [-1, 1].

#### `_extract_reality(mda)` → SolliaRealityMetrics
1. Collects all "revenue" contexts (250 chars each) from MD&A.
2. Checks BEARISH_REVENUE then BULLISH_REVENUE patterns → `direction ∈ {-1, 0, 1}`.
3. Extracts percentage magnitude from growth language.
4. `raw_score = direction * max(magnitude/20, 0.2)` — floors at 0.2 to avoid 0 when direction known.
5. Returns first non-boilerplate revenue sentence as `context_snippet`.

#### `_severity_label(sigma)` → Tuple[str, str]
```
|sigma| >= 2.0 → ("RED", "#EF4444")
|sigma| >= 1.0 → ("AMBER", "#F59E0B")
else            → ("GREEN", "#22C55E")
```

#### `_split_sentences(text)` → List[str]
Splits on `(?<=[.!?])\s+(?=[A-Z])` and merges lines ending in common abbreviations (Mr, Dr, U.S, Inc, etc.).

#### `_balanced_sentence(s)` → bool
Returns True if sentence has a financial keyword + number + (direction claim OR forward-looking language).

#### `_claim_based_sentence(s)` → bool
Returns True if sentence >= 50 chars, has a number, a causal word ("was driven by", "resulted"), a direction word, and no excessive `$` signs (table rows excluded).

#### `_tone_attribution(transcript_text, sentences)` → List[float]
Calls Ollama: given document-level tone score, rate each sentence from -2 to +2. Returns list of floats matching sentence count. Falls back to rule-based pos/neg word counts on Ollama failure.

#### `_check_contradictions(transcript, mda)` → Tuple[bool, str]
Checks each `(positive_phrase, negative_regex)` pair from CONTRADICTION_PAIRS. If transcript contains the positive phrase AND MD&A matches the negative pattern → returns `(True, explanation)`.

#### `_first5(text)` → str
Strips SEC exhibit header boilerplate, returns first 5 words + "..." for citation display.

### Class: `ForensicAccountant`

#### `__init__`
Instantiates `SECEdgarProvider`, `FMPTranscriptProvider`, `_log: List[AgentLogMessage]`, `_reality_cache: Dict`.

#### `run(ticker)` → SolliaForensicResult
Step-by-step:
1. CIK lookup via EDGAR.
2. Fetch up to 12 most recent 10-K filings in parallel (4 threads, 0.15s stagger).
3. Extract CEO name from most recent filing; walk older filings to find CEO tenure start.
4. Fetch all earnings transcripts from FMP; deduplicate by fiscal period; filter to pre-filing.
5. Compute `hist_start` = min(ceo_start, inception_date, oldest_transcript_date), capped at 6 years back.
6. Build `SolliaQuarterlyPair` list by matching each transcript to its closest-date filing.
7. Compute baseline `mean`/`stddev` of historical `gap` values.
8. Compute rolling sigma for each pair.
9. Analyze current period: take most recent transcript, extract `cur_tone` (with Ollama), extract `cur_reality`.
10. `cur_gap = cur_reality.raw_score - cur_tone.raw_score` (positive = CEO underpromising = bullish).
11. `sigma1 = (cur_gap - base_mean) / baseline_stddev`.
12. `s1 = 50 + 30 * tanh(sigma1 / 2)`, clamped [20, 80].
13. Check direct contradictions; if found, force `s1 = 20`.
14. Build top 5 discrepancy signals via V2 sentence-level pairing:
    - Get claim-based sentences from 10-K MD&A.
    - Get balanced sentences from transcript.
    - Call `_tone_attribution` for per-sentence tone scores.
    - For each (transcript_sentence, mda_sentence) with keyword overlap: compute `gap = reality.raw_score - tone_score`, then `sigma = (gap - baseline_mean) / baseline_stddev`.
    - Cluster by transcript sentence (first 80 chars), keep best 10-K match per cluster.
    - Sort by |sigma| descending, keep top 5 with |sigma| > 1.
    - Falls back to V1 behavior (repeating current_pair) if no sentence pairs found.
15. Build `discrepancy_label` and `conclusion` strings.
16. Return `SolliaForensicResult`.

#### `_fetch_filings_parallel(ticker, filings_info)` → List[FilingResult]
Uses `ThreadPoolExecutor(max_workers=4)`. Each thread calls `self._edgar.get_filing_result(ticker, info)` with `time.sleep(idx * 0.15)` stagger. Returns sorted by original index.

#### `_cached_reality(filing)` → SolliaRealityMetrics
Caches `_extract_reality(filing.mda_text)` by `filing_date or accession_number`.

#### `_build_pairs(hist_transcripts, filings, hist_start, hist_end)` → List[SolliaQuarterlyPair]
For each transcript in date range: finds the closest filing whose `period_of_report <= transcript_date`. Computes tone (no Ollama for speed) and reality. Returns list sorted by `transcript_date`.

#### `_best_current_transcript(transcripts, most_recent_filing)` → Optional[TranscriptResult]
Returns the most recent transcript (highest date). Simple sort, no date filtering.

---

## 6. Agent 2 — `NarrativeScout` (`src/agents/narrative_scout.py`)

### Purpose
Measures whether current news sentiment is above or below the company's historical news baseline.

### External dependencies
- `FinnhubNewsProvider` (Google News RSS → Finnhub → Yahoo RSS)
- Python stdlib: `re`, `math`, `statistics`, `datetime`

### Module-level constants
```python
_TOP_PUBLISHERS = frozenset([
    "benzinga", "dowjones", "dow jones", "seekingalpha", "seeking alpha",
    "yahoo", "reuters", "bloomberg", "cnbc", "marketwatch",
    "market watch", "barron", "wsj", "wall street journal",
])

_BULLISH_SIMPLE   = 9 regex patterns (record high, surge, soar, jump, rally, spike, breakout, double/triple/boom)
_BULLISH_COMPOUND = 3 (pattern_A, pattern_B) pairs (beat+estimates, upgrade+buy, strong+results)
_BEARISH_SIMPLE   = 5 regex patterns (plunge/tumble, miss/fell, risk/concern, layoffs, cut+jobs)
_BEARISH_COMPOUND = 2 (pattern_A, pattern_B) pairs (downgrade+sell, weak+results)
```

### Module-level functions

#### `_sentiment(text)` → float
```
bullish = count of _BULLISH_SIMPLE patterns matched (each 0 or 1) +
          count of _BULLISH_COMPOUND pairs where BOTH patterns match
bearish = same for bearish patterns
total = bullish + bearish
return (bullish - bearish) / total if total > 0 else 0.0
```
Range: [-1.0, 1.0].

#### `_days_from_today(date_str)` → float
Returns `(today - article_date).days + 1`, minimum 1.0. Used as denominator for recency weight `w = 1/r`.

### Class: `NarrativeScout`

#### `run(ticker, historical_start, historical_end)` → SolliaNarrativeResult
1. Fetch all headlines via `FinnhubNewsProvider.get_news(ticker, historical_start)`.
2. Publisher filter: keep only headlines from `_TOP_PUBLISHERS`. If fewer than 100 survive, use all.
3. Cap at 100 000 headlines.
4. Split into `base_articles` (before most recent 10-K filing date) and `test_raw` (after).
5. Baseline: compute mean/stddev of `_sentiment(h.headline)` across base_articles.
6. Test sample: if > 100 articles, evenly subsample to 100.
7. Recency-weighted sentiment: `test_ws = Σ(w * s) / Σw` where `w = 1/days_from_today`.
8. `sigma2 = (test_ws - base_mean) / baseline_stddev`.
9. `s2 = 50 + 30 * tanh(sigma2 / 2)`, clamped [20, 80].
10. Build top 5 signals ranked by `|weight * sentiment|`.
11. Build `sentiment_label` and `conclusion`.
12. Return `SolliaNarrativeResult`.

---

## 7. Agent 3 — `FollowThroughAgent` (`src/agents/follow_through.py`)

### Purpose
Tracks specific measurable CEO promises in filings/transcripts and determines whether later filings provide evidence those promises were kept.

### External dependencies
- Local Ollama via `subprocess.run(["ollama", "run", "llama3.2"], ...)` (NOT the HTTP API)
- SEC EDGAR direct HTTP
- FMP via `FMPTranscriptProvider`
- Press releases: Company IR → GlobeNewswire → Google News RSS

### Module-level constants
```python
HEADERS = {"User-Agent": "Sollia Analysis contact@sollia.app", "Accept-Encoding": "gzip, deflate"}
DATA_HEADERS = {same + "Host": "data.sec.gov"}
EDGAR_BASE = "https://www.sec.gov"
SEC_BASE = "https://data.sec.gov"

POSITIVE_PATTERNS = [
    r'\bwe expect\b', r'\bwe plan\b', r'\bwe will\b', r'\bwe are on track\b',
    r'\bwe project\b', r'\bwe forecast\b', r'\bwe anticipate\b', r'\bour target\b',
    r'\bwe aim\b', r'\bwe commit\b', r'\bwe intend\b', r'\bgoal of\b',
    r'\bobjective of\b', r'\btarget of\b',
]

NEGATIVE_PATTERNS = [
    r'no assurance', r'cannot assure', r'cannot guarantee', r'no guarantee',
    r'may not', r'might not', r'could not', r'unless', r'except',
    r'risk factor', r'cautionary', r'forward.looking statements',
    r'should read', r'completely and understand', r'actual results may differ',
]
```

### Module-level functions

#### `_get_transcripts(ticker)` → List[Dict]
Disk cache at `src/.transcript_cache/{ticker}.json`. On hit, returns cached list.
On miss: calls `FMPTranscriptProvider().get_transcripts(ticker)`, serializes to JSON, caches.
Fallback if FMP fails: fetches Yahoo Finance quote page (stores raw HTML, limited utility).
Each dict: `{date, content, fiscal_period, source_url}`.

#### `_ask_ollama(prompt, timeout=45)` → str
`subprocess.run(["ollama", "run", "llama3.2"], input=prompt, capture_output=True, text=True, timeout=timeout)`. Returns stdout stripped. Returns `""` on error.

#### `_strip_html(html)` → str
Removes HTML tags, decodes `&nbsp;`, `&amp;`, `&lt;`, `&gt;`, `&#NNN;`, collapses whitespace.

#### `_split_sentences(text)` → List[str]
Splits on `(?<=[.!?])\s+`. Filters to sentences with >= 5 words.

#### `_extract_mda(html, filing_type)` → str
For "10-K": searches for Item 7 MD&A section header, stops at Item 7A or 8.
For "10-Q": Item 2 MD&A, stops at Item 3 or 4.
Falls back to `html[:30000]`. Returns up to 30 000 chars.

#### `_filter_promises(sentences)` → List[Dict]
Deterministic filter: keeps sentences where:
- At least one POSITIVE_PATTERN matches (case-insensitive).
- No NEGATIVE_PATTERN matches.
- Contains a number with a scale word: `\$?\d+[\d,.]*\s*(?:billion|million|thousand|%|percent|year|quarter)`.

#### `_batch_validate_promises(candidates)` → List[Dict]
Calls `_ask_ollama` with a batch prompt of up to 15 candidates.
Prompt asks for JSON array: `[{"id": 0, "is_promise": true, "severity": 7, "metric": "revenue target"}, ...]`.

JSON parsing strategy:
1. Find `[` in response.
2. Pre-clean: strip ANSI escape codes (`\[\d+[A-Za-z]`, `\[[A-Za-z]`), keep only valid JSON chars.
3. Extract individual `{...}` objects by brace-depth tracking.
4. Fix common Llama mistakes: `"is promise"` → `"is_promise"`, spaces in keys.
5. Fallback: try truncating at last `},` and appending `]`.

If zero promises found after first attempt, retries with simplified prompt on first 5 candidates.

Returns candidates with added `severity` (1-10) and `metric` (string) fields, filtered to `is_promise == True`.

#### `_deterministic_evidence_search(promise, later_sentences)` → Optional[str]
1. Extracts keywords from `promise['metric']` (words > 3 chars, not stop words).
2. Joins first 500 later sentences into one search text.
3. Builds regex: `.{0,200}(?i:keyword1|keyword2|...).{0,200}`.
4. Returns first 500 chars of first match, or `None`.

#### `_llama_evidence_verification(promise, later_sentences)` → Tuple[str, str]
Calls Ollama: given promise text and later filing excerpts (first 30 sentences, 3000 chars), asks for KEPT/BROKEN/PARTIAL/UNCLEAR + evidence quote. Returns `(status, evidence)`. On JSON parse failure returns `("UNCLEAR", "Could not verify")`.

**Note:** This function exists in the code but is NOT called in the current `run()` method. Evidence search uses `_deterministic_evidence_search` only.

#### `_get_cik(ticker)` → Optional[str]
Fetches `https://www.sec.gov/files/company_tickers.json`, finds ticker match, returns zero-padded 10-digit CIK string.

### Class: `FollowThroughAgent`

#### `run(ticker)` → Dict
Returns dict with keys: `ticker, score, promises, total_candidates, confirmed_promises, elapsed_seconds, log`.

Step-by-step:

**Step 1 — CIK lookup:** `_get_cik(ticker)`. Returns `{"error": ..., "promises": [], "score": 50.0}` on failure.

**Step 2 — Fetch SEC submissions:** `GET {SEC_BASE}/submissions/CIK{cik}.json`. Extracts `forms`, `accessions`, `primaryDocuments`, `reportDates` from `filings.recent`.

**Step 3 — Load/build filing URL map (disk-cached):**
- Cache file: `src/.filing_cache/{ticker}_all_filings.json`.
- If cache exists, load it directly.
- If not: fetch recent filings + older archive files (`data.filings.files` list). For each 10-K, 10-Q, or 8-K filing, build URL: `https://www.sec.gov/Archives/edgar/data/{cik_int}/{accno_nodash}/{doc}`. Key: `"{form}_{date}"`.
- Save to disk cache.

**Step 4 — Download filing texts (3-source fallback chain per filing):**
1. Last10K.com (fastest): `https://last10k.com/sec-filings/{ticker}/{docname}`.
2. SEC EDGAR `.htm` URL directly.
3. SEC JSON API re-fetch.
All are stripped with `_strip_html`. If all fail, stores empty string.

**Step 4a — Extract MD&A and split into sentences:**
For each downloaded filing text: call `_extract_mda`, call `_split_sentences`. Each sentence stored as `{text, source, date, url}`.

**Step 4b — Add transcript sentences:**
Calls `_get_transcripts(ticker)`. Each transcript sentence added as `{text, source: "transcript", date, url}`.

**Step 4c — Add press release sentences (3-source fallback):**
1. Company IR page (hardcoded NVIDIA URL + generic patterns like `investor.{ticker}.com/news-releases/` and `{ticker}.com/press/`).
2. GlobeNewswire: `https://www.globenewswire.com/search/keyword/{ticker}?pageSize=10`.
3. Google News RSS: `https://news.google.com/rss/search?q={ticker}+press+release&hl=en-US...`.

All sentences sorted by date.

**Step 5 — Deterministic promise filter:** `_filter_promises(all_sentences)`.

**Step 6 — LLM validation:** If <= 15 candidates, one call. Otherwise chunks of 15. Each chunk validated by `_batch_validate_promises`.

**Step 7 — Deduplication:** Normalize text (strip digits, collapse whitespace, first 150 chars as key). Remove duplicates. Sort by severity descending. Keep top 5.

**Step 8 — Evidence search:** For each of the top 5 promises: find later sentences (`sentence.date > promise.date`), call `_deterministic_evidence_search`. Sets `promise['status']` = "EVIDENCE_FOUND" or "NO_EVIDENCE".

**Step 9 — Score calculation:**
```python
total_possible = sum(severity for promise in top_5)
total_earned   = sum(severity for promise in top_5 if status == "EVIDENCE_FOUND")
score = (total_earned / total_possible) * 100 if total_possible > 0 else 50.0
```
Only KEPT/EVIDENCE_FOUND promises add to the numerator. BROKEN/NO_EVIDENCE do not.

---

## 8. Synthesizer (`src/agents/synthesizer.py`)

### Purpose
Combines s1 (forensic) and s2 (narrative) into `s_final`.

### External dependencies
- Ollama HTTP API for CCA corroboration
- Optional: `sklearn.cross_decomposition.CCA` and `numpy`

### Module-level functions

#### `_rate_dimensions(sentence)` → List[float]
Asks Ollama to rate a sentence on 2 dimensions: Optimism (-1 to +1) and Specificity (-1 to +1).
Returns `[opt, spec]` pair or `[0.0, 0.0]` on failure.

#### `_cca_score(a_vecs, b_vecs)` → float
If sklearn available: fits CCA(n_components=1) on paired vectors, returns absolute Pearson correlation of first canonical component. Requires n >= 3 and n > d.
Fallback: mean pairwise Pearson on first vector component.
Returns value in [0.0, 1.0]. Returns 0.5 if insufficient data.

### Class: `Synthesizer`

#### `run(forensic, narrative, ticker, company_name)` → SolliaAnalysisResult
If both agents run: calls `_two_track`. Otherwise uses single-agent score directly.

```python
s_raw = 50.0 + (track1 - 50.0) * (1.0 - 0.2 * abs(corroboration))
s_final = clamp(s_raw, 20, 80)
```

Builds `SolliaAnalysisResult` and attaches `export_data = result.model_dump(exclude={"export_data"})`.

#### `_two_track(s1, s2, forensic, narrative, logs)` → Tuple[float, float]
**Track 1 — Signal alignment:**
```python
w1 = |s1 - 50| / 30    # confidence weight
w2 = |s2 - 50| / 30
similarity = exp(-((s1 - s2)^2) / (2 * 225))   # Gaussian kernel, width=15 score points
weighted_avg = (s1*w1 + s2*w2) / (w1+w2)
s_track1 = similarity * weighted_avg + (1 - similarity) * 50
```

**Track 2 — Historical corroboration:**
Calls `_corroboration`.

#### `_corroboration(forensic, narrative, logs)` → float
For up to 6 quarterly pairs from forensic: summarizes each via Ollama → rates on 2D → builds vector.
For up to 6 top news signals from narrative: same.
If fewer than 5 vectors on either side: returns 0.5 (default).
Otherwise: returns `_cca_score(fa_vecs, ns_vecs)`.

#### `_classify(score)` → Tuple[str, str]
```
score >= 62 → ("BUY",  "#22C55E")
score >= 39 → ("HOLD", "#F59E0B")
else        → ("SELL", "#EF4444")
```

---

## 9. Pipeline Orchestrator (`src/pipeline/orchestrator.py`)

### `run_analysis(ticker, run_forensic=True, run_narrative=True)` → Generator

A generator that yields either `AgentLogMessage` or `SolliaAnalysisResult` objects.

**Consumers** iterate with `for event in run_analysis(ticker, ...)` and check `isinstance(event, SolliaAnalysisResult)` for the final result.

Flow:
1. CIK lookup via `SECEdgarProvider`. If not found, yield error log + HOLD result and return.
2. If `run_forensic`: instantiate `ForensicAccountant`, call `.run(ticker)`, yield all `forensic.log_messages`, yield summary log.
3. If `run_narrative`: inherit `hist_start/hist_end` from forensic if available, else use 6-year default window. Instantiate `NarrativeScout`, call `.run(ticker, hist_start, hist_end)`, yield log messages.
4. Instantiate `Synthesizer`, call `.run(forensic, narrative, ticker, company_name)`, yield log messages, yield final `SolliaAnalysisResult`.
5. On any exception: yield error log, yield fallback HOLD result.

---

## 10. Data Providers

### 10.1 `SECEdgarProvider` (`src/providers/filings/sec_edgar.py`)

In-memory caches: `_cik_cache: Dict[str, str]`, `_name_cache: Dict[str, str]`.

#### `get_cik(ticker)` → Optional[str]
Fetches `https://www.sec.gov/files/company_tickers.json` (public, no auth). Finds ticker (case-insensitive). Returns zero-padded 10-digit CIK string.

#### `get_company_name(ticker)` → str
Uses cache. Falls back to `GET {SEC_BASE}/submissions/CIK{cik}.json` `.name` field.

#### `get_10k_filings(ticker)` → List[dict]
Fetches submissions JSON. Passes `filings.recent` block to `_parse_filing_block`. Returns list sorted by `filing_date` desc. Each entry:
```python
{
    cik, form, accession_number, accession_nodash,
    filing_date, period_of_report, primary_doc, htm_url
}
```
Only includes `10-K` and `10-K/A`.

#### `fetch_html(cik, acc_nd, doc)` → str
`GET {EDGAR_BASE}/Archives/edgar/data/{cik}/{acc_nd}/{doc}`. 0.15s sleep before each call. Returns raw HTML string.

#### `get_filing_result(ticker, info)` → FilingResult
Orchestrates: `fetch_html` → `extract_mda` → `extract_ceo_name` → returns `FilingResult`.

#### `extract_mda(html)` → str
Strips HTML first. Searches for "Item 7" MD&A header (3 fallback patterns). Skips TOC entries (those followed by page number + "Item 7A"). Extracts up to next Item 7A or Item 8 boundary. Returns up to 30 000 chars.

#### `extract_ceo_name(html)` → str
Searches signature block (`/s/`) patterns for CEO name. Validates: 2-4 word names, not an auditor name. Normalizes capitalization of hyphenated names.

### 10.2 `FMPTranscriptProvider` (`src/providers/transcripts/fmp.py`)

**Note:** Despite the class name, this provider does NOT call the FMP API. It uses a 4-source fallback chain.

#### `get_transcripts(ticker)` → List[TranscriptResult]
1. Check disk cache at `src/.transcript_cache/{ticker}.json` (only if cache file is newer than the source file itself).
2. Try `_sec_8k` → `_stock_analysis` → `_nasdaq` → `_yahoo_finance` in order. First non-empty result wins.
3. Save to cache.

#### `_sec_8k(ticker)` → List[TranscriptResult]
- Fetches SEC submissions JSON for CIK.
- Scans up to 120 8-K filings for index pages containing "transcript" / "earnings" / "commentary" / "ex-99".
- Downloads exhibit `.htm` files, checks for transcript keywords ("transcript", "earnings call", "operator:", "quarter ended").
- Strips HTML, returns up to 8 transcripts. Stops early if oldest transcript > 6 years old.

#### `_stock_analysis(ticker)` → List[TranscriptResult]
Scrapes `stockanalysis.com/stocks/{ticker}/transcripts/`. Validates each transcript date is within ±90 days of expected reporting month for its fiscal quarter.

#### `_nasdaq(ticker)` → List[TranscriptResult]
Scrapes `nasdaq.com/market-activity/stocks/{ticker}/earnings`. Follows transcript/earnings-call links.

#### `_yahoo_finance(ticker)` → List[TranscriptResult]
Parses Yahoo Finance RSS feed. Only returns items whose title contains "transcript" or "earnings call".

### 10.3 `FinnhubNewsProvider` (`src/providers/news/finnhub.py`)

#### `get_news(ticker, start_date)` → Tuple[List[Headline], List[str]]
Chain: Google News RSS (always runs) → Finnhub (always runs) → Yahoo Finance RSS (only if both above return nothing).
Deduplicates by URL (fallback: headline text).
Returns `(all_headlines, providers_used)`.

#### `_google_news_rss(ticker)` → List[Headline]
`https://news.google.com/rss/search?q={ticker}+stock`. Parses up to 200 RSS items.

#### `_finnhub_chunked(ticker, start_date)` → List[Headline]
Breaks time range into 12-month chunks from `start_date` to today. 0.3s delay between chunks. Max 2 retries on HTTP 429. Each chunk: `GET https://finnhub.io/api/v1/company-news?symbol={ticker}&from=...&to=...&token=...`.

#### `_yahoo_rss(ticker)` → List[Headline]
`https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US`. Up to 60 items.

---

## 11. Streamlit App (`sollia_working/app.py`)

**Entry point:** `streamlit run app.py` from the `sollia_working/` directory.

### Session state keys
```
current_ticker: str    — ticker being analyzed
result: Optional[SolliaAnalysisResult]
analyzing: bool
stop_requested: bool
run_fa: bool           — Forensic Accountant checkbox state
run_ns: bool           — Narrative Scout checkbox state
analysis_error: str    — error message to display
```

### Sidebar
- Ticker text input bound to `current_ticker`.
- Two checkboxes: ForensicAccountant, NarrativeScout.
- "Run Analysis" button (primary) and "Stop" button in two columns.
- "← Home" button (only when result is not None) — clears result and reruns.
- Error display if `analysis_error` is set.
- Keyboard hint text.

### Keyboard shortcuts (injected via `streamlit.components.v1.html`)
- `Enter` (when no input is focused) → clicks "Run Analysis" button.
- `Escape` → clicks "Stop" button.

### Ticker validation (`_validate_ticker`)
Fetches SEC EDGAR ticker list. Returns `(True, None)` on match. On mismatch, suggests first ticker starting with same 2 letters. On network failure, returns `(True, None)` (pass-through).

### Analysis execution
Iterates `run_analysis(ticker, run_forensic, run_narrative)` generator. Shows last 18 log lines in real-time. Respects `stop_requested`. Stores `SolliaAnalysisResult` in session state, then reruns.

### UI rendering (`render_human_view`)
- No result → welcome screen with centered SOLLIA text.
- With result → header (score %, signal), executive summary box, two-column layout (Discrepancy | Sentiment), agent log expander, export JSON download button.

---

## 12. UI Renderer (`src/ui/human_view.py`)

All sections render raw HTML via `st.markdown(..., unsafe_allow_html=True)`.

### Color scheme
- Background: `#0F1117` (dark), `#1A1D2E` (card)
- Text: `#F8FAFC` (primary), `#94A3B8` (muted)
- Signal colors: `#22C55E` (green/buy), `#F59E0B` (amber/hold), `#EF4444` (red/sell)
- Link: `#60A5FA`

### `_render_header(result)`
Large centered score (88px), signal label (30px), company name, timestamp.

### `_render_discrepancy(result)`
Shows `discrepancy_label`. If `direct_contradiction`: red warning. If `fallback_used`: amber note.
Renders up to 5 `SolliaDiscrepancySignal` cards with colored left border (severity color).

### `_render_sentiment(result)`
Shows `sentiment_label` and article count + providers.
Renders up to 5 `SolliaSentimentSignal` cards with linked headlines, source, date, score (colored).

### `_render_agent_log(result)`
Expander (expanded by default). Shows forensic/narrative conclusions as bold text. Then all deduplicated log messages: `[HH:MM:SS] Speaker: message`.

### `_render_export(result)`
If `export_data` present: download button for `sollia_{ticker}_{timestamp}.json`.

---

## 13. FastAPI Backend (`sollia_web/backend/main.py`)

```
GET  /api/agents    → list of {id, name, description}
POST /api/analyze   → AnalysisRequest{ticker, agents[]} → analysis result JSON
GET  /              → React SPA (static files from frontend/dist)
```

**Note:** Line 74 has a bug: `ft_result` is referenced but never defined (follow_through result handling not complete in this file).

### `POST /api/analyze` response shape
```json
{
    "ticker": "NVDA",
    "company": "Nvidia Corp",
    "score": 73.5,               // normalized: (s_final - 20) / 60 * 100
    "signal": "BUY",
    "signal_color": "#22C55E",
    "summary": "...",
    "agents_run": ["ForensicAccountant"],
    "track1_score": 65.3,
    "corroboration": 0.72,
    "forensic": {...},           // SolliaForensicResult.model_dump()
    "narrative": {...},
    "follow_through": null,
    "log": [{"speaker": "...", "message": "..."}]
}
```

### CORS
Only `http://localhost:5173` allowed (Vite dev server default).

---

## 14. Agent Registry (`sollia_web/backend/agents.py`)

```python
AGENTS = {
    "forensic":       AgentDefinition(id, name, description, runner=ForensicAccountant()),
    "narrative":      AgentDefinition(id, name, description, runner=NarrativeScout()),
    "follow_through": AgentDefinition(id, name, description, runner=FollowThroughAgent()),
}
```

#### `run_selected_agents(ticker, selected)` → Tuple[Optional[SolliaForensicResult], Optional[SolliaNarrativeResult], List[AgentLogMessage]]

- Runs forensic first, then narrative (inherits `historical_start/end` from forensic).
- For follow_through: stores result in `run_selected_agents._ft_result` (function attribute — awkward pattern). `main.py` reads it as `ft_result` which is a bug (NameError).
- Narrative uses `hist_start = "2020-01-01"` and `hist_end = "2026-01-01"` if no forensic result.

---

## 15. HTML Report Generator (`gen_ui.py`)

Standalone script. Imports orchestrator, runs analysis, generates a self-contained HTML file.

**Output path:** `/Users/ardaturgut/Desktop/small sollia test folder ,layer by layer/sollia_result.html`

Features:
- Large signal percentage display.
- Forensic signals table (citation, severity, description).
- Narrative signals table (headline, source, date, score).
- Embedded JSON (view/download buttons).
- Last 25 agent log entries.

---

## 16. Frontend Tech Stack (`sollia_web/frontend/`)

| Package | Version | Role |
|---|---|---|
| React | 19.2.5 | UI framework |
| Vite | 8.0.10 | Build tool + dev server (port 5173) |
| TailwindCSS | 4.2.4 | Utility CSS |
| Three.js | 0.184.0 | 3D graphics |
| @react-three/fiber | 9.6.1 | React renderer for Three.js |
| React Spring | latest | Physics-based animations |
| Framer Motion | 12.38.0 | Declarative animations |

---

## 17. Disk Caches

| Cache | Location | Key format | Content |
|---|---|---|---|
| Transcripts | `sollia_working/src/.transcript_cache/{TICKER}.json` | ticker | List of TranscriptResult dicts |
| Filing URLs | `sollia_working/src/.filing_cache/{TICKER}_all_filings.json` | ticker | Dict of `{form}_{date}` → `{form, date, url}` |

Transcript cache invalidates when the cache file is older than `fmp.py` itself (source file mtime). Otherwise uses cached data indefinitely.
Filing URL cache: never invalidates once created (must be manually deleted to refresh).

---

## 18. Key Constants & Thresholds

| Constant | Value | Used in |
|---|---|---|
| Max filings fetched for forensic | 12 | `forensic_accountant.py:415` |
| Parallel fetch workers | 4 | `forensic_accountant.py:706` |
| CEO tenure cap | 6 years | `forensic_accountant.py:481` |
| Transcript cache per ticker | 8 transcripts (SEC 8-K path) | `fmp.py:214` |
| Max transcript content | 50 000 chars | `fmp.py:210` |
| Max MD&A text | 30 000 chars | `sec_edgar.py:201` |
| Full text stored in FilingResult | 80 000 chars | `sec_edgar.py:155` |
| Finnhub chunk delay | 0.3s | `finnhub.py:114` |
| SEC fetch stagger | 0.12–0.15s | `sec_edgar.py:104`, `fmp.py:172` |
| Signal thresholds | BUY≥62, SELL≤38 | `synthesizer.py:255` |
| Severity thresholds | RED≥2σ, AMBER≥1σ | `forensic_accountant.py:227` |
| Publisher filter fallback | <100 articles → use all | `narrative_scout.py:163` |
| News cap | 100 000 headlines | `narrative_scout.py:168` |
| Test sample cap | 100 articles (evenly spaced) | `narrative_scout.py:202` |
| Promise batch cap | 15 per Ollama call | `follow_through.py:595` |
| Top promises kept | 5 | `follow_through.py:622` |
| Ollama timeout (follow_through) | 45s default, 30s retry | `follow_through.py:91` |
| Ollama timeout (forensic) | 25s | `forensic_accountant.py:62` |

---

## 19. Known Bugs & Issues

1. **`main.py` line 74:** `ft_result` referenced but not defined in scope. `run_selected_agents` stores it as a function attribute `_ft_result` but `main.py` uses bare `ft_result` → `NameError` when follow_through is selected via FastAPI.

2. **`follow_through.py` step 4a:** `key` variable inside the MD&A extraction loop references the outer loop variable `key` (from filing_texts loop) rather than `ftype`. Should be `info = filings_to_fetch[ftype]`.

3. **IR page press release URLs:** Hardcoded NVIDIA URL (`nvidianews.nvidia.com`) appears in the generic fallback chain, which will fail for non-NVIDIA tickers.

4. **`_llama_evidence_verification`** is defined but never called. The run() method uses only `_deterministic_evidence_search`.

5. **Score formula:** Only `EVIDENCE_FOUND` status counts as earned. This is intentionally conservative, but may score 0% when promises are genuinely unverifiable.

---

## 20. Running the Application

### Streamlit (primary)
```bash
cd /Users/ardaturgut/Desktop/Sollia/sollia_working
streamlit run app.py
```
Requires: Ollama running locally (`ollama serve`) with `llama3.2` pulled.

### FastAPI + React
```bash
# Terminal 1: backend
cd /Users/ardaturgut/Desktop/Sollia/sollia_web/backend
uvicorn main:app --reload --port 8000

# Terminal 2: frontend dev server
cd /Users/ardaturgut/Desktop/Sollia/sollia_web/frontend
npm run dev    # serves on port 5173
```

### HTML report
```bash
cd /Users/ardaturgut/Desktop/Sollia
python gen_ui.py
```

---

## 21. Test Scenarios for AI Agents

### Unit test inputs/outputs

#### `_sentiment(text)`
```python
_sentiment("record high surge in revenue")   # → 1.0 (2 bullish, 0 bearish)
_sentiment("plunged and missed estimates")   # → -1.0 (0 bullish, 2 bearish)
_sentiment("neutral market update")          # → 0.0
_sentiment("")                               # → 0.0
```

#### `_filter_promises(sentences)`
```python
# PASSES: has positive pattern + metric + no negative
_filter_promises([{"text": "We expect revenue to reach $5 billion by 2025."}])
# → 1 candidate

# FAILS: has negative pattern
_filter_promises([{"text": "We expect revenue may not meet $5 billion targets."}])
# → 0 candidates

# FAILS: no metric (no number + scale word)
_filter_promises([{"text": "We expect strong revenue growth."}])
# → 0 candidates
```

#### `_severity_label(sigma)`
```python
_severity_label(2.1)   # → ("RED", "#EF4444")
_severity_label(-2.1)  # → ("RED", "#EF4444")
_severity_label(1.5)   # → ("AMBER", "#F59E0B")
_severity_label(0.5)   # → ("GREEN", "#22C55E")
```

#### Score classification
```python
Synthesizer._classify(62.0)  # → ("BUY", "#22C55E")
Synthesizer._classify(61.9)  # → ("HOLD", "#F59E0B")
Synthesizer._classify(39.0)  # → ("HOLD", "#F59E0B")
Synthesizer._classify(38.9)  # → ("SELL", "#EF4444")
```

#### FollowThroughAgent score formula
```python
# 2 promises, severity 7 and 3. Both EVIDENCE_FOUND.
# total_earned = 10, total_possible = 10 → score = 100.0

# 2 promises, severity 7 and 3. Only first EVIDENCE_FOUND.
# total_earned = 7, total_possible = 10 → score = 70.0

# 0 promises → score = 50.0 (neutral default)
```

### Integration test flow (mock-friendly entry points)
1. `SECEdgarProvider.get_cik("NVDA")` — should return `"0001045810"` (padded).
2. `SECEdgarProvider.get_10k_filings("NVDA")` — should return a list with `form == "10-K"`.
3. `ForensicAccountant.run("NVDA")` — should return `SolliaForensicResult` with `s1 in [20, 80]`.
4. `NarrativeScout.run("NVDA", "2020-01-01", "2025-01-01")` — should return `SolliaNarrativeResult` with `s2 in [20, 80]`.
5. `list(run_analysis("NVDA"))[-1]` — should be `SolliaAnalysisResult` with `signal in ["BUY", "HOLD", "SELL"]`.

### Error cases to handle
- Unknown ticker (e.g. "ZZZZ") → CIK not found → orchestrator yields error log, returns HOLD/50.
- Ollama not running → tone extraction uses fallback (no Ollama), no crash.
- SEC rate limit → 0.12–0.15s sleeps reduce frequency; no explicit retry (except Finnhub 429).
- FMP/transcript provider returns [] → `fallback_used = True`, MD&A used instead of transcript.
- All filing downloads fail for a ticker → empty `filing_texts` → no promise candidates → score 50.0.
