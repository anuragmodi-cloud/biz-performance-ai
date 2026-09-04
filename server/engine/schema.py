"""QueryIntent: the structured shape the LLM's tool-call arguments fill in.

The LLM does natural-language -> structured translation via normal
tool-calling (see tools/ask_calculation_engine.py's JSON schema); this module
owns validating that structure and resolving relative periods ("this month",
"last quarter") into concrete dates against the dataset's own simulation
clock (business.json's simulation_end_date, NOT wall-clock "today" -- the
data is a fixed historical window, so "today" for the purposes of these
questions is the last day of data, not the real calendar date).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

import pandas as pd

METRIC_CATEGORIES = {
    "sales": {"total_revenue", "units_sold", "top_products_by_revenue", "top_customers_by_revenue", "average_order_value", "revenue_by_month", "product_mix_change"},
    "profitability": {"gross_profit", "gross_margin", "margin_by_category", "top_margin_products", "lowest_margin_products", "negative_margin_products"},
    "cash_flow": {"cash_inflow", "cash_outflow", "net_cash_flow", "ending_bank_balance", "cash_by_type", "negative_cash_flow_months"},
    "receivables": {"total_receivables", "overdue_receivables", "top_debtors", "customer_payment_delay", "overdue_invoices"},
    "suppliers": {"total_payables", "top_suppliers_by_spend", "supplier_price_trend", "supplier_concentration", "supplier_delivery_delay"},
    "inventory": {"inventory_value", "slow_moving_products", "inventory_turnover"},
    "customers": {"best_customers", "customer_payment_reliability", "repeat_customer_rate"},
    "diagnostic": {"revenue_vs_profit", "sales_vs_collections", "receivables_vs_revenue_growth"},
}

VALID_PERIODS = {
    "this_month", "last_month", "this_quarter", "last_quarter",
    "this_year", "last_year", "last_30_days", "last_90_days", "all_time", "custom",
}

VALID_ENTITY_TYPES = {"customer", "product", "supplier", "category"}

# Sub_metrics whose entity_type is actually read/branched on downstream --
# for these, an entity_type outside the listed set is meaningless and would
# otherwise be silently ignored by the compute function (e.g.
# customer_payment_delay never reads entity_type at all, so passing
# entity_type="supplier" to it used to pass validation and do nothing,
# instead of failing loudly).
ENTITY_AWARE_SUB_METRICS = {
    "supplier_price_trend": {"product", "supplier"},
    "customer_payment_delay": {"customer"},
    "customer_payment_reliability": {"customer"},
}


class IntentValidationError(ValueError):
    pass


@dataclass
class QueryIntent:
    metric_category: str
    sub_metric: str
    period: str = "all_time"
    date_from: str | None = None
    date_to: str | None = None
    compare_to_previous: bool = False
    entity_type: str | None = None       # "customer" | "product" | "supplier" | "category"
    entity_name: str | None = None
    # Optional second entity, for a "compare X and Y" question -- only a
    # handful of entity-aware sub_metrics read these (see
    # ENTITY_AWARE_SUB_METRICS); others ignore them via **_.
    entity_type_2: str | None = None
    entity_name_2: str | None = None
    top_n: int = 5
    filters: dict = field(default_factory=dict)

    def cache_key(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:24]


def validate_intent(args: dict) -> QueryIntent:
    metric_category = (args.get("metric_category") or "").strip()
    sub_metric = (args.get("sub_metric") or "").strip()

    if metric_category not in METRIC_CATEGORIES:
        raise IntentValidationError(
            f"Unknown metric_category {metric_category!r}. Must be one of {sorted(METRIC_CATEGORIES)}."
        )
    if sub_metric not in METRIC_CATEGORIES[metric_category]:
        raise IntentValidationError(
            f"Unknown sub_metric {sub_metric!r} for category {metric_category!r}. "
            f"Must be one of {sorted(METRIC_CATEGORIES[metric_category])}."
        )

    period = (args.get("period") or "all_time").strip()
    if period not in VALID_PERIODS:
        raise IntentValidationError(f"Unknown period {period!r}. Must be one of {sorted(VALID_PERIODS)}.")

    date_from, date_to = args.get("date_from"), args.get("date_to")
    if period == "custom":
        for label, value in (("date_from", date_from), ("date_to", date_to)):
            if not value or not _is_iso_date(value):
                raise IntentValidationError(
                    f"period='custom' requires {label} as an ISO YYYY-MM-DD date; got {value!r}."
                )

    top_n = args.get("top_n", 5)
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        raise IntentValidationError(f"top_n must be an integer, got {top_n!r}.")
    if not (1 <= top_n <= 50):
        raise IntentValidationError("top_n must be between 1 and 50.")

    entity_type = args.get("entity_type") or None
    if entity_type is not None and entity_type not in VALID_ENTITY_TYPES:
        raise IntentValidationError(f"Unknown entity_type {entity_type!r}.")

    entity_type_2 = args.get("entity_type_2") or None
    if entity_type_2 is not None and entity_type_2 not in VALID_ENTITY_TYPES:
        raise IntentValidationError(f"Unknown entity_type_2 {entity_type_2!r}.")

    # entity_type isn't just a free-form label -- for sub_metrics that
    # actually branch on it, a mismatched value used to pass validation and
    # then be silently ignored downstream (e.g. customer_payment_delay never
    # reads entity_type at all). Fail loudly instead.
    allowed_entity_types = ENTITY_AWARE_SUB_METRICS.get(sub_metric)
    if allowed_entity_types is not None:
        for label, value in (("entity_type", entity_type), ("entity_type_2", entity_type_2)):
            if value is not None and value not in allowed_entity_types:
                raise IntentValidationError(
                    f"{label}={value!r} isn't valid for sub_metric {sub_metric!r} -- must be one of "
                    f"{sorted(allowed_entity_types)} (or omitted)."
                )

    return QueryIntent(
        metric_category=metric_category,
        sub_metric=sub_metric,
        period=period,
        date_from=date_from if period == "custom" else None,
        date_to=date_to if period == "custom" else None,
        compare_to_previous=bool(args.get("compare_to_previous", False)),
        entity_type=entity_type,
        entity_name=(args.get("entity_name") or None),
        entity_type_2=entity_type_2,
        entity_name_2=(args.get("entity_name_2") or None),
        top_n=top_n,
        filters=args.get("filters") or {},
    )


def _is_iso_date(value: str) -> bool:
    try:
        pd.Timestamp(value)
        return True
    except (ValueError, TypeError):
        return False


def resolve_period(period: str, date_from: str | None, date_to: str | None,
                    sim_end: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Resolve a relative period into (start, end) dates, anchored to the
    dataset's own last simulated day (sim_end) rather than wall-clock now."""
    end = sim_end.normalize()

    if period == "custom":
        return pd.Timestamp(date_from), pd.Timestamp(date_to)
    if period == "all_time":
        return pd.Timestamp("2000-01-01"), end
    if period == "last_30_days":
        return end - pd.Timedelta(days=29), end
    if period == "last_90_days":
        return end - pd.Timedelta(days=89), end
    if period == "this_month":
        return end.replace(day=1), end
    if period == "last_month":
        first_this = end.replace(day=1)
        last_of_prev = first_this - pd.Timedelta(days=1)
        return last_of_prev.replace(day=1), last_of_prev
    if period == "this_quarter":
        q_start_month = ((end.month - 1) // 3) * 3 + 1
        return end.replace(month=q_start_month, day=1), end
    if period == "last_quarter":
        q_start_month = ((end.month - 1) // 3) * 3 + 1
        this_q_start = end.replace(month=q_start_month, day=1)
        last_q_end = this_q_start - pd.Timedelta(days=1)
        last_q_start_month = ((last_q_end.month - 1) // 3) * 3 + 1
        return last_q_end.replace(month=last_q_start_month, day=1), last_q_end
    if period == "this_year":
        return end.replace(month=1, day=1), end
    if period == "last_year":
        return pd.Timestamp(year=end.year - 1, month=1, day=1), pd.Timestamp(year=end.year - 1, month=12, day=31)
    raise IntentValidationError(f"Unhandled period {period!r}")


def full_period_span(period: str, resolved_end: pd.Timestamp) -> pd.Timestamp | None:
    """For an in-progress period ("this_month"/"this_quarter"/"this_year"),
    the natural UNCLIPPED end that period would have if it had actually
    finished (e.g. Dec 31 for this_year) -- used by engine.dispatch() to
    detect when the resolved window is short not because of a data gap but
    because the period simply hasn't happened yet as of the dataset's own
    sim_end. Returns None for periods with no such concept: all_time,
    custom, and every last_* period (already-complete by construction)."""
    if period == "this_month":
        next_month_start = pd.Timestamp(year=resolved_end.year, month=resolved_end.month, day=1) + pd.DateOffset(months=1)
        return next_month_start - pd.Timedelta(days=1)
    if period == "this_quarter":
        q_start_month = ((resolved_end.month - 1) // 3) * 3 + 1
        q_start = pd.Timestamp(year=resolved_end.year, month=q_start_month, day=1)
        return q_start + pd.DateOffset(months=3) - pd.Timedelta(days=1)
    if period == "this_year":
        return pd.Timestamp(year=resolved_end.year, month=12, day=31)
    return None


def previous_comparable_window(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The immediately preceding window of the same length, for
    compare_to_previous."""
    span = end - start
    prev_end = start - pd.Timedelta(days=1)
    prev_start = prev_end - span
    return prev_start, prev_end
