#!/usr/bin/env python
"""Automated eval scorer: reads structured test cases (see
baseline_seed42/automated_eval_cases.json for the schema + real examples),
drives them through the running server's real HTTP API (POST /ask, then the
admin API for the resulting query_log entry) -- never imports server code
directly, same eval/-must-stay-outside-the-engine boundary as score_report.py.

Checks four independent layers per case:
  1. resolved_intent  -- did the actor pick the right metric_category/sub_metric/etc.
  2. result            -- did the engine compute the right numbers (subset match, tolerance-aware)
  3. trace              -- did it touch the right tables and stay within a sane step budget
                            (catches "right answer, wrong/runaway reasoning", and is a free,
                            automated version of the "does this ever read ground_truth.json"
                            isolation check)
  4. judge              -- optional, explicit mode: "advisory" (recorded, never fails the case)
                            or "blocking" (a judge fail -- or, with judge_check.runs > 1, a
                            pass_rate below judge_check.min_pass_rate across N independent
                            re-asks -- fails the case). Judge verdicts are themselves an LLM
                            call and not deterministic call-to-call, so this is opt-in per case
                            rather than assumed.

Layers 1-3 are the deterministic pass/fail signal. `category` +
`expected_to_pass_today` then decide what a failure MEANS for the report:
  - expected_to_pass_today=true and it failed  -> BLOCKER_FAIL (a real regression)
  - expected_to_pass_today=true and it passed  -> PASS
  - expected_to_pass_today=false and it failed -> KNOWN_GAP_OPEN (steady state, not a build breaker)
  - expected_to_pass_today=false and it passed -> GAP_CLOSED (good news -- the fixture should be flipped to a regression case)
Exit code is 1 only if any BLOCKER_FAIL exists, so this is safe to wire into CI --
a suite with open known gaps stays green until one of THOSE flips unexpectedly too.

Usage:
    ADMIN_KEY=... python eval/scorer.py --server http://localhost:8010 \
        --cases eval/baseline_seed42/automated_eval_cases.json
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests


# ---------------------------------------------------------------------
# Layer 2: tolerant, partial (subset-match) structural comparison
# ---------------------------------------------------------------------

def compare_value(expected: Any, actual: Any, default_tolerance_pct: float, path: str) -> list[str]:
    """Returns mismatch descriptions; empty list = match. `expected` may be a
    plain value, {"one_of": [...]}, or {"value": ..., "tolerance_pct": ...}
    for a per-field tolerance override. Dicts are matched as a SUBSET --
    only keys present in `expected` are checked, extra keys in `actual` are
    ignored -- so a case only has to assert what it actually cares about."""
    if isinstance(expected, dict) and "one_of" in expected and len(expected) == 1:
        if actual not in expected["one_of"]:
            return [f"{path}: expected one of {expected['one_of']!r}, got {actual!r}"]
        return []
    if isinstance(expected, dict) and "value" in expected and set(expected) <= {"value", "tolerance_pct"}:
        return compare_value(expected["value"], actual, expected.get("tolerance_pct", default_tolerance_pct), path)

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected an object, got {type(actual).__name__} ({actual!r})"]
        errors = []
        for k, v in expected.items():
            if k not in actual:
                errors.append(f"{path}.{k}: missing from actual result")
                continue
            errors.extend(compare_value(v, actual[k], default_tolerance_pct, f"{path}.{k}"))
        return errors

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected a list, got {type(actual).__name__} ({actual!r})"]
        if len(actual) < len(expected):
            return [f"{path}: expected at least {len(expected)} items, got {len(actual)}"]
        errors = []
        for i, exp_item in enumerate(expected):
            errors.extend(compare_value(exp_item, actual[i], default_tolerance_pct, f"{path}[{i}]"))
        return errors

    if isinstance(expected, bool) or isinstance(actual, bool):
        return [] if expected == actual else [f"{path}: expected {expected!r}, got {actual!r}"]

    if isinstance(expected, (int, float)):
        if not isinstance(actual, (int, float)):
            return [f"{path}: expected a number ({expected}), got {actual!r}"]
        if default_tolerance_pct > 0:
            allowed = max(abs(expected) * default_tolerance_pct / 100, 0.01)
            if abs(actual - expected) > allowed:
                return [f"{path}: expected {expected} (+/-{default_tolerance_pct}%), got {actual}"]
            return []
        return [] if actual == expected else [f"{path}: expected exactly {expected}, got {actual}"]

    return [] if actual == expected else [f"{path}: expected {expected!r}, got {actual!r}"]


# ---------------------------------------------------------------------
# Layer 3: trace assertions
# ---------------------------------------------------------------------

def check_trace(expected_trace: dict, trace: dict, default_tolerance_pct: float = 0) -> list[str]:
    if not expected_trace:
        return []
    errors = []
    tables = trace.get("tables_queried", [])
    for t in expected_trace.get("tables_queried_must_include", []):
        if t not in tables:
            errors.append(f"trace.tables_queried missing required {t!r} (got {tables})")
    for t in expected_trace.get("tables_queried_must_not_include", []):
        if t in tables:
            errors.append(f"trace.tables_queried illegally includes {t!r} -- possible data-isolation violation")

    steps = trace.get("steps", [])
    kinds_present = {s.get("kind") for s in steps}
    for k in expected_trace.get("step_kinds_present", []):
        if k not in kinds_present:
            errors.append(f"trace has no step of kind {k!r} (kinds present: {sorted(kinds_present)})")

    max_steps = expected_trace.get("max_steps")
    if max_steps is not None and len(steps) > max_steps:
        errors.append(f"trace has {len(steps)} steps, exceeds max_steps={max_steps} (possible runaway composition)")

    # Exact, ORDERED step-by-step check -- not just "these kinds of steps
    # exist somewhere" but "step 0 is this load, step 1 is this filter with
    # these row counts, step N is this formula with this computed result."
    # This is what actually verifies HOW a number was calculated, not just
    # that a plausible-shaped trace exists -- it's what would have caught
    # supplier_price_trend silently ignoring the requested period: a missing
    # date-range filter step is a concrete, positional mismatch here, not
    # something only a judge's semantic read could notice.
    expected_steps = expected_trace.get("steps")
    if expected_steps is not None:
        errors.extend(compare_value(expected_steps, steps, default_tolerance_pct, "trace.steps"))
    return errors


# ---------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------

def ask(server: str, session_id: str, question: str) -> dict:
    resp = requests.post(f"{server}/ask", json={"session_id": session_id, "question": question}, timeout=90)
    resp.raise_for_status()
    return resp.json()


def get_entry(server: str, headers: dict, log_id: str) -> dict:
    resp = requests.get(f"{server}/admin/query-log/{log_id}", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def run_judge_now(server: str, headers: dict, log_id: str) -> dict:
    """Explicitly triggers the judge (rather than racing JUDGE_AUTO_RUN's
    background scheduling) so the scorer's own behavior is deterministic
    regardless of the server's .env judge-auto-run setting. Long timeout --
    deepseek-reasoner (the default judge model) is a slower "thinking"
    model and can genuinely take over a minute."""
    resp = requests.post(f"{server}/admin/query-log/{log_id}/judge", headers=headers, timeout=280)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------
# Layer 5/6: judge-check mode + category/expected_to_pass_today bucketing
# ---------------------------------------------------------------------

@dataclass
class CaseResult:
    test_case_id: str
    category: str
    expected_to_pass_today: bool
    deterministic_errors: list[str] = field(default_factory=list)
    judge_mode: str | None = None
    judge_pass_rate: float | None = None
    judge_verdicts: list[str] = field(default_factory=list)
    judge_blocking_failed: bool = False
    log_id: str | None = None
    narration: str = ""

    @property
    def deterministic_passed(self) -> bool:
        return not self.deterministic_errors

    @property
    def passed_overall(self) -> bool:
        return self.deterministic_passed and not self.judge_blocking_failed

    @property
    def bucket(self) -> str:
        if self.expected_to_pass_today:
            return "PASS" if self.passed_overall else "BLOCKER_FAIL"
        return "GAP_CLOSED" if self.passed_overall else "KNOWN_GAP_OPEN"


def run_judge_check(server: str, headers: dict, question: str, first_log_id: str,
                     judge_check: dict | None) -> tuple[str | None, float | None, list[str], bool]:
    if not judge_check:
        return None, None, [], False
    mode = judge_check.get("mode", "advisory")
    runs = int(judge_check.get("runs", 1))
    min_pass_rate = float(judge_check.get("min_pass_rate", 1.0))

    verdicts = []
    v = run_judge_now(server, headers, first_log_id)
    verdicts.append(v.get("judge_verdict"))

    for _ in range(max(runs - 1, 0)):
        extra_session = f"eval-judgesample-{uuid.uuid4().hex[:8]}"
        r = ask(server, extra_session, question)
        if not r.get("log_ids"):
            verdicts.append("no_engine_call")
            continue
        v = run_judge_now(server, headers, r["log_ids"][-1])
        verdicts.append(v.get("judge_verdict"))

    pass_rate = sum(1 for x in verdicts if x == "pass") / len(verdicts) if verdicts else 0.0
    blocking_failed = mode == "blocking" and pass_rate < min_pass_rate
    return mode, pass_rate, verdicts, blocking_failed


def score_case(server: str, headers: dict, case: dict) -> CaseResult:
    test_case_id = case["test_case_id"]
    category = case.get("category", "regression")
    expected_to_pass_today = case.get("expected_to_pass_today", True)
    result = CaseResult(test_case_id=test_case_id, category=category, expected_to_pass_today=expected_to_pass_today)

    question = case["input"]["question"]
    session_id = case["input"].get("session_id") or f"eval-{test_case_id}-{uuid.uuid4().hex[:6]}"

    ask_resp = ask(server, session_id, question)
    result.narration = ask_resp.get("narration", "")
    if not ask_resp.get("log_ids"):
        result.deterministic_errors.append("actor never called ask_calculation_engine (no log_ids returned)")
        return result
    log_id = ask_resp["log_ids"][-1]
    result.log_id = log_id

    entry = get_entry(server, headers, log_id)
    tolerance = case.get("default_tolerance_pct", 0)

    if "expected_status" in case:
        if entry.get("status") != case["expected_status"]:
            result.deterministic_errors.append(
                f"status: expected {case['expected_status']!r}, got {entry.get('status')!r}")

    if "expected_resolved_intent" in case:
        result.deterministic_errors.extend(
            compare_value(case["expected_resolved_intent"], entry.get("resolved_intent") or {},
                          tolerance, "resolved_intent"))

    if "expected_result" in case:
        result.deterministic_errors.extend(
            compare_value(case["expected_result"], entry.get("result") or {}, tolerance, "result"))

    if "expected_trace" in case:
        result.deterministic_errors.extend(
            check_trace(case["expected_trace"], entry.get("trace") or {}, tolerance))

    mode, pass_rate, verdicts, blocking_failed = run_judge_check(
        server, headers, question, log_id, case.get("judge_check"))
    result.judge_mode = mode
    result.judge_pass_rate = pass_rate
    result.judge_verdicts = verdicts
    result.judge_blocking_failed = blocking_failed

    return result


# ---------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://localhost:8010")
    parser.add_argument("--cases", required=True, help="Path to a JSON file: a list of test cases.")
    parser.add_argument("--out", default=None, help="Optional path to write full JSON results.")
    args = parser.parse_args()

    admin_key = os.getenv("ADMIN_KEY")
    if not admin_key:
        raise SystemExit("Set ADMIN_KEY in the environment (must match the running server's server/.env ADMIN_KEY).")
    headers = {"X-Admin-Key": admin_key}

    with open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)

    results = [score_case(args.server, headers, case) for case in cases]

    buckets: dict[str, list[CaseResult]] = {}
    for r in results:
        buckets.setdefault(r.bucket, []).append(r)

    print("=" * 70)
    print(f"AUTOMATED EVAL REPORT -- {len(results)} case(s)")
    print("=" * 70)
    for bucket_name in ("BLOCKER_FAIL", "PASS", "KNOWN_GAP_OPEN", "GAP_CLOSED"):
        items = buckets.get(bucket_name, [])
        if not items:
            continue
        print(f"\n{bucket_name} ({len(items)}):")
        for r in items:
            print(f"  [{r.test_case_id}] log_id={r.log_id}")
            for e in r.deterministic_errors:
                print(f"      - {e}")
            if r.judge_mode:
                print(f"      judge({r.judge_mode}): verdicts={r.judge_verdicts} pass_rate={r.judge_pass_rate:.2f}"
                      + (" [BLOCKING FAIL]" if r.judge_blocking_failed else ""))

    print("\n" + "-" * 70)
    print(f"PASS={len(buckets.get('PASS', []))}  "
          f"BLOCKER_FAIL={len(buckets.get('BLOCKER_FAIL', []))}  "
          f"KNOWN_GAP_OPEN={len(buckets.get('KNOWN_GAP_OPEN', []))}  "
          f"GAP_CLOSED={len(buckets.get('GAP_CLOSED', []))}")
    if buckets.get("GAP_CLOSED"):
        print("NOTE: GAP_CLOSED cases now pass despite expected_to_pass_today=false -- "
              "flip that field to true in the fixture, this is good news, not noise.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump([{
                "test_case_id": r.test_case_id, "bucket": r.bucket, "log_id": r.log_id,
                "deterministic_errors": r.deterministic_errors, "judge_mode": r.judge_mode,
                "judge_pass_rate": r.judge_pass_rate, "judge_verdicts": r.judge_verdicts,
                "narration": r.narration,
            } for r in results], f, indent=2, ensure_ascii=False)

    raise SystemExit(1 if buckets.get("BLOCKER_FAIL") else 0)


if __name__ == "__main__":
    main()
