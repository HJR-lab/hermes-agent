"""Remote-assignee bridge: one original card, dispatched exactly once, fail-closed.

Drives the real Mac-side bridge against the real remote journal agent (run as a
local subprocess with a fake ``hermes`` on PATH), so idempotency and crash
recovery go through the actual protocol, journal and detached runner.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# The remote agent detaches its runner (new session), so crash simulation and
# teardown must signal processes outside the pytest subtree — each kill is gated
# on the card id appearing in that pid's argv.
pytestmark = pytest.mark.live_system_guard_bypass

AGENT = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "remote_bridge" / "bridge_agent.py"

FAKE_HERMES = """#!@PYTHON@
import pathlib, sys, time
state = pathlib.Path("@STATE@")
with open(state / "invocations", "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
mode = (state / "mode").read_text().strip()
if mode.startswith("sleep"):
    time.sleep(float(mode.split()[1]))
print("checked hostname and gateway")
if mode == "malformed":
    print('BRIDGE_RESULT: {"outcome": "done", "summary": "ok", "artifacts": [{"kind": "file", "value": "payload.exe"}]}')
else:
    print('BRIDGE_RESULT: {"outcome": "done", "summary": "ZERO healthy", "artifacts": [{"kind": "sha256", "value": "' + "a" * 64 + '"}]}')
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    (hermes_home / "profiles").mkdir(parents=True)
    (hermes_home / "config.yaml").write_text("kanban:\n  remote_assignees:\n    zero:\n      host: zero\n")
    state = tmp_path / "fake_state"
    state.mkdir()
    (state / "mode").write_text("ok")
    (state / "invocations").write_text("")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    hermes = fake_bin / "hermes"
    hermes.write_text(FAKE_HERMES.replace("@PYTHON@", sys.executable).replace("@STATE@", str(state)))
    hermes.chmod(0o755)
    # One HOME serves both sides: the remote agent's journal lives under ~/.hermes/remote-bridge.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    for mod in list(sys.modules):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_remote_bridge as bridge

    assignee = bridge.parse_remote_assignee({"remote_assignees": {"zero": {"host": "zero"}}}, "zero")

    class LocalAgentTransport(bridge.SshTransport):
        calls = 0

        def _argv(self, verb, card_id):
            return [sys.executable, str(AGENT), verb, card_id]

        def call(self, verb, card_id, payload=None):
            type(self).calls += 1
            return super().call(verb, card_id, payload)

    ns = SimpleNamespace(kb=kb, kbc=kbc, bridge=bridge, assignee=assignee, home=home, state=state,
                         hermes_home=hermes_home, transport=LocalAgentTransport(assignee),
                         Transport=LocalAgentTransport)
    yield ns
    # Never leave a detached runner or fake agent behind; both carry the card id in argv.
    for entry in (home / ".hermes" / "remote-bridge" / "journal").glob("*/entry.json"):
        data = json.loads(entry.read_text())
        for pid in (data.get("runner_pid"), data.get("child_pid")):
            if pid and entry.parent.name in _cmdline(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)


def _cmdline(pid) -> str:
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout


def _create(env, body="Report hostname, Hermes version, model/provider and gateway health."):
    with env.kbc.connect_closing() as conn:
        return env.kb.create_task(conn, title="Zero health report", body=body, assignee="zero")


def _tick(env, transport=None):
    with env.kbc.connect_closing() as conn:
        return env.bridge.bridge_tick(conn, env.assignee, transport or env.transport)


def _row(env, card_id):
    with env.kbc.connect_closing() as conn:
        return dict(conn.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (card_id,)).fetchone())


def _event_kinds(env, card_id):
    with env.kbc.connect_closing() as conn:
        return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (card_id,))]


def _journal(env, card_id):
    return json.loads((env.home / ".hermes" / "remote-bridge" / "journal" / card_id / "entry.json").read_text())


def _invocations(env) -> int:
    return len((env.state / "invocations").read_text().splitlines())


