"""Relay Kanban cards assigned to a *remote* assignee to Hermes on another host.

A remote assignee (``kanban.remote_assignees.<name>``) has no local profile and is
never spawned by the dispatcher. ``hermes kanban remote-bridge tick`` — a
deterministic, model-free poller — hands each ready card to the remote host over
the existing authenticated SSH connection and relays heartbeats and the final
result back onto the SAME card. That card is the only ledger; the remote side
keeps a recovery journal keyed by card id (``plugins/kanban/remote_bridge/
bridge_agent.py``), never a second board.

Per card, every step is idempotent so a crash anywhere resumes cleanly:

  accept  remote journals the card (same id + same body hash = same entry)
  claim   local ready -> running, only after a valid acknowledgement
  start   remote runner launches the remote host's own agent
  status  heartbeats relayed; the done/blocked result is validated, then applied

Trust boundary: the remote host is quarantined. Nothing it returns is executed or
written to disk here — results are schema-checked, stripped, redacted, bounded
text. A transport failure leaves the card exactly where it is.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import posixpath
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Optional

CLAIMER_PREFIX = "remote-bridge:"
DEFAULT_REMOTE_AGENT = "~/.hermes/remote-bridge/bridge_agent.py"
DEFAULT_CLAIM_TTL_SECONDS = 30 * 60
DEFAULT_MAX_TURNS = 40
# Relay at most one heartbeat per card per interval, and only when the remote
# runner itself beat recently.
HEARTBEAT_RELAY_SECONDS = 300
REMOTE_HEARTBEAT_FRESH_SECONDS = 180
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SUMMARY_CHARS = 4000
MAX_DETAILS_CHARS = 12000
MAX_REASON_CHARS = 1500
MAX_ARTIFACTS = 20
MAX_ARTIFACT_CHARS = 2000

REMOTE_STATES = frozenset({"accepted", "running", "cancelled", "done", "blocked"})
FINAL_STATES = frozenset({"done", "blocked"})
REMOTE_BLOCK_KINDS = frozenset({"needs_input", "capability", "transient"})
# References only: nothing a remote returns may be a file, script or binary.
ARTIFACT_KINDS = frozenset({"text", "json", "url", "screenshot_url", "sha256"})
_URL_KINDS = frozenset({"url", "screenshot_url"})

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CARD_ID_RE = re.compile(r"^t_[0-9a-f]{6,32}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
_REMOTE_AGENT_RE = re.compile(r"^(~/)?[A-Za-z0-9._/-]+\.py$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_URL_RE = re.compile(r"^https?://[^\s\"'<>`]{1,2000}$")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class TransportError(RuntimeError):
    """The remote was unreachable or answered outside the protocol. Never moves a card."""


class ProtocolRejected(ValueError):
    """The remote answered inside the protocol, but the answer is unacceptable."""


@dataclasses.dataclass(frozen=True)
class RemoteAssignee:
    name: str
    host: str
    remote_agent: str = DEFAULT_REMOTE_AGENT
    max_turns: int = DEFAULT_MAX_TURNS
    claim_ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS

    @property
    def claimer(self) -> str:
        return f"{CLAIMER_PREFIX}{self.name}"


def remote_assignee_names(kanban_cfg: Optional[dict]) -> frozenset[str]:
    """Every configured remote assignee name, valid spec or not — a malformed entry
    must still keep that name out of local spawning."""
    raw = (kanban_cfg or {}).get("remote_assignees")
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(str(name).strip().lower() for name in raw)


def configured_remote_assignee_names() -> frozenset[str]:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly()
    return remote_assignee_names(cfg.get("kanban") if isinstance(cfg, dict) else None)


def parse_remote_assignee(kanban_cfg: Optional[dict], name: str) -> RemoteAssignee:
    """The validated spec for ``name``; raises ``ValueError`` on anything unsafe."""
    key = str(name).strip().lower()
    raw = ((kanban_cfg or {}).get("remote_assignees") or {})
    spec = next((v for k, v in raw.items() if str(k).strip().lower() == key), None) if isinstance(raw, dict) else None
    if not _NAME_RE.match(key) or not isinstance(spec, dict):
        raise ValueError(f"no remote assignee {name!r} under kanban.remote_assignees")
    host = str(spec.get("host") or "")
    remote_agent = str(spec.get("remote_agent") or DEFAULT_REMOTE_AGENT)
    if not _HOST_RE.match(host):
        raise ValueError(f"kanban.remote_assignees.{key}.host must be a plain ssh host alias")
    if not _REMOTE_AGENT_RE.match(remote_agent) or ".." in remote_agent.split("/"):
        raise ValueError(f"kanban.remote_assignees.{key}.remote_agent must be a plain .py path")
    max_turns = int(spec.get("max_turns") or DEFAULT_MAX_TURNS)
    ttl = int(spec.get("claim_ttl_seconds") or DEFAULT_CLAIM_TTL_SECONDS)
    if not 1 <= max_turns <= 200 or ttl < 120:
        raise ValueError(f"kanban.remote_assignees.{key}: max_turns must be 1..200, claim_ttl_seconds >= 120")
    return RemoteAssignee(key, host, remote_agent, max_turns, ttl)


# --- Transport -------------------------------------------------------------------------

class SshTransport:
    """One protocol call = one ``ssh <host> python3 <agent> <verb> <card_id>``.

    Reuses the host's existing authenticated SSH route (the Desktop remote
    connection's host alias). Forwarding of every kind is disabled so the remote
    side gains no path back into this machine.
    """

    VERBS = frozenset({"accept", "start", "status", "cancel"})

    def __init__(self, assignee: RemoteAssignee, *, timeout: float = 60.0):
        self.assignee = assignee
        self.timeout = timeout

    def _argv(self, verb: str, card_id: str) -> list[str]:
        return [
            "ssh", "-T",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
            "-o", "PermitLocalCommand=no",
            self.assignee.host, "python3", self.assignee.remote_agent, verb, card_id,
        ]

    def call(self, verb: str, card_id: str, payload: Optional[dict] = None) -> dict:
        if verb not in self.VERBS or not _CARD_ID_RE.match(card_id):
            raise ValueError(f"refusing remote call {verb!r} {card_id!r}")
        try:
            proc = subprocess.run(
                self._argv(verb, card_id), input=json.dumps(payload or {}).encode("utf-8"),
                capture_output=True, timeout=self.timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransportError(f"{verb}: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            stderr = sanitize_text(proc.stderr.decode("utf-8", "replace"), 300)
            raise TransportError(f"{verb}: exit {proc.returncode}: {stderr}")
        return parse_response(proc.stdout, card_id)


def parse_response(raw: bytes, card_id: str) -> dict:
    """One JSON object for ``card_id``; ``ok: false`` becomes :class:`ProtocolRejected`."""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TransportError("remote response too large")
    try:
        resp = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TransportError("remote response is not JSON") from exc
    if not isinstance(resp, dict) or resp.get("card_id") != card_id or not isinstance(resp.get("ok"), bool):
        raise TransportError("remote response is not for this card")
    if not resp["ok"]:
        raise ProtocolRejected(sanitize_text(str(resp.get("error") or "refused"), 300))
    return resp


# --- Validation ------------------------------------------------------------------------

def sanitize_text(value: Any, limit: int) -> str:
    """Printable, secret-redacted, bounded text. Non-strings are a protocol violation."""
    if not isinstance(value, str):
        raise ProtocolRejected(f"expected text, got {type(value).__name__}")
    from agent.redact import redact_sensitive_text
    text = _CTRL_RE.sub("", _ANSI_RE.sub("", value)).strip()
    text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    if len(text) > limit:
        text = text[: max(0, limit - 14)].rstrip() + " [truncated]"
    return text


def validate_status(resp: dict) -> dict:
    state = resp.get("state")
    if state not in REMOTE_STATES:
        raise ProtocolRejected(f"unknown remote state {state!r}")
    body_sha = resp.get("body_sha256")
    if not isinstance(body_sha, str) or not _SHA256_RE.match(body_sha):
        raise ProtocolRejected("remote status lacks body_sha256")
    attempts = resp.get("attempts")
    age = resp.get("heartbeat_age_seconds")
    if not isinstance(attempts, int) or attempts < 0 or (age is not None and (not isinstance(age, int) or age < 0)):
        raise ProtocolRejected("remote status has malformed counters")
    return {
        "state": state, "body_sha256": body_sha, "attempts": attempts,
        "heartbeat_age_seconds": age, "host": sanitize_text(resp.get("host") or "", 255),
        "result": resp.get("result"),
    }


def _validate_artifact(raw: Any) -> dict:
    if not isinstance(raw, dict) or raw.get("kind") not in ARTIFACT_KINDS:
        kind = raw.get("kind") if isinstance(raw, dict) else type(raw).__name__
        raise ProtocolRejected(f"artifact kind {kind!r} is not allowed")
    kind, value = raw["kind"], raw.get("value")
    if kind == "json" and not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if not isinstance(value, str):
        raise ProtocolRejected(f"{kind} artifact value must be text")
    if kind in _URL_KINDS and not _URL_RE.match(value):
        raise ProtocolRejected(f"{kind} artifact must be an http(s) URL")
    if kind == "sha256" and not _SHA256_RE.match(value):
        raise ProtocolRejected("sha256 artifact must be 64 lowercase hex chars")
    if kind == "json":
        try:
            json.loads(value)
        except ValueError as exc:
            raise ProtocolRejected("json artifact is not valid JSON") from exc
    artifact = {"kind": kind, "value": value if kind == "sha256" else sanitize_text(value, MAX_ARTIFACT_CHARS)}
    if raw.get("label") is not None:
        artifact["label"] = sanitize_text(raw["label"], 200)
    return artifact


def validate_result(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ProtocolRejected("result is not an object")
    outcome = raw.get("outcome")
    if outcome not in FINAL_STATES:
        raise ProtocolRejected(f"result outcome {outcome!r} is not done/blocked")
    summary = sanitize_text(raw.get("summary"), MAX_SUMMARY_CHARS)
    if not summary:
        raise ProtocolRejected("result summary is empty")
    details = raw.get("details")
    block_kind = raw.get("block_kind")
    if block_kind is not None and block_kind not in REMOTE_BLOCK_KINDS:
        raise ProtocolRejected(f"block_kind {block_kind!r} is not allowed")
    artifacts = raw.get("artifacts") or []
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
        raise ProtocolRejected(f"artifacts must be a list of at most {MAX_ARTIFACTS}")
    return {
        "outcome": outcome, "summary": summary,
        "details": None if details is None else sanitize_text(details, MAX_DETAILS_CHARS),
        "block_kind": block_kind, "artifacts": [_validate_artifact(a) for a in artifacts],
    }


def accept_payload(card_id: str, title: str, body: Optional[str], max_turns: int) -> dict:
    """Only the card's id, title and body cross to the remote — no paths, comments or config."""
    body = body or ""
    return {"card_id": card_id, "title": title, "body": body, "max_turns": max_turns,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}


