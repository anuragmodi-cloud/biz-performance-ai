"""Inventory-category compute functions."""
from __future__ import annotations

import pandas as pd

from .. import data_loader as dl
from ..formulas import filter_date_range
from ..trace import StepTrace


def inventory_value(end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    inv = dl.inventory()
    trace.load("inventory_transactions.csv", len(inv))
    products = dl.products()
    trace.load("products.csv", len(products))

    upto = inv[inv["timestamp"] <= end]
    trace.filter(f"inventory movements on or before {end.date()}", len(inv), len(upto))
    balance = upto.sort_values("timestamp").groupby("product_id")["quantity_balance"].last()
    cost_map = products.set_index("product_id")["typical_purchase_cost"]
    value = float((balance * balance.index.map(cost_map).fillna(0)).sum())
    trace.formula(
        "inventory_value",
        f"for each product, last quantity_balance on/before {end.date()} * typical_purchase_cost, summed",
        round(value, 2),
    )
    return {"metric": "inventory_value", "value": round(value, 2), "as_of": str(end.date())}, trace


def slow_moving_products(end, top_n=10, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    sales = dl.sales()
    trace.load("sales_transactions.csv", len(sales))
    products = dl.products()
    trace.load("products.csv", len(products))

    lookback_start = end - pd.Timedelta(days=90)
    recent = filter_date_range(trace, sales, "transaction_date", lookback_start, end, "trailing-90-day sales")
    sold = recent.groupby("product_id")["quantity"].sum()
    monthly_velocity = (sold.reindex(products["product_id"]).fillna(0) / 3.0)
    active = products[products["product_lifecycle_stage"] != "discontinued"].set_index("product_id")
    velocity = monthly_velocity.reindex(active.index)
    slow = velocity.sort_values().head(top_n)
    trace.aggregate(
        "slow_moving_products",
        f"monthly_velocity = trailing-90-day units sold / 3, per product; took {top_n} lowest (excluding discontinued)",
        {pid: round(v, 3) for pid, v in slow.items()},
    )
    items = [{"product_id": pid, "product_name": active.loc[pid, "product_name"], "monthly_units_sold": round(v, 3)}
             for pid, v in slow.items()]
    return {"metric": "slow_moving_products", "items": items, "as_of": str(end.date())}, trace


def inventory_turnover(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    p = dl.purchases()
    trace.load("purchase_transactions.csv", len(p))
    window = filter_date_range(trace, p, "purchase_date", start, end, "purchases in requested period")
    purchase_cost = float(window["net_purchase_value"].sum())

    inv = dl.inventory()
    trace.load("inventory_transactions.csv", len(inv))
    products = dl.products()
    trace.load("products.csv", len(products))
    cost_map = products.set_index("product_id")["typical_purchase_cost"]

    upto = inv[inv["timestamp"] <= end]
    balance = upto.sort_values("timestamp").groupby("product_id")["quantity_balance"].mean()
    avg_inv_value = float((balance * balance.index.map(cost_map).fillna(0)).sum())

    turnover = (purchase_cost / avg_inv_value) if avg_inv_value else None
    trace.formula(
        "inventory_turnover",
        f"purchase_cost_in_period({purchase_cost:.2f}) / "
        f"avg_inventory_value_by_{end.date()}({avg_inv_value:.2f}) -- approximate, "
        f"using mean historical quantity_balance per product as the avg-inventory proxy",
        round(turnover, 3) if turnover is not None else None,
    )
    return {"metric": "inventory_turnover", "value": round(turnover, 3) if turnover is not None else None}, trace
