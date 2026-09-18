"""Kanban worker MCP overrides must target the server entry the runtime migration registers.

Regression for #111707: dispatcher-owned workers on the codex app-server runtime injected
``mcp_servers.hermes-mcp.env.*`` while the migration writes ``[mcp_servers.hermes-tools]``;
codex then saw an env-only entry with no transport and refused to start.

The same env-only entry is reachable without any name drift: a CODEX_HOME where the runtime
migration has not run has no ``[mcp_servers.hermes-tools]`` at all, so the transport must check
the registration before emitting the override (the t_8b0c4e4d guard, dropped by 204f345816).
"""

import logging
import subprocess
import tomllib

import pytest

from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, KANBAN_ENV_KEYS, non_dispatcher_owned_context
from agent.transports import codex_app_server as cas
from hermes_cli.codex_runtime_plugin_migration import migrate


class _RecordingPopen:
    commands: list[list[str]] = []

    def __init__(self, cmd, *args, **kwargs):
        type(self).commands.append(list(cmd))
        self.stdin = self.stdout = self.stderr = None
        self.pid = 1
        self.returncode = None

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture
def launch(monkeypatch, tmp_path):
    """Return ``launch(env) -> list[str]`` of the ``mcp_servers.*`` overrides in the worker argv."""
    _RecordingPopen.commands = []
    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)
    for key in (*KANBAN_ENV_KEYS, DELEGATED_CHILD_ENV_MARKER, "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(key, raising=False)

    def _launch(env: dict[str, str]) -> list[str]:
        with monkeypatch.context() as ctx:
            for key, value in env.items():
                ctx.setenv(key, value)
            client = cas.CodexAppServerClient(codex_bin="codex", codex_home=str(tmp_path / "codex"))
            client._closed = True
        cmd = _RecordingPopen.commands.pop()
        return [arg for arg in cmd if arg.startswith("mcp_servers.")]

    return _launch


def _migrated_server_names(tmp_path) -> set[str]:
    codex_home = tmp_path / "codex"
    report = migrate({"mcp_servers": {}}, codex_home=codex_home, discover_plugins=False, expose_hermes_tools=True)
    assert not report.errors
    return set(tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))["mcp_servers"])


_OWNED_WORKER_ENV = {
    "HERMES_KANBAN_TASK": "11111111-1111-4111-8111-111111111111",
    "HERMES_KANBAN_RUN_ID": "42",
}


def test_worker_overrides_target_the_migrated_server(launch, tmp_path):
    """Producer/consumer contract: every ``-c mcp_servers.<name>.env.*`` override the worker
    launcher emits names an entry the migration actually writes to config.toml."""
    migrated = _migrated_server_names(tmp_path)
    overrides = launch({**_OWNED_WORKER_ENV, "HERMES_KANBAN_DB": str(tmp_path / "board" / "kanban.db")})
    assert overrides, "dispatcher-owned worker must scope the managed MCP endpoint"
    targeted = {arg.split(".env.", 1)[0].removeprefix("mcp_servers.") for arg in overrides}
    assert targeted <= migrated, f"overrides target {targeted - migrated}, which codex has no transport for"
    assert any(arg.startswith(f"mcp_servers.{next(iter(targeted))}.env.HERMES_KANBAN_TASK=") for arg in overrides)


def test_only_dispatcher_owned_workers_get_mcp_overrides(launch):
    """An ordinary launch and a worker that does not own the dispatcher's task emit no
    ``mcp_servers.*`` override at all (nothing to scope, nothing for codex to reject)."""
    assert launch({}) == []
    with non_dispatcher_owned_context():
        assert launch({"HERMES_KANBAN_TASK": "11111111-1111-4111-8111-111111111111"}) == []


@pytest.mark.parametrize("config_toml", [
    pytest.param(None, id="no-config"),
    pytest.param("[mcp_servers.other]\ncommand = \"x\"\n", id="server-absent"),
    pytest.param("[mcp_servers.hermes-tools.env]\nFOO = \"bar\"\n", id="env-only-no-transport"),
    pytest.param("[mcp_servers.hermes-tools\n", id="malformed-toml"),
])
def test_unregistered_server_gets_no_overrides(launch, tmp_path, caplog, config_toml):
    """A CODEX_HOME that does not register ``[mcp_servers.hermes-tools]`` with a transport gets no
    ``mcp_servers.*`` override: the override alone would synthesise an env-only table that codex
    rejects at bootstrap, killing the worker at initialize. Skip the scope, warn, and let codex start."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    if config_toml is not None:
        (codex_home / "config.toml").write_text(config_toml, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="agent.transports.codex_app_server"):
        overrides = launch(_OWNED_WORKER_ENV)
    assert overrides == [], f"override against an unregistered server: {overrides}"
    assert any("mcp_servers.hermes-tools" in rec.getMessage() for rec in caplog.records), (
        "a silently unscoped worker must at least be visible in the log")


def test_registered_server_still_gets_overrides(launch, tmp_path):
    """The guard only withholds scope when the registration is missing: after the migration has
    written the entry, an owned worker is scoped exactly as before (task, run id, delegated-child
    marker)."""
    _migrated_server_names(tmp_path)
    overrides = launch(_OWNED_WORKER_ENV)
    keys = {arg.split(".env.", 1)[1].split("=", 1)[0] for arg in overrides}
    assert {"HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", DELEGATED_CHILD_ENV_MARKER} <= keys
