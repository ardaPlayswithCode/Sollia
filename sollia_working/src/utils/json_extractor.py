"""Universal JSON extractor for LLM output — the single shared parser.

Every agent that parses LLM JSON goes through extract_json(). The models are
prompted to return clean JSON; this module is the belt-and-suspenders layer
that recovers when they don't. Handled failure modes (each covered in
tools/json_extractor_test.py):

  - Markdown fences        ```json ... ```
  - Preamble text          "Here's the output:\n[...]"
  - Trailing text          "[...] Hope this helps!"
  - Trailing commas        [{"a": 1},]
  - Single quotes          [{'a': 1}]
  - Comments               // line and # line comments
  - Python literals        True/False/None -> true/false/null
  - Raw newlines/tabs inside string values (illegal control chars)

Valid JSON passes through unchanged (fast path). On unrecoverable input it
raises ValueError naming the specific failure, never returns garbage.
"""
from __future__ import annotations

import json
import re
from typing import Iterator, List, Tuple, Union

__all__ = ["extract_json"]

_FENCE_RE = re.compile(r"```(?:json|JSON|javascript|python)?\s*(.*?)```", re.DOTALL)
_OPENERS = "[{"
_CLOSERS = {"[": "]", "{": "}"}
_MAX_CANDIDATE_STARTS = 25  # LLM responses are small; this bounds pathological input


def extract_json(text: str) -> Union[list, dict]:
    """Extract valid JSON (list or dict) from raw LLM output.

    Tries, in order: the text as-is, each fenced code block, then every
    balanced [...] / {...} region — each first verbatim, then with repairs
    (comments stripped, single quotes converted, trailing commas removed,
    Python literals mapped, control characters escaped).

    Raises ValueError describing the failure when nothing parses.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty LLM response — nothing to parse")
    text = text.strip()

    # Fast path: already-clean JSON passes through unchanged.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, (list, dict)):
            return parsed
    except json.JSONDecodeError:
        pass

    last_error = "no JSON array or object found in response"
    candidates: List[str] = []

    # Fenced blocks first — when a model wraps output in ```json fences, the
    # fenced content is the authoritative payload.
    for m in _FENCE_RE.finditer(text):
        block = m.group(1).strip()
        if block:
            candidates.append(block)

    candidates.extend(_balanced_regions(text))

    for cand in candidates:
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

    raise ValueError(f"could not extract JSON from LLM response ({last_error}); "
                     f"response began: {text[:120]!r}")


def _balanced_regions(text: str) -> Iterator[str]:
    """Yield candidate substrings that look like complete JSON values.

    For each opener position (left to right), scan forward with a
    string-aware bracket counter to find its balanced close. Unterminated
    regions yield text[start:] so the repair pass can still try.
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
                    yield text[start:i + 1]
                    break
        else:
            yield text[start:]


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