# --- Tick ------------------------------------------------------------------------------

@dataclasses.dataclass
class TickReport:
    assignee: str
    dispatched: list = dataclasses.field(default_factory=list)
    started: list = dataclasses.field(default_factory=list)
    heartbeats: list = dataclasses.field(default_factory=list)
    completed: list = dataclasses.field(default_factory=list)
    blocked: list = dataclasses.field(default_factory=list)
    transport_errors: list = dataclasses.field(default_factory=list)

    def changed(self) -> bool:
        return any((self.dispatched, self.started, self.heartbeats, self.completed,
                    self.blocked, self.transport_errors))

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def bridge_tick(conn, assignee: RemoteAssignee, transport) -> TickReport:
    """Advance this bridge's running cards, then dispatch new ready ones.

    With no ready or bridge-owned running card this is one SQLite read and no
    remote call at all.
    """
    report = TickReport(assignee.name)
    running = conn.execute(
        "SELECT id FROM tasks WHERE lower(assignee) = ? AND status = 'running' AND claim_lock = ? "
        "ORDER BY created_at, id", (assignee.name, assignee.claimer),
    ).fetchall()
    for row in running:
        _poll_running(conn, assignee, transport, row["id"], report)
    ready = conn.execute(
        "SELECT id, title, body FROM tasks WHERE lower(assignee) = ? AND status = 'ready' "
        "AND claim_lock IS NULL ORDER BY priority DESC, created_at, id", (assignee.name,),
    ).fetchall()
    for row in ready:
        _dispatch_ready(conn, assignee, transport, row, report)
    return report


