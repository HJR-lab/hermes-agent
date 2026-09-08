"""A provider outage must be visible in the gateway log.

During the 2026-09-08 Codex wall the dispatcher's summary line only fires when
something spawned, so a fully walled board went completely silent -- the
operator's only signal that anything was wrong was a board that had quietly
stopped moving.

The wall now logs, but exactly once per provider: the outage lasted 3.5 hours
at a 60s tick, so an unthrottled per-tick line would be ~180 identical entries,
and a per-card line would have been thousands.
"""

from __future__ import annotations

import logging

import pytest

from gateway import kanban_watchers_dispatcher as kwd


class _Result:
    def __init__(self, *, spawned=(), notices=(), walled=()):
        self.spawned = list(spawned)
        self.reclaimed = 0
        self.crashed = []
        self.timed_out = []
        self.promoted = 0
        self.auto_blocked = []
        self.provider_wall_notices = list(notices)
        self.skipped_provider_walled = list(walled)


@pytest.fixture(autouse=True)
def _clear_notice_throttle():
    kwd._reset_provider_wall_notices()
    yield
    kwd._reset_provider_wall_notices()


def _wall_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "provider quota wall" in r.getMessage()]


def test_wall_is_logged_even_though_nothing_spawned(caplog):
    """The silent-board bug: zero spawns must not mean zero signal."""
    res = _Result(
        notices=[("openai-codex", 1788858377.0)],
        walled=[("t_1", "deus", "openai-codex", 1788858377.0)],
    )
    with caplog.at_level(logging.WARNING):
        kwd._log_spawn_results([("default", res)])

    lines = _wall_lines(caplog)
    assert len(lines) == 1, f"expected one wall notice, got {lines}"
    assert "openai-codex" in lines[0]


def test_one_notice_covers_every_held_card(caplog):
    """28 held cards, one line."""
    res = _Result(
        notices=[("openai-codex", 1788858377.0)],
        walled=[(f"t_{n}", "deus", "openai-codex", 1788858377.0) for n in range(28)],
    )
    with caplog.at_level(logging.WARNING):
        kwd._log_spawn_results([("default", res)])

    lines = _wall_lines(caplog)
    assert len(lines) == 1
    assert "28" in lines[0], f"notice should say how many cards are held: {lines[0]}"


def test_repeat_ticks_do_not_respam_the_same_wall(caplog):
    """60s ticks across a 3.5h outage must not produce ~180 identical lines."""
    res = _Result(
        notices=[("openai-codex", 1788858377.0)],
        walled=[("t_1", "deus", "openai-codex", 1788858377.0)],
    )
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            kwd._log_spawn_results([("default", res)])

    assert len(_wall_lines(caplog)) == 1


def test_a_new_wall_is_reported_again(caplog):
    """A different provider (or a moved reset) is new information."""
    first = _Result(
        notices=[("openai-codex", 1788858377.0)],
        walled=[("t_1", "deus", "openai-codex", 1788858377.0)],
    )
    second = _Result(
        notices=[("alibaba-coding-plan", 1789445177.0)],
        walled=[("t_2", "duex", "alibaba-coding-plan", 1789445177.0)],
    )
    with caplog.at_level(logging.WARNING):
        kwd._log_spawn_results([("default", first)])
        kwd._log_spawn_results([("default", second)])

    lines = _wall_lines(caplog)
    assert len(lines) == 2
    assert "alibaba-coding-plan" in lines[1]


def test_clearing_the_wall_rearms_the_notice(caplog):
    """Once quota returns and the board drains, a later outage must be
    reported rather than swallowed by the throttle."""
    walled = _Result(
        notices=[("openai-codex", 1788858377.0)],
        walled=[("t_1", "deus", "openai-codex", 1788858377.0)],
    )
    healthy = _Result(spawned=[("t_1", "deus", "/tmp/ws")])
    with caplog.at_level(logging.WARNING):
        kwd._log_spawn_results([("default", walled)])
        kwd._log_spawn_results([("default", healthy)])
        kwd._log_spawn_results([("default", walled)])

    assert len(_wall_lines(caplog)) == 2


def test_spawn_summary_still_logged(caplog):
    """The existing summary line must not regress."""
    res = _Result(spawned=[("t_1", "deus", "/tmp/ws")])
    with caplog.at_level(logging.INFO):
        assert kwd._log_spawn_results([("default", res)]) is True

    assert any("kanban dispatcher [default]" in r.getMessage() for r in caplog.records)
