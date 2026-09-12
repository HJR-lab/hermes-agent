"""Tests for hermes_cli/fallback_config.py — fallback entry API-key resolution and the allow_fallback pin."""

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_cli.fallback_config import (
    get_configured_fallback_chain, get_fallback_chain, resolve_entry_api_key,
)


class TestAllowFallbackPin:
    CHAIN = [{"provider": "openai-codex", "model": "gpt-5.6-sol"}]

    def test_pinned_profile_has_no_runtime_chain_but_keeps_its_entries(self):
        # Every runtime reader goes through get_fallback_chain, so it must come back empty; `hermes
        # fallback` reads the configured chain and writes it back, so that must not lose the entries.
        for off in (False, "false"):
            cfg = {"model": {"provider": "claude-code-cli", "allow_fallback": off}, "fallback_providers": self.CHAIN}
            assert get_fallback_chain(cfg) == []
            assert get_configured_fallback_chain(cfg) == self.CHAIN

    def test_profiles_without_the_pin_keep_their_chain(self):
        for model in ({}, {"allow_fallback": True}, "legacy-model-string"):
            cfg = {"model": model, "fallback_providers": self.CHAIN}
            assert get_fallback_chain(cfg) == get_configured_fallback_chain(cfg) == self.CHAIN


class TestResolveEntryApiKey:
    def test_inline_api_key_wins(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"provider": "custom", "api_key": "inline-key", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "inline-key"


    def test_no_key_fields_returns_none(self):
        assert resolve_entry_api_key({"provider": "openrouter", "model": "glm"}) is None


    def test_whitespace_inline_key_falls_through_to_env(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"api_key": "   ", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "env-key"

    def test_key_env_resolves_from_active_secret_scope_not_raw_env(self, monkeypatch):
        # Multiplexed gateway: os.environ holds another profile's key, but the
        # active per-turn secret scope holds this profile's key. The scoped
        # value must win — a raw os.getenv() would leak the other profile's
        # credential (issue #74311).
        monkeypatch.setenv("FB_KEY", "fake-other-profile-key")
        token = set_secret_scope({"FB_KEY": "fake-active-profile-key"})
        try:
            assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "fake-active-profile-key"
        finally:
            reset_secret_scope(token)

    def test_key_env_falls_back_to_env_when_no_active_scope(self, monkeypatch):
        # Non-multiplexed / single-profile behavior must be unchanged: with no
        # secret scope installed, resolution still reads os.environ.
        monkeypatch.setenv("FB_KEY", "env-key")
        assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "env-key"
