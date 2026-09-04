"""Customer-behavior-category compute functions."""
from __future__ import annotations

from .. import data_loader as dl
from ..formulas import filter_date_range, resolve_entity
from ..trace import StepTrace


def _window(trace: StepTrace, start, end):
    sales = dl.sales()
    trace.load("sales_transactions.csv", len(sales))
    return filter_date_range(trace, sales, "transaction_date", start, end, "sales in requested period")


def best_customers(start, end, top_n=5, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _window(trace, start, end)
    customers = dl.customers()
    trace.load("customers.csv", len(customers))
    all_cust = window.groupby("customer_id")["taxable_value"].sum().sort_values(ascending=False)
    by_cust = all_cust.head(top_n)
    name_map = customers.set_index("customer_id")["customer_name"]
    trace.aggregate("best_customers", f"grouped by customer, summed taxable_value, took top {top_n}",
                     {cid: round(v, 2) for cid, v in by_cust.items()})
    items = [{"customer_id": cid, "customer_name": name_map.get(cid, cid), "revenue": round(v, 2)}
             for cid, v in by_cust.items()]
    return {"metric": "best_customers", "items": items, "total_count": len(all_cust)}, trace


def _resolve_and_reliability(trace: StepTrace, customers, entity_name: str) -> dict:
    """Resolves one customer name and reads their payment-reliability master
    data. Returns either a result dict, {"error": ...}, or {"ambiguous":
    True, "candidates": [...]} -- never silently guesses among several
    matches."""
    resolution = resolve_entity(trace, customers, "customer_id", "customer_name", entity_name, "customers.csv")
    if resolution["status"] == "not_found":
        return {"error": f"no customer found matching {entity_name!r}"}
    if resolution["status"] == "ambiguous":
        return {"ambiguous": True, "candidates": resolution["candidates"]}

    cust = resolution["row"]
    trace.aggregate("customer_payment_reliability",
                     "read payment_reliability (0-1 scale) and agreed_credit_days from customer master data",
                     {"payment_reliability": float(cust["payment_reliability"]),
                      "agreed_credit_days": int(cust["agreed_credit_days"])})
    return {"customer_id": cust["customer_id"], "customer_name": cust["customer_name"],
            "payment_reliability": round(float(cust["payment_reliability"]), 3),
            "agreed_credit_days": int(cust["agreed_credit_days"]), "payer_class": cust["payer_class"]}


def customer_payment_reliability(entity_name=None, entity_name_2=None, **_) -> tuple[dict, StepTrace]:
    """entity_name_2 is optional -- when given (a "compare X and Y's payment
    reliability" question), a second customer is resolved the same way,
    returned alongside the primary one as {"primary": ..., "comparison": ...}."""
    trace = StepTrace()
    customers = dl.customers()
    trace.load("customers.csv", len(customers))
    if not entity_name:
        trace.note("no entity_name given")
        return {"metric": "customer_payment_reliability", "error": "entity_name is required"}, trace

    primary = _resolve_and_reliability(trace, customers, entity_name)
    if "error" in primary or "ambiguous" in primary:
        return {"metric": "customer_payment_reliability", **primary}, trace

    if not entity_name_2:
        return {"metric": "customer_payment_reliability", **primary}, trace

    comparison = _resolve_and_reliability(trace, customers, entity_name_2)
    if "error" in comparison or "ambiguous" in comparison:
        return {"metric": "customer_payment_reliability", "primary": primary, "comparison_error": comparison}, trace

    return {"metric": "customer_payment_reliability", "primary": primary, "comparison": comparison}, trace


def repeat_customer_rate(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    sales = dl.sales()
    trace.load("sales_transactions.csv", len(sales))
    window = filter_date_range(trace, sales, "transaction_date", start, end, "sales in requested period")
    first_purchase = sales.groupby("customer_id")["transaction_date"].min()
    trace.aggregate("first_purchase_lookup", "computed each customer's first-ever transaction_date across the full sales history", None)
    is_repeat = window["transaction_date"] > window["customer_id"].map(first_purchase)
    customers_in_window = window["customer_id"].nunique()
    repeat_customers = window.loc[is_repeat, "customer_id"].nunique()
    rate = (repeat_customers / customers_in_window * 100) if customers_in_window else 0.0
    trace.formula(
        "repeat_customer_rate",
        f"distinct customers in window whose transaction_date > their first-ever "
        f"purchase date ({repeat_customers}) / distinct customers in window ({customers_in_window}) * 100",
        round(rate, 2),
    )
    return {"metric": "repeat_customer_rate", "value": round(rate, 2), "unit": "percent"}, trace
