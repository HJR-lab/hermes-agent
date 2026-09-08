"""Regression tests for the 2026-09-08 duplicate-decomposition / retry-flood
incident (kanban cards t_3365d37e, t_a06ddf52, t_4e723a61 auto-created from
t_89ebac10).

Four distinct defects were observed on the live board and are pinned here:

1. **Duplicate decomposition.** ``auto-decomposer`` fanned an already-worked
   triage card out into three children whose acceptance evidence already
   existed on the card itself. Decomposition now refuses a task that carries
   prior-work signal (runs / ``started_at`` / terminal events) unless the
   caller explicitly forces it.

2. **Workspace aliasing.** All three children were created with
   ``workspace_kind='scratch'`` and the *root's* ``workspace_path``
   (``.../workspaces/t_89ebac10``). Concurrent siblings then shared one
   ephemeral directory that completion is allowed to ``rmtree``. A decomposed
   child no longer inherits a scratch path; it materializes its own.

3. **Give-up was not durable.** The protocol-violation breaker force-trips on
   a violation-only *streak* (3) while ``recompute_ready`` gates re-promotion
   on ``consecutive_failures`` against ``DEFAULT_FAILURE_LIMIT`` (2). The trip
   incremented the counter by one, so ``1 >= 2`` was false and the dispatcher
   promoted the just-given-up card straight back to ``ready``:

       gave_up {failures: 1, effective_limit: 3, protocol_violations: 3}
       promoted
       ... protocol_violation ...
       gave_up {failures: 2, effective_limit: 3, protocol_violations: 4}

   The trip now persists a counter consistent with the limit it tripped on,
   and the violation budget respects the dispatcher's configured limit.

4. **Two terminal notifications.** Two ``gave_up`` events for one terminal
   state produced two "gave up" notifications. Follows from (3): with the
   card staying blocked there is exactly one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


def _events(conn, task_id: str, kind: str) -> list:
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()


def _protocol_violation_run(conn, tid: str, pid: int) -> None:
    """Drive one full 'worker exited rc=0 while still running' cycle."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, tid))
    conn.commit()
    kbd._record_worker_exit(pid, _exited_status(0))


# ---------------------------------------------------------------------------
# (2) Decomposed children must not alias another task's workspace
# ---------------------------------------------------------------------------


def test_scratch_children_do_not_alias_root_workspace(kanban_home):
    """Reproduces the t_89ebac10 fan-out: three scratch siblings must not all
    point at the root's scratch directory."""
    root_ws = str(kanban_home / "kanban" / "workspaces" / "root")
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="already-worked root", triage=True,
            workspace_kind="scratch", workspace_path=root_ws,
        )
        child_ids = decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[
                {"title": "verify reply", "assignee": "zero", "parents": []},
                {"title": "prove rejection", "assignee": "zero", "parents": []},
                {"title": "document endpoints", "assignee": "zero", "parents": []},
            ],
            author="auto-decomposer",
        )
        assert child_ids is not None and len(child_ids) == 3
        for cid in child_ids:
            child = kb.get_task(conn, cid)
            assert child.workspace_kind == "scratch"
            assert child.workspace_path is None, (
                f"child {cid} aliased the root workspace {child.workspace_path!r}; "
                "concurrent siblings would share one ephemeral directory"
            )


def test_dir_children_still_inherit_the_explicit_root_directory(kanban_home, tmp_path):
    """``dir`` is a caller-chosen, never-auto-removed path — inheriting it is
    the intended behaviour and must not regress."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="root in a repo", triage=True,
            workspace_kind="dir", workspace_path=str(repo),
        )
        child_ids = decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "edit the repo", "assignee": "deus", "parents": []}],
            author="auto-decomposer",
        )
        assert child_ids is not None
        child = kb.get_task(conn, child_ids[0])
        assert child.workspace_kind == "dir"
        assert child.workspace_path == str(repo)


def test_explicit_child_workspace_path_still_wins(kanban_home, tmp_path):
    """'unless explicitly intended' — a per-child override is honoured."""
    shared = tmp_path / "shared"
    shared.mkdir()
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="root", triage=True,
            workspace_kind="scratch", workspace_path=str(tmp_path / "root-ws"),
        )
        child_ids = decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{
                "title": "child with explicit workspace", "assignee": "deus",
                "parents": [], "workspace_path": str(shared),
            }],
            author="auto-decomposer",
        )
        assert child_ids is not None
        assert kb.get_task(conn, child_ids[0]).workspace_path == str(shared)


# ---------------------------------------------------------------------------
# (3) + (4) A protocol-violation give-up is terminal
# ---------------------------------------------------------------------------


def test_protocol_violation_give_up_is_not_re_promoted(kanban_home, monkeypatch):
    """The exact t_3365d37e loop: three clean-exit violations trip the
    breaker, and the next dispatcher tick must leave the card blocked with
    exactly one ``gave_up`` event."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="verify authenticated reply", assignee="zero")

        for i in range(kbd._PROTOCOL_VIOLATION_FAILURE_LIMIT):
            _protocol_violation_run(conn, tid, 71000 + i)
            kbd.detect_crashed_workers(conn)
            # recompute_ready runs on every dispatcher tick, including the one
            # that just tripped the breaker.
            kb.recompute_ready(conn, failure_limit=kbd.DEFAULT_FAILURE_LIMIT)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"card was re-promoted to {task.status!r} after giving up; "
            "the next tick would spawn another doomed worker"
        )

        # Further ticks must not resurrect it either.
        for _ in range(3):
            assert kb.recompute_ready(conn, failure_limit=kbd.DEFAULT_FAILURE_LIMIT) == 0
            assert kb.get_task(conn, tid).status == "blocked"

        assert len(_events(conn, tid, "gave_up")) == 1, "one terminal state, one notification"
        assert len(_events(conn, tid, "promoted")) == 0


