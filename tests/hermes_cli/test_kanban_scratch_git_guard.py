"""A 'scratch' workspace that is really a git checkout must not be rmtree'd with work in it.

Incident 2026-09-14: the auto-decomposer made five doe-app cards share one folder under
``kanban/workspaces/`` recorded as ``workspace_kind='scratch'`` while it was a git worktree.
``_cleanup_workspace`` protects ``worktree`` workspaces (dirty or unpushed trees are
preserved) but removed ``scratch`` ones with a bare ``shutil.rmtree`` — so when the last
child card completed and released the deferred parent cleanup, ~3,200 uncommitted lines
were deleted. A scratch folder that holds a git checkout now gets the same guard: any
uncommitted change, unpushed commit, or undeterminable git state preserves it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo whose history is fully pushed to a local bare remote."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _scratch_git_workspace(conn, repo: Path, title: str) -> tuple[str, Path]:
    """A task whose managed scratch folder is a linked git worktree of *repo*."""
    task_id = kb.create_task(conn, title=title)
    ws = kbw.resolve_workspace(kb.get_task(conn, task_id))
    if ws.exists():
        ws.rmdir()
    kbw._ensure_git_worktree(repo, ws, f"wt/{task_id}")
    kbw.set_workspace_path(conn, task_id, ws)
    assert kb.get_task(conn, task_id).workspace_kind == "scratch"
    assert kbw._is_managed_scratch_path(ws)
    return task_id, ws


def test_dirty_git_scratch_workspace_preserved_on_completion(kanban_home, repo):
    with kbc.connect() as conn:
        t, ws = _scratch_git_workspace(conn, repo, "integrate ux work")
        (ws / "confirm-dialog.tsx").write_text("uncommitted work\n", encoding="utf-8")
        kb.complete_task(conn, t, result="integrated")
    assert ws.is_dir(), "a scratch folder holding uncommitted git work must not be deleted"
    assert (ws / "confirm-dialog.tsx").exists()


def test_unpushed_git_scratch_workspace_preserved_on_completion(kanban_home, repo):
    with kbc.connect() as conn:
        t, ws = _scratch_git_workspace(conn, repo, "local commits")
        (ws / "work.txt").write_text("committed, never pushed\n", encoding="utf-8")
        _git("-C", str(ws), "add", "work.txt")
        _git("-C", str(ws), "commit", "-m", "local work")
        kb.complete_task(conn, t, result="done")
    assert ws.is_dir(), "a scratch folder with unpushed commits must not be deleted"


def test_clean_pushed_git_scratch_workspace_still_removed(kanban_home, repo):
    with kbc.connect() as conn:
        t, ws = _scratch_git_workspace(conn, repo, "nothing left behind")
        kb.complete_task(conn, t, result="done")
    assert not ws.exists(), "a provably work-free scratch checkout is still reclaimed"


def test_child_completion_does_not_delete_dirty_shared_parent_scratch(kanban_home, repo):
    """The incident shape: parent done first, last child completes, parent sweep fires."""
    with kbc.connect() as conn:
        parent, ws = _scratch_git_workspace(conn, repo, "integration root")
        child = kb.create_task(
            conn, title="child sharing the folder", workspace_kind="scratch",
            workspace_path=str(ws),
        )
        kb.link_tasks(conn, parent, child)
        (ws / "error-boundary.tsx").write_text("uncommitted\n", encoding="utf-8")

        kb.complete_task(conn, parent, result="integrated, not committed")
        assert ws.is_dir(), "deferred while the child is active"

        kb.complete_task(conn, child, result="scanner done")

    assert ws.is_dir(), "the deferred parent sweep must not rmtree dirty shared git work"
    assert (ws / "error-boundary.tsx").exists()
