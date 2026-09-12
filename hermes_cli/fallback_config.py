"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

from typing import Any


def _normalized_base_url(value: Any) -> str:
    return value.strip().rstrip("/") if isinstance(value, str) else ""


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``api_key_env`` accepted as alias); None when neither
    yields a value so ``resolve_runtime_provider`` falls through to standard credential resolution.
    ``key_env`` goes through ``agent.secret_scope.get_secret``, not raw ``os.getenv``: in a
    multiplexed gateway a bare env read ignores the active profile's scope and can return another
    profile's credential.
    """
    if not isinstance(entry, dict):
        return None
    if inline := str(entry.get("api_key") or "").strip():
        return inline
    if key_env := str(entry.get("key_env") or entry.get("api_key_env") or "").strip():
        from agent.secret_scope import get_secret
        return (get_secret(key_env) or "").strip() or None
    return None


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    candidates = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue
        normalized = {**entry, "provider": provider, "model": model}
        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url
        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def fallback_allowed(config: dict[str, Any] | None) -> bool:
    """False when ``model.allow_fallback`` is explicitly off, pinning the profile to its primary route.

    A pinned profile stops on a provider outage instead of silently serving (and billing) another
    provider — a rate-limited Claude profile once ran two days of kanban work on Codex this way.
    Absent means allowed, so only profiles that opt in change behaviour.
    """
    from utils import is_truthy_value

    model = (config or {}).get("model")
    return not isinstance(model, dict) or is_truthy_value(model.get("allow_fallback"), default=True)


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the fallback chain the runtime may use: the configured chain, or ``[]`` when
    ``model.allow_fallback`` pins the profile to its primary route."""
    return get_configured_fallback_chain(config) if fallback_allowed(config) else []


def get_configured_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the fallback chain as written, merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its order. Legacy
    ``fallback_model`` entries are appended afterwards unless they target the same
    provider/model/base_url route as an earlier entry. The returned list always contains fresh dict
    copies. Ignores ``model.allow_fallback``: this is for editing the chain, not for running it.
    """
    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity not in seen:
                seen.add(identity)
                chain.append(entry)
    return chain
