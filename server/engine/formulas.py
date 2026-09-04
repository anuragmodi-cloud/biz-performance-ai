"""Shared, step-traced building blocks used across engine/compute/*.py.

Every function here takes a StepTrace and appends to it -- this is what
keeps the audit trail complete even as compute modules compose these helpers
rather than duplicating filter/aggregate logic inline.
"""
from __future__ import annotations

import pandas as pd

from .trace import StepTrace


def filter_date_range(trace: StepTrace, df: pd.DataFrame, date_col: str,
                       start: pd.Timestamp, end: pd.Timestamp, label: str) -> pd.DataFrame:
    before = len(df)
    mask = (df[date_col] >= start) & (df[date_col] <= end)
    out = df[mask]
    trace.filter(
        f"{label}: {date_col} between {start.date()} and {end.date()}",
        rows_before=before, rows_after=len(out),
    )
    return out


def revenue(trace: StepTrace, sales_df: pd.DataFrame) -> float:
    total = float(sales_df["taxable_value"].sum())
    trace.aggregate("revenue", "sum(taxable_value) across matched sales lines", total)
    return total


def cogs(trace: StepTrace, sales_df: pd.DataFrame) -> float:
    total = float((sales_df["quantity"] * sales_df["unit_purchase_cost_ref"]).sum())
    trace.aggregate("cogs", "sum(quantity * unit_purchase_cost_ref) across matched sales lines", total)
    return total


def gross_margin(trace: StepTrace, revenue_value: float, cogs_value: float) -> float:
    gp = revenue_value - cogs_value
    margin = (gp / revenue_value) if revenue_value else 0.0
    trace.formula(
        "gross_margin",
        f"gross_profit = revenue({revenue_value:.2f}) - cogs({cogs_value:.2f}) = {gp:.2f}; "
        f"gross_margin = gross_profit / revenue = {margin:.4f}",
        {"gross_profit": gp, "gross_margin": margin},
    )
    return margin


def invoice_level(trace: StepTrace, sales_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse line-item sales rows to one row per invoice -- needed for
    receivables/payables/AOV math where invoice_total etc. are duplicated
    across every line of that invoice."""
    before = len(sales_df)
    g = sales_df.groupby("invoice_number").agg(
        transaction_date=("transaction_date", "first"),
        invoice_total=("invoice_total", "first"),
        taxable_amount=("taxable_amount", "first"),
        customer_id=("customer_id", "first"),
        customer_type=("customer_type", "first"),
        amount_paid=("amount_paid", "sum"),
        amount_outstanding=("amount_outstanding", "sum"),
        payment_date=("payment_date", "first"),
        expected_payment_date=("expected_payment_date", "first"),
        payment_method=("payment_method", "first"),
    ).reset_index()
    trace.aggregate("invoice_collapse", f"collapsed {before} sales line items into {len(g)} invoices", len(g))
    return g


def match_all_words(series: pd.Series, phrase: str) -> pd.Series:
    """Case-insensitive "every word of `phrase` appears somewhere in the
    value" match, not a single exact-substring match. Catalog-style master
    data ("Wyvern Business Laptops WY733") rarely contains a spoken phrase
    ("Wyvern laptop") as a literal substring, so this AND-chains a
    per-word .str.contains instead."""
    words = [w for w in phrase.lower().split() if w]
    if not words:
        return series.astype(bool) & False
    lowered = series.fillna("").str.lower()
    mask = lowered.str.contains(words[0], regex=False)
    for w in words[1:]:
        mask &= lowered.str.contains(w, regex=False)
    return mask


def resolve_entity(trace: StepTrace, df: pd.DataFrame, id_col: str, name_col: str,
                    entity_name: str, table_label: str, max_candidates: int = 8) -> dict:
    """Resolves a caller-named entity against a master table's id/name
    columns. Never silently guesses among multiple matches -- if more than
    one row matches, returns "ambiguous" with the full candidate list
    instead of picking the first one, so a wrong silent pick can't happen
    and the judge evidence bundle can actually see what was ambiguous
    between (previously only a match *count* was ever logged).

    Returns one of:
      {"status": "resolved", "id": ..., "name": ..., "row": pd.Series}
      {"status": "ambiguous", "candidates": [{"id": ..., "name": ...}, ...]}
      {"status": "not_found"}
    """
    before = len(df)
    matches = df[match_all_words(df[name_col], entity_name)]
    trace.filter(f"{table_label} where {name_col} matches all words of {entity_name!r}", before, len(matches))

    if matches.empty:
        trace.note(f"no {table_label} row found matching {entity_name!r}")
        return {"status": "not_found"}

    if len(matches) == 1:
        row = matches.iloc[0]
        trace.aggregate(
            "entity_resolution",
            f"{entity_name!r} resolved unambiguously to {name_col}={row[name_col]!r} ({id_col}={row[id_col]!r})",
            {"resolved_id": row[id_col], "resolved_name": row[name_col]},
        )
        return {"status": "resolved", "id": row[id_col], "name": row[name_col], "row": row}

    candidates = [{"id": r[id_col], "name": r[name_col]} for _, r in matches.head(max_candidates).iterrows()]
    trace.note(
        f"{len(matches)} {table_label} rows matched {entity_name!r} -- ambiguous, not auto-resolving: "
        f"candidates={candidates}"
    )
    return {"status": "ambiguous", "candidates": candidates}


def outstanding_as_of(trace: StepTrace, invoices: pd.DataFrame, as_of: pd.Timestamp,
                       overdue_only: bool = False) -> float:
    opened = invoices["transaction_date"] <= as_of
    paid_date = invoices["payment_date"]
    still_open = paid_date.isna() | (paid_date > as_of)
    outstanding = (invoices["invoice_total"] - invoices["amount_paid"]).clip(lower=0)
    mask = opened & still_open & (outstanding > 0)
    if overdue_only:
        mask = mask & (invoices["expected_payment_date"] < as_of)
    total = float(outstanding[mask].sum())
    label = "overdue_receivables" if overdue_only else "receivables"
    trace.formula(
        label,
        f"{label} as of {as_of.date()} = sum(invoice_total - amount_paid) for invoices "
        f"opened by then and not yet fully paid" + (" and past their expected_payment_date" if overdue_only else ""),
        total,
    )
    return total
