"""`hermes kanban create --skill` is validated against the ASSIGNEE's catalog.

Regression coverage for the silent-kill path: a `--skill` name resolvable in
the author's profile but not in the assignee's went into the row unchecked,
and only surfaced as `Unknown skill(s): ...` inside the spawned worker's log
about a second into bootstrap. Nothing reached the board, so the card looked
like an unexplained "pid gone". Seven deus/duex cards died that way between
2026-09-03 and 2026-09-06 without ever running.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create(argv: list[str]) -> int:
    """Run `kanban create ...` through the real parser and return its exit code."""
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", *argv])
    return kc._cmd_create(args)


def _tasks() -> list:
    with kb.connect_closing() as conn:
        return list(kb.list_tasks(conn))


def _pin_catalog(monkeypatch, catalog, *, seen: list | None = None):
    """Stub the shared profile-catalog probe with a fixed answer."""

    def fake(profile, *, timeout=30.0):
        if seen is not None:
            seen.append(profile)
        return catalog

    monkeypatch.setattr(kb, "installed_skills_for_profile", fake)


# ---------------------------------------------------------------------------
# create-time validation
# ---------------------------------------------------------------------------


def test_resolvable_skill_creates_the_card_unchanged(kanban_home, monkeypatch):
    _pin_catalog(monkeypatch, {"translation", "hermes-agent"})

    rc = _create([
        "create", "good card",
        "--assignee", "deus",
        "--skill", "translation",
    ])

    assert rc == 0
    tasks = _tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "good card"
    assert tasks[0].assignee == "deus"
    assert tasks[0].skills == ["translation"]


def test_unresolvable_skill_rejects_before_the_row_is_written(
    kanban_home, monkeypatch, capsys
):
    _pin_catalog(monkeypatch, {"hermes-agent"})

    rc = _create([
        "create", "bad card",
        "--assignee", "deus",
        "--skill", "hermes-agent",
        "--skill", "translation",
        "--skill", "github-code-review",
    ])

    assert rc == 2
    assert _tasks() == [], "a rejected create must not leave a partial card"

    err = capsys.readouterr().err
    # Names the invalid skills...
    assert "translation" in err
    assert "github-code-review" in err
    # ...and lists what the assignee CAN resolve.
    assert "resolvable for 'deus'" in err
    assert "hermes-agent" in err
    assert "no card was created" in err


def test_assignee_with_no_bundled_skills_rejects_and_says_so(
    kanban_home, monkeypatch, capsys
):
    """An empty catalog is a real answer, not a probe failure.

    `deus`, `duex`, `neon` and `none` all carry a `.no-bundled-skills` marker.
    A profile that resolves nothing can never satisfy a `--skill` request, so
    the create must fail rather than hand the worker a name that will kill it.
    """
    _pin_catalog(monkeypatch, set())

    rc = _create([
        "create", "empty catalog card",
        "--assignee", "neon",
        "--skill", "translation",
    ])

    assert rc == 2
    assert _tasks() == []
    err = capsys.readouterr().err
    assert "translation" in err
    assert "resolves no skills at all" in err


def test_mismatched_catalog_is_judged_against_the_assignee_not_the_author(
    kanban_home, monkeypatch, capsys
):
    """The author's own catalog is irrelevant — only the assignee's counts."""
    seen: list[str] = []
    _pin_catalog(monkeypatch, {"hermes-agent"}, seen=seen)

    rc = _create([
        "create", "cross-profile card",
        "--assignee", "duex",
        "--skill", "dataviz",
    ])

    assert rc == 2
    assert _tasks() == []
    assert seen == ["duex"], f"probed the wrong profile: {seen}"


def test_probe_failure_fails_open_and_still_creates(kanban_home, monkeypatch):
    """A broken probe must not become a card-creation outage."""
    _pin_catalog(monkeypatch, None)

    rc = _create([
        "create", "probe down",
        "--assignee", "deus",
        "--skill", "anything-at-all",
    ])

    assert rc == 0
    tasks = _tasks()
    assert len(tasks) == 1
    assert tasks[0].skills == ["anything-at-all"]


def test_skill_without_assignee_is_rejected(kanban_home, monkeypatch, capsys):
    seen: list[str] = []
    _pin_catalog(monkeypatch, {"translation"}, seen=seen)

    rc = _create(["create", "no assignee", "--skill", "translation"])

    assert rc == 2
    assert _tasks() == []
    assert "--skill requires --assignee" in capsys.readouterr().err
    assert seen == [], "no profile to probe — the probe must not run"


def test_create_without_skills_never_probes(kanban_home, monkeypatch):
    seen: list[str] = []
    _pin_catalog(monkeypatch, {"translation"}, seen=seen)

    rc = _create(["create", "plain card", "--assignee", "deus"])

    assert rc == 0
    tasks = _tasks()
    assert len(tasks) == 1
    assert tasks[0].skills is None
    assert seen == [], "cards without --skill must not pay for a subprocess probe"


# ---------------------------------------------------------------------------
# the shared probe itself
# ---------------------------------------------------------------------------


def _stub_probe(monkeypatch, *, stdout: str, returncode: int = 0):
    import subprocess as _sp

    class _Result:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Result())


def _table(*names: str, enabled: int | None = None) -> str:
    shown = len(names) if enabled is None else enabled
    rows = "\n".join(f"│ {n} │ cat │ local │ local │ enabled │" for n in names)
    return (
        "        Installed Skills (enabled only)\n"
        "┏━━━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━━━━┓\n"
        "│ Name     │ Cat  │ Source│ Trust │ Status  │\n"
        "├──────────┼──────┼───────┼───────┼─────────┤\n"
        f"{rows}\n"
        "└──────────┴──────┴───────┴───────┴─────────┘\n"
        f"0 hub-installed, 0 builtin, {len(names)} local — {shown} enabled shown\n"
    )


def test_probe_parses_the_table(monkeypatch):
    _stub_probe(monkeypatch, stdout=_table("hermes-agent", "dataviz"))
    assert kb.installed_skills_for_profile("deus") == {"hermes-agent", "dataviz"}


def test_probe_returns_empty_set_for_a_profile_with_no_skills(monkeypatch):
    _stub_probe(monkeypatch, stdout=_table())
    assert kb.installed_skills_for_profile("neon") == set()


def test_probe_returns_none_when_table_and_summary_disagree(monkeypatch):
    """Truncated/wrapped output must not be read as a smaller catalog."""
    _stub_probe(monkeypatch, stdout=_table("hermes-agent", enabled=4))
    assert kb.installed_skills_for_profile("deus") is None


def test_probe_returns_none_on_nonzero_exit(monkeypatch):
    _stub_probe(monkeypatch, stdout="", returncode=1)
    assert kb.installed_skills_for_profile("ghost") is None


def test_probe_returns_none_on_unparseable_output(monkeypatch):
    _stub_probe(monkeypatch, stdout="hermes: something went sideways\n")
    assert kb.installed_skills_for_profile("deus") is None


# ---------------------------------------------------------------------------
# dispatcher behaviour is unchanged
# ---------------------------------------------------------------------------


def test_spawn_guard_still_fails_open_on_empty_catalog(monkeypatch):
    """`resolvable_skills_for_profile` keeps its pre-existing fail-open contract.

    Create-time rejects an empty catalog; the DISPATCHER must not start
    stripping skills off already-created cards because of this patch.
    """
    monkeypatch.setattr(
        kb, "installed_skills_for_profile", lambda p, *, timeout=30.0: set()
    )
    assert kb.resolvable_skills_for_profile("neon", ["a", "b"]) == (["a", "b"], [])

    monkeypatch.setattr(
        kb, "installed_skills_for_profile", lambda p, *, timeout=30.0: None
    )
    assert kb.resolvable_skills_for_profile("neon", ["a", "b"]) == (["a", "b"], [])


def test_spawn_guard_still_splits_against_a_known_catalog(monkeypatch):
    monkeypatch.setattr(
        kb, "installed_skills_for_profile", lambda p, *, timeout=30.0: {"keep"}
    )
    assert kb.resolvable_skills_for_profile("deus", ["keep", "drop"]) == (
        ["keep"], ["drop"],
    )


def test_spawn_guard_short_circuits_without_names(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("probe ran for an empty skill list")

    monkeypatch.setattr(kb, "installed_skills_for_profile", boom)
    assert kb.resolvable_skills_for_profile("deus", []) == ([], [])
