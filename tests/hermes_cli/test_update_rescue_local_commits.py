"""`hermes update` must never destroy local-only commits (t_05f9f4be).

A checkout that merely carries unpushed commits looks identical, to the reconcile
path, to a genuine upstream force-push: same branch, diverged, common ancestor
present. The old code reset --hard in both cases, which left the local commits
reachable only from the reflog -- unreachable from any ref and on git's gc clock.
That cost real work here more than once, including commits that were fully
committed, so "just commit your work" is not a fix.

These tests run against throwaway repos in tmp_path; they never touch a real
checkout, which the card requires explicitly.
"""
import subprocess

import pytest

from hermes_cli.update_cmd import _rescue_local_only_commits


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    """A repo on `main` with an `origin` that has moved on by one commit."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-qm", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")

    up = tmp_path / "up"
    subprocess.run(["git", "clone", "-q", str(origin), str(up)], check=True)
    _git(up, "config", "user.email", "t@t")
    _git(up, "config", "user.name", "t")
    (up / "f.txt").write_text("base\nupstream\n")
    _git(up, "commit", "-qam", "upstream commit")
    _git(up, "push", "-q", "origin", "main")

    _git(work, "fetch", "-q", "origin")
    return work


def _git_cmd(repo):
    return ["git", "-C", str(repo)]


def test_local_only_commit_survives_the_reset(repo, capsys):
    """The regression: a local commit ahead of origin/main must outlive reset --hard."""
    (repo / "localfix.txt").write_text("must not be lost\n")
    _git(repo, "add", "localfix.txt")
    _git(repo, "commit", "-qm", "local fix that must survive")
    doomed = _git(repo, "rev-parse", "HEAD")

    rescue = _rescue_local_only_commits(_git_cmd(repo), "main")

    assert rescue, "a rescue branch must be created when local commits exist"
    assert rescue.startswith("hermes-update-rescue/main-")

    # The whole point: reachable from a REF afterwards, not just the reflog.
    _git(repo, "reset", "--hard", "origin/main")
    assert not (repo / "localfix.txt").exists(), "sanity: the reset really is destructive"
    assert _git(repo, "rev-parse", rescue) == doomed
    assert rescue in _git(repo, "branch", "--list", rescue)
    assert "local fix that must survive" in _git(repo, "log", "-1", "--format=%s", rescue)

    out = capsys.readouterr().out
    assert "1 local commit" in out, "the operator must SEE that local work existed"
    assert "local fix that must survive" in out, "and which commit it was"
    assert rescue in out


def test_behind_only_is_a_silent_no_op(repo, capsys):
    """The common path -- a checkout that is merely behind -- must not be touched or narrated."""
    assert _git(repo, "rev-list", "--count", "origin/main..HEAD") == "0"

    assert _rescue_local_only_commits(_git_cmd(repo), "main") is None
    assert capsys.readouterr().out == ""
    assert _git(repo, "branch", "--list", "hermes-update-rescue/*") == ""


def test_branch_creation_failure_never_claims_a_rescue(repo, capsys, monkeypatch):
    """If the rescue branch cannot be written, say so -- never report a backup that isn't there."""
    (repo / "localfix.txt").write_text("x\n")
    _git(repo, "add", "localfix.txt")
    _git(repo, "commit", "-qm", "local fix")

    import hermes_cli.update_cmd as mod

    real = mod._git_run

    def fail_branch(git_cmd, args, *a, **kw):
        if args and args[0] == "branch":
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(git_cmd, args, *a, **kw)

    monkeypatch.setattr(mod, "_git_run", fail_branch)

    assert _rescue_local_only_commits(_git_cmd(repo), "main") is None
    out = capsys.readouterr().out
    assert "Could not create rescue branch" in out
    assert "reflog-only" in out
    assert "git branch <name>" in out, "must hand the operator a recovery command"


def test_unknown_branch_does_not_raise(repo):
    """A git failure in the rescue path must never break the update itself."""
    assert _rescue_local_only_commits(_git_cmd(repo), "no-such-branch") is None
