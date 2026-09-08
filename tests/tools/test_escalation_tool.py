"""``ever_escalate`` — the one tool a domain profile has instead of terminal/kanban.

Rebuilt from the route's README + repair receipt after the original untracked
file was destroyed by a ``git reset --hard origin/main`` (kanban t_919cc864).
These tests pin the containment properties the README claims, so a rebuild or
refactor cannot quietly loosen them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import escalation as esc_tool


@pytest.fixture
def gate_root(tmp_path, monkeypatch):
    """An installed gate with doe-domain as the only authorized caller."""
    root = tmp_path / "escalation"
    (root / "bin").mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({
        "version": 1,
        "target_assignee": "default",
        "authorized_callers": {"doe-domain": {"key_file": "keys/doe-domain.key"}},
    }))
    gate = root / "bin" / "hermes-escalate"
    gate.write_text("#!/bin/sh\nexit 0\n")
    gate.chmod(0o700)
    monkeypatch.setenv("HERMES_ESCALATION_ROOT", str(root))
    return root


def _as_profile(monkeypatch, name):
    monkeypatch.setattr(esc_tool, "_active_profile", lambda: name)


# --- the tool is profile-scoped by construction ----------------------------

def test_offered_only_to_a_registered_caller(gate_root, monkeypatch):
    _as_profile(monkeypatch, "doe-domain")
    assert esc_tool._check_escalation_caller() is True


@pytest.mark.parametrize("profile", ["default", "deus", "ves", None])
def test_not_offered_to_anyone_else(gate_root, monkeypatch, profile):
    """README: 'not offered — check_fn False' for default (EVER) and deus."""
    _as_profile(monkeypatch, profile)
    assert esc_tool._check_escalation_caller() is False


def test_check_fn_fails_closed_without_a_gate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_ESCALATION_ROOT", str(tmp_path / "nothing-here"))
    _as_profile(monkeypatch, "doe-domain")
    assert esc_tool._check_escalation_caller() is False


# --- the model cannot route ------------------------------------------------

def test_schema_offers_no_routing_field():
    """There must be no parameter for assignee/priority/command/etc. The gate
    rejects them as FORBIDDEN_FIELD; the schema should never offer them."""
    props = set(esc_tool._SCHEMA["input_schema"]["properties"])
    forbidden = {
        "assignee", "profile", "target", "target_assignee", "dispatch", "worker",
        "specialist", "parents", "skills", "model", "provider", "workspace",
        "command", "cmd", "priority", "board", "created_by", "caller",
    }
    assert props & forbidden == set()
    assert esc_tool._SCHEMA["input_schema"]["additionalProperties"] is False


def test_caller_comes_from_the_runtime_profile_not_from_args(gate_root, monkeypatch):
    """The filing identity must be unspoofable from model-supplied arguments."""
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        class R:
            returncode = 0
            stdout = "filed as t_abc123"
            stderr = ""
        return R()

    _as_profile(monkeypatch, "doe-domain")
    monkeypatch.setattr(esc_tool.subprocess, "run", fake_run)

    out = esc_tool._handle_ever_escalate({
        "title": "t", "summary": "s", "severity": "high", "marker": "m",
        # all of these must be dropped, not forwarded
        "caller": "default", "assignee": "deus", "priority": 999, "command": "rm -rf /",
    })

    argv = seen["argv"]
    assert argv[argv.index("--caller") + 1] == "doe-domain", "caller must be the runtime profile"
    assert seen["kw"]["shell"] is False, "the gate must never run through a shell"
    payload = json.loads(seen["kw"]["input"])
    assert set(payload) == {"title", "summary", "severity", "marker"}
    for bad in ("caller", "assignee", "priority", "command"):
        assert bad not in payload
    assert "filed" in out


def test_gate_rejection_is_surfaced_verbatim(gate_root, monkeypatch):
    """FORBIDDEN_FIELD / BAD_SIGNATURE / rate limits are the useful part."""
    class R:
        returncode = 3
        stdout = ""
        stderr = "FORBIDDEN_FIELD: assignee"

    _as_profile(monkeypatch, "doe-domain")
    monkeypatch.setattr(esc_tool.subprocess, "run", lambda *a, **k: R())
    out = esc_tool._handle_ever_escalate(
        {"title": "t", "summary": "s", "severity": "low", "marker": "m"}
    )
    assert "FORBIDDEN_FIELD" in out


def test_missing_gate_binary_files_nothing(tmp_path, monkeypatch):
    root = tmp_path / "escalation"
    root.mkdir()
    monkeypatch.setenv("HERMES_ESCALATION_ROOT", str(root))
    _as_profile(monkeypatch, "doe-domain")
    out = esc_tool._handle_ever_escalate(
        {"title": "t", "summary": "s", "severity": "low", "marker": "m"}
    )
    assert "not installed" in out


def test_bad_severity_is_refused_before_the_gate_runs(gate_root, monkeypatch):
    _as_profile(monkeypatch, "doe-domain")
    monkeypatch.setattr(
        esc_tool.subprocess, "run",
        lambda *a, **k: pytest.fail("gate must not be invoked for an invalid payload"),
    )
    out = esc_tool._handle_ever_escalate(
        {"title": "t", "summary": "s", "severity": "URGENT!!", "marker": "m"}
    )
    assert "severity must be one of" in out


def test_escalation_root_honours_hermes_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_ESCALATION_ROOT", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "alt"))
    assert esc_tool._escalation_root() == str(tmp_path / "alt" / "escalation")
