"""Grounds a narration against the engine's own trace: every number the LLM
says out loud should be traceable to a number the engine actually computed.
This is what turns "the bot said a number" into "the bot said a number we can
prove," and is the automatic half of the pass/fail marking in query_log.py.

Deliberately conservative: it flags a MISMATCH only when a number in the
narration doesn't correspond (within tolerance) to anything in the trace.
Small phrasing numbers (e.g. "the top 5 products") are handled by ignoring
integers that also appear in the trace's own step details (so "5" matching
the requested top_n doesn't get treated as a hallucinated financial figure)
and by an integer allowlist for small counting numbers.

The system prompt has the bot speak in Hindi/Hinglish using Indian
lakh/crore numbering ("10.45 crore", "10 करोड़ 45 लाख") rather than the raw
digit string -- this is expected, correct narration, not a hallucination, so
lakh/crore/thousand terms are resolved to their actual value (combining
adjacent terms like "10 crore 45 lakh" into one number) before comparing.
"""
from __future__ import annotations

import re

# Product/SKU codes returned by the engine itself (e.g. "BL972", "JE889")
# glue digits directly onto letters. A plain \d+ regex with edge-only
# lookaround still matches INSIDE such a token once the first attempt is
# blocked (e.g. "BL972" blocks a match starting at "9", but "72" still
# matches starting one character later, since "9" isn't a letter). So
# numbers are extracted in two passes: tokenize on whole alnum runs first,
# then keep only tokens that are ENTIRELY digits/punctuation -- a token
# containing any letter anywhere is an identifier, never partially treated
# as a number.
_TOKEN_RE = re.compile(r"-?[A-Za-z0-9][A-Za-z0-9.,]*")
_PURE_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")
_SMALL_INTEGER_ALLOWLIST = set(range(0, 32))  # "top 5", "3 suppliers", "30 June" (day-of-month) etc.
_PLAUSIBLE_YEAR_RANGE = range(2000, 2100)  # "as of 30 June 2026" -- a year, not a financial figure
# Spoken narration rounds casually ("more than 10 crore", "around X") even
# when the underlying number is 10.45 crore -- 1% was too tight and flagged
# normal rounding language as a mismatch. 5% catches genuine fabrications
# (the test case of "50 crore" vs an actual ~10.45 crore is 380% off) while
# tolerating realistic spoken approximation.
_RELATIVE_TOLERANCE = 0.05

_MULTIPLIERS = {
    "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "लाख": 1e5,
    "crore": 1e7, "crores": 1e7, "cr": 1e7, "करोड़": 1e7, "करोड": 1e7,
    "thousand": 1e3, "हज़ार": 1e3, "हजार": 1e3,
}
_SCALED_NUMBER_RE = re.compile(
    r"(?P<num>\d[\d,]*\.?\d*)\s*(?P<mult>lakhs?|lacs?|crores?|cr\b|thousand|लाख|करोड़?|हज़ार|हजार)",
    re.IGNORECASE,
)
# "10 crore 45 lakh" -- combine scaled terms this close together (chars).
# Genuine compound terms are separated by just a space (~1-2 chars); this
# needs to stay small, or two SEPARATE number mentions a short connector
# apart (e.g. "45 lakh — bulki 1.05 crore", a self-correction restating a
# different figure) get wrongly summed into one bogus combined number.
_COMBINE_GAP_CHARS = 4


def _parse_numbers(text: str) -> list[float]:
    scaled_spans: list[tuple[int, int, float]] = []
    for m in _SCALED_NUMBER_RE.finditer(text):
        raw = m.group("num").replace(",", "")
        mult = _MULTIPLIERS.get(m.group("mult").lower())
        if mult is None:
            continue
        try:
            scaled_spans.append((m.start(), m.end(), float(raw) * mult))
        except ValueError:
            continue

    combined: list[float] = []
    consumed: list[tuple[int, int]] = []
    i = 0
    while i < len(scaled_spans):
        start, end, value = scaled_spans[i]
        total = value
        j = i + 1
        while j < len(scaled_spans) and scaled_spans[j][0] - end < _COMBINE_GAP_CHARS:
            total += scaled_spans[j][2]
            end = scaled_spans[j][1]
            j += 1
        combined.append(total)
        consumed.append((start, end))
        i = j

    out = list(combined)
    for match in _TOKEN_RE.finditer(text):
        if any(s <= match.start() < e for s, e in consumed):
            continue
        token = match.group()
        if not _PURE_NUMBER_RE.fullmatch(token):
            continue  # contains a letter somewhere -- an identifier/code, not a number
        raw = token.replace(",", "")
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def extract_numbers(obj) -> list[float]:
    """Recursively collects every number in a nested dict/list -- used both
    for a trace step's `result` field and for a QueryLogEntry's top-level
    `result` dict (the actual payload handed to the LLM to narrate from)."""
    out: list[float] = []

    def collect(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            out.append(float(v))
        elif isinstance(v, dict):
            for x in v.values():
                collect(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                collect(x)

    collect(obj)
    return out


def numbers_from_serialized_trace(trace: dict) -> list[float]:
    """Same extraction as trace.StepTrace.all_numeric_results(), but over
    the already-serialized (to_dict()'d) trace stored in a QueryLogEntry --
    {"tables_queried": [...], "computations": [...], "steps": [...]}.

    NOTE: a trace step's `result` can record an intermediate value on a
    different scale than what's actually returned to the LLM (e.g.
    formulas.gross_margin's trace step records the raw 0-1 fraction, while
    the compute function's returned result multiplies it by 100 for a
    percent). Always ground against numbers_from_serialized_trace(trace) +
    extract_numbers(entry.result) TOGETHER (see query_log.finalize_log_entries)
    -- the result dict is the canonical, LLM-facing source of truth.
    """
    out: list[float] = []
    for step in trace.get("steps", []):
        out.extend(extract_numbers(step.get("result")))
    return out


def check_narration(narration: str, grounded_numbers: list[float]) -> dict:
    """Returns {"grounded": bool, "unmatched": [floats], "checked": [floats]}."""
    narrated = _parse_numbers(narration)
    # A negative change (e.g. profit_change_pct = -20.87) is standardly -- and
    # correctly -- narrated as a positive magnitude plus a direction word
    # ("21% ki giravat", "a 21% decline"), not with the minus sign spoken
    # aloud. Match against |g| too, not just g itself.
    pool = list(grounded_numbers) + [abs(g) for g in grounded_numbers]
    unmatched = []
    for n in narrated:
        if n == int(n) and (int(n) in _SMALL_INTEGER_ALLOWLIST or int(n) in _PLAUSIBLE_YEAR_RANGE):
            continue
        if any(abs(n - g) <= max(abs(g) * _RELATIVE_TOLERANCE, 0.5) for g in pool):
            continue
        unmatched.append(n)
    return {"grounded": len(unmatched) == 0, "unmatched": unmatched, "checked": narrated}
