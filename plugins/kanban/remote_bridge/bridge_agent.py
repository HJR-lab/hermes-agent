#!/usr/bin/env python3
"""Remote end of the Kanban remote bridge: a per-card recovery journal, never a board.

Deployed to the remote host (``hermes kanban remote-bridge install-agent``) and
driven over SSH by ``hermes_cli/kanban_remote_bridge.py``. Standard library only,
because it runs under the remote host's system ``python3``.

Verbs (``bridge_agent.py <verb> <card_id>``; ``accept`` reads JSON on stdin):

  accept  journal the card; same id + same body hash returns the same entry
  start   launch the runner for an accepted card (no-op once running/final)
  status  report state; recovers a runner that died without a result
  cancel  drop an accepted card that the Mac could not claim
  run     internal: the detached runner that drives the local Hermes agent

Every reply is one JSON object on stdout. Protocol refusals exit 0 with
``"ok": false`` so the Mac can tell them apart from a broken transport.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

AGENT_VERSION = 1
ROOT = Path(os.path.expanduser("~/.hermes/remote-bridge"))
JOURNAL = ROOT / "journal"
CARD_ID_RE = re.compile(r"^t_[0-9a-f]{6,32}$")
RESULT_PREFIX = "BRIDGE_RESULT:"
MAX_ATTEMPTS = 2
HEARTBEAT_SECONDS = 30
POLL_SECONDS = 1.0
MAX_STDIN_BYTES = 512 * 1024
MAX_SUMMARY = 4000
MAX_DETAILS = 12000
MAX_ARTIFACTS = 20
MAX_OUTPUT_TAIL = 2000

PROMPT_TEMPLATE = """\
You are Zero, running on this machine ({host}). This task was relayed from Justin's Mac \
Kanban board by the remote bridge. Mac card {card_id} is the only ledger for it: do not \
create or update Kanban cards, and do not try to reach the Mac.

Task title: {title}

Task body:
{body}

Constraints:
- Work only on this machine. Never copy or print credentials.
- Report back only text, JSON, URLs and SHA-256 hashes. Do not return files, scripts or binaries.