def _tick_until(env, card_id, status, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _tick(env)
        if _row(env, card_id)["status"] == status:
            return
        time.sleep(0.3)
    raise AssertionError(f"{card_id} never reached {status}: {_row(env, card_id)} {_event_kinds(env, card_id)}")


def test_card_dispatched_once_and_result_lands_on_original_card(env):
    card_id = _create(env)
    with env.kbc.connect_closing() as conn:
        row = conn.execute("SELECT title, body FROM tasks WHERE id = ?", (card_id,)).fetchone()
    # A bridge that crashed after the remote acknowledged, before claiming locally.
    env.transport.call("accept", card_id, env.bridge.accept_payload(
        card_id, row["title"], row["body"], env.assignee.max_turns))
    assert _row(env, card_id)["status"] == "ready"

    first = _tick(env)
    assert first.dispatched == [card_id] and first.started == [card_id]
    assert _row(env, card_id) == {"status": "running", "claim_lock": env.assignee.claimer}
    again = _tick(env)
    assert again.dispatched == [] and again.started == []

    _tick_until(env, card_id, "done")
    with env.kbc.connect_closing() as conn:
        runs = conn.execute("SELECT summary, metadata FROM task_runs WHERE task_id = ?", (card_id,)).fetchall()
    assert len(runs) == 1 and runs[0]["summary"] == "ZERO healthy"
    assert json.loads(runs[0]["metadata"])["remote_assignee"] == "zero"
    kinds = _event_kinds(env, card_id)
    assert kinds.count("claimed") == 1 and kinds.count("remote_dispatched") == 1
    assert _journal(env, card_id)["attempts"] == 1 and _invocations(env) == 1

    # Idle: nothing ready or running for zero means no remote call at all.
    calls = env.Transport.calls
    assert not _tick(env).changed() and env.Transport.calls == calls


def test_runner_crash_resumes_without_rerunning_the_agent(env):
    (env.state / "mode").write_text("sleep 3")
    card_id = _create(env)
    _tick(env)
    deadline = time.monotonic() + 15
    while not _journal(env, card_id).get("child_pid") and time.monotonic() < deadline:
        time.sleep(0.1)
    runner_pid = _journal(env, card_id)["runner_pid"]
    assert f"run {card_id}" in _cmdline(runner_pid)
    os.kill(runner_pid, signal.SIGKILL)

    _tick_until(env, card_id, "done")
    assert _invocations(env) == 1
    assert _journal(env, card_id)["attempts"] == 1


def test_remote_liveness_relays_one_bounded_heartbeat_per_interval(env):
    (env.state / "mode").write_text("sleep 6")
    card_id = _create(env)
    _tick(env)
    relayed = []
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        relayed += _tick(env).heartbeats
        time.sleep(0.3)
    assert _row(env, card_id)["status"] == "running"
    # Many ticks inside one relay interval: the card sees the remote alive exactly once.
    assert relayed == [card_id] and _event_kinds(env, card_id).count("heartbeat") == 1
    _tick_until(env, card_id, "done")
    assert _invocations(env) == 1


def test_malformed_result_blocks_instead_of_completing(env):
    (env.state / "mode").write_text("malformed")
    card_id = _create(env)
    _tick_until(env, card_id, "blocked")
    kinds = _event_kinds(env, card_id)
    assert "completed" not in kinds and _journal(env, card_id)["state"] == "done"


def test_transport_failure_never_moves_the_card(env):
    class Dead(env.Transport):
        def _argv(self, verb, card_id):
            return [sys.executable, "-c", "import sys; sys.exit(255)"]

    class Banner(env.Transport):
        def _argv(self, verb, card_id):
            return [sys.executable, "-c", "print('Welcome to Ubuntu')"]

    (env.state / "mode").write_text("sleep 2")
    card_id = _create(env)
    before = _event_kinds(env, card_id)
    for broken in (Dead(env.assignee), Banner(env.assignee)):
        report = _tick(env, broken)
        assert report.transport_errors and not report.dispatched
    assert _row(env, card_id) == {"status": "ready", "claim_lock": None}
    assert _event_kinds(env, card_id) == before

    _tick(env)
    assert _tick(env, Dead(env.assignee)).transport_errors
    assert _row(env, card_id) == {"status": "running", "claim_lock": env.assignee.claimer}
    _tick_until(env, card_id, "done")
    assert _invocations(env) == 1


def test_conflicting_journal_entry_blocks_without_running(env):
    card_id = _create(env, body="original body")
    env.transport.call("accept", card_id, env.bridge.accept_payload(card_id, "t", "a different body", 40))
    report = _tick(env)
    assert report.blocked == [card_id] and not report.dispatched
    assert _journal(env, card_id)["state"] == "accepted" and _invocations(env) == 0


def test_dispatcher_never_spawns_a_remote_assignee_locally(env):
    # A stray same-named local profile must not win over the remote route.
    for name in ("zero", "alpha"):
        (env.hermes_home / "profiles" / name).mkdir()
    zero_card = _create(env)
    with env.kbc.connect_closing() as conn:
        alpha_card = env.kb.create_task(conn, title="local work", assignee="alpha")
    from hermes_cli import kanban_db_dispatch as kbd
    with env.kbc.connect_closing() as conn:
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, dry_run=True)
    spawned = [s[0] for s in result.spawned]
    assert alpha_card in spawned and zero_card not in spawned
    assert zero_card in result.skipped_nonspawnable
