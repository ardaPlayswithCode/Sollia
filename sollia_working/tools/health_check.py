#!/usr/bin/env python3
"""Sollia startup health check (Part 7d, 2026-07-01 final hardening pass).

Verifies, with one CHEAP probe each (no paid Fable 5 tokens spent):
  1. API keys present + live (Groq/Gemini: 1-token completion on the free
     tier; OpenRouter: GET /key metadata — free — plus remaining balance).
  2. SEC endpoints reachable (data.sec.gov submissions + www.sec.gov archives).
  3. Cache integrity (filings JSONs parse + carry the raw-submissions schema
     contract; LLM byte-cache spot-parse; no zero-byte poison files).
  4. Python imports / optional-dependency degradation report.
  5. Abuse-guard configuration echo (budgets, caps) so ops can eyeball them.

Exit code 0 = "System healthy" (degraded-but-working counts as healthy with
warnings); 1 = at least one hard failure.

Usage:  python3.11 tools/health_check.py [--json]
"""
from __future__ import annotations

import os
import sys
import json
import glob
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from src.config.llm_config import (  # noqa: E402
    LLM_PROVIDERS, check_api_keys, MAX_LLM_CALLS_PER_RUN, MAX_COST_PER_RUN_USD,
    MAX_NO_CACHE_RUNS_PER_HOUR)
from src.utils.dependencies import check_dependencies, format_report  # noqa: E402

HEADERS = {"User-Agent": "Sollia contact@sollia.app"}
OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _one_token_probe(url: str, model: str, key: str) -> tuple:
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {key}",
                                        "Content-Type": "application/json"},
                          json={"model": model, "max_tokens": 2, "temperature": 0,
                                "messages": [{"role": "user", "content": "Say OK"}]},
                          timeout=20)
        if r.status_code == 200:
            return OK, f"{model} live"
        if r.status_code == 429:
            return WARN, f"{model} rate-limited right now (key valid)"
        return FAIL, f"{model} HTTP {r.status_code}: {r.text[:80]}"
    except Exception as exc:
        return FAIL, f"{model}: {type(exc).__name__}"


def check_llm_providers() -> list:
    """One cheap probe per INDEPENDENT provider domain. Fable 5 (paid) is NOT
    probed with a completion — its key is validated via the free /key endpoint."""
    results = []
    keys = check_api_keys()
    for env, status in keys.items():
        if status != "ok":
            results.append((FAIL, f"key {env}: {status}"))
    or_key = os.environ.get("OPENROUTER_API_KEY", "")
    if or_key:
        try:
            r = requests.get("https://openrouter.ai/api/v1/key",
                             headers={"Authorization": f"Bearer {or_key}"}, timeout=15)
            if r.status_code == 200:
                d = r.json().get("data", {})
                usage = d.get("usage")
                results.append((OK, f"OpenRouter key live (usage ${usage:.2f})"
                                if isinstance(usage, (int, float)) else "OpenRouter key live"))
            else:
                results.append((FAIL, f"OpenRouter /key HTTP {r.status_code}"))
        except Exception as exc:
            results.append((FAIL, f"OpenRouter: {type(exc).__name__}"))
    gq = os.environ.get("GROQ_API_KEY", "")
    if gq:
        results.append(_one_token_probe("https://api.groq.com/openai/v1/chat/completions",
                                        "llama-3.1-8b-instant", gq))
    gm = os.environ.get("GEMINI_API_KEY", "")
    if gm:
        results.append(_one_token_probe(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "gemini-2.5-flash", gm))
    return results


def check_sec_endpoints() -> list:
    results = []
    probes = [
        ("data.sec.gov submissions", "https://data.sec.gov/submissions/CIK0001045810.json",
         {"User-Agent": HEADERS["User-Agent"], "Host": None}),
        ("www.sec.gov ticker directory", "https://www.sec.gov/files/company_tickers.json", None),
    ]
    for name, url, _h in probes:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            results.append((OK, f"{name} reachable") if r.status_code == 200
                           else (FAIL, f"{name} HTTP {r.status_code}"))
        except Exception as exc:
            results.append((FAIL, f"{name}: {type(exc).__name__}"))
        time.sleep(0.15)
    return results