def _dispatch_ready(conn, assignee: RemoteAssignee, transport, row, report: TickReport) -> None:
    from hermes_cli import kanban_db as kb
    card_id = row["id"]
    if not _CARD_ID_RE.match(card_id):
        return
    payload = accept_payload(card_id, row["title"], row["body"], assignee.max_turns)
    try:
        ack = validate_status(transport.call("accept", card_id, payload))
        if ack["body_sha256"] != payload["body_sha256"]:
            raise ProtocolRejected("acknowledged body hash differs from the card")
    except TransportError as exc:
        report.transport_errors.append([card_id, str(exc)])
        return
    except ProtocolRejected as exc:
        _block(conn, card_id, f"remote {assignee.name} refused dispatch: {exc}", "needs_input", report)
        return
    claimed = kb.claim_task(conn, card_id, ttl_seconds=assignee.claim_ttl_seconds, claimer=assignee.claimer)
    if claimed is None:
        # Lost a race (card moved or claimed meanwhile): release the remote entry.
        if ack["state"] == "accepted":
            with contextlib.suppress(TransportError, ProtocolRejected):
                transport.call("cancel", card_id)
        return
    with kb.write_txn(conn):
        kb._append_event(conn, card_id, "remote_dispatched", {
            "assignee": assignee.name, "remote_host": ack["host"], "remote_state": ack["state"],
        })
    report.dispatched.append(card_id)
    _advance(conn, assignee, transport, card_id, ack, report)


