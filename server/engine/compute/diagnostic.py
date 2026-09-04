"""Diagnostic-category compute functions: composite questions that chain two
periods and two metrics together ("sales are up, why is profit down?").

These correlate against business_events.csv for a *candidate* explanation --
labeled explicitly as a correlation/hypothesis, never asserted as the
confirmed cause, since this engine has no privileged access to which
scenario (if any) was actually injected into the dataset.
"""
from __future__ import annotations

from .. import data_loader as dl
from ..formulas import cogs, filter_date_range, gross_margin, invoice_level, outstanding_as_of, revenue
from ..schema import previous_comparable_window
from ..trace import StepTrace


def _period_financials(trace: StepTrace, sales, start, end, label: str) -> dict:
    window = filter_date_range(trace, sales, "transaction_date", start, end, label)
    rev = revenue(trace, window)
    cost = cogs(trace, window)
    margin = gross_margin(trace, rev, cost)
    return {"revenue": rev, "cogs": cost, "gross_profit": rev - cost, "gross_margin": margin}


def revenue_vs_profit(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    sales = dl.sales()
    trace.load("sales_transactions.csv", len(sales))

    current = _period_financials(trace, sales, start, end, "current period")
    prev_start, prev_end = previous_comparable_window(start, end)
    previous = _period_financials(trace, sales, prev_start, prev_end, "previous comparable period")

    revenue_change_pct = ((current["revenue"] - previous["revenue"]) / previous["revenue"] * 100
                           ) if previous["revenue"] else 0.0
    profit_change_pct = ((current["gross_profit"] - previous["gross_profit"]) / previous["gross_profit"] * 100
                          ) if previous["gross_profit"] else 0.0
    trace.formula(
        "revenue_vs_profit_change",
        f"revenue_change_pct = (current_revenue - previous_revenue) / previous_revenue * 100; "
        f"profit_change_pct = (current_gross_profit - previous_gross_profit) / previous_gross_profit * 100",
        {"revenue_change_pct": round(revenue_change_pct, 2), "profit_change_pct": round(profit_change_pct, 2)},
    )

    divergence = revenue_change_pct > 0 and profit_change_pct < revenue_change_pct
    candidates = []
    if divergence:
        events = dl.business_events()
        trace.load("business_events.csv", len(events))
        overlapping = events[(events["start_date"] <= end) & (events["end_date"] >= start)]
        trace.filter("business_events overlapping the current period", len(events), len(overlapping))
        for _, ev in overlapping.iterrows():
            candidates.append({"event_type": ev["event_type"], "description": ev["description"],
                                "start": str(ev["start_date"].date()), "end": str(ev["end_date"].date())})
        margin_drop_pts = round((previous["gross_margin"] - current["gross_margin"]) * 100, 2)
        trace.note(
            f"margin fell {margin_drop_pts} percentage points ({previous['gross_margin']*100:.2f}% -> "
            f"{current['gross_margin']*100:.2f}%) while revenue rose -- flagging as a candidate explanation, "
            f"not a confirmed cause; cross-check discount levels and per-product cost trend for this window"
        )

    return {
        "metric": "revenue_vs_profit",
        "current": {"revenue": round(current["revenue"], 2), "gross_profit": round(current["gross_profit"], 2),
                     "gross_margin_pct": round(current["gross_margin"] * 100, 2)},
        "previous": {"revenue": round(previous["revenue"], 2), "gross_profit": round(previous["gross_profit"], 2),
                      "gross_margin_pct": round(previous["gross_margin"] * 100, 2)},
        "revenue_change_pct": round(revenue_change_pct, 2),
        "profit_change_pct": round(profit_change_pct, 2),
        "revenue_up_profit_lagging": divergence,
        "candidate_events_in_window": candidates,
    }, trace


def receivables_vs_revenue_growth(start, end, **_) -> tuple[dict, StepTrace]:
    """Composite comparison -- two independently-computed trends (revenue
    growth and receivables growth, each current period vs. the previous
    comparable period) compared AGAINST each other, distinct from
    compare_to_previous (one metric, two periods) or entity_name_2 (one
    metric, two entities). Answers "are my receivables growing faster than
    my revenue?"."""
    trace = StepTrace()
    sales = dl.sales()
    trace.load("sales_transactions.csv", len(sales))

    current_window = filter_date_range(trace, sales, "transaction_date", start, end, "sales in current period")
    current_revenue = revenue(trace, current_window)

    prev_start, prev_end = previous_comparable_window(start, end)
    previous_window = filter_date_range(trace, sales, "transaction_date", prev_start, prev_end,
                                         "sales in previous comparable period")
    previous_revenue = revenue(trace, previous_window)

    invoices = invoice_level(trace, sales)
    current_receivables = outstanding_as_of(trace, invoices, end, overdue_only=False)
    previous_receivables = outstanding_as_of(trace, invoices, prev_end, overdue_only=False)

    revenue_growth_pct = ((current_revenue - previous_revenue) / previous_revenue * 100) if previous_revenue else 0.0
    receivables_growth_pct = (
        (current_receivables - previous_receivables) / previous_receivables * 100
    ) if previous_receivables else 0.0
    trace.formula(
        "receivables_vs_revenue_growth",
        f"revenue_growth_pct = (current_revenue - previous_revenue) / previous_revenue * 100; "
        f"receivables_growth_pct = (current_receivables - previous_receivables) / previous_receivables * 100",
        {"revenue_growth_pct": round(revenue_growth_pct, 2), "receivables_growth_pct": round(receivables_growth_pct, 2)},
    )
    return {
        "metric": "receivables_vs_revenue_growth",
        "current_revenue": round(current_revenue, 2), "previous_revenue": round(previous_revenue, 2),
        "current_receivables": round(current_receivables, 2), "previous_receivables": round(previous_receivables, 2),
        "revenue_growth_pct": round(revenue_growth_pct, 2), "receivables_growth_pct": round(receivables_growth_pct, 2),
        "receivables_growing_faster": receivables_growth_pct > revenue_growth_pct,
    }, trace


def sales_vs_collections(start, end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    sales = dl.sales()
    trace.load("sales_transactions.csv", len(sales))
    window = filter_date_range(trace, sales, "transaction_date", start, end, "sales in current period")
    rev = revenue(trace, window)

    collected = window.dropna(subset=["payment_date"])
    collected = collected[(collected["payment_date"] >= start) & (collected["payment_date"] <= end)]
    collections = float(collected["amount_paid"].sum())
    trace.aggregate("collections", "sum(amount_paid) for payments recorded within the same window", round(collections, 2))

    gap_pct = ((rev - collections) / rev * 100) if rev else 0.0
    collected_pct = 100.0 - gap_pct
    trace.formula(
        "collection_gap_pct",
        "collection_gap_pct = (revenue - collections) / revenue * 100; "
        "collected_pct = 100 - collection_gap_pct -- provided directly so the narration "
        "never has to compute this complement itself",
        {"collection_gap_pct": round(gap_pct, 2), "collected_pct": round(collected_pct, 2)},
    )

    return {"metric": "sales_vs_collections", "revenue": round(rev, 2), "collections": round(collections, 2),
            "collection_gap_pct": round(gap_pct, 2), "collected_pct": round(collected_pct, 2),
            "revenue_growing_faster_than_collections": gap_pct > 15}, trace
