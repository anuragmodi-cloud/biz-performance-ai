"""Engine dispatcher: (metric_category, sub_metric) -> the real-time compute
function, always reading from raw transaction/master data (data_loader.py's
allowlist), always returning a full step trace alongside the result.
"""
from __future__ import annotations

import pandas as pd

from . import data_loader as dl
from .compute import cashflow, customers, diagnostic, inventory, profitability, receivables, sales, suppliers
from .schema import QueryIntent, full_period_span, resolve_period
from .trace import StepTrace

_DISPATCH = {
    ("sales", "total_revenue"): sales.total_revenue,
    ("sales", "units_sold"): sales.units_sold,
    ("sales", "top_products_by_revenue"): sales.top_products_by_revenue,
    ("sales", "top_customers_by_revenue"): sales.top_customers_by_revenue,
    ("sales", "average_order_value"): sales.average_order_value,
    ("sales", "revenue_by_month"): sales.revenue_by_month,
    ("sales", "product_mix_change"): sales.product_mix_change,
    ("profitability", "gross_profit"): profitability.gross_profit,
    ("profitability", "gross_margin"): profitability.gross_margin_metric,
    ("profitability", "margin_by_category"): profitability.margin_by_category,
    ("profitability", "top_margin_products"): profitability.top_margin_products,
    ("profitability", "lowest_margin_products"): profitability.lowest_margin_products,
    ("profitability", "negative_margin_products"): profitability.negative_margin_products,
    ("profitability", "profit_by_month"): profitability.profit_by_month,
    ("cash_flow", "cash_inflow"): cashflow.cash_inflow,
    ("cash_flow", "cash_outflow"): cashflow.cash_outflow,
    ("cash_flow", "net_cash_flow"): cashflow.net_cash_flow,
    ("cash_flow", "ending_bank_balance"): cashflow.ending_bank_balance,
    ("cash_flow", "cash_by_type"): cashflow.cash_by_type,
    ("cash_flow", "negative_cash_flow_months"): cashflow.negative_cash_flow_months,
    ("receivables", "total_receivables"): receivables.total_receivables,
    ("receivables", "overdue_receivables"): receivables.overdue_receivables,
    ("receivables", "top_debtors"): receivables.top_debtors,
    ("receivables", "customer_payment_delay"): receivables.customer_payment_delay,
    ("receivables", "overdue_invoices"): receivables.overdue_invoices,
    ("suppliers", "total_payables"): suppliers.total_payables,
    ("suppliers", "top_suppliers_by_spend"): suppliers.top_suppliers_by_spend,
    ("suppliers", "supplier_price_trend"): suppliers.supplier_price_trend,
    ("suppliers", "supplier_concentration"): suppliers.supplier_concentration,
    ("suppliers", "supplier_delivery_delay"): suppliers.supplier_delivery_delay,
    ("inventory", "inventory_value"): inventory.inventory_value,
    ("inventory", "slow_moving_products"): inventory.slow_moving_products,
    ("inventory", "inventory_turnover"): inventory.inventory_turnover,
    ("customers", "best_customers"): customers.best_customers,
    ("customers", "customer_payment_reliability"): customers.customer_payment_reliability,
    ("customers", "repeat_customer_rate"): customers.repeat_customer_rate,
    ("diagnostic", "revenue_vs_profit"): diagnostic.revenue_vs_profit,
    ("diagnostic", "sales_vs_collections"): diagnostic.sales_vs_collections,
    ("diagnostic", "receivables_vs_revenue_growth"): diagnostic.receivables_vs_revenue_growth,
}


class EngineError(Exception):
    pass


def dispatch(intent: QueryIntent) -> tuple[dict, StepTrace]:
    key = (intent.metric_category, intent.sub_metric)
    fn = _DISPATCH.get(key)
    if fn is None:
        raise EngineError(f"No compute function registered for {key}")

    sim_start, sim_end = dl.simulation_bounds()
    start_unclamped, end = resolve_period(intent.period, intent.date_from, intent.date_to, sim_end)
    # Clamp into the dataset's actual window -- a period like "this_month"
    # resolved against sim_end can still start before the data begins.
    start = max(start_unclamped, sim_start)

    result, trace = fn(
        start=start, end=end, top_n=intent.top_n,
        entity_type=intent.entity_type, entity_name=intent.entity_name,
        entity_type_2=intent.entity_type_2, entity_name_2=intent.entity_name_2,
        filters=intent.filters,
    )
    result["period"] = {"start": str(start.date()), "end": str(end.date()), "label": intent.period}

    # Transparency policy: never let a partial window pass as a complete one
    # without saying so -- "this_year" etc. always end at sim_end, which is
    # virtually never the calendar end of that month/quarter/year (an
    # in-progress period), and any period can also be clipped short by the
    # dataset's own start (a real data-availability gap). Both are detected
    # centrally here so every sub_metric gets this automatically, with no
    # per-function changes.
    partial_reasons = []
    full_end = full_period_span(intent.period, end)
    if full_end is not None and full_end > end:
        partial_reasons.append(
            f"data only goes up to {end.date()}, so {intent.period.replace('_', ' ')} isn't "
            f"complete yet (it would run through {full_end.date()})"
        )
    if intent.period != "all_time" and start > start_unclamped:
        partial_reasons.append(
            f"data only starts {sim_start.date()}, so the full requested window isn't available "
            f"(it would have started {start_unclamped.date()})"
        )
    if partial_reasons:
        result["period"]["is_partial"] = True
        result["period"]["note"] = "Partial period: " + "; ".join(partial_reasons) + "."

    return result, trace
