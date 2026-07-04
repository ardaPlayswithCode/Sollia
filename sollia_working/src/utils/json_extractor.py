"""Universal JSON extractor for LLM output — the single shared parser.

Every agent that parses LLM JSON goes through extract_json(). The models are
prompted to return clean JSON; this module is the belt-and-suspenders layer
that recovers when they don't. Handled failure modes (each covered in
tools/json_extractor_test.py; the 2026-07-03 entries come from the JSON-format
audit — tools/json_format_research.py, 5 live samples per chain model):

  - Markdown fences        ```json ... ```
  - Preamble text          "Here's the output:\n[...]"
  - Trailing text          "[...] Hope this helps!"
  - Trailing commas        [{"a": 1},]
  - Single quotes          [{'a': 1}]
  - Comments               // line and # line comments
  - Python literals        True/False/None -> true/false/null
  - Raw newlines/tabs inside string values (illegal control chars)
  - TRUNCATED output       (2026-07-03) response cut mid-array/mid-string when a
                           thinking model starves its completion budget (observed
                           on every live Gemini-2.5-flash sample: finish=length at
                           ~400 chars, 2/3 ALSO inside an unclosed ```json fence).
                           Recovery drops the incomplete trailing element and
                           closes the root — the parseable prefix beats a hard
                           parse failure on a fallback-tier response.
  - Provider error bodies  (2026-07-03) {"error": {...}} is valid JSON but is a
                           rate-limit/refusal envelope, never agent data — raised
                           as ValueError so parse-failure retries fire.

Pre-parse validation (Part 2c): preparse_validate() classifies a raw response
BEFORE extraction (not JSON at all / provider error / rate-limit text), so
callers can retry instead of parsing garbage. extract_json() runs the same
checks internally — callers get the protection with zero call-site changes.

Valid JSON passes through unchanged (fast path). On unrecoverable input it
raises ValueError naming the specific failure, never returns garbage.
"""
from __future__ import annotations

import json
import re
from typing import Iterator, List, Optional, Tuple, Union

__all__ = ["extract_json", "preparse_validate"]

_FENCE_RE = re.compile(r"```(?:json|JSON|javascript|python)?\s*(.*?)```", re.DOTALL)
_OPENERS = "[{"
_CLOSERS = {"[": "]", "{": "}"}
_MAX_CANDIDATE_STARTS = 25  # LLM responses are small; this bounds pathological input

# Error-shaped text (only consulted when the response has NO JSON opener, so
# legitimate payloads that merely mention "error"/"429" are never affected).
_ERROR_PAT = re.compile(r"429|rate.?limit|quota|too many requests|^\s*error\b",
                        re.IGNORECASE)


def preparse_validate(text: str) -> Tuple[bool, str]:
    """Cheap validation BEFORE extraction (Part 2c). Returns (ok, reason).

    Not-ok reasons ("retry, don't parse"):
      empty                 - nothing to parse
      no_json               - no '[' or '{' anywhere (prose/refusal)
      error_response        - error-shaped text ("429", "rate limit", "error"
                              lead-in) with no JSON payload
      provider_error_body   - parses as {"error": ...} (a provider envelope)
      long_response_no_array- >10,000 chars without a single '[' (refusal or
                              error dump; every Sollia schema contains arrays)
    """
    if not isinstance(text, str) or not text.strip():
        return False, "empty"
    s = text.strip()
    if "[" not in s and "{" not in s:
        if _ERROR_PAT.search(s):
            return False, "error_response"
        return False, "no_json"
    if len(s) > 10_000 and "[" not in s:
        return False, "long_response_no_array"
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict) and set(parsed.keys()) == {"error"}:
            return False, "provider_error_body"
    except json.JSONDecodeError:
        pass
    return True, "ok"