def _poll_running(conn, assignee: RemoteAssignee, transport, card_id: str, report: TickReport) -> None:
    from hermes_cli import kanban_db as kb
    try:
        status = validate_status(transport.call("status", card_id))
    except TransportError as exc:
        # Claim is not extended: if the remote stays unreachable past the TTL the
        # kernel returns the card to ready, and the next accept is idempotent.
        report.transport_errors.append([card_id, str(exc)])
        return
    except ProtocolRejected as exc:
        _block(conn, card_id, f"remote {assignee.name} status rejected: {exc}", "needs_input", report)
        return
    if not kb.heartbeat_claim(conn, card_id, ttl_seconds=assignee.claim_ttl_seconds, claimer=assignee.claimer):
        return
    _advance(conn, assignee, transport, card_id, status, report)


def _advance(conn, assignee: RemoteAssignee, transport, card_id: str, status: dict, report: TickReport) -> None:
    if status["state"] == "accepted":
        try:
            status = validate_status(transport.call("start", card_id))
        except TransportError as exc:
            report.transport_errors.append([card_id, str(exc)])
            return
        except ProtocolRejected as exc:
            _block(conn, card_id, f"remote {assignee.name} refused start: {exc}", "needs_input", report)
            return
        report.started.append(card_id)
    state = status["state"]
    if state == "running":
        _relay_heartbeat(conn, assignee, card_id, status, report)
    elif state in FINAL_STATES:
        _apply_result(conn, assignee, card_id, status, report)
    elif state == "cancelled":
        _block(conn, card_id, f"remote {assignee.name} journal shows this card cancelled", "needs_input", report)


def _relay_heartbeat(conn, assignee: RemoteAssignee, card_id: str, status: dict, report: TickReport) -> None:
    from hermes_cli import kanban_db_dispatch as kbd
    age = status["heartbeat_age_seconds"]
    if age is None or age > REMOTE_HEARTBEAT_FRESH_SECONDS:
        return
    row = conn.execute("SELECT last_heartbeat_at FROM tasks WHERE id = ?", (card_id,)).fetchone()
    last = row["last_heartbeat_at"] if row else None
    if last is not None and time.time() - int(last) < HEARTBEAT_RELAY_SECONDS:
        return
    note = f"remote {assignee.name}@{status['host']} alive (attempt {status['attempts']})"
    if kbd.heartbeat_worker(conn, card_id, note=note):
        report.heartbeats.append(card_id)


