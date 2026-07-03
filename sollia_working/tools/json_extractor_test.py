"""Test suite for src/utils/json_extractor.extract_json — every known LLM
JSON failure mode, plus pass-through of valid JSON. Run:
    python3.11 tools/json_extractor_test.py
Exit 0 = all pass."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.utils.json_extractor import extract_json  # noqa: E402

CASES = [
    # (name, input, expected)
    ("valid array passthrough", '[{"a": 1}, {"b": 2}]', [{"a": 1}, {"b": 2}]),
    ("valid object passthrough", '{"a": {"b": [1, 2]}}', {"a": {"b": [1, 2]}}),
    ("trailing comma array", '[{"a": 1},]', [{"a": 1}]),
    ("trailing comma object", '{"a": 1, "b": 2,}', {"a": 1, "b": 2}),
    ("nested trailing commas", '[{"a": [1, 2,],},]', [{"a": [1, 2]}]),
    ("markdown fence", '```json\n[{"a": 1}]\n```', [{"a": 1}]),
    ("markdown fence no lang", '```\n[{"a": 1}]\n```', [{"a": 1}]),
    ("preamble", 'Here\'s the output:\n[{"a": 1}]', [{"a": 1}]),
    ("preamble + trailing prose", 'Sure! Result:\n[{"a": 1}]\nLet me know if you need more.', [{"a": 1}]),
    ("single quotes", "[{'a': 1}]", [{"a": 1}]),
    ("single quotes with apostrophe-safe content", "[{'quote': 'growth was strong'}]",
     [{"quote": "growth was strong"}]),
    ("line comment //", '[{"a": 1}] // this is a comment', [{"a": 1}]),
    ("comment inside //", '[\n{"a": 1}, // first\n{"b": 2}\n]', [{"a": 1}, {"b": 2}]),
    ("hash comment", '[\n{"a": 1}, # first\n{"b": 2}\n]', [{"a": 1}, {"b": 2}]),
    ("all combined",
     "Here's what you asked for:\n```json\n[\n  {'a': 1, 'note': 'fine',}, // item one\n  {'b': True, 'c': None,},\n]\n```\nHope this helps!",
     [{"a": 1, "note": "fine"}, {"b": True, "c": None}]),
    ("python literals", '[{"ok": True, "bad": False, "missing": None}]',
     [{"ok": True, "bad": False, "missing": None}]),
    ("raw newline in string", '[{"quote": "line one\nline two"}]',
     [{"quote": "line one\nline two"}]),
    ("fenced with preamble", 'The JSON:\n```json\n{"a": [1,2,],}\n```', {"a": [1, 2]}),
    ("hash char inside string survives", '[{"tag": "#1 supplier", "a": 1}]',
     [{"tag": "#1 supplier", "a": 1}]),
    ("slashes inside string survive", '[{"url": "https://sec.gov//x"}]',
     [{"url": "https://sec.gov//x"}]),
    ("apostrophe inside double-quoted string survives",
     '[{"quote": "management\'s plan"}]', [{"quote": "management's plan"}]),
    ("bracket-in-preamble decoy", 'Scores [1-10] follow:\n{"score": 7}', {"score": 7}),
    ("object with embedded fence chars in string",
     '{"code": "use ``` for fences", "n": 1}', {"code": "use ``` for fences", "n": 1}),
    ("BOM/whitespace padding", '\n\n   [{"a": 1}]   \n', [{"a": 1}]),
    ("lone backslash in legal citation", '[{"cite": "28 U.S.C. \\ 1332"}]',
     [{"cite": "28 U.S.C. \\ 1332"}]),
    ("valid escapes survive repair", '[{"s": "a\\"b\\\\c\\nd"}]',
     [{"s": 'a"b\\c\nd'}]),
]

FAILURES = [
    ("empty string", ""),
    ("whitespace only", "   \n  "),
    ("no json at all", "I could not find any risk factor changes."),
    ("scalar only", "42"),
]


def main() -> int:
    passed = failed = 0
    for name, raw, expected in CASES:
        try:
            got = extract_json(raw)
            if got == expected:
                passed += 1
            else:
                failed += 1
                print(f"FAIL {name}: got {got!r}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: raised {exc}")
    for name, raw in FAILURES:
        try:
            got = extract_json(raw)
            failed += 1
            print(f"FAIL {name}: expected ValueError, got {got!r}")
        except ValueError:
            passed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: wrong exception {type(exc).__name__}: {exc}")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
