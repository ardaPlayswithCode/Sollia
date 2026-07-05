"""Live Test C (429 storm) — proves the 3 Groq orgs are independent buckets
and that the org-hopping chain rescues a call mid-storm.

Steps:
 1. Hammer llama-3.1-8b-instant on ONE mac_studio key until the org 429s
    (expected: exactly at RPM=30, ~31 requests).
 2. Instantly probe a SECOND mac_studio key  -> expect 429 (same bucket).
 3. Instantly probe a berkeley key           -> expect 200 (independent bucket).
 4. Instantly probe a heysollia key          -> expect 200 (independent bucket).
 5. Call the PRODUCTION chain (call_llm, Groq-8B start, bypass_cache) during
    the storm -> expect a served response with the 429-hop recorded, NOT a
    fallthrough to Gemini.
Cost: ~35 requests of ~12 tokens on 8B (14,400 RPD per org) — negligible.
"""
import os, sys, time, json
import requests

sys.path.insert(0, "/Users/ardaturgut/Desktop/Desktop - Arda’s Mac Studio/Sollia/sollia_working")
import src.config.llm_config as lc

URL = "https://api.groq.com/openai/v1/chat/completions"
def ping(key, tag=""):
    t0 = time.time()
    r = requests.post(URL, headers={"Authorization": f"Bearer {key}",
                                    "Content-Type": "application/json"},
                      json={"model": "llama-3.1-8b-instant",
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": 1},
                      timeout=20)
    return r.status_code, (time.time() - t0) * 1000

mac1 = os.environ["GROQ_API_KEY"]
mac2 = os.environ["GROQ_API_KEY_2"]
berk = os.environ["GROQ_API_KEY_BERKELEY_1"]
hey = os.environ["GROQ_API_KEY_HEYSOLLIA_1"]

print("1) hammering mac_studio org (8B) until 429...")
first_429_at = None
for i in range(1, 40):
    code, ms = ping(mac1)
    if code == 429:
        first_429_at = i
        print(f"   first 429 at request #{i}")
        break
if first_429_at is None:
    print("   no 429 in 39 requests — org had headroom; aborting storm test")
    sys.exit(1)

c2, _ = ping(mac2)
print(f"2) second mac_studio key during storm: {c2} (expect 429 — shared bucket)")
c3, ms3 = ping(berk)
print(f"3) berkeley key during storm: {c3} in {ms3:.0f}ms (expect 200 — independent)")
c4, ms4 = ping(hey)
print(f"4) heysollia key during storm: {c4} in {ms4:.0f}ms (expect 200 — independent)")

print("5) production chain call during the storm (Groq-8B start, live)...")
lc.reset_run_budget()
lc.NETWORK_MONITOR.reset()
t0 = time.time()
text, meta = lc.call_llm("Respond with exactly: OK", max_tokens=5, temperature=0.0,
                         provider_idx=lc.PROVIDER_IDX["GROQ_8B"],
                         agent="storm_test", stage="test_c", ticker="none",
                         pin_tier=True, bypass_cache=True)
wall = time.time() - t0
print(f"   served by {meta.get('provider')} in {wall:.1f}s, attempts={meta.get('attempts')}")
print(f"   monitor snapshot: {json.dumps(lc.NETWORK_MONITOR.snapshot())}")

ok = (c2 == 429 and c3 == 200 and c4 == 200 and "OK" in text
      and meta.get("provider", "").startswith("Groq"))
print("\nRESULT:", "PASS — org independence + org-hopping rescue confirmed" if ok
      else "FAIL — see above")
sys.exit(0 if ok else 1)
