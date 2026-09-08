"""The escalation gate and the general-assignee allow-list must actually run.

Both controls were implemented, independently verified PASS, and then silently
destroyed: they lived as UNCOMMITTED edits in ``hermes_cli/kanban_db.py`` and a
``git reset --hard origin/main`` during a ``hermes update`` wiped them. Their
on-disk config survived, so ``~/.hermes/config.yaml`` still advertised
``automatic_general_assignees: [deus, duex, neon, zero]`` while nothing read the
key. Dispatched runs on 2026-09-08 were deus 146, zero 24, duex 17, **default
12**, neon 3, **justin 1** — two assignees outside the allow-list, one of them a
human.

These tests pin the behaviour so a future refactor fails loudly instead of
quietly removing a security control.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_escalation as esc
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def no_escalation_route(tmp_path, monkeypatch):
    """Route NOT installed: an empty dir. The gate must stay inert."""
    root = tmp_path / "escalation-absent"
    root.mkdir()
    monkeypatch.setenv("HERMES_ESCALATION_ROOT", str(root))
    return root


@pytest.fixture
def broken_escalation_route(tmp_path, monkeypatch):
    """Route INSTALLED but unusable — a policy file exists and is malformed.
    This is the fail-closed case, distinct from 'never installed'."""
    root = tmp_path / "escalation-broken"
    root.mkdir()
    (root / "dispatch-policy.json").write_text("{ not json")
    monkeypatch.setenv("HERMES_ESCALATION_ROOT", str(root))
    return root


@pytest.fixture
def any_profile_exists(monkeypatch):
    """Every assignee resolves to a profile, so the only thing that can stop a
    spawn in these tests is the control under test."""
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda _name: True))


def _spawned(conn, **kw):
    """Run one dispatch tick, returning the task ids handed to spawn_fn."""
    seen: list[str] = []

    def spawn_fn(task, workspace, *a, **k):
        seen.append(task.id)
        return 4242

    kbd.dispatch_once(conn, spawn_fn=spawn_fn, **kw)
    return seen


# --- the allow-list -------------------------------------------------------

def test_assignee_outside_the_allowlist_is_not_auto_spawned(kanban_home, no_escalation_route, any_profile_exists):
    """The exact production failure: 'default' and 'justin' were dispatched
    while the configured allow-list was [deus, duex, neon, zero]."""
    with kbc.connect() as conn:
        allowed = kb.create_task(conn, title="allowed", assignee="deus")
        denied_default = kb.create_task(conn, title="denied default", assignee="default")
        denied_human = kb.create_task(conn, title="denied human", assignee="justin")

        seen = _spawned(
            conn,
            automatic_general_assignees=["deus", "duex", "neon", "zero"],
        )

    assert allowed in seen
    assert denied_default not in seen, "'default' is outside the allow-list"
    assert denied_human not in seen, "a card assigned to a human must never auto-spawn"


def test_no_allowlist_configured_preserves_dispatch_everything(kanban_home, no_escalation_route, any_profile_exists):
    """``None`` means 'no allow-list' — existing installs must not change."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="anyone", assignee="somebody")
        seen = _spawned(conn, automatic_general_assignees=None)
    assert tid in seen


def test_empty_allowlist_denies_everything(kanban_home, no_escalation_route, any_profile_exists):
    """An explicitly empty list is a real setting, not 'unset'. Collapsing the
    two with a truthy check is how an allow-list silently stops enforcing."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="nobody", assignee="deus")
        seen = _spawned(conn, automatic_general_assignees=[])
    assert tid not in seen


def test_normalize_distinguishes_unset_from_empty():
    assert esc.normalize_general_assignees(None) is None
    assert esc.normalize_general_assignees([]) == set()
    assert esc.normalize_general_assignees(["  deus ", "", "zero"]) == {"deus", "zero"}


# --- the escalation gate --------------------------------------------------

def test_escalation_candidate_is_denied_when_the_policy_is_broken(kanban_home, broken_escalation_route):
    """Fail closed: an escalation-shaped card with no installed policy must be
    refused, and must NOT fall through to the general allow-list even when its
    assignee would pass it."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="[ESCALATION P1] wire transfer", assignee="default")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        ok, reason = esc.may_auto_spawn(conn, row, "default", {"default", "deus"})
    assert ok is False
    assert reason != "assignee_not_in_general_allowlist", (
        "an escalation row must be judged by the gate, never by the allow-list"
    )


