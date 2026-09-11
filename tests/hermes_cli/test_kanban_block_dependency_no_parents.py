"""A dependency block on a PARENTLESS task must actually stop the task.

``_route_block`` parks ``kind="dependency"`` in ``todo`` to wait for
``recompute_ready``. With no open parent there is nothing to wait for, so
recompute promotes it straight back to ``ready`` and the dispatcher respawns
it — the block silently did nothing. Hit for real on 2026-09-06 while
containing a duplicate worker: the card was blocked, returned to ready within
a tick, and spawned again.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    c = kbc.connect()
    try:
        yield c
    finally:
        c.close()


def _mk(conn, title="t", assignee="deus"):
    return kb.create_task(conn, title=title, assignee=assignee, initial_status="running")


def test_dependency_block_without_parents_does_not_land_in_todo(conn):
    """The bug: 'todo' is promotable, so the task comes straight back."""
    tid = _mk(conn)
    assert kb.block_task(conn, tid, reason="stop this", kind="dependency") is True
    task = kb.get_task(conn, tid)
    assert task.status != "todo", (
        "a parentless dependency block parked in todo would be promoted back "
        "to ready by recompute_ready and respawn"
    )
    assert task.status == "blocked"


def test_dependency_block_survives_recompute_ready(conn):
    """End-to-end: the task must NOT be promotable back into the pool."""
    tid = _mk(conn)
    kb.block_task(conn, tid, reason="stop", kind="dependency")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "blocked"


def test_downgrade_is_recorded_in_the_reason(conn):
    tid = _mk(conn)
    kb.block_task(conn, tid, reason="containment", kind="dependency")
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ?", (tid,)
    ).fetchall()
    blob = " ".join(r["body"] for r in rows) if rows else ""
    events = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ?", (tid,)
    ).fetchall()
    blob += " " + " ".join(str(r["payload"]) for r in events)
    assert "no open parents" in blob, (
        "the downgrade must be visible, not a silent change of semantics"
    )


def test_dependency_block_with_open_parent_still_waits_in_todo(conn):
    """Unchanged behaviour where a dependency wait is meaningful."""
    parent = _mk(conn, title="parent")
    child = _mk(conn, title="child")
    kb.link_tasks(conn, parent, child)
    kb.block_task(conn, child, reason="waiting on parent", kind="dependency")
    assert kb.get_task(conn, child).status == "todo"
