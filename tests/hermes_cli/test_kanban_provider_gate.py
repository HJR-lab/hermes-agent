"""Regression tests for the 2026-09-08 Codex quota-wall dispatch flood.

Codex's 5-hour window hit 100% at ~09:55. For the next 3.5 hours the dispatcher
kept spawning workers into a provider that was already answering 429. Each
worker made ZERO tool calls, printed three rate-limit errors and exited rc=0 --
which ``_classify_worker_exit`` reads as ``clean_exit`` and therefore books as a
protocol violation. Trailing 5h on the live board: 149 runs, 128 crashed, 135
with the identical "worker exited cleanly (rc=0)" error, and 28 real business
cards left ``blocked`` for a reason that was false.

The breaker fixed in ``test_kanban_decompose_flood`` caps the damage at ~3
spawns per card. It does not prevent the outage fanning across the board: with
~20 cards queued that is still ~60 doomed spawns per outage.

These tests pin the layer that makes it ZERO: before spawning, the dispatcher
resolves the assignee profile's provider chain and, when EVERY provider in that
chain is walled, holds the card without spawning and without counting a
failure.

The load-bearing constraint is FAIL-OPEN. A missing, stale or unparseable
``quota_snapshot.json`` must never freeze the board -- a broken monitor is not
a reason to stop working.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture(autouse=True)
def _assignees_are_spawnable(monkeypatch):
    """Isolate these tests from the profile-exists guard.

    ``test_unreadable_profile_config_fails_open`` deliberately uses an assignee
    with no profile directory; without this the profile-exists guard would
    short-circuit before the provider gate is ever consulted, and the test
    would pass for the wrong reason.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write_profile(home: Path, name: str, provider: str, fallbacks=()) -> None:
    """Materialize ``profiles/<name>/config.yaml`` the way Hermes writes it."""
    pdir = home / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    lines = ["model:", "  model: gpt-5.6-sol", f"  provider: {provider}"]
    if fallbacks:
        lines.append("fallback_providers:")
        for fb in fallbacks:
            lines.append(f"  - provider: {fb}")
            lines.append("    model: some-model")
    (pdir / "config.yaml").write_text("\n".join(lines) + "\n")


def _write_snapshot(home: Path, providers: dict, *, age_seconds: float = 0.0) -> None:
    """Write ``state/quota_snapshot.json`` in quota_monitor's schema.

    ``providers`` maps snapshot key -> (provider_name, used_percent, status).
    """
    now = time.time()
    body = {"schema_version": 2, "generated_at": now - age_seconds, "providers": {}}
    for key, (provider, used_percent, status) in providers.items():
        body["providers"][key] = {
            "provider": provider,
            "status": status,
            "rate_limits": {
                "primary": {
                    "used_percent": used_percent,
                    "window_minutes": 300.0,
                    "resets_at": now + 3600,
                },
                "secondary": {"used_percent": 16, "window_minutes": 10080.0},
            },
        }
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "quota_snapshot.json").write_text(json.dumps(body))


def _spawn_recorder():
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    return spawns, fake_spawn


def _failures(conn, task_id: str) -> int:
    row = conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# The wall itself
# ---------------------------------------------------------------------------


def test_fully_walled_chain_does_not_spawn(kanban_home):
    """The live incident: deus is openai-codex, Codex is at 100%, and the card
    must not be handed to a worker that can only 429."""
    _write_profile(kanban_home, "deus", "openai-codex")
    _write_snapshot(kanban_home, {"codex": ("openai-codex", 100, "exhausted")})
    spawns, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="DOE inventory reconcile v2", assignee="deus")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)

    assert spawns == [], "spawned a worker into a provider that is answering 429"
    assert not res.spawned
    assert tid in [entry[0] for entry in res.skipped_provider_walled]
    assert task is not None and task.status == "ready", (
        "a quota wall must hold the card in place, not move it"
    )


def test_walled_chain_does_not_count_a_failure(kanban_home):
    """A provider outage is not the card's fault -- it must not consume the
    card's retry budget or trip the breaker."""
    _write_profile(kanban_home, "deus", "openai-codex")
    _write_snapshot(kanban_home, {"codex": ("openai-codex", 100, "exhausted")})
    _, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="invoice line linkage", assignee="deus")
        before = _failures(conn, tid)
        for _ in range(5):
            kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        after = _failures(conn, tid)
        task = kb.get_task(conn, tid)

    assert after == before == 0, f"quota wall consumed retry budget ({before} -> {after})"
    assert task is not None and task.status == "ready"


def test_one_notice_for_many_walled_cards(kanban_home):
    """The outage is reported once per tick, not once per card -- 28 blocked
    cards must not produce 28 notifications."""
    _write_profile(kanban_home, "deus", "openai-codex")
    _write_snapshot(kanban_home, {"codex": ("openai-codex", 100, "exhausted")})
    _, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        for n in range(6):
            kb.create_task(conn, title=f"card {n}", assignee="deus")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert not res.spawned
    assert len(res.skipped_provider_walled) == 6
    assert len(res.provider_wall_notices) == 1, (
        f"expected one board-level notice, got {res.provider_wall_notices}"
    )
    provider, resets_at = res.provider_wall_notices[0]
    assert provider == "openai-codex"
    assert resets_at is not None


# ---------------------------------------------------------------------------
# Scope: the chain, not the primary; the profile, not the board
# ---------------------------------------------------------------------------


