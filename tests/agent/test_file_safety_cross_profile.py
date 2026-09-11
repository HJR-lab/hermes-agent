"""Tests for profile-aware read safety in agent/file_safety."""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers — set up a fake Hermes root with two profiles, monkeypatch the
# resolver helpers so the classifier sees the test layout.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_hermes(tmp_path, monkeypatch):
    """Build a fake Hermes layout:

        <tmp>/
          skills/foo/SKILL.md           # default profile
          plugins/foo/__init__.py
          cron/<state>
          memories/MEMORY.md
          profiles/
            hermes-security/
              skills/foo/SKILL.md       # named profile
              plugins/...
            coder/
              skills/foo/SKILL.md       # another named profile
    """
    root = tmp_path / "fake-hermes"
    (root / "skills" / "foo").mkdir(parents=True)
    (root / "skills" / "foo" / "SKILL.md").write_text("# default skill\n")
    (root / "plugins" / "foo").mkdir(parents=True)
    (root / "memories").mkdir(parents=True)
    (root / "memories" / "USER.md").write_text("private default memory\n")
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "session.jsonl").write_text("private default session\n")
    (root / "cron").mkdir(parents=True)
    for database in ("state.db", "memory_store.db", "retaindb_queue.db"):
        (root / database).write_bytes(b"private database")
    (root / "escalation" / "keys").mkdir(parents=True)
    (root / "escalation" / "keys" / "hermes-security.key").write_bytes(b"key")
    (root / "escalation" / "keys" / "coder.key").write_bytes(b"key")

    sec_home = root / "profiles" / "hermes-security"
    (sec_home / "skills" / "foo").mkdir(parents=True)
    (sec_home / "skills" / "foo" / "SKILL.md").write_text("# sec skill\n")
    (sec_home / "plugins").mkdir(parents=True)
    (sec_home / "memories").mkdir(parents=True)
    (sec_home / "memories" / "USER.md").write_text("private sec memory\n")

    coder_home = root / "profiles" / "coder"
    (coder_home / "skills" / "foo").mkdir(parents=True)
    (coder_home / "skills" / "foo" / "SKILL.md").write_text("# coder skill\n")
    (coder_home / "memories").mkdir(parents=True)
    (coder_home / "memories" / "USER.md").write_text("private coder memory\n")

    # Monkeypatch the resolver functions used by file_safety so each test
    # can choose which profile is "active".
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)

    import agent.file_safety as fs
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: root)

    return {
        "root": root,
        "default_home": root,
        "security_home": sec_home,
        "coder_home": coder_home,
    }


def _set_active_home(monkeypatch, hermes_home: Path):
    """Point file_safety._hermes_home_path at a specific profile dir."""
    import agent.file_safety as fs
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: hermes_home)


# ---------------------------------------------------------------------------
# _resolve_active_profile_name
# ---------------------------------------------------------------------------


class TestResolveActiveProfileName:
    def test_default_when_home_is_root(self, fake_hermes, monkeypatch):
        _set_active_home(monkeypatch, fake_hermes["default_home"])
        from agent.file_safety import _resolve_active_profile_name
        assert _resolve_active_profile_name() == "default"


    def test_falls_back_to_default_on_resolution_failure(self, fake_hermes, monkeypatch):
        """If HERMES_HOME resolution raises, return 'default' rather than crashing the tool."""
        import agent.file_safety as fs

        def _boom():
            raise RuntimeError("simulated")

        monkeypatch.setattr(fs, "_hermes_home_path", _boom)
        # Should not raise — falls back to "default"
        assert fs._resolve_active_profile_name() == "default"


class TestProfileReadBoundaries:
    def test_named_profile_cannot_read_sibling_profile(self, fake_hermes, monkeypatch):
        _set_active_home(monkeypatch, fake_hermes["security_home"])
        from agent.file_safety import get_read_block_error

        target = fake_hermes["coder_home"] / "memories" / "USER.md"
        error = get_read_block_error(str(target))
        assert error and "another Hermes profile" in error

    @pytest.mark.parametrize(
        "relative_path",
        ("memories/USER.md", "sessions/session.jsonl", "state.db", "memory_store.db", "retaindb_queue.db"),
    )
    def test_named_profile_cannot_read_default_private_state(
        self, fake_hermes, monkeypatch, relative_path
    ):
        _set_active_home(monkeypatch, fake_hermes["security_home"])
        from agent.file_safety import get_read_block_error

        error = get_read_block_error(str(fake_hermes["root"] / relative_path))
        assert error and "default Hermes profile" in error

    def test_named_profile_cannot_read_default_state_sqlite_sidecar(
        self, fake_hermes, monkeypatch
    ):
        _set_active_home(monkeypatch, fake_hermes["security_home"])
        from agent.file_safety import get_read_block_error

        target = fake_hermes["root"] / "state.db-wal"
        assert get_read_block_error(str(target)) is not None

    def test_default_profile_cannot_read_named_profile(self, fake_hermes, monkeypatch):
        _set_active_home(monkeypatch, fake_hermes["default_home"])
        from agent.file_safety import get_read_block_error

        target = fake_hermes["security_home"] / "memories" / "USER.md"
        error = get_read_block_error(str(target))
        assert error and "named Hermes profile" in error

    @pytest.mark.parametrize("key_owner", ("hermes-security", "coder"))
    def test_escalation_keys_are_never_directly_readable(
        self, fake_hermes, monkeypatch, key_owner
    ):
        _set_active_home(monkeypatch, fake_hermes["security_home"])
        from agent.file_safety import get_read_block_error

        target = fake_hermes["root"] / "escalation" / "keys" / f"{key_owner}.key"
        error = get_read_block_error(str(target))
        assert error and "escalation signing-key" in error

    def test_default_profile_cannot_read_escalation_key(self, fake_hermes, monkeypatch):
        _set_active_home(monkeypatch, fake_hermes["default_home"])
        from agent.file_safety import get_read_block_error

        target = fake_hermes["root"] / "escalation" / "keys" / "coder.key"
        assert get_read_block_error(str(target)) is not None

    def test_traversal_and_symlink_cannot_bypass_sibling_boundary(
        self, fake_hermes, monkeypatch, tmp_path
    ):
        _set_active_home(monkeypatch, fake_hermes["security_home"])
        from agent.file_safety import get_read_block_error

        traversal = (
            fake_hermes["security_home"]
            / "memories"
            / ".."
            / ".."
            / "coder"
            / "memories"
            / "USER.md"
        )
        link = tmp_path / "linked-sibling-memory"
        link.symlink_to(fake_hermes["coder_home"] / "memories" / "USER.md")
        assert get_read_block_error(str(traversal)) is not None
        assert get_read_block_error(str(link)) is not None

    def test_active_profile_own_benign_file_is_allowed(self, fake_hermes, monkeypatch):
        _set_active_home(monkeypatch, fake_hermes["security_home"])
        from agent.file_safety import get_read_block_error

        target = fake_hermes["security_home"] / "skills" / "foo" / "SKILL.md"
        assert get_read_block_error(str(target)) is None

    def test_ordinary_workspace_file_is_allowed(self, fake_hermes, monkeypatch, tmp_path):
        _set_active_home(monkeypatch, fake_hermes["security_home"])
        from agent.file_safety import get_read_block_error

        target = tmp_path / "workspace" / "README.md"
        target.parent.mkdir()
        target.write_text("ordinary repo file\n")
        assert get_read_block_error(str(target)) is None
