"""`kanban_create` (agent tool) validates `skills` against the ASSIGNEE.

The CLI guard (`hermes_cli/kanban.py::_validate_requested_skills`) only ever
covered `hermes kanban create`. The agent/MCP tool is a second authoring entry
point and was unguarded: an orchestrator calling
``kanban_create(assignee="deus", skills=["translation"])`` wrote a row whose
skill names resolve only in the CALLER's profile, and the mismatch surfaced
about a second into the spawned worker as ``Unknown skill(s): ...`` in a
per-task log nothing renders on the board.

Policy (t_7e1b4953): HARD REJECTION. Requested skills are capability
requirements, so an unresolvable name returns ``tool_error`` before
``create_task`` and writes no row. A probe that returns ``None`` fails open;
an empty successful catalog is authoritative.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Isolated kanban home with a claimed task in HERMES_KANBAN_TASK."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def _pin_catalog(monkeypatch, catalog, *, seen: list | None = None):
    """Stub the shared profile-catalog probe with a fixed answer."""
    from hermes_cli import kanban_db as kb

    def fake(profile, *, timeout=30.0):
        if seen is not None:
            seen.append(profile)
        return catalog

    monkeypatch.setattr(kb, "installed_skills_for_profile", fake)


def _create(**overrides) -> dict:
    from tools import kanban_tools as kt

    args = {"title": "child", "assignee": "deus"}
    args.update(overrides)
    return json.loads(kt._handle_create(args))


def _tasks() -> list:
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        return list(kb.list_tasks(conn))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------

def test_unresolvable_skill_rejected_and_no_row_written(worker_env, monkeypatch):
    _pin_catalog(monkeypatch, {"hermes-agent", "blogwatcher"})
    before = len(_tasks())

    out = _create(skills=["translation", "hermes-agent"])

    assert out["error"] == (
        "Cannot create task: skill(s) not resolvable for assignee 'deus': "
        "translation. Resolvable skills: blogwatcher, hermes-agent. "
        "No task was created."
    )
    assert "task_id" not in out
    assert len(_tasks()) == before, "a rejected create must not write a row"


def test_all_invalid_names_listed_in_request_order(worker_env, monkeypatch):
    _pin_catalog(monkeypatch, {"hermes-agent"})

    out = _create(skills=["zulu", "alpha", "mike"])

    assert "'deus': zulu, alpha, mike." in out["error"]
    assert "Resolvable skills: hermes-agent." in out["error"]
    assert len(_tasks()) == 1  # only the fixture's own task


def test_string_skill_is_normalized_then_validated(worker_env, monkeypatch):
    """A bare string is accepted as one name — and still validated."""
    _pin_catalog(monkeypatch, {"hermes-agent"})

    out = _create(skills="translation")

    assert "not resolvable for assignee 'deus': translation." in out["error"]
    assert len(_tasks()) == 1


def test_empty_catalog_is_authoritative(worker_env, monkeypatch):
    """A successful probe that resolves nothing rejects every requested name."""
    _pin_catalog(monkeypatch, set())
    before = len(_tasks())

    out = _create(skills=["hermes-agent"])

    assert out["error"] == (
        "Cannot create task: skill(s) not resolvable for assignee 'deus': "
        "hermes-agent. Resolvable skills: (none). No task was created."
    )
    assert len(_tasks()) == before


def test_resolvable_list_is_sorted_for_deterministic_output(worker_env, monkeypatch):
    _pin_catalog(monkeypatch, {"zeta", "alpha", "Mike"})

    first = _create(skills=["nope"])["error"]
    second = _create(skills=["nope"])["error"]

    assert first == second
    assert "Resolvable skills: Mike, alpha, zeta." in first


def test_validation_uses_effective_assignee(worker_env, monkeypatch):
    """The catalog probed is the profile the dispatcher will actually spawn."""
    seen: list[str] = []
    _pin_catalog(monkeypatch, {"hermes-agent"}, seen=seen)

    out = _create(assignee="Deus", skills=["translation"])

    assert seen == ["deus"], f"probed the wrong profile: {seen}"
    assert "for assignee 'deus':" in out["error"]


# ---------------------------------------------------------------------------
# Pass-through
# ---------------------------------------------------------------------------

def test_resolvable_skills_are_preserved_unchanged(worker_env, monkeypatch):
    _pin_catalog(monkeypatch, {"hermes-agent", "blogwatcher", "translation"})

    out = _create(skills=["translation", "hermes-agent"])

    assert out["ok"] is True
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        task = kb.get_task(conn, out["task_id"])
    finally:
        conn.close()
    assert task.skills == ["translation", "hermes-agent"]


def test_failed_probe_fails_open(worker_env, monkeypatch):
    """``None`` means the probe could not be trusted — create the card anyway."""
    _pin_catalog(monkeypatch, None)

    out = _create(skills=["anything", "at-all"])

    assert out["ok"] is True
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        task = kb.get_task(conn, out["task_id"])
    finally:
        conn.close()
    assert task.skills == ["anything", "at-all"]


def test_no_skills_requested_skips_the_probe(worker_env, monkeypatch):
    """Cards without `skills` must not pay for (or be blocked by) a subprocess."""
    seen: list[str] = []
    _pin_catalog(monkeypatch, set(), seen=seen)

    out = _create()

    assert out["ok"] is True
    assert seen == []