def test_healthy_fallback_in_chain_still_dispatches(kanban_home):
    """Codex walled but the alibaba fallback healthy -- the profile can still
    work, so it must."""
    _write_profile(kanban_home, "deus", "openai-codex", fallbacks=["alibaba-coding-plan"])
    _write_snapshot(
        kanban_home,
        {
            "codex": ("openai-codex", 100, "exhausted"),
            "alibaba_token_plan": ("alibaba-coding-plan", 12, "ok"),
        },
    )
    spawns, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        kb.create_task(conn, title="still workable", assignee="deus")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 1, "a healthy fallback provider must keep the profile working"
    assert res.spawned


def test_wall_is_scoped_to_the_profiles_own_providers(kanban_home):
    """An exhausted Codex must not stop neon (local) or any other profile."""
    _write_profile(kanban_home, "deus", "openai-codex")
    _write_profile(kanban_home, "neon", "custom")
    _write_snapshot(kanban_home, {"codex": ("openai-codex", 100, "exhausted")})
    spawns, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        kb.create_task(conn, title="codex work", assignee="deus")
        neon_id = kb.create_task(conn, title="local work", assignee="neon")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == [neon_id], (
        f"a walled Codex leaked into an unrelated profile; spawned {spawns}"
    )


def test_dispatch_resumes_once_the_window_resets(kanban_home):
    """The 28 held cards must flow again on their own when quota returns."""
    _write_profile(kanban_home, "deus", "openai-codex")
    _write_snapshot(kanban_home, {"codex": ("openai-codex", 100, "exhausted")})
    spawns, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="held card", assignee="deus")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert spawns == []
        # Window resets: the monitor rewrites the snapshot.
        _write_snapshot(kanban_home, {"codex": ("openai-codex", 3, "ok")})
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == [tid], "card did not resume after the quota window reset"


# ---------------------------------------------------------------------------
# Fail open -- a broken monitor must never freeze the board
# ---------------------------------------------------------------------------


def test_missing_snapshot_fails_open(kanban_home):
    _write_profile(kanban_home, "deus", "openai-codex")
    spawns, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        kb.create_task(conn, title="no snapshot on disk", assignee="deus")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 1, "a missing quota snapshot froze the board"


def test_stale_snapshot_fails_open(kanban_home):
    """Codex only writes quota data as a side effect of a CLI turn, so an idle
    or exhausted Codex reports 'exhausted' forever. A stale reading must not be
    allowed to wedge the board shut."""
    _write_profile(kanban_home, "deus", "openai-codex")
    _write_snapshot(
        kanban_home,
        {"codex": ("openai-codex", 100, "exhausted")},
        age_seconds=6 * 3600,
    )
    spawns, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        kb.create_task(conn, title="stale reading", assignee="deus")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 1, "a stale quota snapshot froze the board"


def test_unparseable_snapshot_fails_open(kanban_home):
    _write_profile(kanban_home, "deus", "openai-codex")
    state = kanban_home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "quota_snapshot.json").write_text("{not json at all")
    spawns, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        kb.create_task(conn, title="corrupt snapshot", assignee="deus")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 1, "an unparseable quota snapshot froze the board"


def test_provider_absent_from_snapshot_fails_open(kanban_home):
    """A provider the monitor doesn't track is unknown, not walled."""
    _write_profile(kanban_home, "deus", "some-unmonitored-provider")
    _write_snapshot(kanban_home, {"codex": ("openai-codex", 100, "exhausted")})
    spawns, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        kb.create_task(conn, title="untracked provider", assignee="deus")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 1, "an untracked provider was treated as walled"


def test_unreadable_profile_config_fails_open(kanban_home):
    """No resolvable provider chain -> unknown -> dispatch."""
    _write_snapshot(kanban_home, {"codex": ("openai-codex", 100, "exhausted")})
    spawns, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        kb.create_task(conn, title="profile has no config", assignee="ghost")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 1, "an unresolvable provider chain froze the board"


# ---------------------------------------------------------------------------
# probe.ok is NOT a usability signal (regression guard)
# ---------------------------------------------------------------------------


def test_failed_credential_probe_does_not_wall_a_provider(kanban_home):
    """``probe.ok: false`` must NOT hold cards back.

    Tried and reverted on 2026-09-08: the alibaba entry reports
    ``{"ok": false, "reason": "DASHSCOPE_API_KEY not present"}`` because the
    probe looks for an environment variable, while Hermes authenticates that
    provider from ``auth.json``. The provider is demonstrably reachable --
    ``zero`` (chain: alibaba-coding-plan) completed cards at 12:19, 12:23 and
    13:10 that same day with that exact probe result, and the live plan balance
    was fetched successfully through it.

    Gating on it froze ``zero`` and ``neon`` outright. A probe failure is an
    ambiguous signal about one credential path, not proof the provider is
    unreachable, so it falls under the module's fail-open rule.
    """
    now = time.time()
    body = {
        "schema_version": 2,
        "generated_at": now,
        "providers": {
            "alibaba_token_plan": {
                "provider": "alibaba-coding-plan",
                "status": "ok",
                "probe": {"ok": False, "reason": "DASHSCOPE_API_KEY not present"},
                "balance": {"used_percent": 78.08},
            }
        },
    }
    state = kanban_home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "quota_snapshot.json").write_text(json.dumps(body))
    _write_profile(kanban_home, "zero", "alibaba-coding-plan")
    spawns, fake_spawn = _spawn_recorder()

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="zero can still work", assignee="zero")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == [tid], (
        "a failed credential probe froze a provider that is demonstrably working"
    )
