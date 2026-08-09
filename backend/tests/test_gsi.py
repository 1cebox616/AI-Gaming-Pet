"""CS2 Game State Integration parsing and receive-path tests."""

import asyncio
from copy import deepcopy
import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from pet.gsi import GSI_CONFIG_CONTENT, RawGsiRecorder, parse_snapshot
from pet.main import app


# Captured in a real M2-T1 casual match; Steam IDs, names, and cosmetic values are scrubbed.
COMPLETE_RECORDED_PAYLOAD: dict[str, object] = {
    "provider": {
        "name": "Counter-Strike: Global Offensive",
        "appid": 730,
        "version": 14174,
        "steamid": "76561198000000001",
        "timestamp": 1786289262,
    },
    "map": {
        "mode": "casual",
        "name": "de_anubis",
        "phase": "live",
        "round": 5,
        "team_ct": {"score": 2},
        "team_t": {"score": 3},
    },
    "round": {"phase": "freezetime"},
    "player": {
        "steamid": "76561198000000001",
        "activity": "playing",
        "team": "CT",
        "state": {
            "health": 100,
            "armor": 100,
            "helmet": True,
            "flashed": 0,
            "smoked": 0,
            "burning": 0,
            "money": 3400,
            "equip_value": 1600,
            "round_kills": 0,
            "round_killhs": 0,
        },
        "match_stats": {"kills": 0, "assists": 0, "deaths": 0, "mvps": 0, "score": 0},
        "weapons": {
            "weapon_0": {"name": "weapon_knife", "state": "holstered"},
            "weapon_1": {"name": "weapon_usp_silencer", "state": "active"},
        },
    },
}


def test_complete_recorded_payload_maps_every_snapshot_group() -> None:
    snapshot = parse_snapshot(COMPLETE_RECORDED_PAYLOAD, received_at=42.5)

    assert snapshot.model_dump() == {
        "ts": 42.5,
        "player_steamid": "76561198000000001",
        "provider_steamid": "76561198000000001",
        "activity": "playing",
        "map_mode": "casual",
        "map_name": "de_anubis",
        "map_phase": "live",
        "round_number": 5,
        "round_phase": "freezetime",
        "round_win_team": None,
        "bomb": None,
        "team": "CT",
        "health": 100,
        "armor": 100,
        "helmet": True,
        "flashed": 0,
        "smoked": 0,
        "burning": 0,
        "money": 3400,
        "equip_value": 1600,
        "round_kills": 0,
        "round_killhs": 0,
        "round_totaldmg": None,
        "match_kills": 0,
        "match_assists": 0,
        "match_deaths": 0,
        "match_mvps": 0,
        "match_score": 0,
        "score_ct": 2,
        "score_t": 3,
        "active_weapon": "weapon_usp_silencer",
    }


def test_recorded_payload_without_map_keeps_player_fields() -> None:
    payload = {key: value for key, value in COMPLETE_RECORDED_PAYLOAD.items() if key != "map"}

    snapshot = parse_snapshot(payload, received_at=1.0)

    assert snapshot.map_mode is None
    assert snapshot.map_name is None
    assert snapshot.round_number is None
    assert snapshot.score_ct is None
    assert snapshot.health == 100
    assert snapshot.match_kills == 0


def test_recorded_payload_without_player_keeps_map_fields() -> None:
    payload = {key: value for key, value in COMPLETE_RECORDED_PAYLOAD.items() if key != "player"}

    snapshot = parse_snapshot(payload, received_at=1.0)

    assert snapshot.player_steamid is None
    assert snapshot.health is None
    assert snapshot.active_weapon is None
    assert snapshot.map_mode == "casual"
    assert snapshot.round_number == 5


def test_type_errors_are_isolated_and_warn_once(caplog: pytest.LogCaptureFixture) -> None:
    payload = deepcopy(COMPLETE_RECORDED_PAYLOAD)
    payload["map"]["mode"] = 17  # type: ignore[index]
    payload["map"]["round"] = "six"  # type: ignore[index]
    payload["map"]["team_ct"] = []  # type: ignore[index]
    payload["player"]["state"]["health"] = "full"  # type: ignore[index]
    payload["player"]["state"]["helmet"] = 1  # type: ignore[index]
    caplog.set_level(logging.WARNING, logger="pet.gsi")

    first = parse_snapshot(payload, received_at=2.0)
    parse_snapshot(payload, received_at=3.0)

    assert first.provider_steamid == "76561198000000001"
    assert first.map_mode is None
    assert first.round_number is None
    assert first.health is None
    assert first.helmet is None
    assert first.score_ct is None
    assert caplog.text.count("map.mode has type int") == 1
    assert caplog.text.count("player.state.health has type str") == 1


def test_empty_payload_has_only_receive_timestamp() -> None:
    snapshot = parse_snapshot({}, received_at=9.25)

    assert snapshot.ts == 9.25
    assert all(value is None for key, value in snapshot.model_dump().items() if key != "ts")


def test_recorded_spectator_payload_preserves_both_identities() -> None:
    # Real death-spectator update with identities scrubbed; state belongs to the teammate.
    payload = {
        "provider": {"steamid": "76561198000000001"},
        "player": {
            "steamid": "76561198000000999",
            "activity": "playing",
            "team": "CT",
            "state": {"health": 22, "armor": 94, "round_kills": 3, "round_killhs": 1},
            "match_stats": {"kills": 6, "assists": 0, "deaths": 1, "mvps": 0, "score": 14},
            "weapons": {"weapon_4": {"name": "weapon_ak47", "state": "active"}},
        },
    }

    snapshot = parse_snapshot(payload, received_at=10.0)

    assert snapshot.provider_steamid == "76561198000000001"
    assert snapshot.player_steamid == "76561198000000999"
    assert snapshot.health == 22
    assert snapshot.round_kills == 3
    assert snapshot.match_kills == 6
    assert snapshot.active_weapon == "weapon_ak47"


def test_invalid_json_and_non_object_payloads_are_acknowledged() -> None:
    client = TestClient(app)

    malformed = client.post("/gsi", content=b"{not-json", headers={"content-type": "application/json"})
    non_object = client.post("/gsi", json=["unexpected"])

    assert malformed.status_code == 200
    assert malformed.json() == {"status": "ok"}
    assert non_object.status_code == 200
    assert non_object.json() == {"status": "ok"}


def test_raw_recorder_writes_timestamped_jsonl_without_blocking_callers(tmp_path: Path) -> None:
    async def exercise() -> Path:
        recorder = RawGsiRecorder(True, tmp_path)
        await recorder.start()
        assert recorder.path is not None
        recorder.record(12.5, {"player": {"activity": "menu"}})
        await recorder.shutdown()
        return recorder.path

    recording_path = asyncio.run(exercise())
    lines = recording_path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "ts": 12.5,
        "payload": {"player": {"activity": "menu"}},
    }


def test_generated_config_requests_every_required_data_group() -> None:
    for group in (
        "provider",
        "map",
        "round",
        "player_id",
        "player_state",
        "player_match_stats",
        "player_weapons",
        "bomb",
    ):
        assert f'"{group}" "1"' in GSI_CONFIG_CONTENT
    assert '"uri" "http://127.0.0.1:8737/gsi"' in GSI_CONFIG_CONTENT
