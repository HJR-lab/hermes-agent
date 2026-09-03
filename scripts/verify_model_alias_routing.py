#!/usr/bin/env python3
"""Independent executable acceptance check for direct alias routing.

This intentionally does not use pytest: it records every runtime-resolution
call and fails if the alias asks for the configured Codex primary or omits its
local endpoint.
"""

from hermes_cli import model_switch
from hermes_cli import runtime_provider


calls = []
model_switch.DIRECT_ALIASES.clear()
model_switch.DIRECT_ALIASES["qwen-local"] = model_switch.DirectAlias(
    "qwen3.5:9b",
    "ollama",
    "http://127.0.0.1:11434/v1",
    api_mode="chat_completions",
    extra_body={"think": False},
)

route = model_switch.resolve_startup_model_route(
    "qwen-local", current_provider="openai-codex"
)
assert route.provider == "custom", route
assert route.model == "qwen3.5:9b", route
assert route.base_url == "http://127.0.0.1:11434/v1", route
assert route.api_mode == "chat_completions", route
assert route.request_overrides == {"think": False}, route

original = runtime_provider.resolve_runtime_provider


def record(**kwargs):
    calls.append(kwargs)
    assert kwargs.get("requested") != "openai-codex", kwargs
    assert kwargs.get("explicit_base_url") == route.base_url, kwargs
    return {
        "provider": "custom",
        "model": route.model,
        "base_url": route.base_url,
        "api_mode": route.api_mode,
        "api_key": "no-key-required",
    }


runtime_provider.resolve_runtime_provider = record
try:
    runtime_provider.resolve_runtime_provider(
        requested=route.provider,
        target_model=route.model,
        explicit_base_url=route.base_url,
    )
finally:
    runtime_provider.resolve_runtime_provider = original

assert len(calls) == 1, calls
print("PASS direct alias route:", calls[0])
