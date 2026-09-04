#!/usr/bin/env python
"""Standalone scoring report: calls the running server's admin API (never
imports server code directly, since eval/ must stay outside anything the
engine/LLM path can reach) to pull the query log + ground-truth cross-check,
and prints a human-readable pass/fail summary.

Usage:
    ADMIN_KEY=... python eval/score_report.py --server http://localhost:8010 --dataset baseline_seed42
"""
from __future__ import annotations

import argparse
import json
import os

import requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://localhost:8010")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--session-id", default=None)
    args = parser.parse_args()

    admin_key = os.getenv("ADMIN_KEY")
    if not admin_key:
        raise SystemExit("Set ADMIN_KEY in the environment (must match the running server's server/.env ADMIN_KEY).")
    headers = {"X-Admin-Key": admin_key}

    summary = requests.get(f"{args.server}/admin/summary", headers=headers).json()
    log = requests.get(f"{args.server}/admin/query-log", headers=headers,
                        params={"session_id": args.session_id} if args.session_id else {}).json()
    score = requests.get(f"{args.server}/admin/score/{args.dataset}", headers=headers,
                          params={"session_id": args.session_id} if args.session_id else {}).json()

    print("=" * 70)
    print(f"EVALUATION REPORT — dataset={args.dataset}")
    print("=" * 70)
    print(f"\nTotal asks logged: {len(log)}")
    print(f"By status: {json.dumps(score['by_status'], indent=2)}")
    print(f"Cache hit rate: {summary['query_log'].get('cache_hit_rate_pct')}%")
    print(f"Admin-reviewed: {summary['query_log'].get('admin_reviewed')}  "
          f"(fail rate among reviewed: {summary['query_log'].get('admin_fail_rate_pct')}%)")

    print(f"\nGround truth available for this dataset: {json.dumps(score['ground_truth_available'], indent=2)}")

    flagged = score.get("flagged_for_manual_review", [])
    print(f"\n{len(flagged)} ask(s) flagged for manual review (diagnostic/judgment-call questions "
          f"the automatic hallucination check can't fully resolve on its own):")
    for item in flagged:
        print(f"  - [{item['log_id']}] {item['question_text']!r}")

    print(f"\nUnfulfilled asks (engine error / invalid request):")
    for entry in log:
        if entry["status"] == "unfulfilled":
            print(f"  - [{entry['log_id']}] {entry['question_text']!r} -- {entry.get('error')}")

    print(f"\nPossible hallucinations (narrated a number not present in the trace):")
    for entry in log:
        if entry["status"] == "possible_hallucination":
            print(f"  - [{entry['log_id']}] {entry['question_text']!r}")
            print(f"      narrated: {entry['narrated_text']!r}")

    print("\nTo confirm/dispute a flagged or answered entry:")
    print(f"  POST {args.server}/admin/query-log/<log_id>/review  {{\"verdict\": \"pass\"|\"fail\", \"note\": \"...\"}}")


if __name__ == "__main__":
    main()