def test_violation_budget_respects_a_higher_dispatcher_failure_limit(kanban_home, monkeypatch):
    """A board configured with ``--failure-limit`` above the violation budget
    must not be able to re-promote a given-up card either: the trip and the
    promotion guard have to agree on one effective limit."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    high_limit = kbd._PROTOCOL_VIOLATION_FAILURE_LIMIT + 5

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="high failure limit board", assignee="zero")
        for i in range(high_limit + 2):
            if kb.get_task(conn, tid).status == "blocked":
                break
            _protocol_violation_run(conn, tid, 72000 + i)
            kbd.detect_crashed_workers(conn, failure_limit=high_limit)
            kb.recompute_ready(conn, failure_limit=high_limit)

        assert kb.get_task(conn, tid).status == "blocked"
        for _ in range(3):
            assert kb.recompute_ready(conn, failure_limit=high_limit) == 0
            assert kb.get_task(conn, tid).status == "blocked"
        assert len(_events(conn, tid, "gave_up")) == 1


def test_per_task_max_retries_still_overrides_the_violation_budget(kanban_home, monkeypatch):
    """``max_retries`` remains the top of the precedence chain, and the card
    it trips still stays blocked."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="one shot", assignee="zero", max_retries=1)
        _protocol_violation_run(conn, tid, 73001)
        kbd.detect_crashed_workers(conn)
        kb.recompute_ready(conn, failure_limit=kbd.DEFAULT_FAILURE_LIMIT)

        assert kb.get_task(conn, tid).status == "blocked"
        assert len(_events(conn, tid, "gave_up")) == 1


def test_ordinary_crash_below_the_limit_still_retries(kanban_home, monkeypatch):
    """Legitimate recovery retries are preserved — a single violation must
    leave the card ``ready`` for another attempt."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="retryable", assignee="zero")
        _protocol_violation_run(conn, tid, 74001)
        kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert not _events(conn, tid, "gave_up")


# ---------------------------------------------------------------------------
# (1) Decomposition must not duplicate work that already happened
# ---------------------------------------------------------------------------


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist (mirrors test_kanban_decompose.py)."""
    from types import SimpleNamespace
    from unittest.mock import patch

    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch(
            "hermes_cli.profiles.get_active_profile_name",
            return_value=names[0] if names else "default",
        ),
    ]


def _decompose_with_fanout(tid: str, *, force: bool = False):
    """Run ``decompose_task`` against a mocked aux LLM that always fans out."""
    import json as jsonlib
    from unittest.mock import MagicMock, patch

    payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "split it",
        "tasks": [
            {"title": "verify reply", "body": "b", "assignee": "zero", "parents": []},
            {"title": "prove rejection", "body": "b", "assignee": "zero", "parents": []},
        ],
    })
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = payload

    patches = _patch_list_profiles(["orchestrator", "zero"])
    for p in patches:
        p.start()
    try:
        with patch("agent.auxiliary_client.call_llm", return_value=resp), \
             patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={}):
            from hermes_cli import kanban_decompose as decomp
            return decomp.decompose_task(tid, author="auto-decomposer", force=force)
    finally:
        for p in patches:
            p.stop()


def _worked_then_retriaged(conn, title: str) -> str:
    """Build the card t_89ebac10 actually was: dispatched and worked (a real
    run, ``started_at`` set, a worker comment), then sitting back in triage."""
    tid = kb.create_task(conn, title=title, assignee="zero")
    kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, "zero", "Retry verification: evidence already collected.")
    conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
    conn.commit()
    assert kb.get_task(conn, tid).started_at, "fixture must carry prior-work signal"
    return tid


def test_decompose_refuses_a_triage_card_that_was_already_worked(kanban_home):
    """t_89ebac10 had three worker comments and a completed run when the
    auto-decomposer fanned it into three duplicate children."""
    with kbc.connect() as conn:
        tid = _worked_then_retriaged(conn, "Execute remaining acceptance tests")

    outcome = _decompose_with_fanout(tid)

    assert not outcome.ok
    assert "prior work" in outcome.reason.lower(), outcome.reason
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        assert not _events(conn, tid, "decomposed")


def test_decompose_force_overrides_the_prior_work_guard(kanban_home):
    """An operator running ``hermes kanban decompose --force`` still can."""
    with kbc.connect() as conn:
        tid = _worked_then_retriaged(conn, "Execute remaining acceptance tests")

    outcome = _decompose_with_fanout(tid, force=True)

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 2


def test_decompose_still_fans_out_a_fresh_triage_card(kanban_home):
    """The guard must not touch the normal path: an untouched idea decomposes."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rough new idea", triage=True)

    outcome = _decompose_with_fanout(tid)

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 2
