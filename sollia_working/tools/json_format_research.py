"""Part 2a — JSON format research: what does each chain model ACTUALLY return?

Sends the REAL Agent-4 risk prompt (benchmark fixture test4) N times to each
model — direct HTTP, no chain fallthrough, so every sample is genuinely from
the target model — and records the RAW response before any parsing.

Resumable: one file per (model, attempt) under
.test_results/json_format_research/; existing samples are never re-fetched
(re-running after the OpenRouter :free daily reset fills in the backups).

Usage:
    python3.11 tools/json_format_research.py            # fetch missing samples
    python3.11 tools/json_format_research.py --report   # classification table only
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config.llm_config import (LLM_PROVIDERS, fable5_effort_level,  # noqa: E402
                                   FABLE5_MIN_REASONING)
import requests  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, ".cache", "benchmark_fixtures", "test4_agent4_risk.json")
OUT_DIR = os.path.join(ROOT, ".test_results", "json_format_research")

# samples per model: 5 for free tiers; 3 for Fable 5 (PAID ~$0.13/call — its
# behavior on this exact shape was already measured 3x in routing_benchmark:
# fenced / empty / truncated. 3 fresh + 3 prior = 6 total observations).
PLAN = [
    ("Fable5", 3),
    ("Groq-70B", 5),
    ("Groq-8B", 5),
    ("Gemini-2.5-flash", 5),
    ("OR-Gemma4-31B", 5),
    ("OR-Gemma4-26B", 5),
    ("OR-Nemotron3-Super", 5),
    ("OR-Llama33-70B", 5),
]

PROVIDERS = {p["name"]: p for p in LLM_PROVIDERS}


def fetch_one(provider, prompt, max_tokens):
    key = os.environ.get(provider["key_env"], "")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    if "openrouter.ai" in provider["url"]:
        headers["HTTP-Referer"] = "https://sollia.app"
        headers["X-Title"] = "Sollia"
    payload = {"model": provider["model"],
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.0, "max_tokens": max_tokens}
    if provider.get("adaptive_reasoning"):
        rbudget = max(fable5_effort_level(len(prompt.split())), FABLE5_MIN_REASONING)
        payload["reasoning"] = {"max_tokens": rbudget}
        payload["max_tokens"] = max_tokens + rbudget
    t0 = time.time()
    r = requests.post(provider["url"], headers=headers, json=payload,
                      timeout=int(provider.get("timeout", 90)))
    ms = round((time.time() - t0) * 1000)
    if r.status_code != 200:
        return {"status": r.status_code, "raw": None, "error": r.text[:300], "ms": ms}
    data = r.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    return {"status": 200, "raw": content, "usage": data.get("usage"), "ms": ms,
            "finish_reason": (data.get("choices") or [{}])[0].get("finish_reason")}


# ---------------------------------------------------------------------------
# Variant classification (Part 2a table columns)
# ---------------------------------------------------------------------------
def classify(raw):
    """Return a dict of booleans for each format-variant column."""
    v = {"clean": False, "fenced": False, "preamble": False, "trailing_comma": False,
         "single_quotes": False, "comments": False, "truncated": False, "other": ""}
    if raw is None or not raw.strip():
        v["other"] = "EMPTY"
        return v
    s = raw.strip()
    try:
        json.loads(s)
        v["clean"] = True
        return v
    except json.JSONDecodeError:
        pass
    if "```" in s:
        v["fenced"] = True
    first_bracket = min([i for i in (s.find("["), s.find("{")) if i >= 0] or [-1])
    if first_bracket > 0 and not v["fenced"]:
        v["preamble"] = True
    if first_bracket == -1:
        v["other"] = "NO-JSON (prose/refusal)"
        return v
    body = s[first_bracket:]
    # fenced content: classify what's inside the fence too
    m = re.search(r"```(?:json)?\s*(.*?)(?:```|$)", s, re.DOTALL)
    if v["fenced"] and m:
        body = m.group(1).strip()
        if s.index("```") > 0:
            v["preamble"] = True
    if re.search(r",\s*[\]}]", body):
        v["trailing_comma"] = True
    if re.search(r"(?<![\w\"])'[^']*'\s*:", body) or re.search(r":\s*'[^']*'", body):
        v["single_quotes"] = True
    if re.search(r"^\s*//", body, re.MULTILINE) or re.search(r"^\s*#", body, re.MULTILINE):
        v["comments"] = True
    # truncation: bracket balance of the JSON body (string-aware quick check)
    depth = 0
    in_str = False
    esc = False
    for ch in body:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
    if depth != 0 or in_str:
        v["truncated"] = True
    return v


def report():
    cols = ["clean", "fenced", "preamble", "trailing_comma", "single_quotes",
            "comments", "truncated"]
    print(f"\n{'Model':20s} | " + " | ".join(f"{c:>14s}" for c in cols) + " | other")
    print("-" * 150)
    table = {}
    for name, want in PLAN:
        d = os.path.join(OUT_DIR, re.sub(r"[^\w.-]", "_", name))
        rows = []
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                with open(os.path.join(d, fn)) as f:
                    rec = json.load(f)
                if rec.get("status") == 200:
                    rows.append(classify(rec.get("raw")))
        n = len(rows)
        if not n:
            print(f"{name:20s} | " + " | ".join(f"{'—':>14s}" for _ in cols) + " | no samples yet")
            continue
        counts = {c: sum(1 for r in rows if r[c]) for c in cols}
        others = [r["other"] for r in rows if r["other"]]
        table[name] = {"n": n, **counts, "other": others}
        print(f"{name:20s} | " + " | ".join(f"{counts[c]}/{n:>2}".rjust(14) for c in cols)
              + f" | {', '.join(others) if others else '—'}")
    with open(os.path.join(OUT_DIR, "_summary.json"), "w") as f:
        json.dump(table, f, indent=2)
    return table


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="classification table only")
    ap.add_argument("--only", help="comma-separated model names to fetch")
    args = ap.parse_args()
    if args.report:
        report()
        return

    with open(FIXTURE) as f:
        fx = json.load(f)
    prompt, max_tokens = fx["prompt"], fx.get("max_tokens", 2500)
    only = set(args.only.split(",")) if args.only else None

    # Round-robin across models so separate rate buckets (Groq per-model TPM,
    # Gemini, OpenRouter) interleave instead of serializing.
    pending = []
    for name, want in PLAN:
        if only and name not in only:
            continue
        d = os.path.join(OUT_DIR, re.sub(r"[^\w.-]", "_", name))
        os.makedirs(d, exist_ok=True)
        have = len([fn for fn in os.listdir(d) if fn.endswith(".json")])
        for i in range(have, want):
            pending.append((name, i))
    print(f"{len(pending)} samples to fetch")

    stall = {}  # model -> retry-not-before timestamp
    while pending:
        progressed = False
        for item in list(pending):
            name, i = item
            if time.time() < stall.get(name, 0):
                continue
            provider = PROVIDERS[name]
            print(f"[{name} #{i + 1}] ...", end=" ", flush=True)
            try:
                rec = fetch_one(provider, prompt, max_tokens)
            except Exception as e:
                print(f"EXC {type(e).__name__}: {e}")
                stall[name] = time.time() + 30
                continue
            if rec["status"] == 429:
                ra = 65
                m = re.search(r"in\s+([\d.]+)s", rec.get("error") or "")
                if m:
                    ra = float(m.group(1)) + 2
                daily = "per-day" in (rec.get("error") or "")
                if daily:
                    print("429 DAILY-QUOTA — skipping model this run")
                    pending = [p for p in pending if p[0] != name]
                else:
                    print(f"429 — retry in {ra:.0f}s")
                    stall[name] = time.time() + min(ra, 120)
                continue
            d = os.path.join(OUT_DIR, re.sub(r"[^\w.-]", "_", name))
            with open(os.path.join(d, f"attempt_{i + 1}.json"), "w") as f:
                json.dump({"model": provider["model"], **rec}, f, indent=2)
            pending.remove(item)
            progressed = True
            raw = rec.get("raw") or ""
            print(f"{rec['status']} {rec['ms']}ms {len(raw)}ch "
                  f"finish={rec.get('finish_reason')}")
        if pending and not progressed:
            nxt = min(stall.get(n, time.time() + 5) for n, _ in pending)
            wait = max(1.0, nxt - time.time())
            time.sleep(min(wait, 65))
    report()


if __name__ == "__main__":
    main()
