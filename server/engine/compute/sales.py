"""Sales-category compute functions. Each returns (result: dict, trace: StepTrace)."""
from __future__ import annotations

import pandas as pd

from .. import data_loader as dl
from ..formulas import filter_date_range, invoice_level, revenue
from ..schema import previous_comparable_window
from ..trace import StepTrace


def _load_and_window(trace: StepTrace, start, end):
    sales = dl.sales()
    trace.load("sales_transactions.csv", len(sales))
    return filter_date_range(trace, sales, "transaction_date", start, end, "sales in requested period")


def total_revenue(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _load_and_window(trace, start, end)
    total = revenue(trace, window)
    return {"metric": "total_revenue", "value": round(total, 2), "unit": "INR"}, trace


def units_sold(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _load_and_window(trace, start, end)
    total = int(window["quantity"].sum())
    trace.aggregate("units_sold", "sum(quantity)", total)
    return {"metric": "units_sold", "value": total}, trace


def top_products_by_revenue(start, end, top_n=5, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _load_and_window(trace, start, end)
    all_products = window.groupby(["product_id", "product_name"])["taxable_value"].sum().sort_values(ascending=False)
    by_product = all_products.head(top_n)
    trace.aggregate("top_products_by_revenue", f"grouped by product, summed taxable_value, took top {top_n}",
                     {name: round(v, 2) for (_, name), v in by_product.items()})
    items = [{"product_id": pid, "product_name": name, "revenue": round(v, 2)}
             for (pid, name), v in by_product.items()]
    return {"metric": "top_products_by_revenue", "items": items, "total_count": len(all_products)}, trace


def top_customers_by_revenue(start, end, top_n=5, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _load_and_window(trace, start, end)
    customers = dl.customers()
    trace.load("customers.csv", len(customers))
    all_customers = window.groupby("customer_id")["taxable_value"].sum().sort_values(ascending=False)
    by_cust = all_customers.head(top_n)
    name_map = customers.set_index("customer_id")["customer_name"]
    trace.aggregate("top_customers_by_revenue", f"grouped by customer_id, summed taxable_value, took top {top_n}",
                     {cid: round(v, 2) for cid, v in by_cust.items()})
    items = [{"customer_id": cid, "customer_name": name_map.get(cid, cid), "revenue": round(v, 2)}
             for cid, v in by_cust.items()]
    return {"metric": "top_customers_by_revenue", "items": items, "total_count": len(all_customers)}, trace


def revenue_by_month(start, end, **_) -> tuple[dict, StepTrace]:
    """Month-by-month revenue over the requested window, plus a simple
    seasonality signal -- the ONLY sub_metric that can actually answer "is
    my business seasonal" questions. Needs a wide period (all_time/last_year)
    to be meaningful; a single-month window just returns one data point.
    """
    trace = StepTrace()
    window = _load_and_window(trace, start, end)
    window = window.copy()
    window["month"] = window["transaction_date"].dt.to_period("M").astype(str)
    by_month = window.groupby("month")["taxable_value"].sum().sort_index()
    trace.aggregate("revenue_by_month", "grouped by calendar month, summed taxable_value",
                     {m: round(v, 2) for m, v in by_month.items()})
    items = [{"month": m, "revenue": round(v, 2)} for m, v in by_month.items()]

    peak_month = trough_month = None
    variability_pct = None
    if len(by_month) >= 2:
        avg = float(by_month.mean())
        std = float(by_month.std())
        variability_pct = round((std / avg) * 100, 2) if avg else 0.0
        peak_month = str(by_month.idxmax())
        trough_month = str(by_month.idxmin())
        trace.formula(
            "seasonality_signal",
            f"month_to_month_variability_pct = stdev(monthly_revenue)/mean(monthly_revenue) * 100 "
            f"= {variability_pct}%; peak_month={peak_month}, trough_month={trough_month} "
            f"(higher variability_pct = more seasonal; well under ~20% is fairly flat/non-seasonal)",
            {"variability_pct": variability_pct, "peak_month": peak_month, "trough_month": trough_month},
        )

    return {
        "metric": "revenue_by_month", "items": items,
        "peak_month": peak_month, "trough_month": trough_month,
        "month_to_month_variability_pct": variability_pct,
    }, trace


def product_mix_change(start, end, top_n=10, **_) -> tuple[dict, StepTrace]:
    """Composite comparison -- how has revenue SHARE per category shifted
    between this period and the previous comparable one, not just whether
    the category's own revenue went up or down (a category can grow in
    absolute revenue while still shrinking as a % of the mix, if everything
    else grew faster)."""
    trace = StepTrace()
    current_window = _load_and_window(trace, start, end)
    current_by_cat = current_window.groupby("category")["taxable_value"].sum()
    current_total = float(current_by_cat.sum())
    current_share = (current_by_cat / current_total * 100) if current_total else current_by_cat * 0

    prev_start, prev_end = previous_comparable_window(start, end)
    previous_window = _load_and_window(trace, prev_start, prev_end)
    previous_by_cat = previous_window.groupby("category")["taxable_value"].sum()
    previous_total = float(previous_by_cat.sum())
    previous_share = (previous_by_cat / previous_total * 100) if previous_total else previous_by_cat * 0

    merged = current_share.to_frame("current_share_pct").join(
        previous_share.to_frame("previous_share_pct"), how="outer"
    ).fillna(0.0)
    merged["share_delta_pct_points"] = merged["current_share_pct"] - merged["previous_share_pct"]
    merged = merged.reindex(merged["share_delta_pct_points"].abs().sort_values(ascending=False).index)

    trace.formula(
        "product_mix_change",
        "per category: current_share_pct = category revenue / total revenue * 100 (this period), same for "
        "previous comparable period; share_delta_pct_points = current - previous",
        {cat: round(row["share_delta_pct_points"], 2) for cat, row in merged.head(top_n).iterrows()},
    )
    items = [{"category": cat, "current_share_pct": round(row["current_share_pct"], 2),
              "previous_share_pct": round(row["previous_share_pct"], 2),
              "share_delta_pct_points": round(row["share_delta_pct_points"], 2)}
             for cat, row in merged.head(top_n).iterrows()]
    return {"metric": "product_mix_change", "items": items, "total_count": len(merged)}, trace


def average_order_value(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _load_and_window(trace, start, end)
    invoices = invoice_level(trace, window)
    aov = float(invoices["invoice_total"].mean()) if len(invoices) else 0.0
    trace.formula("average_order_value", "mean(invoice_total) across matched invoices", round(aov, 2))
    return {"metric": "average_order_value", "value": round(aov, 2), "invoice_count": len(invoices)}, trace
