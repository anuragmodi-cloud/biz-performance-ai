# llm_factory.py
"""Pipecat LLM service factory -- copied from kyc-voice-agent's
llm_factory.py near-verbatim, for Phase 2's bot.py (the real voice pipeline).

Not used by Phase 1's dev_llm_client.py (which talks to the same providers'
raw SDKs directly, since there's no Pipecat pipeline to plug an LLMService
into for a plain-text dev endpoint) -- kept here now so Phase 2 only has to
wire this into bot.py, not write it from scratch.
"""
import os


def build_llm():
    provider = os.getenv("LLM_PROVIDER", "deepseek").lower()

    if provider == "deepseek":
        from pipecat.services.deepseek.llm import DeepSeekLLMService
        kwargs = {}
        base_url = os.getenv("DEEPSEEK_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        gateway_provider_id = os.getenv("DEEPSEEK_GATEWAY_PROVIDER_ID")
        if gateway_provider_id:
            kwargs["default_headers"] = {"X-CostGuard-Provider": gateway_provider_id}
        return DeepSeekLLMService(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            model=os.getenv("LLM_MODEL") or "deepseek-chat",
            **kwargs,
        )
    if provider == "anthropic":
        from anthropic import AsyncAnthropic
        from pipecat.services.anthropic.llm import AnthropicLLMService

        api_key = os.getenv("ANTHROPIC_API_KEY")
        # CostGuard's gateway (when ANTHROPIC_API_KEY holds a cgmk_...
        # monitoring key, routed via ANTHROPIC_BASE_URL) only reads
        # `Authorization: Bearer <key>`, not the anthropic SDK's default
        # `x-api-key` header -- same fix as dev_llm_client.py's
        # _run_anthropic. AnthropicLLMService accepts a pre-built client.
        client = AsyncAnthropic(api_key=api_key, default_headers={"Authorization": f"Bearer {api_key}"})
        return AnthropicLLMService(
            api_key=api_key,
            model=os.getenv("LLM_MODEL") or "claude-sonnet-5",
            client=client,
        )
    if provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService
        return OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model=os.getenv("LLM_MODEL") or "gpt-4o",
        )
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}")
