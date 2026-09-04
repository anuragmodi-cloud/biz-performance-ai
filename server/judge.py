"""Judge LLM: an independent, typically higher-capability model that audits
one ask's full evidence trail (question -> resolved intent -> engine trace ->
result -> final narration) after the fact, and returns a strict pass/fail
verdict naming the EARLIEST pipeline stage that actually went wrong.

Deliberately a separate model/provider from the actor LLM (dev_llm_client.py)
-- JUDGE_LLM_PROVIDER/JUDGE_LLM_MODEL are independent env vars (falling back
to the actor's LLM_PROVIDER if unset). This is what lets a cheap/fast model
run the live conversation while a stronger model (a bigger DeepSeek model,
or Claude via ANTHROPIC_API_KEY) does the after-the-fact grading, where
judgment quality matters more than latency.

Called from two places: dev_llm_client.py's fire-and-forget scheduling
(never blocks the user-facing /ask response) and admin.py's on-demand
trigger (POST /admin/query-log/{log_id}/judge).
"""
from __future__ import annotations

import json
import os

from query_log import QueryLogEntry

JUDGE_TOOL_NAME = "submit_verdict"
JUDGE_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "failed_stage": {
            "type": "string",
            "enum": ["intent_resolution", "computation", "narration", "none"],
            "description": "The EARLIEST pipeline stage that went wrong. 'none' only if verdict is 'pass'.",
        },
        "reason": {
            "type": "string",
            "description": "One or two specific sentences naming the actual mismatch -- never 'something seems off'.",
        },
        "downstream_impact": {
            "type": "string",
            "description": "If failed_stage is intent_resolution or computation, one sentence on what that made "
                            "incorrect/meaningless downstream. Empty string if not applicable or verdict is pass.",
        },
    },
    "required": ["verdict", "failed_stage", "reason", "downstream_impact"],
}

JUDGE_SYSTEM_PROMPT = """You are a strict, independent quality auditor reviewing one interaction between a \
user, a business-performance voice assistant (the "actor"), and its calculation engine.

You are the JUDGE model -- separate from and more capable than the actor model being audited. Your job is \
NOT to answer the user's question yourself. Your job is to determine whether the pipeline answered it \
correctly, and if not, pinpoint exactly where it first went wrong.

PIPELINE STAGES, IN ORDER:

1. intent_resolution -- the actor read the user's natural-language question and chose a metric_category \
+ sub_metric + period + entity from a FIXED catalog of calculation functions (it cannot invent a new one). \
A failure here means the WRONG function or parameters were chosen for what the user actually asked -- \
e.g. asking about product launches but the actor called "units_sold", asking about "this quarter" but a \
different period got resolved, or a fuzzy customer/supplier name match picked the wrong entity.

2. computation -- given the (possibly already-wrong) intent, a deterministic Python function computed a \
result; you can see its exact steps in engine_trace. Since this is fixed, reviewed code, a failure here is \
rare and means the trace's own arithmetic/logic looks wrong GIVEN its own resolved_intent -- not a \
mismatched intent, that is stage 1.

3. narration -- the actor turned engine_result into the user-facing final narration. A failure here means: \
it stated a number not present in engine_result/engine_trace, misrepresented what a number means, omitted \
something critical the question needed, or otherwise didn't accurately convey what the engine computed -- \
even though intent_resolution and computation were both correct.

ROOT-CAUSE RULE: failures cascade. If intent_resolution picked the wrong function, the computation and \
narration that follow are usually "internally correct but pointless" -- they correctly compute and clearly \
state the WRONG thing. In that case the root-cause failed_stage is intent_resolution, NOT narration, even \
though the visible symptom (a wrong-sounding answer) only becomes obvious in the narration. Always name \
the EARLIEST stage that actually went wrong, not just where the mistake becomes visible to the user.

You will receive a JSON evidence bundle: the user's question, the actor's resolved_intent, the engine's \
full trace (tables queried, named computations, and every load/filter/aggregate/formula step with its own \
result), the engine's final result payload, the actor's final narration, and a cheap automatic \
numeric-grounding check's verdict (it only checks whether narrated numbers trace back to the result -- it \
cannot judge whether the right question was even answered, that is your job).

Call submit_verdict. Be strict:
- "pass" only if the question was genuinely, correctly, and completely answered.
- A technically-not-wrong but evasive non-answer is still a FAIL if the question was answerable from the \
data available.
- A hedged, uncertain narration for a genuinely ambiguous/diagnostic question is FINE if it's honestly \
hedged and every number in it is grounded -- do not fail correct epistemic humility.
- If automatic_numeric_grounding_check.status is "ambiguous_entity", the engine found several plausible \
entity matches (e.g. two customers/products/suppliers whose names both matched what the caller said) and \
refused to silently guess -- check engine_result.candidates against the caller's actual wording: if the \
candidates are genuinely similar/ambiguous and the actor asked a clarifying question instead of picking one, \
that is CORRECT behavior and should PASS. Only fail it if one candidate was clearly, unambiguously what the \
caller meant and asking back was unnecessary pedantry.
- reason must name the actual mismatch specifically (which metric was expected vs. chosen, which number \
was wrong and by how much, what was omitted) -- generic reasons are not acceptable.
"""


