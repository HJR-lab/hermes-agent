"""`hermes gateway stop|restart|uninstall` must refuse from a DESCENDANT of the live gateway.

A kanban worker / terminal child inherits ``_HERMES_GATEWAY=1`` but is not the gateway PID, so the
self-targeting guard alone let it restart its own supervisor mid-turn. Deliberate restarters
(restart watcher, dashboard actions, update recovery) strip the marker and must stay allowed.
"""
from __future__ import annotations

import os
from argparse import Namespace

import pytest

import hermes_cli.gateway as gw

GATEWAY_PID = 900_002
WORKER_PID = 900_001


class _Reached(Exception):
    """The guard let control through to the real command path."""


def _sentinel(*_a, **_k):
    raise _Reached()


@pytest.fixture
def cli(monkeypatch):
    """Short-circuit every command's first downstream call so nothing real is stopped."""
    monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", _sentinel)
    monkeypatch.setattr(gw, "_dispatch_all_via_service_manager_if_s6", _sentinel)
    monkeypatch.setattr(gw, "is_managed", _sentinel)
    monkeypatch.setattr("gateway.status._looks_like_gateway_process", lambda pid: False)
    return monkeypatch


def _ancestry(monkeypatch, *chain: int) -> None:
    """This process's parent chain: os.getpid() -> chain[0] -> ... -> chain[-1]."""
    links = dict(zip((os.getpid(), *chain), chain))
    monkeypatch.setattr(gw, "_get_parent_pid", lambda pid: links.get(pid))


def _args(verb: str) -> Namespace:
    return Namespace(gateway_command=verb, all=False, system=False)


VERBS = ["stop", "restart", "uninstall"]


@pytest.mark.parametrize("verb", VERBS)
def test_descendant_of_live_gateway_is_refused(cli, capsys, verb):
    cli.setenv("_HERMES_GATEWAY", "1")
    cli.setattr("gateway.status.get_running_pid", lambda *a, **k: GATEWAY_PID)
    _ancestry(cli, WORKER_PID, GATEWAY_PID, 1)

    with pytest.raises(SystemExit) as exc_info:
        gw.gateway_command(_args(verb))

    assert exc_info.value.code == 1
    err = capsys.readouterr()
    message = err.out + err.err
    assert str(GATEWAY_PID) in message
    assert "operator" in message


@pytest.mark.parametrize("verb", VERBS)
def test_descendant_of_other_profile_gateway_is_refused(cli, verb):
    """PID file resolves to a different profile (or none): match the ancestor's ``gateway run`` argv."""
    cli.setenv("_HERMES_GATEWAY", "1")
    cli.setattr("gateway.status.get_running_pid", lambda *a, **k: None)
    cli.setattr("gateway.status._looks_like_gateway_process", lambda pid: pid == GATEWAY_PID)
    _ancestry(cli, WORKER_PID, GATEWAY_PID, 1)

    with pytest.raises(SystemExit) as exc_info:
        gw.gateway_command(_args(verb))
    assert exc_info.value.code == 1


@pytest.mark.parametrize("verb", VERBS)
def test_non_descendant_shell_with_marker_is_allowed(cli, verb):
    """Importing gateway.run sets the marker in plain CLIs too; ancestry, not the marker, decides."""
    cli.setenv("_HERMES_GATEWAY", "1")
    cli.setattr("gateway.status.get_running_pid", lambda *a, **k: GATEWAY_PID)
    _ancestry(cli, WORKER_PID, 1)

    with pytest.raises(_Reached):
        gw.gateway_command(_args(verb))


@pytest.mark.parametrize("verb", VERBS)
def test_descendant_that_stripped_marker_is_allowed(cli, verb):
    """Restart watcher / dashboard / update recovery pop ``_HERMES_GATEWAY`` to restart on purpose."""
    cli.delenv("_HERMES_GATEWAY", raising=False)
    cli.setattr("gateway.status.get_running_pid", lambda *a, **k: GATEWAY_PID)
    _ancestry(cli, WORKER_PID, GATEWAY_PID, 1)

    with pytest.raises(_Reached):
        gw.gateway_command(_args(verb))


def test_ancestry_walk_survives_a_parent_cycle(cli):
    cli.setenv("_HERMES_GATEWAY", "1")
    cli.setattr("gateway.status.get_running_pid", lambda *a, **k: GATEWAY_PID)
    links = {os.getpid(): WORKER_PID, WORKER_PID: os.getpid()}
    cli.setattr(gw, "_get_parent_pid", lambda pid: links.get(pid))

    with pytest.raises(_Reached):
        gw.gateway_command(_args("stop"))
