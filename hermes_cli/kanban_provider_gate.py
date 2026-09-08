"""Provider-level dispatch gate: never hand a card to a walled provider.

On 2026-09-08 Codex's 5-hour window hit 100% and the dispatcher spent the next
3.5 hours spawning workers that could only ever receive a 429. Each one exited
rc=0 having made zero tool calls, which the crash sweep reads as a protocol
violation, so 28 real business cards ended up ``blocked`` for a reason that was
false.

The per-card breaker caps that at ~3 spawns per card. This module removes the
spawns entirely: the information was already on disk in
``state/quota_snapshot.json`` (written by ``scripts/quota_monitor.py``), it was
simply never consulted before spawning.

**Fail open.** Every unknown -- missing snapshot, stale snapshot, unparseable
JSON, unreadable profile config, a provider the monitor does not track --
resolves to "not walled" and the card dispatches. A broken quota monitor must
never be able to freeze the board; the cost of a wrong "walled" is a stalled
board, the cost of a wrong "healthy" is one doomed spawn.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# A reading older than this is treated as UNKNOWN, never as a wall. Codex only
# writes quota data as a side effect of a CLI turn, so an idle or exhausted
# Codex reports its last known state indefinitely -- without this bound, one
# exhausted reading would wedge the board shut forever.
SNAPSHOT_MAX_AGE_SECONDS = 900.0


@dataclass(frozen=True)
class ProviderWall:
    """The wall that is holding a card back, for the board-level notice."""

    provider: str
    resets_at: Optional[float]


@dataclass(frozen=True)
class _ProviderState:
    walled: bool
    resets_at: Optional[float]


def _default_root() -> Optional[Path]:
    """The DEFAULT Hermes root -- not ``get_hermes_home()``, which under
    ``hermes -p <name>`` is ``profiles/<name>`` and would look for the shared
    snapshot inside a single profile."""
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:
        return None


def provider_chain(profile: str) -> list[str]:
    """``model.provider`` followed by each ``fallback_providers[].provider``.

    Empty when the profile has no readable config -- an unresolvable chain is an
    unknown, and unknowns fail open.
    """
    root = _default_root()
    if root is None or not profile:
        return []
    try:
        import yaml

        data = yaml.safe_load((root / "profiles" / profile / "config.yaml").read_text())
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    chain: list[str] = []
    model = data.get("model")
    if isinstance(model, dict):
        primary = str(model.get("provider") or "").strip()
        if primary:
            chain.append(primary)
    fallbacks = data.get("fallback_providers")
    if isinstance(fallbacks, list):
        for entry in fallbacks:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("provider") or "").strip()
            if name and name not in chain:
                chain.append(name)
    return chain


def _entry_state(entry: dict) -> _ProviderState:
    """One snapshot provider block -> walled or not.

    Gates on ``status`` and ``rate_limits.primary/secondary.used_percent``.
    Deliberately NOT on ``hermes_burn`` (which sums lifetime per-session totals
    and over-reports by orders of magnitude) nor on
    ``credentials[].error_reset_at`` (a different, longer counter).
    """
    resets_at: Optional[float] = None
    walled = str(entry.get("status") or "").strip().lower() == "exhausted"
    # A provider with no usable credentials cannot absorb work no matter what
    # its plan balance says. On 2026-09-08 the alibaba fallback reported plan
    # status 'ok' at 78% used while its probe said "DASHSCOPE_API_KEY not
    # present" -- treating that as a healthy link would have let the whole deus
    # queue flood into an unreachable fallback. Only an EXPLICIT ok=False
    # counts; a missing probe block stays an unknown and fails open.
    probe = entry.get("probe")
    if isinstance(probe, dict) and probe.get("ok") is False:
        walled = True
    limits = entry.get("rate_limits")
    if isinstance(limits, dict):
        for key in ("primary", "secondary"):
            window = limits.get(key)
            if not isinstance(window, dict):
                continue
            try:
                used = float(window.get("used_percent"))
            except (TypeError, ValueError):
                used = None
            if used is not None and used >= 100.0:
                walled = True
                try:
                    resets_at = float(window.get("resets_at"))
                except (TypeError, ValueError):
                    resets_at = resets_at
                break
        if resets_at is None:
            primary = limits.get("primary")
            if isinstance(primary, dict):
                try:
                    resets_at = float(primary.get("resets_at"))
                except (TypeError, ValueError):
                    resets_at = None
    return _ProviderState(walled=walled, resets_at=resets_at)


def provider_states(*, now: Optional[float] = None) -> Optional[dict[str, _ProviderState]]:
    """Provider name -> state from the quota snapshot.

    ``None`` means "cannot tell" (missing / stale / unparseable) and every
    caller must treat that as not-walled.
    """
    root = _default_root()
    if root is None:
        return None
    try:
        raw = json.loads((root / "state" / "quota_snapshot.json").read_text())
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        age = (time.time() if now is None else now) - float(raw.get("generated_at"))
    except (TypeError, ValueError):
        return None
    # Negative age = clock skew; also untrustworthy.
    if abs(age) > SNAPSHOT_MAX_AGE_SECONDS:
        return None
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return None
    states: dict[str, _ProviderState] = {}
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("provider") or "").strip()
        if name:
            states[name] = _entry_state(entry)
    return states or None


def walled_provider_for(profile: str, *, now: Optional[float] = None) -> Optional[ProviderWall]:
    """The wall holding ``profile`` back, or ``None`` when it can still work.

    Returns a wall ONLY when every provider in the profile's chain is known to
    be walled. One healthy or unknown link is enough to dispatch.
    """
    chain = provider_chain(profile)
    if not chain:
        return None
    states = provider_states(now=now)
    if states is None:
        return None
    for name in chain:
        state = states.get(name)
        if state is None or not state.walled:
            # Untracked provider or a healthy link -> the profile can work.
            return None
    primary = states[chain[0]]
    return ProviderWall(provider=chain[0], resets_at=primary.resets_at)