def test_escalation_candidate_denied_on_malformed_policy(kanban_home, tmp_path, monkeypatch):
    root = tmp_path / "escalation-bad"
    root.mkdir()
    (root / "dispatch-policy.json").write_text("{ this is not json")
    monkeypatch.setenv("HERMES_ESCALATION_ROOT", str(root))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="[ESCALATION P1] payout", assignee="default")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        ok, _ = esc.may_auto_spawn(conn, row, "default", {"default"})
    assert ok is False


def test_on_disk_policy_can_only_widen_the_governed_set(tmp_path, monkeypatch):
    """Tampering with the policy file must not let a card escape the gate by
    shrinking the governed set — the static fallbacks always apply."""
    root = tmp_path / "escalation-narrow"
    root.mkdir()
    (root / "dispatch-policy.json").write_text(json.dumps({"assignee": "someone-else"}))
    monkeypatch.setenv("HERMES_ESCALATION_ROOT", str(root))
    assignees, titles, creators = esc._escalation_identity()
    assert "default" in assignees, "static fallback assignee must survive"
    assert "someone-else" in assignees, "on-disk value adds to the set"
    assert "[ESCALATION " in titles
    assert "escalation-gate:" in creators


def test_ordinary_card_is_not_escalation_governed(kanban_home, no_escalation_route):
    """The gate returns None for a plainly non-escalation row, so ordinary
    dispatch is untouched."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="tidy the inbox", assignee="deus")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert esc.escalation_dispatch_decision(conn, row, "deus") is None


def test_gate_beats_the_allowlist_through_a_real_dispatch_tick(
    kanban_home, broken_escalation_route, any_profile_exists
):
    """End-to-end through ``dispatch_once``: escalation-governed rows must not
    spawn when the route cannot be verified, EVEN THOUGH their assignee is on
    the allow-list. The gate's verdict is final — it never falls through.

    Note ``default`` is itself a governed assignee (``_ESCALATION_FALLBACK_ASSIGNEES``),
    so every ``default`` card is gated, escalation-titled or not — the receipt's
    case "2b. ordinary default card rejected". The ungated control therefore uses
    a different allow-listed assignee."""
    with kbc.connect() as conn:
        escalation = kb.create_task(
            conn, title="[ESCALATION P1] approve the wire", assignee="default",
        )
        plain_default = kb.create_task(conn, title="ordinary work", assignee="default")
        ungated = kb.create_task(conn, title="ordinary work", assignee="deus")
        seen = _spawned(conn, automatic_general_assignees=["default", "deus"])

    assert ungated in seen, "a non-governed card on the allow-list still dispatches"
    assert escalation not in seen, (
        "escalation-shaped card dispatched despite an unverifiable route — "
        "the gate must not fall through to the allow-list"
    )
    assert plain_default not in seen, (
        "'default' is a governed assignee; without a verifiable policy every "
        "default card must fail closed, allow-list membership notwithstanding"
    )


def test_gate_is_inert_when_the_route_is_not_installed(kanban_home, no_escalation_route, any_profile_exists):
    """Opt-in. ``default`` is a governed assignee, so without this an install
    that never heard of the escalation route would silently stop dispatching
    every ``default`` card the moment this module shipped."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="[ESCALATION P1] looks the part", assignee="default")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert esc.escalation_dispatch_decision(conn, row, "default") is None
        assert not esc.escalation_route_installed()
        seen = _spawned(conn, automatic_general_assignees=["default"])
    assert tid in seen


def test_installed_route_still_fails_closed_when_tampered(kanban_home, broken_escalation_route):
    """Opt-in must not become opt-out: once the route IS installed, breaking
    its policy denies rather than disabling the gate."""
    assert esc.escalation_route_installed()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="[ESCALATION P1] payout", assignee="default")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        ok, _ = esc.may_auto_spawn(conn, row, "default", {"default"})
    assert ok is False


def test_escalation_root_honours_hermes_home(tmp_path, monkeypatch):
    """An alternate HERMES_HOME must not inherit the real user's route."""
    monkeypatch.delenv("HERMES_ESCALATION_ROOT", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "alt-home"))
    assert esc._escalation_root() == str(tmp_path / "alt-home" / "escalation")
    assert esc.escalation_route_installed() is False