def extract_json(text: str) -> Union[list, dict]:
    """Extract valid JSON (list or dict) from raw LLM output.

    Tries, in order: the text as-is, each fenced code block, then every
    balanced [...] / {...} region — each first verbatim, then with repairs
    (comments stripped, single quotes converted, trailing commas removed,
    Python literals mapped, control characters escaped). As a last resort,
    truncated output is completed (incomplete trailing element dropped, root
    closed) — see module docstring.

    Raises ValueError describing the failure when nothing parses.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty LLM response — nothing to parse")
    text = text.strip()

    # Fast path: already-clean JSON passes through unchanged — except a bare
    # provider error envelope, which is valid JSON but never agent data.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and set(parsed.keys()) == {"error"}:
            raise ValueError(
                "LLM response is a provider error body, not data: "
                f"{text[:120]!r}")
        if isinstance(parsed, (list, dict)):
            return parsed
    except json.JSONDecodeError:
        pass

    # Pre-parse validation (Part 2c): classify non-JSON shapes up front so the
    # error names the real problem (rate-limit text, refusal prose, error dump)
    # instead of a generic "no JSON found".
    ok, reason = preparse_validate(text)
    if not ok:
        raise ValueError(f"LLM response failed pre-parse validation ({reason}); "
                         f"response began: {text[:120]!r}")

    last_error = "no JSON array or object found in response"

    def _try(cand: str):
        nonlocal last_error
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, (list, dict)):
                return parsed
        except json.JSONDecodeError as exc:
            last_error = f"direct parse failed: {exc}"
        try:
            parsed = json.loads(_repair(cand))
            if isinstance(parsed, (list, dict)):
                return parsed
        except json.JSONDecodeError as exc:
            last_error = f"parse after repair failed: {exc}"
        return None

    # Fenced blocks first — when a model wraps output in ```json fences, the
    # fenced content is the authoritative payload.
    for m in _FENCE_RE.finditer(text):
        block = m.group(1).strip()
        if block:
            got = _try(block)
            if got is not None:
                return got

    # Balanced regions, outermost first. The FIRST region that runs off the end
    # of the text is the truncation root: completion (drop the incomplete
    # trailing element, close the root) is attempted on IT — and only it —
    # BEFORE falling back to inner balanced sub-regions, which would otherwise
    # "win" with a lone inner object where the caller expects the whole array.
    # Completion never applies to inner regions: salvaging a fragment of a
    # fragment is how garbage gets returned.
    completion_attempted = False
    for region, terminated in _balanced_regions(text):
        got = _try(region)
        if got is not None:
            return got
        if terminated or completion_attempted:
            continue
        completion_attempted = True
        # Truncated output (2026-07-03 audit: every live Gemini sample) — try
        # the raw region, then its repaired form (single quotes etc. would
        # otherwise hide string boundaries from the completion walker).
        for base in (region, _repair(region)):
            completed = _complete_truncated(base)
            if completed is None:
                continue
            for attempt in (completed, _repair(completed)):
                try:
                    parsed = json.loads(attempt)
                    if isinstance(parsed, (list, dict)) and parsed:
                        return parsed
                except json.JSONDecodeError as exc:
                    last_error = f"parse after truncation-completion failed: {exc}"

    raise ValueError(f"could not extract JSON from LLM response ({last_error}); "
                     f"response began: {text[:120]!r}")


def _complete_truncated(candidate: str) -> Optional[str]:
    """Best-effort completion of a TRUNCATED JSON value.

    Walks the candidate with a string-aware bracket stack; if it ends
    unbalanced (or inside a string), cuts back to the end of the last COMPLETE
    element at depth 1 and closes the root. Returns None when the candidate is
    already balanced (not truncated) or no complete element exists — the
    caller falls through to the normal failure path. Never invents content:
    the result is always a strict prefix of the model's output plus closers.
    """
    if not candidate or candidate[0] not in _OPENERS:
        return None
    depth = 0
    in_str = False
    escape = False
    last_safe = None   # index AFTER the close of a complete depth-1 element
    last_comma = None  # index OF a depth-1 comma (everything before is complete)
    for i, ch in enumerate(candidate):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
                # A depth-1 string close is only a safe cut in an ARRAY root —
                # inside an object it may be a key awaiting its value.
                if depth == 1 and candidate[0] == "[":
                    last_safe = i + 1
            continue
        if ch == '"':
            in_str = True
        elif ch in _OPENERS:
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 1:
                last_safe = i + 1
            elif depth <= 0:
                return None  # root closed — not truncated
        elif ch == "," and depth == 1:
            last_comma = i   # scalar elements (numbers/indices) end at commas
    if depth <= 0 and not in_str:
        return None  # balanced after all
    cut = last_safe if last_safe is not None else last_comma
    if cut is None:
        return None  # nothing complete to salvage
    head = candidate[:cut].rstrip().rstrip(",")
    return head + _CLOSERS[candidate[0]]


def _balanced_regions(text: str) -> Iterator[Tuple[str, bool]]:
    """Yield (candidate, terminated) substrings that look like JSON values.

    For each opener position (left to right), scan forward with a
    string-aware bracket counter to find its balanced close. Unterminated
    regions yield (text[start:], False) — the truncation-completion pass in
    extract_json keys off that flag.
    """
    starts = [i for i, ch in enumerate(text) if ch in _OPENERS]
    for start in starts[:_MAX_CANDIDATE_STARTS]:
        opener = text[start]
        closer = _CLOSERS[opener]
        depth = 0
        in_str = False
        escape = False
        quote = ""
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_str = False
                continue
            if ch in "\"'":
                in_str, quote = True, ch
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1], True
                    break
        else:
            yield text[start:], False


def _repair(candidate: str) -> str:
    """Best-effort normalization of almost-JSON into JSON."""
    normalized = _normalize_strings_and_comments(candidate)
    segments = _split_string_segments(normalized)
    out: List[str] = []
    for is_string, seg in segments:
        if is_string:
            # Raw control characters are illegal inside JSON strings.
            seg = seg.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            # A lone backslash that isn't a valid JSON escape (LLMs emit these
            # in legal citations like "28 U.S.C. \ 1332") gets doubled.
            seg = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", seg)
            out.append(seg)
        else:
            seg = re.sub(r",\s*([\]}])", r"\1", seg)          # trailing commas
            seg = re.sub(r"\bTrue\b", "true", seg)             # Python literals
            seg = re.sub(r"\bFalse\b", "false", seg)
            seg = re.sub(r"\bNone\b", "null", seg)
            out.append(seg)
    return "".join(out)


def _normalize_strings_and_comments(s: str) -> str:
    """One character walk: strip // and # comments (outside strings) and
    convert single-quoted strings to double-quoted (escaping inner quotes)."""
    out: List[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == '"':                                   # double-quoted string: copy verbatim
            out.append(ch)
            i += 1
            while i < n:
                out.append(s[i])
                if s[i] == "\\" and i + 1 < n:
                    out.append(s[i + 1])
                    i += 2
                    continue
                if s[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "'":                                   # single-quoted string: convert
            out.append('"')
            i += 1
            while i < n:
                c = s[i]
                if c == "\\" and i + 1 < n:
                    nxt = s[i + 1]
                    out.append(nxt if nxt == "'" else "\\" + nxt)
                    i += 2
                    continue
                if c == "'":
                    i += 1
                    break
                out.append('\\"' if c == '"' else c)
                i += 1
            out.append('"')
            continue
        if ch == "/" and i + 1 < n and s[i + 1] == "/":  # // comment
            while i < n and s[i] != "\n":
                i += 1
            continue
        if ch == "#":                                    # # comment
            while i < n and s[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_string_segments(s: str) -> List[Tuple[bool, str]]:
    """Split into alternating (is_string, segment) runs. Assumes strings are
    double-quoted (run _normalize_strings_and_comments first)."""
    segments: List[Tuple[bool, str]] = []
    buf: List[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == '"':
            if buf:
                segments.append((False, "".join(buf)))
                buf = []
            j = i + 1
            while j < n:
                if s[j] == "\\":
                    j += 2
                    continue
                if s[j] == '"':
                    break
                j += 1
            segments.append((True, s[i:min(j + 1, n)]))
            i = j + 1
        else:
            buf.append(s[i])
            i += 1
    if buf:
        segments.append((False, "".join(buf)))
    return segments
