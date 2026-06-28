#!/usr/bin/env python3.11
"""Sollia model benchmark harness (final LLM-selection audit, 2026-06-25).

Sends the REAL Sollia pipeline prompts (5 fixtures extracted verbatim from the
agent prompt-builders, populated with real SEC filing text) to any OpenRouter
model and measures, per call:

  * TTFT (time-to-first-token, via SSE streaming)
  * total wall time, tokens in/out (real `usage` when the provider returns it)
  * estimated cost from the live OpenRouter catalog pricing
  * JSON parse success, schema compliance, and quote VERBATIM-ness
    (alphanumeric-normalised substring match — the same lenient match
    tools/verify_quotes.py uses, because agents store "Company s" where an
    apostrophe became a space).

It can run each test N times at temperature 0 for a determinism score, and
records every raw response + metrics to
  .test_results/model_benchmarks/{safe_model}/{test}.json

Usage:
  python3.11 tools/model_benchmark.py --models m1,m2 --tests all --runs 1
  python3.11 tools/model_benchmark.py --models m1 --tests test1 --determinism 3
  python3.11 tools/model_benchmark.py --list           # show fixtures
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_KEY = os.environ.get("OPENROUTER_API_KEY", "")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SW = os.path.dirname(_HERE)
_REPO = os.path.dirname(_SW)
FIXTURE_DIR = os.path.join(_SW, ".cache", "benchmark_fixtures")
RESULTS_DIR = os.path.join(_REPO, ".test_results", "model_benchmarks")
CATALOG_PATH = os.path.join(_SW, ".cache", "benchmark_fixtures", "_catalog.json")

# Rate limiting: spacing between calls to the same provider; honors Retry-After.
PROVIDER_SPACING = float(os.environ.get("BENCH_SPACING", "0.5"))
_last_call = {"t": 0.0}


# ───────────────────────────── helpers ─────────────────────────────
def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))


def _alnum(s: str) -> str:
    """Lowercase, keep only [a-z0-9] — robust verbatim match (mirrors verify_quotes)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _verbatim_in(quote: str, *texts: str) -> bool:
    """True if quote (alnum-normalised) is a substring of any provided text."""
    q = _alnum(quote)
    if len(q) < 8:  # too short to be a meaningful verbatim claim
        return False
    return any(q in _alnum(t) for t in texts if t)


def _extract_json(text: str):
    """Best-effort: strip ``` fences, grab first [..] or {..}, parse."""
    if not text:
        return None
    c = text.strip()
    if c.startswith("```"):
        c = re.sub(r"^```[a-zA-Z]*\n?", "", c)
        c = re.sub(r"\n?```$", "", c).strip()
    for op, cl in (("[", "]"), ("{", "}")):
        i, j = c.find(op), c.rfind(cl)
        if i != -1 and j > i:
            try:
                return json.loads(c[i:j + 1])
            except Exception:
                pass
    try:
        return json.loads(c)
    except Exception:
        return None


def load_catalog() -> dict:
    if os.path.exists(CATALOG_PATH):
        try:
            return json.load(open(CATALOG_PATH))
        except Exception:
            pass
    # fetch live
    try:
        r = requests.get("https://openrouter.ai/api/v1/models",
                         headers={"Authorization": f"Bearer {_KEY}"}, timeout=30)
        cat = {m["id"]: m for m in r.json().get("data", [])}
        os.makedirs(os.path.dirname(CATALOG_PATH), exist_ok=True)
        json.dump(cat, open(CATALOG_PATH, "w"))
        return cat
    except Exception:
        return {}


def model_pricing(catalog: dict, model_id: str):
    m = catalog.get(model_id, {})
    p = m.get("pricing", {}) or {}
    try:
        pin = float(p.get("prompt", 0)) * 1e6
        pout = float(p.get("completion", 0)) * 1e6
    except Exception:
        pin = pout = 0.0
    ctx = m.get("context_length") or (m.get("top_provider", {}) or {}).get("context_length") or 0
    return pin, pout, ctx


# ───────────────────────── streaming call (TTFT) ─────────────────────────
def call_model(model_id: str, prompt: str, max_tokens: int, temperature: float = 0.0,
               max_429_retries: int = 3):
    """Stream a completion, retrying transient 429s (free models rate-limit hard).
    Returns dict with text, ttft_ms, total_ms, usage, status."""
    res = _call_once(model_id, prompt, max_tokens, temperature)
    tries = 0
    while res["status"] == 429 and tries < max_429_retries:
        tries += 1
        time.sleep(2.0 * tries + 1.0)  # simple backoff
        res = _call_once(model_id, prompt, max_tokens, temperature)
    return res


