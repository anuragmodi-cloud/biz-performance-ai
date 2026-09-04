"""Receivables-category compute functions."""
from __future__ import annotations

from .. import data_loader as dl
from ..formulas import invoice_level, outstanding_as_of, resolve_entity
from ..trace import StepTrace


def _invoices(trace: StepTrace):
    sales = dl.sales()
    trace.load("sales_transactions.csv", len(sales))
    return invoice_level(trace, sales)


def total_receivables(end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    invoices = _invoices(trace)
    total = outstanding_as_of(trace, invoices, end, overdue_only=False)
    return {"metric": "total_receivables", "value": round(total, 2), "as_of": str(end.date())}, trace


def overdue_receivables(end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    invoices = _invoices(trace)
    total = outstanding_as_of(trace, invoices, end, overdue_only=True)
    return {"metric": "overdue_receivables", "value": round(total, 2), "as_of": str(end.date())}, trace


def top_debtors(end, top_n=5, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    invoices = _invoices(trace)
    customers = dl.customers()
    trace.load("customers.csv", len(customers))

    opened = invoices["transaction_date"] <= end
    still_open = invoices["payment_date"].isna() | (invoices["payment_date"] > end)
    outstanding = (invoices["invoice_total"] - invoices["amount_paid"]).clip(lower=0)
    mask = opened & still_open & (outstanding > 0)
    all_per_customer = outstanding[mask].groupby(invoices.loc[mask, "customer_id"]).sum().sort_values(ascending=False)
    per_customer = all_per_customer.head(top_n)
    trace.aggregate("top_debtors", f"grouped open-invoice outstanding by customer_id as of {end.date()}, took top {top_n}",
                     {cid: round(v, 2) for cid, v in per_customer.items()})

    name_map = customers.set_index("customer_id")["customer_name"]
    items = [{"customer_id": cid, "customer_name": name_map.get(cid, cid), "outstanding": round(v, 2)}
             for cid, v in per_customer.items()]
    total_top_n = round(float(per_customer.sum()), 2)
    trace.formula("total_outstanding_top_n",
                  f"sum of the {top_n} amounts above -- provided directly so the narration never has to add these up itself",
                  total_top_n)
    return {"metric": "top_debtors", "items": items, "total_outstanding_top_n": total_top_n,
            "total_count": len(all_per_customer), "as_of": str(end.date())}, trace


def overdue_invoices(end, top_n=10, filters=None, **_) -> tuple[dict, StepTrace]:
    """Threshold/exception question ("which invoices are overdue beyond N
    days") -- unlike overdue_receivables (a single total) this lists the
    actual invoices, filtered by a caller-nameable day threshold rather than
    the fixed "any overdue at all" used elsewhere.
    filters['days_overdue_min'] (default 30) -- only invoices whose
    expected_payment_date is at least this many days before `end`."""
    trace = StepTrace()
    invoices = _invoices(trace)
    customers = dl.customers()
    trace.load("customers.csv", len(customers))
    days_min = (filters or {}).get("days_overdue_min", 30)

    days_overdue = (end - invoices["expected_payment_date"]).dt.days
    outstanding = (invoices["invoice_total"] - invoices["amount_paid"]).clip(lower=0)
    still_open = invoices["payment_date"].isna() | (invoices["payment_date"] > end)
    mask = still_open & (outstanding > 0) & (days_overdue >= days_min)
    matched = invoices[mask].copy()
    matched["days_overdue"] = days_overdue[mask]
    matched["outstanding"] = outstanding[mask]
    matched = matched.sort_values("days_overdue", ascending=False)
    trace.filter(f"invoices open, unpaid, and >= {days_min} days overdue as of {end.date()}",
                 len(invoices), len(matched))

    name_map = customers.set_index("customer_id")["customer_name"]
    total_outstanding = round(float(matched["outstanding"].sum()), 2)
    trace.formula("overdue_invoices_total", f"sum(outstanding) across all {len(matched)} matching invoices",
                  total_outstanding)
    items = [{"customer_name": name_map.get(row["customer_id"], row["customer_id"]),
              "days_overdue": int(row["days_overdue"]), "outstanding": round(float(row["outstanding"]), 2)}
             for _, row in matched.head(top_n).iterrows()]
    return {"metric": "overdue_invoices", "items": items, "total_count": len(matched),
            "total_outstanding": total_outstanding, "days_overdue_min": days_min,
            "as_of": str(end.date())}, trace


def _resolve_and_delay(trace: StepTrace, invoices, customers, entity_name: str) -> dict:
    """Resolves one customer name and computes their average payment delay.
    Returns either a result dict, {"error": ...}, or {"ambiguous": True,
    "candidates": [...]} -- never silently guesses among several matches."""
    resolution = resolve_entity(trace, customers, "customer_id", "customer_name", entity_name, "customers.csv")
    if resolution["status"] == "not_found":
        return {"error": f"no customer found matching {entity_name!r}"}
    if resolution["status"] == "ambiguous":
        return {"ambiguous": True, "candidates": resolution["candidates"]}

    cid, cname = resolution["id"], resolution["name"]
    cust_invoices = invoices[(invoices["customer_id"] == cid) & invoices["payment_date"].notna()]
    trace.filter(f"invoices for customer_id={cid} with a recorded payment_date",
                 len(invoices), len(cust_invoices))
    if cust_invoices.empty:
        trace.note("no paid invoices on record for this customer")
        return {"customer_id": cid, "customer_name": cname, "value": None, "note": "no paid invoices on record"}

    days_to_pay = (cust_invoices["payment_date"] - cust_invoices["transaction_date"]).dt.days
    avg_days = float(days_to_pay.mean())
    trace.aggregate("customer_payment_delay",
                     "average (payment_date - transaction_date) in days across this customer's paid invoices",
                     round(avg_days, 1))
    return {"customer_id": cid, "customer_name": cname,
            "value": round(avg_days, 1), "unit": "days", "invoice_count": len(cust_invoices)}


def _payment_delay_ranking(trace: StepTrace, invoices, customers, top_n: int) -> dict:
    """No entity named -- "which customers delay payment the most" is a
    cross-customer ranking question, not a single-entity lookup. Ranks every
    customer with at least one paid invoice by their average days-to-pay."""
    paid = invoices[invoices["payment_date"].notna()]
    trace.filter("invoices with a recorded payment_date", len(invoices), len(paid))
    days_to_pay = (paid["payment_date"] - paid["transaction_date"]).dt.days
    all_per_customer = days_to_pay.groupby(paid["customer_id"]).mean().sort_values(ascending=False)
    per_customer = all_per_customer.head(top_n)
    trace.aggregate(
        "customer_payment_delay_ranking",
        f"grouped paid invoices by customer_id, averaged (payment_date - transaction_date) in days, "
        f"took top {top_n} slowest-paying customers",
        {cid: round(v, 1) for cid, v in per_customer.items()},
    )
    name_map = customers.set_index("customer_id")["customer_name"]
    items = [{"customer_id": cid, "customer_name": name_map.get(cid, cid), "avg_days_to_pay": round(v, 1)}
              for cid, v in per_customer.items()]
    return {"metric": "customer_payment_delay", "items": items, "unit": "days", "total_count": len(all_per_customer)}


def customer_payment_delay(entity_name=None, entity_name_2=None, top_n=5, **_) -> tuple[dict, StepTrace]:
    """No entity_name -- ranks customers by average payment delay (slowest
    payers first), same shape as top_debtors/top_products_by_revenue.
    entity_name -- a single customer's own average payment delay.
    entity_name_2 (single-entity mode only) is optional -- when given (a
    "compare X and Y's payment delay" question), a second customer is
    resolved and computed the same way, returned alongside the primary one
    as {"primary": ..., "comparison": ...}."""
    trace = StepTrace()
    invoices = _invoices(trace)
    customers = dl.customers()
    trace.load("customers.csv", len(customers))

    if not entity_name:
        return _payment_delay_ranking(trace, invoices, customers, top_n), trace

    primary = _resolve_and_delay(trace, invoices, customers, entity_name)
    if "error" in primary or "ambiguous" in primary:
        return {"metric": "customer_payment_delay", **primary}, trace

    if not entity_name_2:
        return {"metric": "customer_payment_delay", **primary}, trace

    comparison = _resolve_and_delay(trace, invoices, customers, entity_name_2)
    if "error" in comparison or "ambiguous" in comparison:
        return {"metric": "customer_payment_delay", "primary": primary, "comparison_error": comparison}, trace

    return {"metric": "customer_payment_delay", "primary": primary, "comparison": comparison}, trace
