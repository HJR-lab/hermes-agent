"""The decomposer must not fan out scope an open card already owns.

On 2026-09-06 it decomposed the SAME problem twice, 17 minutes apart, into
seven cards, while a claimed card was already working it. Two of the resulting
Deus workers then edited the same source files concurrently — whole-file
writes on the module the live gateway imports. Wasted tokens were the least of
it.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_decompose import _overlapping_open_card, _scope_tokens


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    c = kbc.connect()
    try:
        yield c
    finally:
        c.close()


def _mk(conn, title, status="running"):
    return kb.create_task(conn, title=title, assignee="deus", initial_status=status)


# The two titles that actually collided in the incident.
_OWNED = "Validate --skill against the assignee's resolvable skill set at card-create time"
_DUPE = "Validate card skills against the assignee profile"


def test_detects_the_real_incident_duplicate(conn):
    owner = _mk(conn, _OWNED)
    mine = _mk(conn, _DUPE)
    hit = _overlapping_open_card(conn, mine, _DUPE)
    assert hit is not None and hit[0] == owner


def test_unrelated_cards_do_not_collide(conn):
    _mk(conn, "Audit Zero Windows hardware and benchmark local Qwen models")
    mine = _mk(conn, "Build secure private transport for the Executive Room")
    assert _overlapping_open_card(
        conn, mine, "Build secure private transport for the Executive Room"
    ) is None


def test_terminal_cards_are_not_owners(conn):
    """A done/archived card owns nothing — its scope is free again."""
    owner = _mk(conn, _OWNED)
    kb.complete_task(conn, owner, result="shipped")
    mine = _mk(conn, _DUPE)
    assert _overlapping_open_card(conn, mine, _DUPE) is None


def test_a_card_never_matches_itself(conn):
    mine = _mk(conn, _OWNED)
    assert _overlapping_open_card(conn, mine, _OWNED) is None


def test_short_titles_are_ignored(conn):
    """Too few significant tokens to judge — must not guess."""
    _mk(conn, "fix it")
    mine = _mk(conn, "fix it")
    assert _overlapping_open_card(conn, mine, "fix it") is None


def test_stopwords_alone_do_not_trigger(conn):
    _mk(conn, "Review the task for the card and the report")
    mine = _mk(conn, "Deploy the service to the cluster and the mesh")
    assert _overlapping_open_card(
        conn, mine, "Deploy the service to the cluster and the mesh"
    ) is None


def test_plural_singular_tokens_match():
    """skill/skills and profile/profiles must not read as different scopes."""
    assert "skill" in _scope_tokens("kanban skills")
    assert "profile" in _scope_tokens("assignee profiles")
