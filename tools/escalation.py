"""``ever_escalate`` — the single bounded escalation path from a domain profile.

Rebuilt 2026-09-08 (kanban t_919cc864). The original was NEVER COMMITTED — it
was an untracked file in this repo, and a ``git reset --hard origin/main``
during a ``hermes update`` destroyed it. It is in no git object, so this is a
reconstruction from ``~/.hermes/escalation/README.md`` §"the tool" and
``REPAIR-RECEIPT-t_0a901ae7.md``, against the gate's actual argv contract.

The profile that owns this tool (``doe-domain``) has had ``terminal`` removed
and its ``kanban_*`` schemas gated off. This tool is what it has instead: it can
file ONE escalation to a fixed recipient, and nothing else.

Containment properties, all enforced here rather than trusted:

* The filing identity is derived from the RUNTIME PROFILE, never from a model
  argument. There is no ``caller`` parameter — the model cannot spoof one.
* Only ``ALLOWED_PAYLOAD_KEYS`` are constructible. There is deliberately no
  parameter for ``assignee``, ``priority``, ``command``, ``parents``, ``model``,
  ``board`` or any other routing field. The gate rejects them independently as
  ``FORBIDDEN_FIELD``; this schema simply never offers them.
* The recipient is fixed by the gate's own config, not by the caller.
* The gate runs as an argv list with ``shell=False``. No shell string is ever
  constructed from model output.
* ``check_fn`` returns True only when the ACTIVE profile is a registered caller
  in the gate config, so the tool is profile-scoped by construction and is not
  offered to ``default``/``deus``/anyone else.

This file is committed. That is the point: the control it provides was already
lost once by living outside version control.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Optional

from tools.registry import registry

# Mirrors the gate's own ALLOWED_PAYLOAD_KEYS / REQUIRED_PAYLOAD_KEYS. The gate
# re-validates independently — this is the narrow surface, not the security
# boundary.
_SEVERITIES = ("low", "medium", "high", "critical")
_GATE_TIMEOUT_SECONDS = 60


def _escalation_root() -> str:
    """Route root, honouring ``HERMES_HOME`` before ``~`` so an alternate home
    never inherits the real user's gate."""
    explicit = os.environ.get("HERMES_ESCALATION_ROOT")
    if explicit:
        return explicit
    home = os.environ.get("HERMES_HOME")
    if home:
        return os.path.join(home, "escalation")
    return os.path.join(os.path.expanduser("~"), ".hermes", "escalation")


def _gate_config() -> dict:
    """The gate's ``config.json``, or ``{}`` when unreadable.

    Read with no schema checks on purpose: this answers "is this profile a
    registered caller?", i.e. whether to OFFER the tool. Whether a submission is
    permitted is decided by the gate itself, which does verify.
    """
    try:
        with open(os.path.join(_escalation_root(), "config.json"), "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except Exception:
        return {}
    return blob if isinstance(blob, dict) else {}


def _active_profile() -> Optional[str]:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception:
        return None


def _check_escalation_caller(*_a: Any, **_kw: Any) -> bool:
    """Offer the tool only to a profile registered as an authorized caller.

    Fail CLOSED: an unreadable config, an unknown profile, or any error means
    the tool is not offered.
    """
    profile = _active_profile()
    if not profile:
        return False
    callers = _gate_config().get("authorized_callers")
    if not isinstance(callers, dict):
        return False
    return profile in callers


_SCHEMA = {
    "name": "ever_escalate",
    "description": (
        "File an escalation to EVER. Use this when something needs a decision, "
        "authority, or action outside your own scope. The recipient is fixed; "
        "you cannot choose who receives it, assign it to anyone, set its "
        "priority, or attach a command — describe the situation and what you "
        "need, and the gate handles routing and attribution."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "One-line summary of what needs attention.",
            },
            "summary": {
                "type": "string",
                "description": (
                    "What happened, what you already checked, and why it needs "
                    "someone else. Write it for a reader with no context."
                ),
            },
            "severity": {
                "type": "string",
                "enum": list(_SEVERITIES),
                "description": (
                    "How urgent this is. Drives priority at the recipient; you "
                    "cannot set priority directly."
                ),
            },
            "marker": {
                "type": "string",
                "description": (
                    "Short stable identifier for this escalation, so a repeat "
                    "of the same issue is recognisable. "
                    "Letters, digits, dot, colon, underscore, hyphen; max 64."
                ),
            },
            "evidence": {
                "type": "string",
                "description": "Optional. Concrete supporting detail — figures, log excerpts, ids.",
            },
            "requested_action": {
                "type": "string",
                "description": "Optional. The specific thing you are asking for.",
            },
            "source_ref": {
                "type": "string",
                "description": "Optional. Where this came from — a card id, invoice number, thread.",
            },
        },
        "required": ["title", "summary", "severity", "marker"],
        "additionalProperties": False,
    },
}


def _handle_ever_escalate(args: dict, **_kw: Any) -> str:
    profile = _active_profile()
    if not profile:
        return "Escalation refused: the active profile could not be determined."

    payload = {
        key: args[key]
        for key in ("title", "summary", "severity", "marker",
                    "evidence", "requested_action", "source_ref")
        if isinstance(args.get(key), str) and args.get(key).strip()
    }
    missing = [k for k in ("title", "summary", "severity", "marker") if k not in payload]
    if missing:
        return f"Escalation refused: missing required field(s): {', '.join(missing)}."
    if payload["severity"] not in _SEVERITIES:
        return (
            f"Escalation refused: severity must be one of {', '.join(_SEVERITIES)}, "
            f"got {payload['severity']!r}."
        )

    gate = os.path.join(_escalation_root(), "bin", "hermes-escalate")
    if not os.path.exists(gate):
        return "Escalation refused: the escalation gate is not installed on this machine."

    # argv list, shell=False. --caller comes from the runtime profile, never
    # from the model. The payload goes over stdin so it never touches a
    # command line.
    argv = [gate, "submit", "--caller", profile, "--payload-file", "-"]
    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            shell=False,
            timeout=_GATE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"Escalation gate timed out after {_GATE_TIMEOUT_SECONDS}s. Nothing was filed."
    except Exception as exc:  # noqa: BLE001
        return f"Escalation gate could not be run ({type(exc).__name__}). Nothing was filed."

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        # The gate's rejection codes (FORBIDDEN_FIELD, BAD_SIGNATURE,
        # STALE_ENVELOPE, UNAUTHORIZED_CALLER, rate limits) are the useful part
        # — surface them rather than a generic failure.
        return f"Escalation rejected by the gate: {err or out or f'exit {proc.returncode}'}"
    return out or "Escalation filed."


registry.register(
    name="ever_escalate",
    toolset="escalation",
    schema=_SCHEMA,
    handler=_handle_ever_escalate,
    check_fn=_check_escalation_caller,
    emoji="🚨",
    description="File a bounded escalation to EVER through the signed gate.",
)
