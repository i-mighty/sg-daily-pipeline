"""
LLM provider abstraction.

Supported providers (set via the LLM_PROVIDER env var):
  openai    — OpenAI API                      (default)
  anthropic — direct Anthropic API
  bedrock   — AWS Bedrock (Anthropic models, same SDK, different client class)

Model "tiers" decouple call sites from concrete model IDs:
  quality — main analysis / writing
  fast    — discovery / cheap high-volume work
  batch   — bulk / lowest-cost work

For OpenAI the concrete model per tier is read from env vars
(OPENAI_QUALITY_MODEL / OPENAI_FAST_MODEL / OPENAI_BATCH_MODEL) with sane defaults.

Public API:
    from providers import resolve_model, run_agentic_loop, complete

    model = resolve_model("quality")                 # provider-correct model ID
    text  = await run_agentic_loop(prompt, TOOLS, execute_tool, tier="quality")
    text  = await complete(user, system=..., tier="fast")   # one call, no tools

Tools are passed in a provider-neutral shape:
    {"name": str, "description": str, "parameters": <JSON schema dict>}
and `execute_tool(name, inputs_dict) -> str` runs the tool.
"""

import json
import os

# ── Model tiers ───────────────────────────────────────────────────────────────

# OpenAI: (env var, default model). Balanced quality/cost posture:
#   quality — gpt-4.1      : live research (agentic) + outreach (email writing)
#   fast    — gpt-4.1-mini : discovery loop, live contact + score stages
#   batch   — gpt-4o-mini  : the small batch rounds (contact/score/outreach)
# The batch *research* round uses OPENAI_BATCH_QUALITY_MODEL (default gpt-4.1) —
# read directly in scripts/batch_submit.py, since the Batch API has its own model allowlist.
_OPENAI_TIERS: dict[str, tuple[str, str]] = {
    "quality": ("OPENAI_QUALITY_MODEL", "gpt-4.1"),
    "fast":    ("OPENAI_FAST_MODEL",    "gpt-4.1-mini"),
    "batch":   ("OPENAI_BATCH_MODEL",   "gpt-4o-mini"),
}

_ANTHROPIC_TIERS: dict[str, str] = {
    "quality": "claude-sonnet-4-6",
    "fast":    "claude-haiku-4-5-20251001",
    "batch":   "claude-haiku-4-5-20251001",
}

_BEDROCK_TIERS: dict[str, str] = {
    "quality": "us.anthropic.claude-sonnet-4-6-20250514-v1:0",
    "fast":    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "batch":   "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}


def _default_provider() -> str:
    return os.environ.get("LLM_PROVIDER", "openai").lower()


def resolve_model(tier: str, provider: str | None = None) -> str:
    """Translate a tier (quality/fast/batch) into the provider-specific model ID."""
    p = (provider or _default_provider()).lower()
    if p == "openai":
        env, default = _OPENAI_TIERS.get(tier, _OPENAI_TIERS["quality"])
        return os.environ.get(env) or default
    if p == "bedrock":
        return _BEDROCK_TIERS.get(tier, _BEDROCK_TIERS["quality"])
    return _ANTHROPIC_TIERS.get(tier, _ANTHROPIC_TIERS["quality"])


# ── Clients ───────────────────────────────────────────────────────────────────

def get_async_client(provider: str | None = None):
    """Return an async client for the given provider. SDKs are imported lazily."""
    p = (provider or _default_provider()).lower()
    if p == "openai":
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    import anthropic
    if p == "bedrock":
        return anthropic.AsyncAnthropicBedrock(
            aws_access_key=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            aws_region=os.environ.get("AWS_BEDROCK_REGION", "us-east-1"),
        )
    return anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def require_api_key(provider: str | None = None) -> None:
    """Raise a clear error if the active provider's API key is missing."""
    p = (provider or _default_provider()).lower()
    key = {"openai": "OPENAI_API_KEY", "bedrock": "AWS_ACCESS_KEY_ID"}.get(p, "ANTHROPIC_API_KEY")
    if not os.environ.get(key):
        raise SystemExit(f"ERROR: {key} is not set (LLM_PROVIDER={p}).")


# ── Single-shot completion (no tools) ─────────────────────────────────────────

async def complete(
    user: str,
    *,
    system: str | None = None,
    tier: str = "fast",
    max_tokens: int = 4000,
    provider: str | None = None,
) -> str:
    """One LLM call with an optional system prompt; returns the assistant text.

    Used by the staged analysis pipeline for the no-tool stages (contact, score,
    outreach). For tool-using stages (research/discovery) use run_agentic_loop.
    """
    p = (provider or _default_provider()).lower()
    client = get_async_client(p)
    model = resolve_model(tier, p)
    if p == "openai":
        messages: list = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        resp = await client.chat.completions.create(
            model=model, max_tokens=max_tokens, messages=messages,
        )
        return resp.choices[0].message.content or ""
    kwargs = {"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": user}]}
    if system:
        kwargs["system"] = system
    resp = await client.messages.create(**kwargs)
    return "".join(b.text for b in resp.content if hasattr(b, "text"))


