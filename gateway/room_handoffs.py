"""Restart-safe local handoffs between profiles sharing a messaging room.

Telegram does not deliver bot-authored group messages to other bots.  This
ledger mirrors completed agent replies locally and exposes them as one-shot
per-turn context to another configured profile; it never creates a synthetic
platform event or transcript user row.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from hermes_constants import get_default_hermes_root

logger = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 32_000
_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_MAX_ENTRIES = 1_000


@dataclass(frozen=True)
class RoomHandoffRoute:
    platform: str
    chat_id: str
    profiles: tuple[str, str]
    retention_days: int = _DEFAULT_RETENTION_DAYS
    max_entries: int = _DEFAULT_MAX_ENTRIES

    def peer_for(self, profile: str) -> str | None:
        if profile == self.profiles[0]:
            return self.profiles[1]
        if profile == self.profiles[1]:
            return self.profiles[0]
        return None


def parse_room_handoff_routes(value: Any) -> list[RoomHandoffRoute]:
    """Validate opt-in room routes; malformed entries are skipped visibly."""
    if value in (None, []):
        return []
    if not isinstance(value, list):
        logger.warning("Invalid gateway.room_handoffs (expected a list); disabling room handoffs")
        return []

    routes: list[RoomHandoffRoute] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise ValueError("entry must be a mapping")
            platform = str(raw.get("platform") or "").strip().lower()
            chat_id = str(raw.get("chat_id") or "").strip()
            profiles_raw = raw.get("profiles")
            if platform != "telegram":
                raise ValueError("only platform=telegram is supported")
            if not chat_id:
                raise ValueError("chat_id is required")
            if not isinstance(profiles_raw, list) or len(profiles_raw) != 2:
                raise ValueError("profiles must contain exactly two profile names")
            profiles: tuple[str, str] = (
                str(profiles_raw[0]).strip(), str(profiles_raw[1]).strip()
            )
            if not all(profiles) or profiles[0] == profiles[1]:
                raise ValueError("profiles must contain two distinct non-empty names")
            retention_days = int(raw.get("retention_days", _DEFAULT_RETENTION_DAYS))
            max_entries = int(raw.get("max_entries", _DEFAULT_MAX_ENTRIES))
            if not 1 <= retention_days <= 365:
                raise ValueError("retention_days must be between 1 and 365")
            if not 1 <= max_entries <= 100_000:
                raise ValueError("max_entries must be between 1 and 100000")
            key = (platform, chat_id)
            if key in seen:
                raise ValueError("platform/chat_id is duplicated")
            seen.add(key)
            routes.append(RoomHandoffRoute(
                platform=platform, chat_id=chat_id, profiles=profiles,
                retention_days=retention_days, max_entries=max_entries,
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("Skipping invalid gateway.room_handoffs[%d]: %s", index, exc)
    return routes


def routes_to_dict(routes: Iterable[RoomHandoffRoute | Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for route in routes:
        if isinstance(route, RoomHandoffRoute):
            item = asdict(route)
            item["profiles"] = list(route.profiles)
        else:
            item = dict(route)
        result.append(item)
    return result


def effective_profile_name(source_profile: Any = None) -> str:
    """Explicit multiplex owner, else the active single-gateway profile."""
    explicit = str(source_profile or "").strip()
    if explicit:
        return explicit
    try:
        from hermes_cli.profiles import get_active_profile_name

        return str(get_active_profile_name() or "default").strip() or "default"
    except Exception:
        logger.exception("Room handoff could not resolve the active profile; using default")
        return "default"


def _ledger_path() -> Path:
    return get_default_hermes_root() / "room-handoffs.db"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or _ledger_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS room_handoffs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               platform TEXT NOT NULL,
               chat_id TEXT NOT NULL,
               message_id TEXT NOT NULL,
               source_profile TEXT NOT NULL,
               target_profile TEXT NOT NULL,
               content TEXT NOT NULL,
               created_at REAL NOT NULL,
               consumed_at REAL,
               UNIQUE(platform, chat_id, message_id, source_profile)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_room_handoffs_pending "
        "ON room_handoffs(platform, chat_id, target_profile, consumed_at, id)"
    )
    conn.commit()
    with contextlib.suppress(OSError):
        os.chmod(db_path, 0o600)
    return conn


def _route_for(
    routes: Sequence[RoomHandoffRoute], platform: str, chat_id: str, source_profile: str,
) -> tuple[RoomHandoffRoute, str] | None:
    for route in routes:
        if route.platform == platform and route.chat_id == chat_id:
            target = route.peer_for(source_profile)
            if target:
                return route, target
    return None


def _redact(content: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(content, force=True)
    except Exception:
        logger.exception("Room handoff secret redaction failed; refusing to persist reply")
        raise


def _prune(conn: sqlite3.Connection, route: RoomHandoffRoute, now: float) -> None:
    conn.execute(
        "DELETE FROM room_handoffs WHERE platform=? AND chat_id=? AND created_at<?",
        (route.platform, route.chat_id, now - route.retention_days * 86400),
    )
    conn.execute(
        """DELETE FROM room_handoffs WHERE id IN (
               SELECT id FROM room_handoffs WHERE platform=? AND chat_id=?
               ORDER BY id DESC LIMIT -1 OFFSET ?
           )""",
        (route.platform, route.chat_id, route.max_entries),
    )


def publish_reply(
    routes: Sequence[RoomHandoffRoute], *, platform: str, chat_id: str,
    message_id: str, source_profile: str, content: str, path: Path | None = None,
    now: float | None = None,
) -> bool:
    """Persist one reply for its configured peer; duplicate source turns are no-ops."""
    matched = _route_for(routes, platform, chat_id, source_profile)
    if matched is None or not message_id or not str(content).strip():
        return False
    route, target_profile = matched
    timestamp = time.time() if now is None else now
    safe_content = _redact(str(content).strip())[:_MAX_CONTENT_CHARS]
    with _connect(path) as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO room_handoffs
               (platform, chat_id, message_id, source_profile, target_profile, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (platform, chat_id, str(message_id), source_profile, target_profile, safe_content, timestamp),
        )
        _prune(conn, route, timestamp)
        inserted = cursor.rowcount == 1
    if inserted:
        logger.info(
            "Room handoff recorded: platform=%s chat=%s message=%s source=%s target=%s",
            platform, chat_id, message_id, source_profile, target_profile,
        )
    return inserted


def consume_pending(
    routes: Sequence[RoomHandoffRoute], *, platform: str, chat_id: str,
    target_profile: str, path: Path | None = None, now: float | None = None,
) -> list[str]:
    """Atomically consume pending peer replies and render provenance-labelled notes."""
    route = next((
        item for item in routes
        if item.platform == platform and item.chat_id == chat_id and target_profile in item.profiles
    ), None)
    if route is None:
        return []
    timestamp = time.time() if now is None else now
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _prune(conn, route, timestamp)
        rows = conn.execute(
            """SELECT id, message_id, source_profile, content, created_at
               FROM room_handoffs
               WHERE platform=? AND chat_id=? AND target_profile=? AND consumed_at IS NULL
               ORDER BY id""",
            (platform, chat_id, target_profile),
        ).fetchall()
        if rows:
            conn.executemany(
                "UPDATE room_handoffs SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                [(timestamp, row["id"]) for row in rows],
            )
    notes = [
        "[Internal room handoff — agent-authored context, not a human message]\n"
        f"Source profile: {row['source_profile']}\n"
        f"Telegram room: {chat_id}\n"
        f"Source Telegram turn message: {row['message_id']}\n"
        f"Recorded at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(row['created_at']))}\n"
        f"Peer reply:\n{row['content']}"
        for row in rows
    ]
    if notes:
        logger.info(
            "Room handoff consumed: platform=%s chat=%s target=%s count=%d",
            platform, chat_id, target_profile, len(notes),
        )
    return notes
