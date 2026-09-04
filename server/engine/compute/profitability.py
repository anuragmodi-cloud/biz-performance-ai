"""Profitability-category compute functions."""
from __future__ import annotations

from .. import data_loader as dl
from ..formulas import cogs, filter_date_range, gross_margin, revenue
from ..trace import StepTrace


def _window(trace: StepTrace, start, end):
    sales = dl.sales()
    trace.load("sales_transactions.csv", len(sales))
    return filter_date_range(trace, sales, "transaction_date", start, end, "sales in requested period")


def gross_profit(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _window(trace, start, end)
    rev = revenue(trace, window)
    cost = cogs(trace, window)
    gp = rev - cost
    trace.formula("gross_profit", f"revenue({rev:.2f}) - cogs({cost:.2f})", round(gp, 2))
    return {"metric": "gross_profit", "value": round(gp, 2), "revenue": round(rev, 2), "cogs": round(cost, 2)}, trace


def gross_margin_metric(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _window(trace, start, end)
    rev = revenue(trace, window)
    cost = cogs(trace, window)
    margin = gross_margin(trace, rev, cost)
    return {"metric": "gross_margin", "value": round(margin * 100, 2), "unit": "percent",
            "revenue": round(rev, 2), "cogs": round(cost, 2)}, trace


def margin_by_category(start, end, top_n=10, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _window(trace, start, end)
    g = window.groupby("category").apply(
        lambda d: (d["taxable_value"].sum() - (d["quantity"] * d["unit_purchase_cost_ref"]).sum())
        / d["taxable_value"].sum() if d["taxable_value"].sum() else 0.0,
        include_groups=False,
    ).sort_values(ascending=False)
    trace.aggregate("margin_by_category", f"gross_margin per category = (revenue - cogs) / revenue, grouped by category, took top {top_n}",
                     {k: round(v * 100, 2) for k, v in g.head(top_n).items()})
    items = [{"category": k, "gross_margin_pct": round(v * 100, 2)} for k, v in g.head(top_n).items()]
    return {"metric": "margin_by_category", "items": items, "total_count": len(g)}, trace


def _margin_by_product(trace: StepTrace, window):
    g = window.groupby(["product_id", "product_name"]).apply(
        lambda d: (d["taxable_value"].sum() - (d["quantity"] * d["unit_purchase_cost_ref"]).sum())
        / d["taxable_value"].sum() if d["taxable_value"].sum() else 0.0,
        include_groups=False,
    )
    return g


def top_margin_products(start, end, top_n=5, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _window(trace, start, end)
    all_g = _margin_by_product(trace, window).sort_values(ascending=False)
    g = all_g.head(top_n)
    trace.aggregate("top_margin_products", f"grouped by product, computed gross_margin_pct, took top {top_n} highest",
                     {name: round(v * 100, 2) for (_, name), v in g.items()})
    items = [{"product_id": pid, "product_name": name, "gross_margin_pct": round(v * 100, 2)}
             for (pid, name), v in g.items()]
    return {"metric": "top_margin_products", "items": items, "total_count": len(all_g)}, trace


def lowest_margin_products(start, end, top_n=5, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _window(trace, start, end)
    all_g = _margin_by_product(trace, window).sort_values(ascending=True)
    g = all_g.head(top_n)
    trace.aggregate("lowest_margin_products", f"grouped by product, computed gross_margin_pct, took top {top_n} lowest",
                     {name: round(v * 100, 2) for (_, name), v in g.items()})
    items = [{"product_id": pid, "product_name": name, "gross_margin_pct": round(v * 100, 2)}
             for (pid, name), v in g.items()]
    return {"metric": "lowest_margin_products", "items": items, "total_count": len(all_g)}, trace


def negative_margin_products(start, end, top_n=10, filters=None, **_) -> tuple[dict, StepTrace]:
    """Threshold/exception question ("which products are selling at a loss")
    rather than a fixed top-N ranking -- returns EVERY product below the
    threshold (capped to top_n for narration, with the true total_count
    alongside), not just whichever N happen to be lowest.
    filters['margin_threshold_pct'] (default 0) -- products with
    gross_margin_pct strictly below this are included."""
    trace = StepTrace()
    window = _window(trace, start, end)
    threshold_pct = (filters or {}).get("margin_threshold_pct", 0)
    g = _margin_by_product(trace, window)
    below = g[g < threshold_pct / 100].sort_values(ascending=True)
    trace.aggregate(
        "negative_margin_products",
        f"grouped by product, computed gross_margin_pct, kept products below {threshold_pct}% margin "
        f"({len(below)} of {len(g)} products matched)",
        {name: round(v * 100, 2) for (_, name), v in below.head(top_n).items()},
    )
    items = [{"product_id": pid, "product_name": name, "gross_margin_pct": round(v * 100, 2)}
             for (pid, name), v in below.head(top_n).items()]
    return {"metric": "negative_margin_products", "items": items, "total_count": len(below),
            "margin_threshold_pct": threshold_pct}, trace
