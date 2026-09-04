"""Loads raw transaction/master-data CSVs for the calculation engine.

This is the ONE place allowed to touch the filesystem for business data, and
it hard-allowlists exactly which filenames it will open. This is deliberate
defense-in-depth: ground_truth.json, business_questions.json, and every
*_metrics.csv are excluded from the allowlist below, so even if DATA_DIR ever
got pointed at a folder that contains them (a misconfigured sync, a copy-paste
mistake), this loader still refuses to read them. The engine computes
everything itself from line-item data -- there is no code path in this
project that reads a precomputed aggregate.
"""
from __future__ import annotations

import json
import os
import threading
from functools import lru_cache
from pathlib import Path

import pandas as pd

ALLOWED_FILES = {
    "sales_transactions.csv",
    "purchase_transactions.csv",
    "bank_transactions.csv",
    "inventory_transactions.csv",
    "products.csv",
    "suppliers.csv",
    "customers.csv",
    "business_events.csv",
    "business.json",
}

_BLOCKED_HINT_SUBSTRINGS = ("ground_truth", "business_questions", "_metrics", "validation_report")

_lock = threading.Lock()


def _data_dir() -> Path:
    raw = os.getenv("DATA_DIR", "data/baseline_seed42")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path


def _guard(filename: str) -> None:
    if filename not in ALLOWED_FILES:
        raise ValueError(
            f"Refusing to load {filename!r}: not in the calculation engine's "
            f"data allowlist ({sorted(ALLOWED_FILES)}). This engine only reads "
            f"raw transaction/master tables, never precomputed metrics or "
            f"ground-truth/evaluation files."
        )
    lowered = filename.lower()
    for bad in _BLOCKED_HINT_SUBSTRINGS:
        if bad in lowered:
            # Should be unreachable given ALLOWED_FILES above, but kept as an
            # explicit second check rather than relying on the allowlist alone.
            raise ValueError(f"Refusing to load {filename!r}: looks like an eval/metrics artifact.")


@lru_cache(maxsize=16)
def _read_csv_cached(path_str: str, mtime: float) -> pd.DataFrame:
    # mtime is part of the cache key purely to auto-invalidate if the
    # underlying file changes (e.g. a fresh sync_data.py run) without needing
    # an explicit cache-clear call.
    return pd.read_csv(path_str)


def load_csv(filename: str) -> pd.DataFrame:
    _guard(filename)
    path = _data_dir() / filename
    if not path.exists():
        raise FileNotFoundError(
            f"{filename} not found under DATA_DIR={_data_dir()}. Run "
            f"scripts/sync_data.py to populate server/data/<dataset>/ first."
        )
    with _lock:
        df = _read_csv_cached(str(path), path.stat().st_mtime)
    return df.copy()


def load_json(filename: str) -> dict:
    _guard(filename)
    path = _data_dir() / filename
    if not path.exists():
        raise FileNotFoundError(f"{filename} not found under DATA_DIR={_data_dir()}.")
    with open(path) as f:
        return json.load(f)


# Convenience typed accessors -------------------------------------------------

def sales() -> pd.DataFrame:
    df = load_csv("sales_transactions.csv")
    for col in ("transaction_date", "payment_date", "expected_payment_date", "actual_payment_date", "return_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def purchases() -> pd.DataFrame:
    df = load_csv("purchase_transactions.csv")
    for col in ("purchase_date", "due_date", "payment_date", "expected_delivery_date", "actual_delivery_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def bank() -> pd.DataFrame:
    df = load_csv("bank_transactions.csv")
    df["transaction_date"] = pd.to_datetime(df["transaction_date"], errors="coerce")
    return df


def inventory() -> pd.DataFrame:
    df = load_csv("inventory_transactions.csv")
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df


def products() -> pd.DataFrame:
    return load_csv("products.csv")


def suppliers() -> pd.DataFrame:
    return load_csv("suppliers.csv")


def customers() -> pd.DataFrame:
    return load_csv("customers.csv")


def business_events() -> pd.DataFrame:
    df = load_csv("business_events.csv")
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    df["end_date"] = pd.to_datetime(df["end_date"], errors="coerce")
    return df


def business_info() -> dict:
    return load_json("business.json")


def simulation_bounds() -> tuple[pd.Timestamp, pd.Timestamp]:
    info = business_info()
    return pd.Timestamp(info["simulation_start_date"]), pd.Timestamp(info["simulation_end_date"])