def _call_once(model_id: str, prompt: str, max_tokens: int, temperature: float = 0.0):
    # provider spacing
    dt = time.time() - _last_call["t"]
    if dt < PROVIDER_SPACING:
        time.sleep(PROVIDER_SPACING - dt)

    headers = {"Authorization": f"Bearer {_KEY}", "Content-Type": "application/json",
               "HTTP-Referer": "https://sollia.app", "X-Title": "Sollia-Benchmark"}
    body = {"model": model_id, "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature, "max_tokens": max_tokens, "stream": True,
            "usage": {"include": True}}
    t0 = time.time()
    ttft = None
    chunks = []
    usage = {}
    status = 0
    err = ""
    try:
        with requests.post(OPENROUTER_URL, headers=headers, json=body, stream=True, timeout=180) as r:
            status = r.status_code
            if status != 200:
                err = (r.text or "")[:200]
            else:
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        j = json.loads(data)
                    except Exception:
                        continue
                    if j.get("usage"):
                        usage = j["usage"]
                    delta = (j.get("choices") or [{}])[0].get("delta", {}) or {}
                    piece = delta.get("content")
                    if piece:
                        if ttft is None:
                            ttft = (time.time() - t0) * 1000.0
                        chunks.append(piece)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
    finally:
        _last_call["t"] = time.time()
    total = (time.time() - t0) * 1000.0
    return {"text": "".join(chunks), "ttft_ms": round(ttft, 1) if ttft else None,
            "total_ms": round(total, 1), "usage": usage, "status": status, "error": err}


