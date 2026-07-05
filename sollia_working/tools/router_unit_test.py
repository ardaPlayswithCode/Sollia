"""Unit tests for the network-aware router: select_best_tier under every
network condition (injected snapshots), NetworkMonitor state transitions,
org interleaving, and org-of-key resolution."""
import sys
sys.path.insert(0, "/Users/ardaturgut/Desktop/Desktop - Arda’s Mac Studio/Sollia/sollia_working")

import src.config.llm_config as lc

FABLE, G70, G8, GEM = (lc.PROVIDER_IDX["FABLE"], lc.PROVIDER_IDX["GROQ_70B"],
                       lc.PROVIDER_IDX["GROQ_8B"], lc.PROVIDER_IDX["GEMINI"])

NORMAL = {"groq:mac_studio": "normal", "groq:berkeley": "normal",
          "groq:heysollia": "normal", "openrouter:account": "normal",
          "gemini:account": "normal"}
GROQ_STORM = dict(NORMAL, **{"groq:mac_studio": "congested",
                             "groq:berkeley": "congested",
                             "groq:heysollia": "congested"})
OR_DOWN = dict(NORMAL, **{"openrouter:account": "down"})
ALL_BAD_BUT_GROQ = dict(GROQ_STORM, **{"gemini:account": "down"})

passed = failed = 0
def check(name, got, want):
    global passed, failed
    ok = got == want
    passed += ok; failed += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}: {name} (got {got}, want {want})")

print("== select_best_tier: normal network ==")
check("strict json medium", lc.select_best_tier(3000, True, False, network=NORMAL), G70)
check("strict json jumbo", lc.select_best_tier(8000, True, False, network=NORMAL), G70)
check("plain long", lc.select_best_tier(2500, False, False, network=NORMAL), G70)
check("plain short", lc.select_best_tier(400, False, False, network=NORMAL), G8)
check(">90k long-ctx -> Fable5", lc.select_best_tier(95_000, False, True, network=NORMAL), FABLE)

print("== select_best_tier: OpenRouter down ==")
check(">90k avoids Fable5 -> Gemini", lc.select_best_tier(95_000, False, True, network=OR_DOWN), GEM)
check("strict stays Groq", lc.select_best_tier(8000, True, False, network=OR_DOWN), G70)

print("== select_best_tier: total Groq 429 storm ==")
check("strict -> Gemini refuge", lc.select_best_tier(8000, True, False, network=GROQ_STORM), GEM)
check("short -> Gemini refuge", lc.select_best_tier(400, False, False, network=GROQ_STORM), GEM)
check("groq+gemini dead -> Groq anyway (fail-open)",
      lc.select_best_tier(8000, True, False, network=ALL_BAD_BUT_GROQ), G70)

print("== select_best_tier: cost gate ==")
check(">90k at empty budget -> Gemini (soft gate)",
      lc.select_best_tier(95_000, False, True, network=NORMAL, remaining_budget_cents=5.0), GEM)
check(">90k with full budget -> Fable5",
      lc.select_best_tier(95_000, False, True, network=NORMAL, remaining_budget_cents=100.0), FABLE)

print("== NetworkMonitor transitions ==")
m = lc.NetworkMonitor()
check("fresh = normal", m.state_of("Groq-70B", "berkeley"), "normal")
for _ in range(3):
    m.record_result("Groq-70B", "berkeley", 429, 200.0)
check("3x429 = congested", m.state_of("Groq-70B", "berkeley"), "congested")
check("other org unaffected", m.state_of("Groq-70B", "heysollia"), "normal")
check("8B shares the org family view", m.state_of("Groq-8B", "berkeley"), "congested")
m.record_result("Groq-70B", "berkeley", 200, 900.0)
# congested has a TTL; a success resets counters but only fast 200 flips state
check("fast 200 resets to normal", m.state_of("Groq-70B", "berkeley"), "normal")
for _ in range(2):
    m.record_result("Fable5", "account", None, 10000.0)
check("2 transport errors = down", m.state_of("Fable5"), "down")
check("OR family shared with Gemma tiers", m.state_of("OR-Gemma4-31B"), "down")
check("gemini independent", m.state_of("Gemini-2.5-flash"), "normal")
m2 = lc.NetworkMonitor()
m2.record_result("Fable5", "account", None, 195000.0, hard_timeout=True)
check("one hard timeout = down (hang class)", m2.state_of("Fable5"), "down")
m3 = lc.NetworkMonitor()
for _ in range(3):
    m3.record_result("Gemini-2.5-flash", "account", 200, 15000.0)
check("3 slow 200s = degraded", m3.state_of("Gemini-2.5-flash"), "degraded")

print("== org plumbing ==")
il = lc._interleave_org_keys({"a": ["a1", "a2", "a3"], "b": ["b1"], "c": ["c1", "c2"]})
check("interleave hops orgs", il, ["a1", "b1", "c1", "a2", "c2", "a3"])
import os
check("org of berkeley key", lc._org_of_key("Groq-70B", os.environ.get("GROQ_API_KEY_BERKELEY_2", "")), "berkeley")
check("org of heysollia key", lc._org_of_key("Groq-8B", os.environ.get("GROQ_API_KEY_HEYSOLLIA_1", "")), "heysollia")
check("org of mac key", lc._org_of_key("Groq-70B", os.environ.get("GROQ_API_KEY", "")), "mac_studio")
check("openrouter keys are one account", lc._org_of_key("Fable5", os.environ.get("OPENROUTER_API_KEY", "")), "account")
n_groq = len(lc._provider_keys(lc.LLM_PROVIDERS[1]))
check("17 groq keys wired", n_groq, 17)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
