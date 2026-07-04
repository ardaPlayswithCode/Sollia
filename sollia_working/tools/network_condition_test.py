"""Network-condition testing for the LLM chain (router calibration pass).

Conditions 2-5 (429 storm, provider down, high latency, slow trickle) are
CHAIN-level behaviors: every one of the 8 providers is served by the same
request path (_post_with_wallclock -> key cycling -> fallthrough), so they are
exercised here with LOCAL mock providers spliced into the real chain code —
deterministic, free, and runnable without burning provider quotas or breaking
the day's rate budgets. Real-provider evidence for condition 2 (Groq org-level
429 storm, OpenRouter account/upstream 429s, transient 402) was captured live
earlier in the pass and is cited in LLM_ROUTING_REPORT.md.

Scenarios (each records: serving provider, attempts, wall time, validity):
  normal          control: healthy primary answers
  429-storm       primary always 429s on every key -> key cycling, cutoff at 2
                  consecutive 429s, clean fallthrough
  provider-down   primary port closed (connection refused) -> instant advance
  provider-404    primary returns HTTP 404 -> recorded, advance
  high-latency-5s primary answers after 5s (< read timeout) -> SERVED, no guard
  read-timeout    primary answers after 5s but read timeout is 2s -> advance
  slow-trickle    primary sends headers then drips bytes forever (the
                  Cloudflare-hang class): read timeout NEVER fires; the
                  wall-clock guard must fire at timeout+slack and advance
  hang-memo       after the trickle hang, the provider's next read timeout is
                  clamped (HUNG_PROVIDER_TIMEOUT) and select_best_tier stops
                  promoting >90k-word calls to a hung Fable5

Run:  python3.11 tools/network_condition_test.py
"""
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Speed up the harness: tiny slack so trickle tests take seconds, no global
# cooldown pauses between scenarios, no inter-call spacing.
os.environ["SOLLIA_LLM_WALLCLOCK_SLACK"] = "5"
os.environ["SOLLIA_LLM_COOLDOWN"] = "1"
os.environ["SOLLIA_LLM_RATE_LIMIT"] = "0"
os.environ["SOLLIA_LLM_HISTORY"] = "0"

from src.config import llm_config as lc  # noqa: E402

OK_BODY = json.dumps({
    "choices": [{"message": {"content": '[{"category": "ESCALATED", "ok": true}]'},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
}).encode()


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        mode = self.path.strip("/")
        if mode == "normal":
            time.sleep(0.2)
            self._ok()
        elif mode == "slow5":
            time.sleep(5)
            self._ok()
        elif mode == "storm":
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "rate limit"}}')
        elif mode == "missing":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "no such model"}}')
        elif mode == "trickle":
            # The Cloudflare-hang class: valid headers, then a byte every 2s
            # forever. requests' read timeout never fires (activity continues);
            # only the wall-clock guard can end this.
            self.send_response(200)
            self.send_header("Content-Length", str(10_000))
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(2)
            except Exception:
                pass
        else:
            self._ok()

    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(OK_BODY)))
        self.end_headers()
        self.wfile.write(OK_BODY)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def mock_provider(name, port, mode, timeout=30, n_keys=1, **extra):
    keys = [f"MOCK_KEY_{name}_{i}" for i in range(n_keys)]
    for k in keys:
        os.environ[k] = "mock-" + k
    return {"name": name, "url": f"http://127.0.0.1:{port}/{mode}",
            "model": f"mock/{name}", "key_env": keys[0], "key_envs": keys,
            "tier": "mock", "timeout": timeout, **extra}


def run_scenario(label, providers, expect_provider, expect_wall=None,
                 requires_strict_json=False, tolerance=4.0):
    lc.LLM_PROVIDERS = providers
    lc.reset_run_budget()
    t0 = time.time()
    outcome = {"scenario": label}
    try:
        # pin_tier: the harness tests CHAIN mechanics from tier 0 of the mock
        # chain; the adaptive router would demote a 3-word probe below it.
        text, meta = lc.call_llm("network condition probe", max_tokens=50,
                                 provider_idx=0, pin_tier=True, bypass_cache=True,
                                 requires_strict_json=requires_strict_json,
                                 agent="netcond", stage=label, ticker="mock")
        wall = time.time() - t0
        valid = True
        try:
            from src.utils.json_extractor import extract_json
            extract_json(text)
        except Exception:
            valid = False
        outcome.update({"served_by": meta.get("provider"), "attempts": meta.get("attempts"),
                        "wall_s": round(wall, 2), "valid_json": valid, "error": None})
    except Exception as e:
        wall = time.time() - t0
        outcome.update({"served_by": None, "attempts": None, "wall_s": round(wall, 2),
                        "valid_json": False, "error": f"{type(e).__name__}: {str(e)[:90]}"})
    ok = outcome["served_by"] == expect_provider
    if expect_wall is not None and outcome["wall_s"] is not None:
        ok = ok and abs(outcome["wall_s"] - expect_wall) <= tolerance
    outcome["expected_provider"] = expect_provider
    outcome["pass"] = ok
    print(f"{'PASS' if ok else 'FAIL'} {label:16s} served_by={outcome['served_by']!s:14s} "
          f"attempts={outcome['attempts']!s:4s} wall={outcome['wall_s']}s "
          f"valid={outcome['valid_json']}" + (f" err={outcome['error']}" if outcome["error"] else ""))
    return outcome


