"""Spawn-time guard: an unresolvable --skills name must never kill a worker.

Regression cover for incident 2026-09-06 (t_1891af08). Cards carrying skill
names that are not installed on the assignee's profile made the worker exit 1
about a second into bootstrap, with the reason visible only in the per-task
worker log. Ten cards sat blocked this way, four of them for three days,
presenting as an unexplained cross-profile "spawn failure".
"""
import subprocess
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb


def _spawn_argv(monkeypatch, tmp_path, skills, resolvable):
    """Run _default_spawn with the resolver stubbed; return the argv used."""
    task = mock.Mock()
    task.id = "t_guard01"
    task.assignee = "deus"
    task.skills = skills
    task.tenant = None
    task.branch_name = None
    task.current_run_id = None
    task.claim_lock = None
    task.goal_mode = False
    task.goal_max_turns = None
    task.max_runtime_seconds = None
    task.model_override = None
    task.provider_override = None
    task.reasoning_effort = None

    keep = [s for s in skills if s in resolvable]
    drop = [s for s in skills if s not in resolvable]
    monkeypatch.setattr(
        kb, "resolvable_skills_for_profile", lambda p, n, **kw: (keep, drop)
    )
    # Never touch the real board from a spawn test.
    monkeypatch.setattr(kb, "add_comment", lambda *a, **k: 1)
    monkeypatch.setattr(kb, "connect", lambda *a, **k: mock.MagicMock())

    captured = {}

    class _Proc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    kb._default_spawn(task, str(tmp_path))
    return captured["cmd"]


def test_unresolvable_skill_is_stripped_not_fatal(monkeypatch, tmp_path):
    """The whole point: a bad name is dropped, the worker still launches."""
    argv = _spawn_argv(
        monkeypatch, tmp_path,
        skills=["consumer-product-ui-redesign", "html-artifacts"],
        resolvable=set(),
    )
    assert "--skills" not in argv, (
        "unresolvable skills must not reach the worker — they kill it at bootstrap"
    )
    # It still spawns a real worker for the task.
    assert "chat" in argv and "-q" in argv


def test_resolvable_skills_survive(monkeypatch, tmp_path):
    argv = _spawn_argv(
        monkeypatch, tmp_path,
        skills=["mlops-model-ops", "hermes-agent"],
        resolvable={"hermes-agent"},
    )
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--skills"]
    assert pairs == ["hermes-agent"]
    assert "mlops-model-ops" not in argv


def test_all_resolvable_is_unchanged_behaviour(monkeypatch, tmp_path):
    argv = _spawn_argv(
        monkeypatch, tmp_path,
        skills=["hermes-agent"],
        resolvable={"hermes-agent"},
    )
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--skills"]
    assert pairs == ["hermes-agent"]


def test_probe_fails_open(monkeypatch):
    """A broken probe must not strip skills off an otherwise healthy worker."""
    def _boom(*a, **k):
        raise OSError("probe unavailable")
    monkeypatch.setattr(subprocess, "run", _boom)
    keep, drop = kb.resolvable_skills_for_profile("deus", ["anything"])
    assert keep == ["anything"] and drop == []


def test_probe_empty_parse_fails_open(monkeypatch):
    """Parsing nothing means probe failure, not 'profile has zero skills'."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: mock.Mock(returncode=0, stdout="", stderr=""),
    )
    keep, drop = kb.resolvable_skills_for_profile("deus", ["x", "y"])
    assert keep == ["x", "y"] and drop == []


def test_probe_nonzero_exit_fails_open(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: mock.Mock(returncode=2, stdout="", stderr="nope"),
    )
    keep, drop = kb.resolvable_skills_for_profile("deus", ["x"])
    assert keep == ["x"] and drop == []


def test_probe_parses_table_and_splits(monkeypatch):
    table = (
        "┏━━━┓\n"
        "┃ Name ┃ Category ┃\n"
        "│ hermes-agent │ autonomous-ai-agents │\n"
        "│ html-artifacts │ creative │\n"
        "└───┘\n"
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: mock.Mock(returncode=0, stdout=table, stderr=""),
    )
    keep, drop = kb.resolvable_skills_for_profile(
        "deus", ["html-artifacts", "not-installed"]
    )
    assert keep == ["html-artifacts"]
    assert drop == ["not-installed"]


def test_no_skills_is_noop(monkeypatch):
    called = []
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: called.append(1)
    )
    assert kb.resolvable_skills_for_profile("deus", []) == ([], [])
    assert not called, "must not shell out when the card requests no skills"
