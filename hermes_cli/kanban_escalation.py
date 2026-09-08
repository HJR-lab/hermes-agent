"""DOE→EVER escalation dispatch gate and the general-assignee allow-list.

Both controls were lost once already: they lived as UNCOMMITTED edits inside
``hermes_cli/kanban_db.py`` and a ``git reset --hard origin/main`` during a
``hermes update`` destroyed them, leaving their on-disk config behind so the
board still *looked* protected while nothing enforced it (kanban t_919cc864 /
t_983e8288, 2026-09-08). They live in their own tracked module now so an
upstream refactor of ``kanban_db.py`` cannot silently take them with it.

Recovered from ``stash@{1}`` (``ever-preserve-fix-kanban-skill-profile-alignment-20260906``)
and ported to the post-split dispatcher layout, where ``_dispatch_once_locked``
lives in ``kanban_db_dispatch.py`` rather than ``kanban_db.py``.

The gate is FAIL-CLOSED. :func:`escalation_dispatch_decision` returns ``None``
only when the row is provably not escalation-governed; for anything governed it
returns ``(dispatchable, reason)`` and the caller MUST honour it and must NOT
fall through to the general allow-list. Missing, unreadable, malformed,
insecurely-permissioned or disabled policy all deny.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Iterable, Optional

_log = logging.getLogger(__name__)


_ESCALATION_FALLBACK_ASSIGNEES = ("default",)
_ESCALATION_FALLBACK_TITLE_PREFIXES = ("[ESCALATION ",)
_ESCALATION_FALLBACK_CREATED_BY_PREFIXES = ("escalation-gate:",)


def _escalation_root() -> str:
    """Where the escalation route lives.

    ``HERMES_HOME`` is honoured before ``~``: the original resolved straight to
    ``~/.hermes/escalation``, so an install (or a test) running under an
    alternate ``HERMES_HOME`` silently read the REAL user's policy and key, and
    inherited a gate it never installed.
    """
    explicit = os.environ.get("HERMES_ESCALATION_ROOT")
    if explicit:
        return explicit
    home = os.environ.get("HERMES_HOME")
    if home:
        return os.path.join(home, "escalation")
    return os.path.join(os.path.expanduser("~"), ".hermes", "escalation")


def escalation_route_installed() -> bool:
    """Whether the escalation route is installed on this machine at all.

    The gate is OPT-IN, then FAIL-CLOSED — two different questions that must not
    be conflated:

    * Route absent entirely (no ``~/.hermes/escalation`` holding a policy, config
      or key) -> the control was never installed. ``default`` is an ordinary
      assignee and dispatch behaves exactly as upstream. Without this, every
      install that has never heard of the escalation route would silently stop
      dispatching every ``default`` card, because ``default`` is a governed
      assignee in :data:`_ESCALATION_FALLBACK_ASSIGNEES`.
    * Route installed but its policy/key/ledger is missing, unreadable,
      malformed, insecurely permissioned or disabled -> DENY. Tampering with an
      installed control must never be a way to switch it off.

    Removing the whole directory does disable the gate. That is uninstalling the
    control, not bypassing it, and it sits inside the same-UNIX-user threat model
    the route's README §3/§6.1 already documents: every profile runs as one user,
    so that user can edit the policy or write kanban.db directly regardless.
    """
    root = _escalation_root()
    if os.environ.get("HERMES_ESCALATION_GOVERNED_ASSIGNEES"):
        return True
    for probe in ("dispatch-policy.json", "config.json", os.path.join("keys", "dispatch.key")):
        if os.path.exists(os.path.join(root, probe)):
            return True
    return False


def _escalation_identity() -> tuple[set[str], tuple[str, ...], tuple[str, ...]]:
    """Best-effort identity of the escalation route.

    Returns ``(governed_assignees, title_prefixes, created_by_prefixes)``.

    This deliberately reads the policy/config files with NO permission or
    schema checks and swallows every error: it answers "which cards belong to
    the escalation route?", not "may this card dispatch?". On-disk values can
    only ADD to the static fallbacks, never remove them, so tampering with
    those files cannot shrink the governed set to escape the gate.
    """
    assignees = set(_ESCALATION_FALLBACK_ASSIGNEES)
    titles = set(_ESCALATION_FALLBACK_TITLE_PREFIXES)
    creators = set(_ESCALATION_FALLBACK_CREATED_BY_PREFIXES)
    env = os.environ.get("HERMES_ESCALATION_GOVERNED_ASSIGNEES")
    if env:
        assignees.update(part.strip() for part in env.split(",") if part.strip())
    root = _escalation_root()
    for name, assignee_keys in (
        ("dispatch-policy.json", ("assignee",)),
        ("config.json", ("target_assignee",)),
    ):
        try:
            with open(os.path.join(root, name), "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except Exception:
            continue
        if not isinstance(blob, dict):
            continue
        for key in assignee_keys:
            value = blob.get(key)
            if isinstance(value, str) and value:
                assignees.add(value)
        for key, bucket in (
            ("title_prefix", titles),
            ("created_by_prefix", creators),
        ):
            value = blob.get(key)
            if isinstance(value, str) and value:
                bucket.add(value)
    return assignees, tuple(sorted(titles)), tuple(sorted(creators))


def _row_field(row, name):
    try:
        return row[name]
    except Exception:
        return None


def _is_escalation_candidate(row, effective_assignee=None) -> bool:
    """True when this row belongs to the escalation route.

    A row is a candidate if its assignee (as stored, or as the dispatcher
    would auto-assign it) is governed, or if the card carries the escalation
    gate's shape. Shape matters because an unassigned card auto-assigned to
    the escalation target, or a card impersonating a gate-issued title, must
    not be able to slip into the general allow-list lane.
    """
    assignees, title_prefixes, creator_prefixes = _escalation_identity()
    for candidate_assignee in (_row_field(row, "assignee"), effective_assignee):
        if candidate_assignee and candidate_assignee in assignees:
            return True
    for field_name, prefixes in (
        ("title", title_prefixes),
        ("created_by", creator_prefixes),
    ):
        value = _row_field(row, field_name)
        if value and any(str(value).startswith(p) for p in prefixes if p):
            return True
    return False


def _escalation_dispatch_decision(
    conn, row, effective_assignee=None
) -> Optional[tuple[bool, str]]:
    """Return None when this row is not escalation-governed at all.

    Otherwise return (dispatchable, reason) — the caller must honour it and
    must NOT fall through to the general allow-list. The card is read back
    through the dispatcher's OWN board connection, so the token is always
    checked against the board actually being dispatched.
    """
    if not escalation_route_installed():
        # Opt-in: the control is not installed here, so nothing is governed.
        return None
    candidate = False
    try:
        candidate = _is_escalation_candidate(row, effective_assignee)
    except Exception:
        # Identity resolution is pure local file/dict reads; if even that
        # fails the safest reading is "we do not know", and the static
        # fallback below re-checks the assignee without touching disk.
        stored = _row_field(row, "assignee")
        candidate = (stored in _ESCALATION_FALLBACK_ASSIGNEES) or (
            effective_assignee in _ESCALATION_FALLBACK_ASSIGNEES
        )
    try:
        import sys as _sys
        esc_root = _escalation_root()
        if esc_root not in _sys.path:
            _sys.path.insert(0, esc_root)
        try:
            import lib_dispatch_token as _dt
        except Exception:
            # Route library absent/broken. If the row is not a candidate the
            # route simply is not installed for it -> ordinary dispatch. If it
            # IS a candidate we cannot verify anything -> refuse.
            return (False, "escalation_lib_unavailable") if candidate else None
        policy = _dt.load_policy()
        if policy is None:
            # Missing, unreadable, malformed, group/world-writable, or
            # explicitly disabled policy. Never a licence to fall through.
            return (False, "policy_unusable") if candidate else None
        if not candidate and _row_field(row, "assignee") != policy.get("assignee"):
            return None
        # From here the row IS escalation-governed: it must prove itself.
        card = conn.execute(
            "SELECT id, title, assignee, created_by, created_at FROM tasks WHERE id = ?",
            (row["id"],),
        ).fetchone()
        if not card:
            return (False, "card_not_readable")
        return _dt.verify_card(
            policy, card[0], card[1], card[2], card[3], card[4]
        )
    except Exception as exc:  # noqa: BLE001
        # Unknown state around a security control => refuse to auto-spawn any
        # row we have reason to believe is escalation-governed. Rows that are
        # provably not candidates keep the pre-existing allow-list behaviour.
        if candidate:
            return (False, "escalation_check_error:%s" % type(exc).__name__)
        return None


# --- general assignee allow-list -------------------------------------------

def normalize_general_assignees(
    automatic_general_assignees: Optional[Iterable[str]],
) -> Optional[set[str]]:
    """``kanban.automatic_general_assignees`` as a set, or ``None`` when unset.

    ``None`` means "no allow-list configured" and preserves the pre-existing
    dispatch-everything behaviour; an empty/blank list normalizes to an empty
    SET, which denies every non-escalation auto-spawn. That distinction is the
    whole control, so it must not be collapsed to a falsy check.
    """
    if automatic_general_assignees is None:
        return None
    return {
        str(name).strip()
        for name in automatic_general_assignees
        if str(name).strip()
    }


def escalation_dispatch_decision(conn, row, effective_assignee=None):
    """Public name for the fail-closed gate (see module docstring)."""
    return _escalation_dispatch_decision(conn, row, effective_assignee)


def may_auto_spawn(conn, row, effective_assignee, allowed: Optional[set[str]]):
    """``(ok, reason)`` for one ready row — the single decision both dispatch
    loops call.

    Escalation wins outright: a governed row NEVER falls through to the
    allow-list, in either direction. Only a row the gate declines to govern
    (``None``) is measured against ``allowed``.
    """
    decision = _escalation_dispatch_decision(conn, row, effective_assignee)
    if decision is not None:
        ok, reason = decision
        if not ok:
            _log.info(
                "kanban dispatch: escalation card %s not dispatchable (%s)",
                _row_field(row, "id"), reason,
            )
        return ok, reason
    if allowed is not None and effective_assignee not in allowed:
        return False, "assignee_not_in_general_allowlist"
    return True, "ok"