When you are finished, the very last line of your reply must be exactly one line in this \
format (single-line JSON, no code fence):
BRIDGE_RESULT: {{"outcome": "done", "summary": "<what you found or did, max 1500 chars>", \
"details": "<optional longer report>", "artifacts": [{{"kind": "url|sha256|text|json", "value": "..."}}]}}
If you could not finish, use "outcome": "blocked" and add "block_kind": \
"needs_input" | "capability" | "transient".
"""


def _now() -> int:
    return int(time.time())


def _card_dir(card_id: str) -> Path:
    return JOURNAL / card_id


@contextlib.contextmanager
def _journal_lock():
    JOURNAL.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(JOURNAL / ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _load(card_id: str):
    try:
        return json.loads((_card_dir(card_id) / "entry.json").read_text("utf-8"))
    except FileNotFoundError:
        return None


def _save(entry: dict) -> None:
    path = _card_dir(entry["card_id"]) / "entry.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(entry, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _view(entry: dict) -> dict:
    view = {key: entry.get(key) for key in (
        "state", "body_sha256", "accepted_at", "started_at", "finished_at", "attempts",
    )}
    heartbeat = entry.get("heartbeat_at")
    view["heartbeat_age_seconds"] = None if heartbeat is None else max(0, _now() - int(heartbeat))
    if entry.get("state") in ("done", "blocked"):
        view["result"] = entry.get("result")
    return view


def _reply(card_id: str, entry=None, *, error=None) -> int:
    out = {"ok": error is None, "card_id": card_id, "host": socket.gethostname(),
           "agent_version": AGENT_VERSION}
    if error is not None:
        out["error"] = error
    if entry is not None:
        out.update(_view(entry))
    print(json.dumps(out, sort_keys=True))
    return 0


def _cmdline(pid) -> str:
    if not pid:
        return ""
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    try:
        # No /proc (macOS test hosts): ps prints nothing for a dead pid.
        proc = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                              capture_output=True, text=True, check=False)
        return proc.stdout.strip()
    except OSError:
        return ""


def _pid_matches(pid, marker: str) -> bool:
    """Alive AND still the process we started — pids get reused."""
    return bool(marker) and marker in _cmdline(pid)


def _runner_marker(card_id: str) -> str:
    return f"run {card_id}"


def _prompt_path(card_id: str) -> Path:
    return _card_dir(card_id) / "prompt.txt"


def _bounded(value, limit: int):
    return value[:limit] if isinstance(value, str) else value


def _parse_result(text: str):
    """The last ``BRIDGE_RESULT:`` line as a bounded dict, or None when absent/invalid."""
    for line in reversed(text.splitlines()):
        stripped = line.strip().strip("`").strip()
        if not stripped.startswith(RESULT_PREFIX):
            continue
        try:
            result = json.loads(stripped[len(RESULT_PREFIX):].strip())
        except ValueError:
            return None
        if not isinstance(result, dict):
            return None
        result["summary"] = _bounded(result.get("summary"), MAX_SUMMARY)
        result["details"] = _bounded(result.get("details"), MAX_DETAILS)
        if isinstance(result.get("artifacts"), list):
            result["artifacts"] = result["artifacts"][:MAX_ARTIFACTS]
        return result
    return None


def _output_text(card_id: str) -> str:
    try:
        return (_card_dir(card_id) / "output.txt").read_text("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _blocked(summary: str, card_id: str) -> dict:
    tail = _output_text(card_id)[-MAX_OUTPUT_TAIL:]
    return {"outcome": "blocked", "block_kind": "transient", "summary": summary,
            "details": f"Last agent output:\n{tail}" if tail else None, "artifacts": []}


def _finalize(entry: dict, result: dict, exit_code=None) -> None:
    entry["state"] = "done" if result.get("outcome") == "done" else "blocked"
    entry["result"] = result
    entry["finished_at"] = _now()
    entry["exit_code"] = exit_code


def _spawn_runner(entry: dict) -> None:
    card_dir = _card_dir(entry["card_id"])
    with open(card_dir / "runner.log", "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "run", entry["card_id"]],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=str(card_dir), start_new_session=True, close_fds=True,
        )
    entry["runner_pid"] = proc.pid


def cmd_accept(card_id: str) -> int:
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        return _reply(card_id, error="accept payload too large")
    try:
        payload = json.loads(raw)
    except ValueError:
        return _reply(card_id, error="accept payload is not JSON")
    if not isinstance(payload, dict) or payload.get("card_id") != card_id:
        return _reply(card_id, error="accept payload card_id mismatch")
    body, title = payload.get("body"), payload.get("title")
    if not isinstance(body, str) or not isinstance(title, str):
        return _reply(card_id, error="accept payload needs string title and body")
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if payload.get("body_sha256") != body_sha:
        return _reply(card_id, error="accept payload body_sha256 does not match body")
    max_turns = payload.get("max_turns")
    if not isinstance(max_turns, int) or not 1 <= max_turns <= 200:
        return _reply(card_id, error="accept payload max_turns must be an int in 1..200")
    with _journal_lock():
        entry = _load(card_id)
        if entry is not None:
            if entry.get("body_sha256") != body_sha:
                return _reply(card_id, error="conflict: journal holds a different body for this card id")
            if entry.get("state") == "cancelled":
                entry["state"] = "accepted"
                _save(entry)
            return _reply(card_id, entry)
        _card_dir(card_id).mkdir(parents=True, exist_ok=True, mode=0o700)
        _prompt_path(card_id).write_text(PROMPT_TEMPLATE.format(
            host=socket.gethostname(), card_id=card_id, title=title, body=body,
        ), "utf-8")
        entry = {"card_id": card_id, "title": title, "body_sha256": body_sha, "state": "accepted",
                 "accepted_at": _now(), "attempts": 0, "max_turns": max_turns}
        _save(entry)
        return _reply(card_id, entry)


def cmd_start(card_id: str) -> int:
    with _journal_lock():
        entry = _load(card_id)
        if entry is None:
            return _reply(card_id, error="unknown card")
        if entry["state"] == "cancelled":
            return _reply(card_id, entry, error="card was cancelled")
        if entry["state"] == "accepted":
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            entry["state"] = "running"
            entry["started_at"] = _now()
            _spawn_runner(entry)
            _save(entry)
        return _reply(card_id, entry)


def cmd_cancel(card_id: str) -> int:
    with _journal_lock():
        entry = _load(card_id)
        if entry is None:
            return _reply(card_id, error="unknown card")
        if entry["state"] == "accepted":
            entry["state"] = "cancelled"
            _save(entry)
        return _reply(card_id, entry)


def cmd_status(card_id: str) -> int:
    with _journal_lock():
        entry = _load(card_id)
        if entry is None:
            return _reply(card_id, error="unknown card")
        if entry["state"] == "running" and not _pid_matches(entry.get("runner_pid"), _runner_marker(card_id)):
            # The runner died without writing a final state. Never start a second
            # agent beside a live one, and never re-run work whose result is on disk.
            child_alive = _pid_matches(entry.get("child_pid"), str(_prompt_path(card_id)))
            recovered = None if child_alive else _parse_result(_output_text(card_id))
            if recovered is not None:
                _finalize(entry, recovered)
            elif child_alive:
                _spawn_runner(entry)
            elif int(entry.get("attempts") or 0) < MAX_ATTEMPTS:
                entry["attempts"] = int(entry.get("attempts") or 0) + 1
                entry.setdefault("resumed_at", []).append(_now())
                entry["child_pid"] = None
                _spawn_runner(entry)
            else:
                _finalize(entry, _blocked(
                    f"remote runner exited without a result after {entry['attempts']} attempts", card_id,
                ))
            _save(entry)
        return _reply(card_id, entry)


def _hermes_argv(entry: dict) -> list:
    hermes = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
    return [hermes, "chat", "-Q", "--query-file", str(_prompt_path(entry["card_id"])),
            "--max-turns", str(int(entry.get("max_turns") or 40)), "--source", "kanban-bridge"]


def cmd_run(card_id: str) -> int:
    marker = str(_prompt_path(card_id))
    child = None
    with _journal_lock():
        entry = _load(card_id)
        if entry is None or entry["state"] != "running":
            return 0
        entry["runner_pid"] = os.getpid()
        child_pid = entry.get("child_pid")
        if not _pid_matches(child_pid, marker):
            with open(_card_dir(card_id) / "output.txt", "wb") as out:
                child = subprocess.Popen(_hermes_argv(entry), stdin=subprocess.DEVNULL, stdout=out,
                                         stderr=subprocess.STDOUT, cwd=str(_card_dir(card_id)))
            child_pid = child.pid
            entry["child_pid"] = child_pid
        entry["heartbeat_at"] = _now()
        _save(entry)

    last_beat = time.monotonic()
    while (child.poll() is None) if child is not None else _pid_matches(child_pid, marker):
        time.sleep(POLL_SECONDS)
        if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
            last_beat = time.monotonic()
            with _journal_lock():
                entry = _load(card_id)
                if entry is None or entry["state"] != "running":
                    return 0
                entry["heartbeat_at"] = _now()
                _save(entry)

    exit_code = child.returncode if child is not None else None
    result = _parse_result(_output_text(card_id)) or _blocked(
        "Zero's agent finished without a valid BRIDGE_RESULT line", card_id,
    )
    with _journal_lock():
        entry = _load(card_id)
        if entry is not None and entry["state"] == "running":
            _finalize(entry, result, exit_code)
            _save(entry)
    return 0


VERBS = {"accept": cmd_accept, "start": cmd_start, "status": cmd_status,
         "cancel": cmd_cancel, "run": cmd_run}


def main(argv: list) -> int:
    if len(argv) != 3 or argv[1] not in VERBS or not CARD_ID_RE.match(argv[2]):
        print(json.dumps({"ok": False, "error": "usage: bridge_agent.py <verb> <card_id>"}))
        return 2
    os.umask(0o077)
    return VERBS[argv[1]](argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
