"""Supplier-category compute functions."""
from __future__ import annotations

from .. import data_loader as dl
from ..formulas import filter_date_range, resolve_entity
from ..trace import StepTrace


def _purchases_window(trace: StepTrace, start, end):
    p = dl.purchases()
    trace.load("purchase_transactions.csv", len(p))
    return filter_date_range(trace, p, "purchase_date", start, end, "purchases in requested period")


def total_payables(end, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    p = dl.purchases()
    trace.load("purchase_transactions.csv", len(p))
    opened = p["purchase_date"] <= end
    still_open = p["payment_date"].isna() | (p["payment_date"] > end)
    outstanding = (p["invoice_total"] - p["amount_paid"]).clip(lower=0)
    mask = opened & still_open & (outstanding > 0)
    total = float(outstanding[mask].sum())
    trace.formula(
        "total_payables",
        f"total_payables as of {end.date()} = sum(invoice_total - amount_paid) for purchase orders "
        f"placed by then and not yet fully paid",
        round(total, 2),
    )
    return {"metric": "total_payables", "value": round(total, 2), "as_of": str(end.date())}, trace


def top_suppliers_by_spend(start, end, top_n=5, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _purchases_window(trace, start, end)
    all_suppliers = window.groupby(["supplier_id", "supplier_name"])["net_purchase_value"].sum() \
        .sort_values(ascending=False)
    by_supplier = all_suppliers.head(top_n)
    trace.aggregate("top_suppliers_by_spend", f"grouped by supplier, summed net_purchase_value, took top {top_n}",
                     {name: round(v, 2) for (_, name), v in by_supplier.items()})
    items = [{"supplier_id": sid, "supplier_name": name, "spend": round(v, 2)}
             for (sid, name), v in by_supplier.items()]
    return {"metric": "top_suppliers_by_spend", "items": items, "total_count": len(all_suppliers)}, trace


def _price_trend_from(trace: StepTrace, sub, label_field: str, label_value: str, metric: str) -> dict:
    """Shared early-vs-late purchase_price_per_unit trend calc, once `sub`
    (a purchases-window already filtered to one supplier OR one product) is
    known -- used by both branches of supplier_price_trend below."""
    if len(sub) < 4:
        trace.note("fewer than 4 purchase orders in the requested period -- trend not meaningful")
        return {"metric": metric, label_field: label_value,
                "note": "not enough purchase history in this period for a trend -- try a wider period "
                        "like all_time or last_year"}

    mid = sub["purchase_date"].min() + (sub["purchase_date"].max() - sub["purchase_date"].min()) / 2
    early = sub[sub["purchase_date"] < mid]["purchase_price_per_unit"]
    late = sub[sub["purchase_date"] >= mid]["purchase_price_per_unit"]
    early_avg, late_avg = float(early.mean()), float(late.mean())
    change_pct = ((late_avg - early_avg) / early_avg * 100) if early_avg else 0.0
    trace.formula(
        metric,
        f"split purchase history at midpoint date {mid.date()}; "
        f"early_avg_price={early_avg:.2f} (n={len(early)}), late_avg_price={late_avg:.2f} (n={len(late)}); "
        f"change_pct = (late-early)/early*100",
        round(change_pct, 2),
    )
    return {"metric": metric, label_field: label_value,
            "early_avg_price": round(early_avg, 2), "late_avg_price": round(late_avg, 2),
            "change_pct": round(change_pct, 2)}


def _resolve_and_trend(trace: StepTrace, p, entity_name: str, entity_type: str | None, start, end) -> dict:
    """Resolves one entity_name/entity_type pair against its master table
    and computes its purchase-price trend WITHIN [start, end]. Returns
    either a trend result dict, {"error": ...}, or {"ambiguous": True,
    "candidates": [...]} -- never silently guesses among several matching
    rows (see formulas.resolve_entity)."""
    if entity_type == "product":
        products = dl.products()
        trace.load("products.csv", len(products))
        resolution = resolve_entity(trace, products, "product_id", "product_name", entity_name, "products.csv")
        if resolution["status"] == "not_found":
            return {"error": f"no product found matching {entity_name!r}"}
        if resolution["status"] == "ambiguous":
            return {"ambiguous": True, "candidates": resolution["candidates"]}
        pid, pname = resolution["id"], resolution["name"]
        sub = p[p["product_id"] == pid].sort_values("purchase_date")
        trace.filter(f"purchase_transactions.csv for product_id={pid}", len(p), len(sub))
        sub = filter_date_range(trace, sub, "purchase_date", start, end, "purchases in requested period")
        return _price_trend_from(trace, sub, "product_name", pname, "supplier_price_trend")

    suppliers = dl.suppliers()
    trace.load("suppliers.csv", len(suppliers))
    resolution = resolve_entity(trace, suppliers, "supplier_id", "supplier_name", entity_name, "suppliers.csv")
    if resolution["status"] == "not_found":
        return {"error": f"no supplier found matching {entity_name!r}"}
    if resolution["status"] == "ambiguous":
        return {"ambiguous": True, "candidates": resolution["candidates"]}
    sid, sname = resolution["id"], resolution["name"]
    sub = p[p["supplier_id"] == sid].sort_values("purchase_date")
    trace.filter(f"purchases for supplier_id={sid}", len(p), len(sub))
    sub = filter_date_range(trace, sub, "purchase_date", start, end, "purchases in requested period")
    result = _price_trend_from(trace, sub, "supplier_name", sname, "supplier_price_trend")
    result["supplier_id"] = sid
    return result


def supplier_price_trend(start, end, entity_name=None, entity_type=None, entity_name_2=None, entity_type_2=None,
                          **_) -> tuple[dict, StepTrace]:
    """entity_type="product" traces one PRODUCT's own purchase unit-cost
    trend (its cost across whichever suppliers it was bought from); anything
    else (the default) traces one SUPPLIER's average price trend across
    everything bought from them. Two genuinely different questions ("is
    Wyvern laptop's cost rising" vs "is supplier X raising prices") that
    both land on this sub_metric -- the caller's entity_type is what
    disambiguates which one was actually asked.

    The early-vs-late split happens WITHIN [start, end] -- previously this
    ignored the resolved period entirely and always split the full purchase
    history, so a result labeled period="this_quarter" could actually be
    describing a multi-year trend. Needs a wide period (all_time/last_year)
    to have enough purchase orders for a meaningful split; a narrow period
    with fewer than 4 matching orders honestly returns "not enough purchase
    history for a trend" instead of a number.

    entity_name_2/entity_type_2 are optional -- when given (a "compare X and
    Y" question), a second entity is resolved and trended the same way, and
    both are returned together as {"primary": ..., "comparison": ...}.
    """
    trace = StepTrace()
    p = dl.purchases()
    trace.load("purchase_transactions.csv", len(p))

    if not entity_name:
        trace.note("no entity_name given")
        return {"metric": "supplier_price_trend", "error": "entity_name is required"}, trace

    primary = _resolve_and_trend(trace, p, entity_name, entity_type, start, end)
    if "error" in primary or "ambiguous" in primary:
        return {"metric": "supplier_price_trend", **primary}, trace

    if not entity_name_2:
        return primary, trace

    comparison = _resolve_and_trend(trace, p, entity_name_2, entity_type_2, start, end)
    if "error" in comparison or "ambiguous" in comparison:
        return {"metric": "supplier_price_trend", "primary": primary, "comparison_error": comparison}, trace

    return {"metric": "supplier_price_trend", "primary": primary, "comparison": comparison}, trace


def supplier_delivery_delay(start, end, top_n=5, filters=None, **_) -> tuple[dict, StepTrace]:
    """Threshold/exception + ranking: which suppliers deliver the slowest,
    using the dataset's own precomputed delivery_delay_days column, averaged
    per supplier across their DELIVERED orders in the window (orders still
    in transit have no actual_delivery_date and are excluded, not counted
    as zero delay).
    filters['min_avg_delay_days'] (optional) -- only suppliers averaging at
    least this many days late."""
    trace = StepTrace()
    window = _purchases_window(trace, start, end)
    delivered = window[window["actual_delivery_date"].notna()]
    trace.filter("purchases with a recorded actual_delivery_date (excludes still-in-transit orders)",
                 len(window), len(delivered))
    by_supplier = delivered.groupby(["supplier_id", "supplier_name"])["delivery_delay_days"].mean().sort_values(ascending=False)

    min_delay = (filters or {}).get("min_avg_delay_days")
    if min_delay is not None:
        by_supplier = by_supplier[by_supplier >= min_delay]

    top_slice = by_supplier.head(top_n)
    trace.aggregate("supplier_delivery_delay",
                     f"grouped by supplier, averaged delivery_delay_days across delivered orders, took top {top_n} slowest"
                     + (f" (min_avg_delay_days={min_delay})" if min_delay is not None else ""),
                     {name: round(v, 1) for (_, name), v in top_slice.items()})
    items = [{"supplier_id": sid, "supplier_name": name, "avg_delivery_delay_days": round(v, 1)}
             for (sid, name), v in top_slice.items()]
    return {"metric": "supplier_delivery_delay", "items": items, "unit": "days", "total_count": len(by_supplier)}, trace


def supplier_concentration(start, end, top_n=5, **_) -> tuple[dict, StepTrace]:
    trace = StepTrace()
    window = _purchases_window(trace, start, end)
    by_supplier = window.groupby("supplier_id")["net_purchase_value"].sum().sort_values(ascending=False)
    total = float(by_supplier.sum())
    top1_share = float(by_supplier.iloc[0] / total * 100) if total and len(by_supplier) else 0.0
    top5_share = float(by_supplier.head(5).sum() / total * 100) if total else 0.0
    trace.formula(
        "supplier_concentration",
        f"top1_share_pct = top supplier's net_purchase_value / total * 100; "
        f"top5_share_pct = top-5 suppliers' net_purchase_value / total * 100",
        {"top1_share_pct": round(top1_share, 2), "top5_share_pct": round(top5_share, 2)},
    )
    return {"metric": "supplier_concentration", "top1_share_pct": round(top1_share, 2),
            "top5_share_pct": round(top5_share, 2)}, trace
