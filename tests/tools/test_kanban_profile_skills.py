"""Agent-facing skill visibility: kanban_profile_skills.

Third leg of the skill/profile alignment fix (t_bfada73e). The spawn guard
stops a bad ``skills=[...]`` name from killing a worker and create-time
validation refuses the card, but until this tool existed the AUTHOR still had
no way to satisfy the contract ``kanban_create`` states: an agent sees only its
own profile's skill catalog and cannot enumerate the assignee's.

Covered here:
  - happy path, and the resolvable/unresolvable split for named skills
  - an untrusted probe reports "unknown", never an empty catalog
  - an empty catalog is reported as a real answer, distinctly from "unknown"
  - unknown profile is refused (such a card would never dispatch)
  - missing assignee is refused
  - the tool is registered on the kanban toolset
"""
from __future__ import annotations

import json

import pytest

import tools.kanban_tools as kt


@pytest.fixture
def profile_env(monkeypatch):
    """Make every profile lookup deterministic; never touch ~/.hermes."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "normalize_profile_name", lambda n: str(n).strip())
    monkeypatch.setattr(profiles, "profile_exists", lambda n: n in {"deus", "zero"})
    monkeypatch.setattr(profiles, "list_profile_names", lambda: ["deus", "zero"])


def _probe(monkeypatch, value):
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(
        kb, "installed_skills_for_profile", lambda profile, **kw: value
    )


def test_lists_the_assignee_catalog(profile_env, monkeypatch):
    _probe(monkeypatch, {"hermes-agent"})
    out = json.loads(kt._handle_profile_skills({"assignee": "deus"}))
    assert out["ok"] is True
    assert out["profile"] == "deus"
    assert out["status"] == "ok"
    assert out["skills"] == ["hermes-agent"]
    assert out["count"] == 1


def test_splits_requested_names(profile_env, monkeypatch):
    """The check an author actually needs before calling kanban_create."""
    _probe(monkeypatch, {"hermes-agent"})
    out = json.loads(kt._handle_profile_skills({
        "assignee": "deus",
        "skills": ["hermes-agent", "definitely-not-real"],
    }))
    assert out["resolvable"] == ["hermes-agent"]
    assert out["unresolvable"] == ["definitely-not-real"]
    assert "REJECT" in out["note"]


def test_all_requested_names_resolve(profile_env, monkeypatch):
    _probe(monkeypatch, {"hermes-agent", "translation"})
    out = json.loads(kt._handle_profile_skills({
        "assignee": "deus",
        "skills": ["translation"],
    }))
    assert out["resolvable"] == ["translation"]
    assert out["unresolvable"] == []
    assert "note" not in out


def test_untrusted_probe_reports_unknown_not_empty(profile_env, monkeypatch):
    """A failed probe must never read as 'this profile has no skills'."""
    _probe(monkeypatch, None)
    out = json.loads(kt._handle_profile_skills({"assignee": "deus"}))
    assert out["ok"] is True
    assert out["status"] == "unknown"
    assert out["skills"] is None
    assert out["count"] is None
    assert "fails open" in out["note"]


def test_empty_catalog_is_a_real_answer(profile_env, monkeypatch):
    _probe(monkeypatch, set())
    out = json.loads(kt._handle_profile_skills({"assignee": "deus"}))
    assert out["status"] == "empty"
    assert out["skills"] == []
    assert out["count"] == 0
    assert "no enabled skills" in out["note"]


def test_unknown_profile_is_refused(profile_env, monkeypatch):
    _probe(monkeypatch, {"hermes-agent"})
    out = json.loads(kt._handle_profile_skills({"assignee": "ghost"}))
    assert out.get("ok") is not True
    text = json.dumps(out)
    assert "ghost" in text
    # The known set is named so the author can correct the assignee.
    assert "deus" in text and "zero" in text


def test_missing_assignee_is_refused(profile_env):
    out = json.loads(kt._handle_profile_skills({}))
    assert out.get("ok") is not True
    assert "assignee is required" in json.dumps(out)


def test_probe_exception_is_reported_not_raised(profile_env, monkeypatch):
    from hermes_cli import kanban_db as kb

    def _boom(profile, **kw):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(kb, "installed_skills_for_profile", _boom)
    out = json.loads(kt._handle_profile_skills({"assignee": "deus"}))
    assert out.get("ok") is not True
    assert "probe exploded" in json.dumps(out)


def test_tool_is_registered_on_the_kanban_toolset():
    from tools.registry import registry

    spec = registry.get_entry("kanban_profile_skills")
    assert spec is not None, "kanban_profile_skills is not registered"
    assert spec.toolset == "kanban"
    schema = kt.KANBAN_PROFILE_SKILLS_SCHEMA
    assert schema["parameters"]["required"] == ["assignee"]
    assert "skills" in schema["parameters"]["properties"]


def test_create_schema_points_authors_at_the_checker():
    """The contract text must name the tool that makes it satisfiable."""
    desc = kt.KANBAN_CREATE_SCHEMA["parameters"]["properties"]["skills"]["description"]
    assert "kanban_profile_skills" in desc


def test_tool_is_exposed_in_a_worker_schema(monkeypatch, tmp_path):
    """Registration is not enough — the name must also be in the toolset
    allowlists in toolsets.py, or the model never sees the tool."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema_check")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # noqa: F401 — ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    assert "kanban_profile_skills" in names
