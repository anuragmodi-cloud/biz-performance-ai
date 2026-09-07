"""The ask_calculation_engine tool.

This is the ONE tool the LLM has. It translates a caller's natural-language
business question into the engine's structured QueryIntent (the LLM does
that translation via normal tool-calling -- there's no separate NLU layer),
checks the session's query_cache first, and otherwise calls the real-time
engine and logs the ask.

Provider-agnostic on purpose: TOOL_NAME/TOOL_DESCRIPTION/TOOL_PARAMETERS are
plain dicts/JSON-schema, and `handle()` takes/returns plain dicts, so both
Phase 1's dev_llm_client.py (raw provider SDK tool-calling) and a future
Phase 2 Pipecat FunctionSchema adapter can wrap the same handler without
duplicating this logic -- same spirit as kyc-voice-agent's
tools/submit_user_details.py enforcing its business rules in the handler
itself, not just via prompt instructions.
"""
from __future__ import annotations

import time

from engine.engine import EngineError, dispatch
from engine.schema import METRIC_CATEGORIES, IntentValidationError, validate_intent
from query_log import QueryLogEntry, STATUS_AMBIGUOUS_ENTITY, STATUS_ANSWERED, STATUS_UNFULFILLED, append
from session_store import CachedAnswer, get_session, update_session

TOOL_NAME = "ask_calculation_engine"

# Rupee field names that are unambiguously currency across every sub_metric
# they appear in.
_CURRENCY_FIELD_NAMES = {
    "revenue", "cogs", "gross_profit", "cash_inflow", "cash_outflow", "net_cash_flow",
    "spend", "outstanding", "net_amount", "early_avg_price", "late_avg_price", "collections",
    "current_revenue", "previous_revenue", "current_receivables", "previous_receivables",
    "total_outstanding", "total_outstanding_top_n", "amount_paid", "amount_outstanding",
    "invoice_total", "taxable_value",
}
# "value" is ambiguous -- depending on sub_metric it can be a rupee amount, a
# unit count, a percentage, a day count, or a ratio -- so it's only ever
# treated as currency for the specific metrics where it actually is one.
_CURRENCY_VALUE_METRICS = {
    "total_revenue", "average_order_value", "total_payables", "cash_inflow", "cash_outflow",
    "net_cash_flow", "ending_bank_balance", "total_receivables", "overdue_receivables",
    "gross_profit", "inventory_value",
}

# Full-series sub_metrics are exempt from the >5-entity truncation policy --
# showing every period IS the point (seasonality needs the whole shape), not
# a ranking of many entities being narrowed down.
FULL_SERIES_SUB_METRICS = {"revenue_by_month"}
MAX_NARRATED_ITEMS = 5
# Comfortably exceeds any real group size in this dataset (max ~1000
# customers) -- used only internally when confirmed_full_list=true, never
# actor-facing, so it bypasses validate_intent's normal 1-50 top_n cap.
FULL_LIST_TOP_N = 2000