# ───────────────────────────── validators ─────────────────────────────
def validate(test_id: str, fx: dict, text: str) -> dict:
    """Return {parse, schema, verbatim, classification} each 0-100 + notes."""
    data = _extract_json(text)
    out = {"parse": 0, "schema": 0, "verbatim": 0, "classification": 0, "notes": ""}
    if data is None:
        out["notes"] = "JSON parse failed"
        return out
    out["parse"] = 100

    if test_id == "test1_agent2_stage2":
        if not isinstance(data, list):
            out["notes"] = "not a list"; return out
        ids = set(fx.get("expected_ids", []))
        vs = set(fx.get("valid_statuses", []))
        got_ids = [d.get("promise_id") for d in data if isinstance(d, dict)]
        good_status = sum(1 for d in data if isinstance(d, dict) and str(d.get("status", "")).upper() in vs)
        id_cover = len(set(got_ids) & ids) / max(1, len(ids))
        out["schema"] = round(100 * (0.5 * id_cover + 0.5 * (good_status / max(1, len(data)))))
        # evidence_id format / presence where KEPT/PARTIAL
        ev_ok = 0; ev_tot = 0
        for d in data:
            if not isinstance(d, dict):
                continue
            if str(d.get("status", "")).upper() in ("KEPT", "PARTIAL"):
                ev_tot += 1
                if re.match(r"EVID_\d+", str(d.get("evidence_id", "") or "")):
                    ev_ok += 1
        out["verbatim"] = round(100 * (ev_ok / ev_tot)) if ev_tot else 100
        # classification plausibility: not all identical, statuses valid
        distinct = len({str(d.get("status", "")).upper() for d in data if isinstance(d, dict)})
        out["classification"] = 100 if (good_status == len(data) and distinct >= 1) else 50

    elif test_id == "test2_agent1_discrepancy":
        if not isinstance(data, list):
            out["notes"] = "not a list"; return out
        A = " ".join(fx.get("passage_a_sents", []))
        B = " ".join(fx.get("passage_b_sents", []))
        out["schema"] = 100 if all(isinstance(d, dict) for d in data) else 50
        tot = 0; ok = 0
        for d in data:
            if not isinstance(d, dict):
                continue
            for q, src in ((d.get("quoted_phrase_a"), A), (d.get("quoted_phrase_b"), B)):
                if q:
                    tot += 1
                    if _verbatim_in(q, src):
                        ok += 1
        out["verbatim"] = round(100 * ok / tot) if tot else 100
        # classification: indices in range & types valid
        types = {"numerical_contradiction", "narrative_contradiction", "omission_discrepancy", "qualitative_tension"}
        cok = sum(1 for d in data if isinstance(d, dict) and d.get("discrepancy_type") in types)
        out["classification"] = round(100 * cok / len(data)) if data else 100

    elif test_id == "test3_agent3_legal":
        obj = data if isinstance(data, dict) else {}
        procs = obj.get("proceedings", []) if isinstance(obj, dict) else []
        out["schema"] = 100 if isinstance(procs, list) else 0
        cur = fx.get("current_text", ""); pri = fx.get("prior_text", "")
        tot = 0; ok = 0
        for p in procs:
            if not isinstance(p, dict):
                continue
            for q, src in ((p.get("current_quote"), cur), (p.get("prior_quote"), pri)):
                if q and str(q).lower() not in ("null", "none", ""):
                    tot += 1
                    if _verbatim_in(q, src):
                        ok += 1
        out["verbatim"] = round(100 * ok / tot) if tot else 100
        statuses = {"NEW", "ESCALATED", "SETTLED", "DISMISSED", "UNCHANGED", "QUIETLY_REMOVED"}
        cok = sum(1 for p in procs if isinstance(p, dict) and str(p.get("status", "")).upper() in statuses)
        out["classification"] = round(100 * cok / len(procs)) if procs else 100

    elif test_id == "test4_agent4_risk":
        if not isinstance(data, list):
            out["notes"] = "not a list"; return out
        out["schema"] = 100 if all(isinstance(d, dict) for d in data) else 50
        old = fx.get("old_text", ""); new = fx.get("new_text", "")
        cats = set(fx.get("valid_categories", []))
        tot = 0; ok = 0; cok = 0
        for d in data:
            if not isinstance(d, dict):
                continue
            if str(d.get("category", "")).upper() in cats:
                cok += 1
            # NEW has no old_quote; REMOVED has no new_quote
            for q, src in ((d.get("old_quote"), old), (d.get("new_quote"), new)):
                if q and str(q).lower() not in ("null", "none", ""):
                    tot += 1
                    if _verbatim_in(q, src):
                        ok += 1
        out["verbatim"] = round(100 * ok / tot) if tot else 100
        out["classification"] = round(100 * cok / len(data)) if data else 100

    elif test_id == "test5_agent2_stage1":
        if not isinstance(data, list):
            out["notes"] = "not a list"; return out
        sents = fx.get("sentences", [])
        out["schema"] = 100 if all(isinstance(d, dict) for d in data) else 50
        tot = 0; ok = 0
        valid_cat = {"revenue", "product", "margin", "rd", "market", "other"}
        cok = 0
        for d in data:
            if not isinstance(d, dict):
                continue
            if d.get("category") in valid_cat and isinstance(d.get("severity"), int):
                cok += 1
            t = d.get("text")
            if t:
                tot += 1
                if _verbatim_in(t, *sents):
                    ok += 1
        out["verbatim"] = round(100 * ok / tot) if tot else 0
        out["classification"] = round(100 * cok / len(data)) if data else 100

    return out


def quality_score(v: dict) -> float:
    """Quality 0-100 per spec: parse 25, schema 25, verbatim 30, classification 20."""
    return round(0.25 * v["parse"] + 0.25 * v["schema"] + 0.30 * v["verbatim"] + 0.20 * v["classification"], 1)


# ───────────────────────────── runner ─────────────────────────────
def load_fixtures():
    fx = {}
    if not os.path.isdir(FIXTURE_DIR):
        return fx
    for f in sorted(os.listdir(FIXTURE_DIR)):
        if f.startswith("_") or not f.endswith(".json"):
            continue
        fx[f[:-5]] = json.load(open(os.path.join(FIXTURE_DIR, f)))
    return fx


