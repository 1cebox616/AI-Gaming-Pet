"""CS2 Game State Integration parsing and receive-path tests."""

import asyncio
from copy import deepcopy
import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from pet.gsi import GSI_CONFIG_CONTENT, RawGsiRecorder, RoundWin, parse_snapshot
from pet.main import app

T7_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_t7_samples.json"

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
        "match_kills": 0,
        "match_assists": 0,
        "match_deaths": 0,
        "match_mvps": 0,
        "match_score": 0,
        "score_ct": 2,
        "score_t": 3,
        "ct_consecutive_round_losses": None,
        "t_consecutive_round_losses": None,
        "round_wins": None,
        "active_weapon": "weapon_usp_silencer",
    }


def test_real_payload_parses_team_consecutive_round_losses() -> None:
    """The new fields are taken from a scrubbed fragment of a real casual recording."""
    loaded: object = json.loads(T7_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    samples: object = loaded["death_after_kill"]["samples"]
    assert isinstance(samples, list)

    snapshot = parse_snapshot(samples[0]["payload"], received_at=samples[0]["ts"])

    assert snapshot.ct_consecutive_round_losses == 1
    assert snapshot.t_consecutive_round_losses == 0


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


def test_real_round_wins_history_preserves_round_team_and_method() -> None:
    # Captured after a real 2:8 casual match; unrelated payload fields are omitted.
    payload = {
        "map": {
            "round_wins": {
                "1": "t_win_elimination",
                "4": "ct_win_defuse",
                "10": "t_win_bomb",
            }
        }
    }

    snapshot = parse_snapshot(payload, received_at=20.0)

    assert snapshot.round_wins == (
        RoundWin(round=1, team="T", method="elimination"),
        RoundWin(round=4, team="CT", method="defuse"),
        RoundWin(round=10, team="T", method="bomb"),
    )


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
        "map_round_wins",
    ):
        assert f'"{group}" "1"' in GSI_CONFIG_CONTENT
    for unused_group in ("phase_countdowns", "bomb", "allplayers_state"):
        assert f'"{unused_group}" "1"' not in GSI_CONFIG_CONTENT
    assert '"uri" "http://127.0.0.1:8737/gsi"' in GSI_CONFIG_CONTENT
