"""`hermes gateway restart` must discharge the fleet-restart obligation (#95294 follow-up).

The stale-gateway banner short-circuits on the ``fleet_restart_pending`` marker with no
reconciliation, and the marker was only ever cleared from inside ``hermes update``. So the
banner's own advice ("Run `hermes update` or `hermes gateway restart`") was half false: the
restart could not silence it, which funnelled operators into ``hermes update`` — a command
that hard-resets the checkout to origin/main and discards locally merged fixes.

A successful restart IS the obligation the marker records, so it must clear it — but the
marker carries no runtime inventory, so only a live, identity-matched fleet may discharge it.
Fail closed on anything else.

No live gateway, no network. The fleet probe and service call are mocked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import gateway as gateway_cmd
import hermes_cli.update_cmd_fleet as update_cmd_fleet

SHA = "a" * 40


def _restart_args():
    return SimpleNamespace(system=False, all=False)


@pytest.fixture
def marker():
    """A pending pull->restart obligation on disk (sandboxed HERMES_HOME)."""
    update_cmd_fleet._write_fleet_restart_pending_marker(expected_sha=SHA)
    path = update_cmd_fleet._fleet_restart_pending_marker_path()
    assert path.is_file()
    return path


@pytest.fixture
def stub_restart(monkeypatch):
    """Neutralize everything _cmd_restart does except the marker bookkeeping."""
    monkeypatch.setattr(gateway_cmd, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(gateway_cmd, "_dispatch_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gateway_cmd, "_dispatch_all_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gateway_cmd, "_installed_service_kind_for", lambda *a, **k: "launchd")
    monkeypatch.setattr(gateway_cmd, "_service_call", lambda *a, **k: None)


def _patch_fleet(monkeypatch, rows):
    monkeypatch.setattr(update_cmd_fleet, "_current_checkout_sha", lambda: SHA)
    import hermes_cli.update_receipt as update_receipt
    monkeypatch.setattr(update_receipt, "collect_fleet_versions", lambda **k: rows)


def test_restart_clears_marker_when_every_gateway_runs_current_code(
    monkeypatch, marker, stub_restart
):
    _patch_fleet(monkeypatch, [
        {"profile": "default", "state": "current", "code_sha": SHA},
        {"profile": "doe-domain", "state": "current", "code_sha": SHA},
    ])

    gateway_cmd._cmd_restart(_restart_args())

    assert not marker.exists()


def test_restart_keeps_marker_when_a_gateway_is_still_stale(
    monkeypatch, marker, stub_restart
):
    _patch_fleet(monkeypatch, [
        {"profile": "default", "state": "current", "code_sha": SHA},
        {"profile": "doe-domain", "state": "stale", "code_sha": "b" * 40},
    ])

    gateway_cmd._cmd_restart(_restart_args())

    assert marker.is_file()


def test_restart_keeps_marker_when_fleet_cannot_be_probed(
    monkeypatch, marker, stub_restart
):
    _patch_fleet(monkeypatch, [])

    gateway_cmd._cmd_restart(_restart_args())

    assert marker.is_file()


def test_restart_keeps_marker_when_the_restart_itself_fails(
    monkeypatch, marker, stub_restart
):
    _patch_fleet(monkeypatch, [{"profile": "default", "state": "current", "code_sha": SHA}])

    def _boom(*a, **k):
        raise SystemExit(1)

    monkeypatch.setattr(gateway_cmd, "_service_call", _boom)

    with pytest.raises(SystemExit):
        gateway_cmd._cmd_restart(_restart_args())

    assert marker.is_file()


def test_helper_is_a_noop_when_no_obligation_is_recorded(monkeypatch):
    _patch_fleet(monkeypatch, [{"profile": "default", "state": "current", "code_sha": SHA}])
    path = update_cmd_fleet._fleet_restart_pending_marker_path()
    assert not path.exists()

    assert update_cmd_fleet.clear_fleet_restart_pending_if_fleet_current() is False