def main():
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    dead_port = free_port()  # nothing listens here -> connection refused
    original_providers = lc.LLM_PROVIDERS
    results = []
    fallback = lambda: mock_provider("Fallback", port, "normal")  # noqa: E731

    # 1. NORMAL — healthy primary serves.
    results.append(run_scenario(
        "normal",
        [mock_provider("Primary", port, "normal"), fallback()],
        expect_provider="Primary", expect_wall=0.2, tolerance=2.0))

    # 2. 429 STORM — primary 429s on ALL 3 keys; the _KEY_429_CUTOFF=2 stops
    #    cycling after 2 consecutive 429s; the fallback serves the call.
    results.append(run_scenario(
        "429-storm",
        [mock_provider("Storming", port, "storm", n_keys=3), fallback()],
        expect_provider="Fallback", expect_wall=1.0, tolerance=3.0))

    # 3a. PROVIDER DOWN — connection refused advances instantly.
    results.append(run_scenario(
        "provider-down",
        [mock_provider("Dead", dead_port, "normal"), fallback()],
        expect_provider="Fallback", expect_wall=0.5, tolerance=2.5))

    # 3b. PROVIDER 404 — bad endpoint path (wrong URL class) advances.
    results.append(run_scenario(
        "provider-404",
        [mock_provider("Missing", port, "missing"), fallback()],
        expect_provider="Fallback", expect_wall=0.5, tolerance=2.5))

    # 4a. HIGH LATENCY (5s < read timeout) — primary still serves; the
    #     wall-clock guard must NOT fire.
    results.append(run_scenario(
        "high-latency-5s",
        [mock_provider("Slow", port, "slow5", timeout=30), fallback()],
        expect_provider="Slow", expect_wall=5.2, tolerance=2.0))

    # 4b. READ TIMEOUT (5s delay > 2s read timeout) — socket timeout advances.
    results.append(run_scenario(
        "read-timeout",
        [mock_provider("TooSlow", port, "slow5", timeout=2), fallback()],
        expect_provider="Fallback", expect_wall=2.3, tolerance=2.0))

    # 5. SLOW TRICKLE (Cloudflare-hang class) — read timeout NEVER fires; the
    #    wall-clock guard must fire at timeout+slack (3+5=8s here; production
    #    slack is 15s) and the chain must advance.
    results.append(run_scenario(
        "slow-trickle",
        [mock_provider("Trickler", port, "trickle", timeout=3), fallback()],
        expect_provider="Fallback", expect_wall=8.3, tolerance=2.5))

    # 6. HANG MEMO — the trickler breached the wall clock above, so (a) its
    #    read timeout is clamped for the next call, (b) select_best_tier stops
    #    promoting >90k-word long-context calls to a hung "Fable5".
    memo_ok = lc._recently_hung("Trickler")
    lc._note_hard_timeout("Fable5")
    tier = lc.select_best_tier(prompt_word_count=95_000, requires_long_context=True)
    memo_routes = tier == lc.PROVIDER_IDX["GROQ_70B"]
    print(f"{'PASS' if memo_ok else 'FAIL'} hang-memo        Trickler remembered as hung")
    print(f"{'PASS' if memo_routes else 'FAIL'} hang-reroute     >90k-word call routed to "
          f"Groq-70B while Fable5 memo is fresh (tier={tier})")
    results.append({"scenario": "hang-memo", "pass": bool(memo_ok)})
    results.append({"scenario": "hang-reroute", "pass": bool(memo_routes)})

    lc.LLM_PROVIDERS = original_providers
    server.shutdown()
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       ".test_results", "network_condition_results.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    n_pass = sum(1 for r in results if r.get("pass"))
    print(f"\n{n_pass}/{len(results)} scenarios pass — saved {out}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
