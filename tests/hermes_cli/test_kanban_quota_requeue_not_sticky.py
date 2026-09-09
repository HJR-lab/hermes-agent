"""A quota-wall requeue must not permanently park the card afterwards.

The rate-limit requeue (KANBAN_RATE_LIMIT_EXIT_CODE) deliberately does NOT count
a failure -- its whole point is that a provider outage is not the card's fault.
But it stamped ``last_failure_error`` with text containing "rate-limited", and
``_RESPAWN_BLOCKER_RE`` matches "rate limit". While the newest run is still
``rate_limited`` the guard returns early, so nothing shows. The moment ANY later
run supersedes it, that early return no longer applies, ``blocker_auth`` matches
the stale text, and the card is guarded indefinitely -- long after quota returned.

Observed live 2026-09-09: seven duex review cards sat unspawnable with
``blocker_auth`` while Codex was at 0% of its 5h window, each carrying
"pid N exited rate-limited (quota wall) — requeued without counting a failure".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


QUOTA_REQUEUE_TEXT = (
    "pid 36722 exited rate-limited (quota wall) — requeued without counting a failure"
)


def _finish_run(conn, task_id: str, outcome: str, error: str | None = None,
                *, ended_ago: int = 90) -> None:
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, error)"
        " VALUES (?,?,?,strftime('%s','now')-?,strftime('%s','now')-?,?,?)",
        (task_id, "duex", outcome, ended_ago + 10, ended_ago, outcome, error),
    )
    if error is not None:
        conn.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?", (error, task_id))
    conn.commit()


def test_quota_requeue_does_not_park_the_card_once_a_later_run_supersedes_it(kanban_home):
    """The live duex case: quota requeue, then a review handoff, then quota is back."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="DOE app: void an invoice", assignee="duex")
        _finish_run(conn, tid, "rate_limited", QUOTA_REQUEUE_TEXT, ended_ago=7200)
        # A later, unrelated run supersedes the rate-limited one.
        _finish_run(conn, tid, "review_requested", ended_ago=60)

        reason = kbd.check_respawn_guard(conn, tid, lane="review")

    assert reason is None, (
        f"card parked as {reason!r} by the text its own quota requeue stamped; "
        "the provider has recovered and this card can never be picked up again"
    )


def test_a_real_auth_failure_still_parks_the_card(kanban_home):
    """The guard must keep doing its job -- only the quota-requeue text is exempt."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="calls a broken API", assignee="duex")
        _finish_run(conn, tid, "crashed", "HTTP 403 forbidden: invalid api key", ended_ago=7200)
        _finish_run(conn, tid, "review_requested", ended_ago=60)

        reason = kbd.check_respawn_guard(conn, tid, lane="review")

    assert reason == "blocker_auth", "a genuine auth failure must still be guarded"
