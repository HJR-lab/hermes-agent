"""Regression coverage for startup direct-alias routing."""

from hermes_cli import model_switch


def test_local_alias_route_is_atomic_before_client_creation(monkeypatch):
    monkeypatch.setattr(
        model_switch,
        "DIRECT_ALIASES",
        {
            "qwen-local": model_switch.DirectAlias(
                "qwen3.5:9b",
                "ollama",
                "http://127.0.0.1:11434/v1",
                api_mode="chat_completions",
                extra_body={"think": False},
            )
        },
    )
    route = model_switch.resolve_startup_model_route(
        "qwen-local", current_provider="openai-codex"
    )
    assert route.model == "qwen3.5:9b"
    # URL-bearing aliases are host-owned and must not retain the Codex label.
    assert route.provider == "custom"
    assert route.base_url == "http://127.0.0.1:11434/v1"
    assert route.api_mode == "chat_completions"
    assert route.request_overrides == {"think": False}


def test_alias_optional_fields_inherit_safely(monkeypatch):
    monkeypatch.setattr(
        model_switch,
        "DIRECT_ALIASES",
        {"local": model_switch.DirectAlias("qwen3.5:9b", "ollama", "")},
    )
    route = model_switch.resolve_startup_model_route("local")
    assert route == model_switch.StartupModelRoute("qwen3.5:9b", "ollama", "")
    assert route.api_mode == ""
    assert route.request_overrides is None


def test_alias_loader_preserves_api_mode_and_extra_body(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model_aliases": {
                "local": {
                    "model": "qwen3.5:9b",
                    "provider": "ollama",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "api_mode": "chat_completions",
                    "extra_body": {"think": False},
                }
            }
        },
    )
    alias = model_switch._load_direct_aliases()["local"]
    assert alias.api_mode == "chat_completions"
    assert alias.extra_body == {"think": False}


def test_ordinary_provider_model_override_remains_unchanged(monkeypatch):
    monkeypatch.setattr(model_switch, "DIRECT_ALIASES", {})
    route = model_switch.resolve_startup_model_route(
        "ollama/qwen3.5:9b",
        current_provider="openai-codex",
        user_providers={"ollama": {"base_url": "http://127.0.0.1:11434/v1"}},
    )
    assert route == model_switch.StartupModelRoute("qwen3.5:9b", "ollama", "")
