"""`hermes update` must never destroy local commits without leaving a ref.

`_reconcile_diverged_checkout` already merged instead of resetting on a CUSTOM
branch, and already wrote a rescue ref for ORPHAN divergence (no common
ancestor). It missed the ordinary case: you commit to `main`, so `main` is
simply AHEAD of `origin/main`. A common ancestor exists, the old guard stayed
silent, and `reset --hard origin/main` sent every local commit to the reflog
only — unreachable from any ref and on git's gc clock.

That is not hypothetical. It destroyed four committed kanban fixes (recovered
2026-09-08, kanban t_acefc80c) and, separately, the in-repo half of the DOE
escalation guard and the routing allow-list. Those two were uncommitted, so
there was nothing to recover — and because their config lives OUTSIDE the repo
and survived, both controls read as active while enforcing nothing for days
(t_919cc864, t_983e8288).

These tests drive real git repos rather than mocks: the bug was in what git
actually does to refs, so mocking git would have proved nothing.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli import update_cmd


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def _commit(repo, name, body="x"):
    (repo / name).write_text(body)
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def diverged(tmp_path):
    """An upstream that force-pushed, and a local clone with its own commit on
    main — the shape that used to lose work silently."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "base")

    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(upstream), str(local)], check=True)

    # Local work committed on main.
    local_sha = _commit(local, "my-local-fix", "the work that keeps disappearing")

    # Upstream rewrites history so a fast-forward is impossible.
    _commit(upstream, "upstream-rewrite")
    _git(upstream, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--amend", "--no-edit", "-m", "upstream-rewrite-amended")
    _git(local, "fetch", "-q", "origin")
    return local, local_sha


def _rescue_refs(repo):
    out = _git(repo, "for-each-ref", "--format=%(refname)",
               "refs/hermes-update-backups/").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _reconcile(repo, capsys):
    """Run the real reconcile against ``repo``, returning its printed output."""
    git_cmd = ["git", "-C", str(repo)]
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()

    def _run(cmd, args, cwd=None):
        return subprocess.run([*cmd, *args], capture_output=True, text=True)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(update_cmd, "_git_run", _run)
        update_cmd._reconcile_diverged_checkout(git_cmd, "main", pre)
    finally:
        monkey.undo()
    return capsys.readouterr().out


def test_local_commits_are_preserved_behind_a_ref(diverged, capsys):
    """The regression: a local commit on main must survive the reset, reachable
    from a real ref — not just the reflog."""
    repo, local_sha = diverged

    out = _reconcile(repo, capsys)

    refs = _rescue_refs(repo)
    assert refs, "no rescue ref written — the local commit is reflog-only and will be gc'd"
    assert any("ahead-main-" in r for r in refs), refs

    # The commit must be genuinely reachable from that ref.
    reachable = _git(repo, "merge-base", "--is-ancestor", local_sha, refs[0], check=False)
    assert reachable.returncode == 0, "rescue ref does not contain the local commit"


def test_the_operator_is_told_what_was_at_risk(diverged, capsys):
    """A silent backup is barely better than no backup: this failure went
    unnoticed for days precisely because nothing said anything."""
    repo, _ = diverged
    out = _reconcile(repo, capsys)
    assert "local commit(s)" in out
    assert "would be destroyed" in out
    assert "refs/hermes-update-backups/ahead-main-" in out


def test_reset_still_happens(diverged, capsys):
    """Preserving work must not block updating — the checkout still ends up on
    origin/main."""
    repo, _ = diverged
    _reconcile(repo, capsys)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    origin = _git(repo, "rev-parse", "origin/main").stdout.strip()
    assert head == origin


def test_no_rescue_ref_when_there_is_nothing_to_lose(tmp_path, capsys):
    """No local commits ahead: don't litter refs on an ordinary forced update."""
    upstream = tmp_path / "up"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "base")
    local = tmp_path / "loc"
    subprocess.run(["git", "clone", "-q", str(upstream), str(local)], check=True)
    _commit(upstream, "next")
    _git(upstream, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--amend", "--no-edit", "-m", "next-amended")
    _git(local, "fetch", "-q", "origin")

    _reconcile(local, capsys)
    assert not [r for r in _rescue_refs(local) if "ahead-" in r]
