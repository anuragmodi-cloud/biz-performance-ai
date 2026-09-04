#!/usr/bin/env python
"""Populates server/data/<name>/ (engine-readable) and eval/<name>/
(admin-only) from a costguard/synthetic_business/output/<dataset>/ folder.

This is the ONLY place in the whole project that ever touches the full
generator output folder -- it's what enforces the split between what the
calculation engine/LLM can see and what only a human evaluator can see.

Usage:
    python scripts/sync_data.py --source ../costguard/synthetic_business/output/baseline_seed42 --name baseline_seed42
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ENGINE_FILES = {
    "sales_transactions.csv", "purchase_transactions.csv", "bank_transactions.csv",
    "inventory_transactions.csv", "products.csv", "suppliers.csv", "customers.csv",
    "business_events.csv", "business.json",
}
EVAL_FILES = {"ground_truth.json", "business_questions.json"}

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Path to a synthetic_business/output/<dataset> folder")
    parser.add_argument("--name", required=True, help="Dataset name to use under server/data/ and eval/")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.is_dir():
        raise SystemExit(f"Source folder not found: {source}")

    engine_dest = ROOT / "server" / "data" / args.name
    eval_dest = ROOT / "eval" / args.name
    engine_dest.mkdir(parents=True, exist_ok=True)
    eval_dest.mkdir(parents=True, exist_ok=True)

    copied_engine, copied_eval, skipped = [], [], []
    for f in sorted(source.iterdir()):
        if not f.is_file():
            continue
        if f.name in ENGINE_FILES:
            shutil.copy2(f, engine_dest / f.name)
            copied_engine.append(f.name)
        elif f.name in EVAL_FILES:
            shutil.copy2(f, eval_dest / f.name)
            copied_eval.append(f.name)
        else:
            skipped.append(f.name)

    print(f"Engine data  -> {engine_dest}  ({len(copied_engine)} files: {', '.join(sorted(copied_engine))})")
    print(f"Eval-only    -> {eval_dest}  ({len(copied_eval)} files: {', '.join(sorted(copied_eval))})")
    if skipped:
        print(f"Skipped (not in either allowlist): {', '.join(sorted(skipped))}")

    missing_engine = ENGINE_FILES - set(copied_engine)
    if missing_engine:
        print(f"WARNING: source was missing expected engine files: {sorted(missing_engine)}")


if __name__ == "__main__":
    main()