def _build_evidence(entry: QueryLogEntry) -> dict:
    return {
        "user_question": entry.question_text,
        "actor_resolved_intent": entry.resolved_intent,
        "cache_hit": entry.cache_hit,
        "engine_trace": entry.trace,
        "engine_result": entry.result,
        "actor_final_narration": entry.narrated_text,
        "automatic_numeric_grounding_check": {"status": entry.status, "error": entry.error},
    }


async def judge_entry(entry: QueryLogEntry) -> dict:
    provider = (os.getenv("JUDGE_LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or "deepseek").lower()
    evidence = _build_evidence(entry)
    user_content = "Evidence bundle for the ask you are auditing:\n\n" + json.dumps(evidence, indent=2, default=str)

    if provider in ("deepseek", "openai"):
        return await _judge_openai_compatible(provider, user_content)
    if provider == "anthropic":
        return await _judge_anthropic(user_content)
    raise ValueError(f"Unknown JUDGE_LLM_PROVIDER: {provider}")


def _tool_schema_openai() -> dict:
    return {
        "type": "function",
        "function": {"name": JUDGE_TOOL_NAME, "description": "Submit your audit verdict.",
                     "parameters": JUDGE_TOOL_PARAMETERS},
    }


async def _judge_openai_compatible(provider: str, user_content: str) -> dict:
    from openai import AsyncOpenAI

    if provider == "deepseek":
        client_kwargs = {
            "api_key": os.getenv("JUDGE_DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY"),
            "base_url": os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
        }
        gateway_provider_id = os.getenv("DEEPSEEK_GATEWAY_PROVIDER_ID")
        if gateway_provider_id:
            client_kwargs["default_headers"] = {"X-CostGuard-Provider": gateway_provider_id}
        client = AsyncOpenAI(**client_kwargs)
        # deepseek-reasoner is the stronger/slower sibling of deepseek-chat --
        # a sensible default judge when the actor is running deepseek-chat.
        model = os.getenv("JUDGE_LLM_MODEL") or "deepseek-reasoner"
    else:
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        model = os.getenv("JUDGE_LLM_MODEL") or "gpt-4o"

    messages = [{"role": "system", "content": JUDGE_SYSTEM_PROMPT}, {"role": "user", "content": user_content}]
    # Reasoning models (deepseek-reasoner, and generally "thinking mode"
    # models across providers) commonly reject a FORCED tool_choice --
    # start with auto, which every model here supports, and fall back to
    # forced only if the model didn't call the tool on its own (auto is
    # usually reliable for a single, clearly-instructed tool, but forced is
    # a stronger guarantee when the provider allows it).
    resp = await client.chat.completions.create(
        model=model, messages=messages, tools=[_tool_schema_openai()], tool_choice="auto",
    )
    msg = resp.choices[0].message
    if not msg.tool_calls:
        try:
            resp = await client.chat.completions.create(
                model=model, messages=messages, tools=[_tool_schema_openai()],
                tool_choice={"type": "function", "function": {"name": JUDGE_TOOL_NAME}},
            )
            msg = resp.choices[0].message
        except Exception:
            pass  # provider/model doesn't support forced tool_choice either -- fall through to the error below
    if not msg.tool_calls:
        raise RuntimeError(
            f"Judge model {provider}:{model} did not call {JUDGE_TOOL_NAME} "
            f"(replied with plain text instead): {(msg.content or '')[:300]!r}"
        )
    tc = msg.tool_calls[0]
    verdict = json.loads(tc.function.arguments)
    verdict["judge_model"] = f"{provider}:{model}"
    return verdict


async def _judge_anthropic(user_content: str) -> dict:
    from anthropic import AsyncAnthropic

    api_key = os.getenv("JUDGE_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    # Same CostGuard-gateway auth fix as dev_llm_client.py's _run_anthropic:
    # the gateway only reads Authorization: Bearer, not the SDK's default
    # x-api-key header, when ANTHROPIC_API_KEY is a cgmk_... monitoring key.
    client = AsyncAnthropic(api_key=api_key, default_headers={"Authorization": f"Bearer {api_key}"})
    model = os.getenv("JUDGE_LLM_MODEL") or "claude-opus-5"
    tool_schema = {"name": JUDGE_TOOL_NAME, "description": "Submit your audit verdict.",
                   "input_schema": JUDGE_TOOL_PARAMETERS}

    resp = await client.messages.create(
        model=model, system=JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        tools=[tool_schema], tool_choice={"type": "tool", "name": JUDGE_TOOL_NAME},
        max_tokens=1024,
    )
    tool_use = next(b for b in resp.content if b.type == "tool_use")
    verdict = dict(tool_use.input)
    verdict["judge_model"] = f"anthropic:{model}"
    return verdict