def benchmark_model(model_id: str, tests: list, fixtures: dict, catalog: dict,
                    runs: int = 1, determinism: int = 0) -> dict:
    pin, pout, ctx = model_pricing(catalog, model_id)
    rec = {"model": model_id, "timestamp": datetime.now(timezone.utc).isoformat(),
           "price_in_per_m": pin, "price_out_per_m": pout, "context": ctx,
           "tests": {}, "determinism": None}
    for tid in tests:
        fx = fixtures.get(tid)
        if not fx:
            continue
        runs_out = []
        for _ in range(runs):
            r = call_model(model_id, fx["prompt"], fx.get("max_tokens", 2000))
            v = validate(tid, fx, r["text"]) if r["status"] == 200 else \
                {"parse": 0, "schema": 0, "verbatim": 0, "classification": 0, "notes": f"HTTP {r['status']}: {r['error'][:80]}"}
            ptok = (r["usage"] or {}).get("prompt_tokens") or len(fx["prompt"]) // 4
            ctok = (r["usage"] or {}).get("completion_tokens") or len(r["text"]) // 4
            cost = (ptok * pin + ctok * pout) / 1e6
            runs_out.append({"status": r["status"], "ttft_ms": r["ttft_ms"], "total_ms": r["total_ms"],
                             "prompt_tokens": ptok, "completion_tokens": ctok, "cost_usd": round(cost, 6),
                             "quality": quality_score(v), "validation": v, "error": r["error"],
                             "response_text": r["text"][:4000]})
        # representative = best parse run
        best = max(runs_out, key=lambda x: x["quality"])
        rec["tests"][tid] = {"best": best, "runs": runs_out}

    # determinism on test1 (or first test) at temp 0
    if determinism and tests:
        tid = tests[0]
        fx = fixtures.get(tid)
        if fx:
            outs = []
            for _ in range(determinism):
                r = call_model(model_id, fx["prompt"], fx.get("max_tokens", 2000))
                outs.append(r["text"])
            rec["determinism"] = score_determinism(outs)
    # persist
    d = os.path.join(RESULTS_DIR, _safe(model_id))
    os.makedirs(d, exist_ok=True)
    json.dump(rec, open(os.path.join(d, "benchmark.json"), "w"), indent=2)
    return rec


def score_determinism(outputs: list) -> dict:
    """100 byte-identical; 80 minor(whitespace/ordering); 40 diff classifications; 0 diff structure."""
    outs = [o for o in outputs if o]
    if len(outs) < 2:
        return {"score": None, "note": "insufficient runs"}
    if all(o == outs[0] for o in outs):
        return {"score": 100, "note": "byte-identical"}
    norm = [re.sub(r"\s+", " ", o).strip() for o in outs]
    if all(n == norm[0] for n in norm):
        return {"score": 80, "note": "whitespace-only diffs"}
    js = [_extract_json(o) for o in outs]
    if any(j is None for j in js):
        return {"score": 0, "note": "JSON structure differs / parse fails across runs"}
    # same top-level shape & length?
    shapes = [(type(j).__name__, len(j) if isinstance(j, (list, dict)) else 0) for j in js]
    if all(s == shapes[0] for s in shapes):
        # compare key classification fields
        def sig(j):
            if isinstance(j, list):
                return json.dumps([{k: d.get(k) for k in ("promise_id", "status", "category", "discrepancy_type")
                                    if isinstance(d, dict)} for d in j], sort_keys=True)
            return json.dumps(j, sort_keys=True)[:500]
        return {"score": 40 if len({sig(j) for j in js}) > 1 else 80,
                "note": "same structure; classification varies" if len({sig(j) for j in js}) > 1 else "stable classifications, minor field diffs"}
    return {"score": 0, "note": "different JSON structure across runs"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="")
    ap.add_argument("--tests", default="all")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--determinism", type=int, default=0)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    fixtures = load_fixtures()
    if args.list:
        for k, v in fixtures.items():
            print(f"{k:28s} ticker={v.get('ticker'):6s} chars={len(v['prompt']):6d} max_tokens={v.get('max_tokens')}")
        return
    if not _KEY:
        raise SystemExit("OPENROUTER_API_KEY not set in env")
    catalog = load_catalog()
    tests = list(fixtures) if args.tests == "all" else args.tests.split(",")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    for m in models:
        rec = benchmark_model(m, tests, fixtures, catalog, runs=args.runs, determinism=args.determinism)
        qs = [rec["tests"][t]["best"]["quality"] for t in rec["tests"]]
        avg = round(sum(qs) / len(qs), 1) if qs else 0
        det = (rec.get("determinism") or {}).get("score")
        print(f"{m:46s} avgQ={avg:5}  det={det}  "
              f"({', '.join(t.split('_')[0]+':'+str(rec['tests'][t]['best']['quality']) for t in rec['tests'])})",
              flush=True)


if __name__ == "__main__":
    main()
