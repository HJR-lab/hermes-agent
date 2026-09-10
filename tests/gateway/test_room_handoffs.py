import sqlite3

from gateway.config import GatewayConfig, load_gateway_config
from gateway.room_handoffs import consume_pending, parse_room_handoff_routes, publish_reply


ROOM = "-1004406187322"


def test_room_handoff_is_restart_safe_bidirectional_deduplicated_and_bounded(tmp_path):
    ledger = tmp_path / "room-handoffs.db"
    routes = parse_room_handoff_routes([{
        "platform": "telegram",
        "chat_id": ROOM,
        "profiles": ["default", "doe-domain"],
        "max_entries": 2,
        "retention_days": 1,
    }])

    assert publish_reply(
        routes, platform="telegram", chat_id=ROOM, message_id="101",
        source_profile="default", content="Ever status sk-abcdefghijklmnop", path=ledger, now=100_000,
    )
    assert not publish_reply(
        routes, platform="telegram", chat_id=ROOM, message_id="101",
        source_profile="default", content="duplicate", path=ledger, now=100_001,
    )
    assert publish_reply(
        routes, platform="telegram", chat_id=ROOM, message_id="102",
        source_profile="doe-domain", content="DOE escalation to Ever", path=ledger, now=100_002,
    )
    # Re-open through a fresh call/connection: both directions survive process restarts.
    doe_notes = consume_pending(
        routes, platform="telegram", chat_id=ROOM, target_profile="doe-domain",
        path=ledger, now=100_003,
    )
    ever_notes = consume_pending(
        routes, platform="telegram", chat_id=ROOM, target_profile="default",
        path=ledger, now=100_004,
    )

    assert len(doe_notes) == len(ever_notes) == 1
    assert "agent-authored context, not a human message" in doe_notes[0]
    assert "Source profile: default" in doe_notes[0]
    assert "Source Telegram turn message: 101" in doe_notes[0]
    assert "abcdefghijklmnop" not in doe_notes[0]
    assert "Source profile: doe-domain" in ever_notes[0]
    assert "DOE escalation to Ever" in ever_notes[0]
    assert consume_pending(
        routes, platform="telegram", chat_id=ROOM, target_profile="default",
        path=ledger, now=100_005,
    ) == []

    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT COUNT(*) FROM room_handoffs").fetchone()[0] <= 2


def test_room_handoff_config_is_exactly_scoped_and_round_trips(caplog, tmp_path, monkeypatch):
    cfg = GatewayConfig.from_dict({"gateway": {"room_handoffs": [{
        "platform": "telegram",
        "chat_id": -1004406187322,
        "profiles": ["default", "doe-domain"],
    }]}})

    assert len(cfg.room_handoffs) == 1
    route = cfg.room_handoffs[0]
    assert route.chat_id == ROOM
    assert route.peer_for("default") == "doe-domain"
    assert route.peer_for("doe-domain") == "default"
    assert route.peer_for("ves") is None
    assert cfg.to_dict()["room_handoffs"][0]["profiles"] == ["default", "doe-domain"]

    # Exercise the real config.yaml bridge used at gateway startup, not only from_dict.
    (tmp_path / "config.yaml").write_text(
        "gateway:\n"
        "  room_handoffs:\n"
        "    - platform: telegram\n"
        f"      chat_id: '{ROOM}'\n"
        "      profiles: [default, doe-domain]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert load_gateway_config().room_handoffs[0].chat_id == ROOM

    disabled = GatewayConfig.from_dict({"gateway": {"room_handoffs": [{
        "platform": "discord", "chat_id": ROOM, "profiles": ["default", "doe-domain"],
    }]}})
    assert disabled.room_handoffs == []
    assert "only platform=telegram is supported" in caplog.text
