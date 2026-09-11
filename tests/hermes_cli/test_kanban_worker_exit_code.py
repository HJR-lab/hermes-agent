"""A kanban worker that only hit a quota wall must say so in its exit code.

The dispatcher already handles this correctly: ``KANBAN_RATE_LIMIT_EXIT_CODE``
(75) is classified ``rate_limited``, requeued to the source phase, and counts no
failure. The mechanism was simply unreachable -- the exit-code logic lived only
on the quiet (``-Q``) path, while kanban workers run the normal single-query
path through ``cli.chat()``, which falls off the end and exits 0.

rc=0 while the task row is still ``running`` is, by definition, a protocol
violation. That is how a 3.5-hour Codex outage on 2026-09-08 produced 135
"worker exited cleanly (rc=0)" runs and left 28 business cards falsely blocked.
"""

from __future__ import annotations

import pytest

from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
from hermes_cli.kanban_worker_exit import worker_exit_code


@pytest.mark.parametrize("reason", ["rate_limit", "billing"])
def test_quota_wall_exits_with_the_rate_limit_sentinel(reason):
    """The live incident: the only failure was the provider refusing to serve."""
    assert worker_exit_code(
        {"failed": True, "failure_reason": reason}, kanban_task="t_a43b3d9d"
    ) == KANBAN_RATE_LIMIT_EXIT_CODE


def test_sentinel_is_not_emitted_outside_a_kanban_worker():
    """An interactive user hitting a quota wall gets the ordinary failure code;
    75 is a private protocol between the worker and the dispatcher."""
    assert worker_exit_code(
        {"failed": True, "failure_reason": "rate_limit"}, kanban_task=""
    ) == 1


def test_a_real_task_failure_still_counts_as_a_failure():
    """Only a quota wall is exempt. A genuine failure must keep consuming the
    retry budget, or a broken card would retry forever."""
    assert worker_exit_code(
        {"failed": True, "failure_reason": "tool_error"}, kanban_task="t_1"
    ) == 1


def test_unclassified_failure_is_not_laundered_into_a_free_retry():
    assert worker_exit_code({"failed": True}, kanban_task="t_1") == 1


def test_success_exits_zero():
    assert worker_exit_code({"final_response": "done"}, kanban_task="t_1") == 0


def test_missing_result_exits_zero():
    """No turn result at all is not a failure signal."""
    assert worker_exit_code(None, kanban_task="t_1") == 0


def test_non_dict_result_exits_zero():
    assert worker_exit_code("some string", kanban_task="t_1") == 0


def test_partial_quota_wall_still_signals_the_wall():
    """A turn that produced some output before the wall is still a wall: the
    work is incomplete and retrying after the reset is the right move."""
    assert worker_exit_code(
        {"partial": True, "failure_reason": "rate_limit"}, kanban_task="t_1"
    ) == KANBAN_RATE_LIMIT_EXIT_CODE


def test_dispatcher_reads_the_sentinel_as_a_quota_wall():
    """Closes the loop: the code this helper emits is the one the dispatcher
    already knows how to treat as a non-failure."""
    from hermes_cli import kanban_db_dispatch as kbd

    pid = 987654
    kbd._record_worker_exit(pid, KANBAN_RATE_LIMIT_EXIT_CODE << 8)
    kind, code = kbd._classify_worker_exit(pid)

    assert kind == "rate_limited", f"dispatcher read exit {code} as {kind!r}"
    dead = kbd._classify_dead_worker(pid, "host:w1")
    assert dead.rate_limited is True
    assert dead.protocol_violation is False
    assert dead.run_outcome == "rate_limited"