TOOL_DESCRIPTION = (
    "Answer a business-performance question by running it through the real-time "
    "calculation engine. ALWAYS call this for any question about sales, revenue, "
    "profit/margin, cash flow, receivables, payables, suppliers, inventory, or "
    "customer behavior -- never estimate or answer from memory. "
    "Translate the caller's question into the structured arguments below "
    f"(valid metric_category -> sub_metric combinations: "
    f"{ {k: sorted(v) for k, v in METRIC_CATEGORIES.items()} }). "
    "receivables.customer_payment_delay also answers TWO different questions depending on entity_name: "
    "entity_name omitted = 'which customers delay payment the most' (a top_n ranking across all customers); "
    "entity_name=<customer name> = one specific customer's own average payment delay. "
    "suppliers.supplier_price_trend answers TWO different questions depending on entity_type: "
    "entity_type='product' + entity_name=<product name> traces that PRODUCT's own purchase unit-cost "
    "trend (e.g. 'is the Wyvern laptop's cost rising'); entity_type='supplier' (or omitted) + "
    "entity_name=<supplier name> traces that SUPPLIER's average price trend across everything bought "
    "from them (e.g. 'is supplier X raising prices'). Match entity_type to what the caller actually named. "
    "supplier_price_trend computes its early-vs-late split WITHIN the resolved period, so if the caller doesn't "
    "name a specific period, use period='all_time' -- a narrow period like this_month/this_quarter often won't "
    "have enough purchase orders for a meaningful trend and will honestly report that instead of a number. "
    "If the caller names TWO entities to compare (e.g. 'compare Wyvern laptop and Griplex smartphone costs', "
    "'is Sharma Traders or Verma Retail slower to pay'), fill entity_name_2/entity_type_2 with the second one -- "
    "supported by supplier_price_trend, customer_payment_delay, and customer_payment_reliability. "
    "If the result contains ambiguous=true, DO NOT GUESS which candidate was meant -- read the candidate "
    "names in `candidates` aloud to the caller, ask them which one, then call this tool again with a more "
    "specific entity_name once they clarify. "
    "A common way to CAUSE an avoidable ambiguous=true: truncating entity_name to just the brand word instead of "
    "keeping the caller's full description. 'Kya Wyvern laptop ka cost badh raha hai' must be entity_name='Wyvern "
    "laptop' -- passing just entity_name='Wyvern' matches every Wyvern-branded product (phones, headphones, bags, "
    "the laptop) and produces a needless clarifying question when the caller's words already picked one uniquely. "
    "THRESHOLD/EXCEPTION questions (e.g. 'which products are selling at a loss', 'invoices overdue beyond 60 "
    "days', 'suppliers averaging more than 10 days late') use the `filters` object, not entity_name -- "
    "profitability.negative_margin_products reads filters.margin_threshold_pct (percent, default 0 -- products "
    "below this are included); receivables.overdue_invoices reads filters.days_overdue_min (default 30); "
    "suppliers.supplier_delivery_delay optionally reads filters.min_avg_delay_days to narrow to only the "
    "worst offenders. Omit filters entirely for the default threshold. "
    "diagnostic.receivables_vs_revenue_growth and sales.product_mix_change are COMPOSITE comparisons -- they "
    "already compare the current period against the previous comparable one internally, so do NOT set "
    "compare_to_previous or entity_name for these; just resolve the period the caller means. "
    "If the result has more_available=true, it has MORE than 5 matching entities but only shows the top 5 in "
    "`items` (total_count says how many exist in total) -- narrate ONLY those 5, then tell the caller the total "
    "and ask if they want the complete list. ONLY after they explicitly say yes, call this tool again with the "
    "exact same arguments plus confirmed_full_list=true to get everything -- never guess or assume they want the "
    "full list, and never fabricate the remaining entities yourself. "
    "If result.period.is_partial=true, its `note` explains that the requested period isn't fully covered (either "
    "it hasn't finished yet, e.g. 'this_year' when only half the year has happened, or the data doesn't go back "
    "far enough) -- ALWAYS relay this honestly in the narration (e.g. 'yeh sirf ab tak ka data hai, poora saal "
    "nahi'). Never state a partial-period figure as if it were the complete period. "
    "After calling this, narrate the answer using ONLY the numbers returned in "
    "`result` -- do not introduce, round unusually, or infer any additional figures. "
    "Every rupee amount that has a lakh/crore-scale sibling field named '<field>_inr_words' (e.g. "
    "result.value_inr_words = '5.5 crore rupees') MUST be spoken using that EXACT phrase -- never convert the raw "
    "number to lakh/crore yourself. Manual lakh-vs-crore conversion is exactly the kind of mental arithmetic that's "
    "easy to get wrong (e.g. stating a real 5.5 crore figure as '55 lakh', which is actually 10x smaller) -- the "
    "correct phrase is already computed for you, just read it back."
)

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "original_question": {"type": "string", "description": "The caller's question, verbatim or close to it."},
        "metric_category": {"type": "string", "enum": sorted(METRIC_CATEGORIES.keys())},
        "sub_metric": {"type": "string", "description": "Must be valid for the chosen metric_category -- see the tool description's mapping."},
        "period": {
            "type": "string",
            "enum": ["this_month", "last_month", "this_quarter", "last_quarter", "this_year",
                     "last_year", "last_30_days", "last_90_days", "all_time", "custom"],
            "description": "Resolved relative to the dataset's own last simulated day, not real-world today.",
        },
        "date_from": {"type": "string", "description": "ISO YYYY-MM-DD, required only when period='custom'."},
        "date_to": {"type": "string", "description": "ISO YYYY-MM-DD, required only when period='custom'."},
        "compare_to_previous": {"type": "boolean", "description": "Whether the caller wants a comparison to the prior comparable period."},
        "entity_type": {"type": "string", "enum": ["customer", "product", "supplier", "category"]},
        "entity_name": {"type": "string", "description": "The caller's own words for the specific customer/product/supplier, if any. "
                         "'Partial' means you don't need the full catalog SKU string ('Wyvern Business Laptops WY733') -- it does NOT "
                         "mean shortening or dropping words the caller actually said. Keep every descriptive word (product type, "
                         "brand, model) exactly as spoken: for 'Wyvern laptop' pass entity_name='Wyvern laptop', never just 'Wyvern'. "
                         "Dropping a qualifying word turns a name that matches ONE entity into one that matches several unrelated "
                         "ones, forcing an unnecessary clarifying question the caller didn't need."},
        "entity_type_2": {"type": "string", "enum": ["customer", "product", "supplier", "category"],
                           "description": "Only for a 'compare X and Y' question -- the SECOND entity's type."},
        "entity_name_2": {"type": "string", "description": "Only for a 'compare X and Y' question -- the SECOND entity's name, "
                           "same rule as entity_name: keep every descriptive word the caller used, don't shorten to just the brand."},
        "top_n": {"type": "integer", "description": "How many items for a 'which products/customers...' style ranking question. Default 5."},
        "confirmed_full_list": {"type": "boolean", "description": "Set true ONLY when re-calling after the caller "
                                 "explicitly said yes to hearing the complete list (a previous result had "
                                 "more_available=true). Never set this on a first ask."},
        "filters": {
            "type": "object",
            "description": "Only for threshold/exception sub_metrics -- see the tool description for which key each "
                            "one reads (margin_threshold_pct, days_overdue_min, min_avg_delay_days). Omit for every "
                            "other sub_metric.",
            "properties": {
                "margin_threshold_pct": {"type": "number", "description": "For negative_margin_products. Percent, e.g. 0."},
                "days_overdue_min": {"type": "integer", "description": "For overdue_invoices. Days, e.g. 60."},
                "min_avg_delay_days": {"type": "number", "description": "For supplier_delivery_delay. Days, e.g. 10."},
            },
        },
    },
    "required": ["original_question", "metric_category", "sub_metric", "period"],
}


