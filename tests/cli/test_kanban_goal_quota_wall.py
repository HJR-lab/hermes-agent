"""A kanban goal-mode worker exits rate-limited as soon as a turn hits a quota wall.

The goal loop used to judge the failed turn and keep re-prompting: every extra turn hit the same
wall, the judge ran on the auxiliary route, and the card could end blocked for "turn budget
exhausted" — a false reason — while the provider simply was not serving. The dispatcher's own
quota-wall requeue (exit 75) is the truthful outcome.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import cli
from agent.turn_author import TURN_AUTHOR_ENV
from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

WALL = {
    "final_response": "API call failed after 3 retries: You've hit your session limit · resets 12am",
    "failed": True, "failure_reason": "rate_limit",
}
PROGRESS = {"final_response": "still working"}


@pytest.fixture
def board(monkeypatch):
    """A running goal-mode card whose judge always says 'continue'; records judge calls and blocks."""
    from hermes_cli import goals, kanban_db, kanban_db_connect

    monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_wall")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv(TURN_AUTHOR_ENV, raising=False)

    @contextmanager
    def _conn(*_a, **_k):
        yield None

    state = SimpleNamespace(judged=[], blocked=[])
    monkeypatch.setattr(kanban_db_connect, "connect_closing", _conn)
    monkeypatch.setattr(kanban_db, "get_task",
                        lambda _c, _t: SimpleNamespace(title="Ship it", body="criteria", goal_max_turns=10))
    monkeypatch.setattr(kanban_db, "goal_run_status", lambda *_a: "running")
    monkeypatch.setattr(kanban_db, "block_task", lambda *_a, **kw: state.blocked.append(kw))
    monkeypatch.setattr(goals, "judge_goal",
                        lambda _g, resp: state.judged.append(resp) or ("continue", "not done", False, None, False))
    return state


def _work(turns):
    prompts = []

    def run_conversation(**kwargs):
        prompts.append(kwargs["user_message"])
        return turns[len(prompts) - 1]

    agent = SimpleNamespace(run_conversation=run_conversation, session_id="s-1")
    with pytest.raises(SystemExit) as exc:
        cli._run_quiet_single_query(SimpleNamespace(agent=agent, conversation_history=[], session_id="s-1"), "go")
    return exc.value.code, prompts


def test_first_turn_quota_wall_exits_rate_limited_without_judging(board):
    code, prompts = _work([WALL])

    assert code == KANBAN_RATE_LIMIT_EXIT_CODE
    assert len(prompts) == 1
    assert board.judged == [] and board.blocked == []


def test_quota_wall_inside_the_goal_loop_stops_it_and_exits_rate_limited(board):
    code, prompts = _work([PROGRESS, WALL, PROGRESS])

    assert code == KANBAN_RATE_LIMIT_EXIT_CODE
    assert len(prompts) == 2  # never re-prompts into the wall
    assert board.blocked == []  # no false "turn budget" block
