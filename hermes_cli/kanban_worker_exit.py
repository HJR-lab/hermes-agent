"""Automation exit code for a finished single-query turn.

A kanban worker's exit code is a protocol with the dispatcher:

* ``0``  -- worker finished cleanly. If the task row is still ``running`` this
  is a PROTOCOL VIOLATION (it should have called ``kanban_complete`` /
  ``kanban_block``) and costs the card a retry.
* ``1``  -- ordinary failure; counts against the card's retry budget.
* ``75`` -- ``KANBAN_RATE_LIMIT_EXIT_CODE``: the provider refused to serve.
  Requeued to the source phase, no failure counted, breaker untouched.

Until this module existed the 75 path was implemented only inside
``_run_quiet_single_query`` (the ``-Q`` path), while kanban workers run the
normal single-query path through ``cli.chat()`` -- which had no exit-code logic
at all and simply fell off the end at 0. A 3.5-hour Codex outage on 2026-09-08
therefore produced 135 "worker exited cleanly (rc=0)" runs and left 28 business
cards blocked for a reason that was false.
"""

from __future__ import annotations

from typing import Any, Optional

# Reasons that mean "the provider would not serve us", as opposed to "this task
# failed". Matches agent.error_classifier's FailoverReason values.
QUOTA_WALL_REASONS = ("rate_limit", "billing")


def quota_wall_reason(result: Any) -> Optional[str]:
    """The quota-wall reason a turn ended on, or ``None``.

    ``partial`` counts: a turn that produced some output before the wall is
    still incomplete, and retrying after the reset is the right move.
    """
    if not isinstance(result, dict):
        return None
    if not (result.get("failed") or result.get("partial")):
        return None
    reason = str(result.get("failure_reason") or "").strip()
    return reason if reason in QUOTA_WALL_REASONS else None


def worker_exit_code(result: Any, *, kanban_task: str = "") -> int:
    """Automation exit code for ``result``.

    ``kanban_task`` is ``HERMES_KANBAN_TASK``. The 75 sentinel is a private
    protocol between a dispatcher-spawned worker and the dispatcher, so an
    interactive user hitting the same wall gets the ordinary failure code.
    """
    if not isinstance(result, dict):
        return 0
    if not (result.get("failed") or result.get("partial")):
        return 0
    if kanban_task and quota_wall_reason(result) is not None:
        from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

        return KANBAN_RATE_LIMIT_EXIT_CODE
    # ``partial`` alone is not a failed run for automation purposes; only an
    # explicit ``failed`` is.
    return 1 if result.get("failed") else 0