# ── Unified agentic loop ──────────────────────────────────────────────────────

DEFAULT_FINISH = "You've done enough research. Now produce your final output using the data gathered."


async def run_agentic_loop(
    prompt: str,
    tools: list[dict],
    execute_tool,
    *,
    tier: str = "quality",
    max_tokens: int = 8000,
    max_tool_calls: int = 20,
    provider: str | None = None,
    finish_instruction: str = DEFAULT_FINISH,
    on_tool=None,
    system: str | None = None,
) -> str:
    """Run a multi-turn tool-use conversation until the model stops calling tools
    or hits `max_tool_calls`. Returns the concatenated assistant text.

    `tools`        — provider-neutral list of {name, description, parameters}.
    `execute_tool` — callable(name, inputs_dict) -> str.
    `on_tool`      — optional callable(name, inputs_dict, call_number) for logging.
    `system`       — optional system prompt applied to every turn.
    """
    p = (provider or _default_provider()).lower()
    client = get_async_client(p)
    model = resolve_model(tier, p)
    if p == "openai":
        return await _openai_loop(client, model, prompt, tools, execute_tool,
                                  max_tokens, max_tool_calls, finish_instruction, on_tool, system)
    return await _anthropic_loop(client, model, prompt, tools, execute_tool,
                                 max_tokens, max_tool_calls, finish_instruction, on_tool, system)


async def _anthropic_loop(client, model, prompt, tools, execute_tool,
                          max_tokens, max_tool_calls, finish_instruction, on_tool,
                          system=None) -> str:
    anth_tools = [
        {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
        for t in tools
    ]
    messages: list = [{"role": "user", "content": prompt}]
    full_text = ""
    n = 0
    _sys = {"system": system} if system else {}

    while True:
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens, tools=anth_tools, messages=messages, **_sys,
        )
        for block in resp.content:
            if hasattr(block, "text"):
                full_text += block.text

        if resp.stop_reason != "tool_use":
            break

        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                n += 1
                if on_tool:
                    on_tool(block.name, block.input, n)
                result = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id, "content": result,
                })

        messages.append({"role": "assistant", "content": resp.content})

        if n >= max_tool_calls:
            messages.append({"role": "user", "content": tool_results + [
                {"type": "text", "text": finish_instruction},
            ]})
            final = await client.messages.create(
                model=model, max_tokens=max_tokens, messages=messages, **_sys,
            )
            for block in final.content:
                if hasattr(block, "text"):
                    full_text += block.text
            break

        messages.append({"role": "user", "content": tool_results})

    return full_text


async def _openai_loop(client, model, prompt, tools, execute_tool,
                       max_tokens, max_tool_calls, finish_instruction, on_tool,
                       system=None) -> str:
    oa_tools = [
        {"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["parameters"],
        }}
        for t in tools
    ]
    messages: list = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    full_text = ""
    n = 0

    while True:
        resp = await client.chat.completions.create(
            model=model, max_tokens=max_tokens, tools=oa_tools, messages=messages,
        )
        msg = resp.choices[0].message
        if msg.content:
            full_text += msg.content

        if not msg.tool_calls:
            break

        # Echo the assistant turn (with its tool calls) back into the history.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        # Every tool call must be answered with a tool message before anything else.
        for tc in msg.tool_calls:
            n += 1
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if on_tool:
                on_tool(tc.function.name, args, n)
            result = execute_tool(tc.function.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

        if n >= max_tool_calls:
            messages.append({"role": "user", "content": finish_instruction})
            final = await client.chat.completions.create(
                model=model, max_tokens=max_tokens, messages=messages,
            )
            if final.choices[0].message.content:
                full_text += final.choices[0].message.content
            break

    return full_text
