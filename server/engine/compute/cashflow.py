"""Cash-flow-category compute functions, from bank_transactions.csv."""
from __future__ import annotations

from .. import data_loader as dl
from ..formulas import filter_date_range
from ..trace import StepTrace


def _window(trace: StepTrace, start, end):
    b = dl.bank()
    trace.load("bank_transactions.csv", len(b))
    return filter_date_range(trace, b, "transaction_date", start, end, "bank transactions in requested period")


def cash_inflow(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _window(trace, start, end)
    total = float(window["credit"].sum())
    trace.aggregate("cash_inflow", "sum(credit) where transaction_date in period", round(total, 2))
    return {"metric": "cash_inflow", "value": round(total, 2)}, trace


def cash_outflow(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _window(trace, start, end)
    total = float(window["debit"].sum())
    trace.aggregate("cash_outflow", "sum(debit) where transaction_date in period", round(total, 2))
    return {"metric": "cash_outflow", "value": round(total, 2)}, trace


def net_cash_flow(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _window(trace, start, end)
    inflow = float(window["credit"].sum())
    outflow = float(window["debit"].sum())
    net = inflow - outflow
    trace.formula("net_cash_flow", f"cash_inflow({inflow:.2f}) - cash_outflow({outflow:.2f})", round(net, 2))
    return {"metric": "net_cash_flow", "value": round(net, 2), "cash_inflow": round(inflow, 2),
            "cash_outflow": round(outflow, 2)}, trace


def ending_bank_balance(start, end, **_) -> tuple[dict, StepTrace]:
    """end is treated as the as-of date -- the most recent transaction's
    balance_after_transaction on or before it."""
    trace = StepTrace()
    b = dl.bank()
    trace.load("bank_transactions.csv", len(b))
    upto = b[b["transaction_date"] <= end].sort_values("bank_transaction_id")
    trace.filter(f"bank transactions on or before {end.date()}", len(b), len(upto))
    if upto.empty:
        trace.note("no bank transactions on or before this date")
        return {"metric": "ending_bank_balance", "value": None}, trace
    balance = float(upto.iloc[-1]["balance_after_transaction"])
    trace.aggregate("ending_bank_balance",
                     f"balance_after_transaction of the most recent matching row (as of {end.date()})",
                     round(balance, 2))
    return {"metric": "ending_bank_balance", "value": round(balance, 2), "as_of": str(end.date())}, trace


def negative_cash_flow_months(start, end, **_) -> tuple[dict, StepTrace]:
    """Threshold/exception: which months in the window had negative net
    cash flow (spent more than came in), not just the overall period total.
    Needs a wide period (all_time/last_year/this_year) to be meaningful --
    a single-month window can only ever return zero or one month."""
    trace = StepTrace()
    window = _window(trace, start, end)
    window = window.copy()
    window["month"] = window["transaction_date"].dt.to_period("M").astype(str)
    net_by_month = (window["credit"] - window["debit"]).groupby(window["month"]).sum().sort_index()
    negative = net_by_month[net_by_month < 0]
    trace.aggregate("negative_cash_flow_months",
                     f"grouped by calendar month, net = sum(credit - debit), kept months below zero "
                     f"({len(negative)} of {len(net_by_month)} months matched)",
                     {m: round(v, 2) for m, v in negative.items()})
    items = [{"month": m, "net_cash_flow": round(v, 2)} for m, v in negative.items()]
    return {"metric": "negative_cash_flow_months", "items": items, "total_count": len(negative)}, trace


def cash_by_type(start, end, top_n=10, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _window(trace, start, end)
    net_by_type = (window["credit"] - window["debit"]).groupby(window["transaction_type"]).sum() \
        .sort_values(ascending=True)
    trace.aggregate("cash_by_type", f"grouped by transaction_type, net = sum(credit - debit), took top {top_n}",
                     {k: round(v, 2) for k, v in net_by_type.head(top_n).items()})
    items = [{"transaction_type": k, "net_amount": round(v, 2)} for k, v in net_by_type.head(top_n).items()]
    return {"metric": "cash_by_type", "items": items, "total_count": len(net_by_type)}, trace