def _track(session_id: str, session, log_id: str) -> None:
    """Records that this ask happened during the CURRENT turn -- read (and
    cleared) by grounding.finalize_turn() once the turn's full narration is
    known, regardless of whether that's dev_llm_client.py's text path or
    bot.py's voice path."""
    update_session(session_id, pending_log_ids=session.pending_log_ids + [log_id])


def _format_inr_words(amount: float) -> str:
    """Renders a raw rupee amount as the exact Indian lakh/crore phrase --
    computed here in code, not left to the LLM's own mental arithmetic.
    That arithmetic has repeatedly and consistently mis-converted
    crore-scale figures down to a 10x-too-small lakh figure when speaking
    the amount out loud (e.g. a real Rs 5.5 crore value stated as "55
    lakh", which is actually what Rs 55 lakh would be -- 10x smaller). The
    narration is instructed (see TOOL_DESCRIPTION) to read this string
    verbatim instead of converting the raw number itself."""
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    if a >= 1_00_00_000:
        n, unit = a / 1_00_00_000, "crore"
    elif a >= 1_00_000:
        n, unit = a / 1_00_000, "lakh"
    else:
        return f"{sign}{a:,.2f} rupees"
    n_str = f"{n:.2f}".rstrip("0").rstrip(".")
    return f"{sign}{n_str} {unit} rupees"


def _annotate_currency(obj, metric: str | None = None):
    """Recursively walks a result dict/list and, for every numeric field
    that's unambiguously (or, for "value", contextually via the enclosing
    dict's own "metric") a rupee amount, adds a sibling "<field>_inr_words"
    string alongside it -- see _format_inr_words for why."""
    if isinstance(obj, dict):
        inner_metric = obj.get("metric", metric)
        out = {}
        for k, v in obj.items():
            out[k] = _annotate_currency(v, inner_metric)
            is_currency = (
                isinstance(v, (int, float)) and not isinstance(v, bool) and v is not None
                and (k in _CURRENCY_FIELD_NAMES or (k == "value" and inner_metric in _CURRENCY_VALUE_METRICS))
            )
            if is_currency:
                out[f"{k}_inr_words"] = _format_inr_words(v)
        return out
    if isinstance(obj, list):
        return [_annotate_currency(x, metric) for x in obj]
    return obj


def _apply_display_truncation(result: dict, intent) -> dict:
    """Never let more than 5 ranked entities reach the actor (and thus the
    caller) undisclosed. session.query_cache always keeps the FULL result
    (see handle()); this only shapes what's actually returned/narrated/logged
    for THIS call, so a later confirmed_full_list=true re-ask can still see
    everything, and the judge evaluates against exactly what was said, not
    the hidden rest."""
    if intent.sub_metric in FULL_SERIES_SUB_METRICS:
        return result
    items = result.get("items")
    if not isinstance(items, list):
        return result
    # Base this on total_count, not len(items) -- a compute function may
    # already have limited items to top_n (e.g. the actor's own default
    # top_n=5) while total_count reveals a larger population existed, and
    # that's exactly the case that must still be disclosed (this is what
    # supplier_delivery_delay's original bug looked like: 5 items shown,
    # total_count=9, and nothing said "there are 9 total").
    total = result.get("total_count", len(items))
    if total <= MAX_NARRATED_ITEMS:
        return result
    return {
        **result,
        "items": items[:MAX_NARRATED_ITEMS],
        "shown_count": min(MAX_NARRATED_ITEMS, len(items)),
        "total_count": total,
        "more_available": True,
    }