def check_caches() -> list:
    """Spot-validate cache integrity: parseable JSON + the shared filings-cache
    schema contract ({"filings":{"recent":{...}}}) + no zero-byte files."""
    results = []
    filings_dir = os.path.join(_ROOT, ".cache", "filings")
    bad_schema, bad_parse, checked = [], [], 0
    for path in sorted(glob.glob(os.path.join(filings_dir, "*_filings.json")))[:50]:
        name = os.path.basename(path)
        if "_hist_" in name:
            continue
        checked += 1
        try:
            with open(path) as f:
                data = json.load(f)
            if not isinstance(data.get("filings", {}).get("recent"), dict):
                bad_schema.append(name)
        except Exception:
            bad_parse.append(name)
    if bad_parse:
        results.append((FAIL, f"filings cache: {len(bad_parse)} unparseable: {bad_parse[:3]}"))
    if bad_schema:
        results.append((FAIL, f"filings cache: {len(bad_schema)} violate the raw-submissions "
                              f"schema contract: {bad_schema[:3]}"))
    if not bad_parse and not bad_schema:
        results.append((OK, f"filings cache: {checked} files, schema contract holds"))

    llm_cache = glob.glob(os.path.join(_ROOT, ".llm_cache", "*.json"))
    bad = 0
    for path in llm_cache[:200]:
        try:
            if os.path.getsize(path) == 0:
                bad += 1
                continue
            with open(path) as f:
                json.load(f)
        except Exception:
            bad += 1
    results.append((OK, f"LLM byte-cache: {len(llm_cache)} entries, spot-check clean")
                   if bad == 0 else (WARN, f"LLM byte-cache: {bad} corrupt entries (will re-fetch)"))

    try:
        from src.utils.cache_janitor import cache_size_bytes, CACHE_MAX_BYTES
        size = cache_size_bytes()
        level = OK if size < CACHE_MAX_BYTES else WARN
        results.append((level, "cache size: %.2f GB of %.0f GB cap"
                        % (size / 1024**3, CACHE_MAX_BYTES / 1024**3)))
    except Exception as exc:
        results.append((WARN, f"cache size check failed: {type(exc).__name__}"))
    return results


def check_imports() -> list:
    results = []
    dep = check_dependencies()
    for name, info in dep.items():
        if info.get("required") and not info["available"]:
            results.append((FAIL, f"{name}: {info['detail']}"))
        elif not info["available"]:
            results.append((WARN, f"{name}: {info['detail']}"))
    try:
        import src.agents.corporate_discrepancy  # noqa: F401
        import src.agents.promise_audit  # noqa: F401
        import src.agents.legal_proceedings  # noqa: F401
        import src.agents.risk_factors  # noqa: F401
        import src.agents.constraint_auditor  # noqa: F401
        import src.agents.moat_auditor  # noqa: F401
        results.append((OK, "all 6 agent modules import"))
    except Exception as exc:
        results.append((FAIL, f"agent import failed: {type(exc).__name__}: {exc}"))
    if not results:
        results.append((OK, "all dependencies available"))
    return results


def main() -> int:
    as_json = "--json" in sys.argv
    sections = {
        "llm_providers": check_llm_providers(),
        "sec_endpoints": check_sec_endpoints(),
        "caches": check_caches(),
        "imports_dependencies": check_imports(),
    }
    sections["configuration"] = [(OK,
        f"budgets: {MAX_LLM_CALLS_PER_RUN} LLM calls/run, ${MAX_COST_PER_RUN_USD:.2f} "
        f"billed/run, {MAX_NO_CACHE_RUNS_PER_HOUR} cache-busted runs/hour, "
        f"chain has {len(LLM_PROVIDERS)} tiers")]

    hard_fail = any(lvl == FAIL for res in sections.values() for lvl, _ in res)
    if as_json:
        print(json.dumps({k: [{"level": l, "detail": d} for l, d in v]
                          for k, v in sections.items()}, indent=2))
    else:
        for name, res in sections.items():
            print(f"\n== {name} ==")
            for lvl, detail in res:
                print(f"  [{lvl:4s}] {detail}")
        print("\n" + ("SYSTEM UNHEALTHY — fix the FAIL items above." if hard_fail
                      else "System healthy." + (" (some features degraded — see WARN)"
                           if any(l == WARN for r in sections.values() for l, _ in r) else "")))
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