def _apply_result(conn, assignee: RemoteAssignee, card_id: str, status: dict, report: TickReport) -> None:
    from hermes_cli import kanban_db as kb
    try:
        result = validate_result(status["result"])
    except ProtocolRejected as exc:
        _block(conn, card_id, f"remote {assignee.name} returned a malformed result: {exc}", "needs_input", report)
        return
    if status["state"] == "done" and result["outcome"] == "done":
        metadata = {"remote_assignee": assignee.name, "remote_host": status["host"],
                    "remote_attempts": status["attempts"], "artifacts": result["artifacts"]}
        if result["details"]:
            metadata["details"] = result["details"]
        if kb.complete_task(conn, card_id, summary=result["summary"], metadata=metadata):
            report.completed.append(card_id)
        return
    if result["details"] or result["artifacts"]:
        kb.add_comment(conn, card_id, assignee.claimer, json.dumps(
            {"details": result["details"], "artifacts": result["artifacts"]}, indent=1,
        ))
    _block(conn, card_id, f"[{assignee.name}] {result['summary']}", result["block_kind"] or "needs_input", report)


def _block(conn, card_id: str, reason: str, kind: str, report: TickReport) -> None:
    from hermes_cli import kanban_db as kb
    if kb.block_task(conn, card_id, reason=reason[:MAX_REASON_CHARS], kind=kind):
        report.blocked.append(card_id)


@contextlib.contextmanager
def tick_lock(assignee: RemoteAssignee) -> Iterator[bool]:
    """Non-blocking per-assignee lock so overlapping ticks never double-dispatch."""
    from hermes_cli import kanban_db as kb
    lock_dir = kb.kanban_db_path().parent / "remote-bridge"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"{assignee.name}.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --- CLI -------------------------------------------------------------------------------

def _agent_source() -> Path:
    return Path(__file__).resolve().parents[1] / "plugins" / "kanban" / "remote_bridge" / "bridge_agent.py"


def install_remote_agent(assignee: RemoteAssignee) -> int:
    """Copy the journal agent to the remote host; succeeds only on a matching sha256."""
    source = _agent_source().read_bytes()
    target = assignee.remote_agent
    script = (
        f"umask 077 && mkdir -p {posixpath.dirname(target)}/journal && cat > {target}.tmp && "
        f"python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' {target}.tmp && "
        f"mv -f {target}.tmp {target} && sha256sum {target}"
    )
    proc = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ClearAllForwardings=yes",
         "-o", "ForwardAgent=no", "-o", "ForwardX11=no", assignee.host, script],
        input=source, capture_output=True, timeout=60, check=False,
    )
    local_sha = hashlib.sha256(source).hexdigest()
    remote_sha = proc.stdout.decode("utf-8", "replace").split()[:1]
    ok = proc.returncode == 0 and remote_sha == [local_sha]
    print(json.dumps({"assignee": assignee.name, "host": assignee.host, "remote_agent": target,
                      "sha256": local_sha, "installed": ok}))
    if not ok:
        print(sanitize_text(proc.stderr.decode("utf-8", "replace"), 500), file=sys.stderr)
    return 0 if ok else 1


def cmd_remote_bridge(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.config import load_config
    cfg = load_config()
    try:
        assignee = parse_remote_assignee(cfg.get("kanban") if isinstance(cfg, dict) else None, args.assignee)
    except ValueError as exc:
        print(f"kanban remote-bridge: {exc}", file=sys.stderr)
        return 2
    if args.remote_bridge_action == "install-agent":
        return install_remote_agent(assignee)
    with tick_lock(assignee) as acquired:
        if not acquired:
            return 0
        with kbc.connect_closing() as conn:
            report = bridge_tick(conn, assignee, SshTransport(assignee))
    # Silent when nothing changed, so an idle poller writes no log lines.
    if report.changed() or args.verbose:
        print(json.dumps({"ts": int(time.time()), **report.as_dict()}, sort_keys=True))
    return 1 if report.transport_errors else 0