async def handle(session_id: str, arguments: dict) -> dict:
    start_t = time.perf_counter()
    session = get_session(session_id)
    question_text = arguments.get("original_question", "")
    confirmed_full_list = bool(arguments.get("confirmed_full_list", False))

    try:
        intent = validate_intent(arguments)
    except IntentValidationError as e:
        entry = append(QueryLogEntry(
            session_id=session_id, question_text=question_text, resolved_intent=arguments,
            status=STATUS_UNFULFILLED, error=str(e),
            latency_ms=round((time.perf_counter() - start_t) * 1000, 2),
        ))
        _track(session_id, session, entry.log_id)
        return {"log_id": entry.log_id, "error": str(e)}

    # Internal-only override, applied AFTER normal validation so the actor's
    # own top_n is still bounded 1-50 as usual -- this bypasses that cap only
    # for the "caller already confirmed they want everything" case, and
    # changes cache_key() so it's a fresh, fully-populated dispatch.
    if confirmed_full_list:
        intent.top_n = FULL_LIST_TOP_N

    cache_key = intent.cache_key()
    cached = session.query_cache.get(cache_key)
    if cached is not None:
        display_result = _annotate_currency(
            cached.result if confirmed_full_list else _apply_display_truncation(cached.result, intent)
        )
        entry = append(QueryLogEntry(
            session_id=session_id, question_text=question_text, resolved_intent=vars(intent),
            cache_hit=True, trace=cached.trace, result=display_result, status=STATUS_ANSWERED,
            latency_ms=round((time.perf_counter() - start_t) * 1000, 2),
        ))
        _track(session_id, session, entry.log_id)
        return {"log_id": entry.log_id, "result": display_result, "cache_hit": True}

    try:
        result, trace = dispatch(intent)
    except EngineError as e:
        entry = append(QueryLogEntry(
            session_id=session_id, question_text=question_text, resolved_intent=vars(intent),
            status=STATUS_UNFULFILLED, error=str(e),
            latency_ms=round((time.perf_counter() - start_t) * 1000, 2),
        ))
        _track(session_id, session, entry.log_id)
        return {"log_id": entry.log_id, "error": str(e)}

    trace_dict = trace.to_dict()
    session.query_cache[cache_key] = CachedAnswer(result=result, trace=trace_dict)
    update_session(session_id, query_cache=session.query_cache)

    display_result = _annotate_currency(result if confirmed_full_list else _apply_display_truncation(result, intent))

    # The engine refused to silently guess among several plausible entity
    # matches (engine/formulas.py's resolve_entity) -- log this distinctly
    # from a normal answered ask so the actor's clarifying question isn't
    # mistaken for a grounded result, and the judge doesn't fail it for
    # being "incomplete" when asking back was the correct call.
    is_ambiguous = bool(display_result.get("ambiguous")) or bool((display_result.get("comparison_error") or {}).get("ambiguous"))
    status = STATUS_AMBIGUOUS_ENTITY if is_ambiguous else STATUS_ANSWERED

    entry = append(QueryLogEntry(
        session_id=session_id, question_text=question_text, resolved_intent=vars(intent),
        cache_hit=False, trace=trace_dict, result=display_result, status=status,
        latency_ms=round((time.perf_counter() - start_t) * 1000, 2),
    ))
    _track(session_id, session, entry.log_id)
    return {"log_id": entry.log_id, "result": display_result, "cache_hit": False}


def build_pipecat_tool(session_id: str):
    """Phase 2 adapter: wraps the same provider-agnostic `handle()` above as
    a Pipecat FunctionSchema, bound to one session_id -- same shape as
    kyc-voice-agent's tools/submit_user_details.py. Imported lazily so
    Phase 1's text-only dev_llm_client.py never needs pipecat-ai installed.
    """
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.services.llm_service import FunctionCallParams

    async def pipecat_handler(params: FunctionCallParams) -> None:
        result = await handle(session_id, params.arguments)
        await params.result_callback(result)

    return FunctionSchema(
        name=TOOL_NAME, description=TOOL_DESCRIPTION,
        properties=TOOL_PARAMETERS["properties"], required=TOOL_PARAMETERS["required"],
        handler=pipecat_handler,
    )
