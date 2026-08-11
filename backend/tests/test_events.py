"""Event detector tests built from scrubbed fragments of real GSI recordings."""

import json
from pathlib import Path
from typing import Any

import pytest

from pet.config import EventsConfig, load_config
from pet.events import EventDetector, GameEvent
from pet.gsi import GSI_SILENCE_SECONDS, GameSnapshot, parse_snapshot
from pet.replay import format_replay, replay_recording
from pet.session import GameSessionTracker

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_event_samples.json"
T7_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_t7_samples.json"
OVER_DEATH_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "gsi_over_death_samples.json"
)
SELF_STEAMID = "76561198000000001"


@pytest.fixture(scope="module")
def event_samples() -> dict[str, Any]:
    """Load trimmed real payloads whose identities have been scrubbed."""
    loaded: object = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def t7_samples() -> dict[str, Any]:
    """Load additional scrubbed fragments added from the same real recording set."""
    loaded: object = json.loads(T7_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _snapshots(event_samples: dict[str, Any], name: str) -> tuple[GameSnapshot, ...]:
    entries: object = event_samples[name]["samples"]
    assert isinstance(entries, list)
    return tuple(
        parse_snapshot(entry["payload"], received_at=entry["ts"]) for entry in entries
    )


def _t7_snapshots(t7_samples: dict[str, Any], name: str) -> tuple[GameSnapshot, ...]:
    entries: object = t7_samples[name]["samples"]
    assert isinstance(entries, list)
    return tuple(
        parse_snapshot(entry["payload"], received_at=entry["ts"]) for entry in entries
    )


def _detect(
    snapshots: tuple[GameSnapshot, ...], config: EventsConfig | None = None
) -> tuple[GameEvent, ...]:
    detector = EventDetector(config or EventsConfig())
    tracker = GameSessionTracker(GSI_SILENCE_SECONDS)
    return tuple(
        event
        for snapshot in snapshots
        for event in detector.observe(snapshot, tracker.observe(snapshot))
    )


def test_pitfall_cold_start_existing_stats_only_establishes_baseline(
    event_samples: dict[str, Any],
) -> None:
    snapshots = _snapshots(event_samples, "cold_start_existing_stats")
    detector = EventDetector(EventsConfig())
    tracker = GameSessionTracker(GSI_SILENCE_SECONDS)

    assert snapshots[0].round_kills == 3
    assert detector.observe(snapshots[0], tracker.observe(snapshots[0])) == ()


def test_pitfall_cold_start_round_result_only_establishes_baseline(
    event_samples: dict[str, Any],
) -> None:
    result_snapshot = _snapshots(event_samples, "round_win_dedup")[1]
    tracker = GameSessionTracker(GSI_SILENCE_SECONDS)

    assert EventDetector(EventsConfig()).observe(
        result_snapshot, tracker.observe(result_snapshot)
    ) == ()


def test_pitfall_round_reset_does_not_create_negative_or_death_events(
    event_samples: dict[str, Any],
) -> None:
    events = _detect(_snapshots(event_samples, "round_reset"))

    assert events == ()


def test_pitfall_multi_kill_delta_emits_each_kill_and_crossed_threshold(
    event_samples: dict[str, Any],
) -> None:
    events = _detect(_snapshots(event_samples, "multi_kill_after_dropped_push"))

    assert [event.type for event in events] == ["kill", "kill", "multi_kill"]
    assert [event.facts for event in events] == [
        {
            "round_kill_index": 1,
            "delta": 2,
            "weapon": "weapon_m4a1_silencer",
            "self_team": "CT",
            "self_score": 2,
            "opponent_score": 3,
            "score_situation": "落后",
            "team_consecutive_round_losses": None,
        },
        {
            "round_kill_index": 2,
            "delta": 2,
            "weapon": "weapon_m4a1_silencer",
            "self_team": "CT",
            "self_score": 2,
            "opponent_score": 3,
            "score_situation": "落后",
            "team_consecutive_round_losses": None,
        },
        {
            "count": 2,
            "self_team": "CT",
            "self_score": 2,
            "opponent_score": 3,
            "score_situation": "落后",
            "team_consecutive_round_losses": None,
        },
    ]
    assert [event.id for event in events] == [
        "event-00000001",
        "event-00000002",
        "event-00000003",
    ]
    assert all(event.round_number == 6 for event in events)


def test_pitfall_warmup_menu_offline_and_match_over_emit_nothing(
    event_samples: dict[str, Any],
) -> None:
    snapshots = _snapshots(event_samples, "suppressed_states")
    warmup_events = _detect(snapshots[:2])
    match_over_events = _detect(snapshots[2:3])
    menu_and_missing_events = _detect(snapshots[3:])

    assert snapshots[1].round_kills == 3
    assert warmup_events == ()
    assert match_over_events == ()
    assert menu_and_missing_events == ()


def test_pitfall_spectating_keeps_teammate_baseline_and_marks_events_non_self(
    event_samples: dict[str, Any],
) -> None:
    events = _detect(_snapshots(event_samples, "spectating_teammate_headshot"))

    assert [event.type for event in events] == ["kill_headshot", "multi_kill"]
    assert events[0].facts == {
        "round_kill_index": 3,
        "delta": 1,
        "weapon": "weapon_ak47",
        "self_team": "CT",
        "self_score": 2,
        "opponent_score": 2,
        "score_situation": "追平",
        "team_consecutive_round_losses": None,
    }
    assert events[1].facts == {
        "count": 3,
        "self_team": "CT",
        "self_score": 2,
        "opponent_score": 2,
        "score_situation": "追平",
        "team_consecutive_round_losses": None,
    }
    assert all(event.subject_steamid == "76561198000000901" for event in events)
    assert all(event.subject_is_self is False for event in events)


def test_pitfall_spectating_subject_switch_does_not_compare_two_teammates(
    event_samples: dict[str, Any],
) -> None:
    snapshots = _snapshots(event_samples, "spectating_subject_switch")

    assert snapshots[0].round_kills == 1
    assert snapshots[1].round_kills == 2
    assert _detect(snapshots) == ()


def test_first_round_result_has_finished_round_number_and_is_deduplicated(
    event_samples: dict[str, Any],
) -> None:
    events = _detect(_snapshots(event_samples, "round_win_dedup"))

    assert [event.type for event in events] == ["round_win"]
    assert events[0].subject_steamid == SELF_STEAMID
    assert events[0].subject_is_self is True
    assert events[0].round_number == 1
    assert events[0].facts == {
        "method": "elimination",
        "score_ct": 0,
        "score_t": 1,
        "self_team": "T",
        "self_score": 1,
        "opponent_score": 0,
        "score_situation": "领先",
        "team_consecutive_round_losses": None,
    }


def test_real_settlement_snapshot_uses_same_round_in_session_and_event(
    event_samples: dict[str, Any],
) -> None:
    snapshots = _snapshots(event_samples, "round_win_dedup")
    tracker = GameSessionTracker(GSI_SILENCE_SECONDS)
    detector = EventDetector(EventsConfig())
    game = None
    events: list[GameEvent] = []
    for snapshot in snapshots:
        game = tracker.observe(snapshot)
        events.extend(detector.observe(snapshot, game))

    assert game is not None
    assert game.state == "round_over"
    assert [event.type for event in events] == ["round_win"]
    assert game.round == events[0].round_number == 1


def test_second_round_loss_has_finished_round_number_and_uses_self_team(
    event_samples: dict[str, Any],
) -> None:
    events = _detect(_snapshots(event_samples, "round_loss_uses_self_team"))

    assert [event.type for event in events] == ["round_loss"]
    assert events[0].subject_steamid == SELF_STEAMID
    assert events[0].subject_is_self is True
    assert events[0].round_number == 2
    assert events[0].facts == {
        "method": "bomb",
        "score_ct": 0,
        "score_t": 2,
        "self_team": "CT",
        "self_score": 0,
        "opponent_score": 2,
        "score_situation": "落后",
        "team_consecutive_round_losses": None,
    }


def test_same_snapshot_trade_kill_is_classified_as_death_after_kill(
    event_samples: dict[str, Any],
) -> None:
    events = _detect(_snapshots(event_samples, "ordinary_death_with_trade_kill"))

    assert [event.type for event in events] == ["kill_headshot", "death_after_kill"]
    assert events[0].facts == {
        "round_kill_index": 1,
        "delta": 1,
        "weapon": None,
        "self_team": "T",
        "self_score": 1,
        "opponent_score": 0,
        "score_situation": "领先",
        "team_consecutive_round_losses": None,
    }
    assert events[1].facts["survival_seconds"] == pytest.approx(11.7535846)
    assert events[1].facts["round_kills"] == 1
    assert events[1].facts["seconds_since_last_kill"] == 0
    assert events[1].facts["equip_value"] == 4200
    assert events[1].subject_is_self is True


def test_death_after_kill_uses_real_kill_interval_and_positive_classification(
    t7_samples: dict[str, Any],
) -> None:
    events = _detect(_t7_snapshots(t7_samples, "death_after_kill"))

    assert [event.type for event in events] == ["kill", "multi_kill", "death_after_kill"]
    death = events[-1]
    assert death.round_number == 6
    assert death.facts == {
        "survival_seconds": None,
        "round_kills": 3,
        "seconds_since_last_kill": pytest.approx(0.7124858),
        "equip_value": 4500,
        "self_team": "CT",
        "self_score": 2,
        "opponent_score": 3,
        "score_situation": "落后",
        "team_consecutive_round_losses": 1,
    }


def test_death_classifications_are_mutually_exclusive_on_real_fragments(
    event_samples: dict[str, Any], t7_samples: dict[str, Any]
) -> None:
    after_kill = _detect(_t7_snapshots(t7_samples, "death_after_kill"))[-1]
    ordinary = _detect(_snapshots(event_samples, "teammate_thrown_away"))[-1]
    thrown_away = _detect(
        _snapshots(event_samples, "teammate_thrown_away"),
        EventsConfig(thrown_away_max_survival_seconds=30),
    )[-1]

    assert after_kill.type == "death_after_kill"
    assert ordinary.type == "death"
    assert thrown_away.type == "death_thrown_away"
    assert {after_kill.type, ordinary.type, thrown_away.type} == {
        "death_after_kill",
        "death",
        "death_thrown_away",
    }


def test_death_after_kill_threshold_changes_real_fragment_classification(
    t7_samples: dict[str, Any],
) -> None:
    """Reuse recorded payloads while stretching only their replay time spacing."""
    snapshots = _t7_snapshots(t7_samples, "death_after_kill")
    stretched = (
        snapshots[0],
        snapshots[1],
        snapshots[2].model_copy(update={"ts": snapshots[1].ts + 5.0}),
    )

    permissive = _detect(stretched, EventsConfig(death_after_kill_max_seconds=8))
    strict = _detect(stretched, EventsConfig(death_after_kill_max_seconds=4))

    assert permissive[-1].type == "death_after_kill"
    assert permissive[-1].facts["seconds_since_last_kill"] == pytest.approx(5.0)
    assert strict[-1].type == "death"


def test_score_situation_facts_cover_leading_trailing_and_tied_real_states(
    event_samples: dict[str, Any], t7_samples: dict[str, Any]
) -> None:
    leading = _detect(_snapshots(event_samples, "round_win_dedup"))[0]
    trailing = _detect(_snapshots(event_samples, "round_loss_uses_self_team"))[0]
    tied = _detect(_t7_snapshots(t7_samples, "tied_death"))[-1]

    assert leading.facts["score_situation"] == "领先"
    assert trailing.facts["score_situation"] == "落后"
    assert tied.facts["score_situation"] == "追平"
    assert tied.facts["team_consecutive_round_losses"] == 0


def test_configured_thrown_away_threshold_changes_same_real_death_classification(
    event_samples: dict[str, Any], tmp_path: Path
) -> None:
    snapshots = _snapshots(event_samples, "teammate_thrown_away")
    strict_path = tmp_path / "strict.toml"
    lenient_path = tmp_path / "lenient.toml"
    strict_path.write_text(
        "[events]\nthrown_away_max_survival_seconds = 15\n"
        "thrown_away_min_equip_value = 3000\n",
        encoding="utf-8",
    )
    lenient_path.write_text(
        "[events]\nthrown_away_max_survival_seconds = 30\n"
        "thrown_away_min_equip_value = 3000\n",
        encoding="utf-8",
    )
    strict_config = load_config(strict_path, tmp_path / "missing-local.toml").events
    lenient_config = load_config(lenient_path, tmp_path / "missing-local.toml").events

    default_events = _detect(snapshots, strict_config)
    lenient_events = _detect(snapshots, lenient_config)

    assert [event.type for event in default_events] == ["death"]
    assert [event.type for event in lenient_events] == ["death_thrown_away"]
    assert default_events[0].facts["survival_seconds"] == pytest.approx(23.6857683)
    assert default_events[0].facts["round_kills"] == 0
    assert default_events[0].facts["equip_value"] == 4350
    assert default_events[0].subject_steamid == "76561198000000902"
    assert default_events[0].subject_is_self is False


def test_death_detected_during_over_phase_belongs_to_finished_round() -> None:
    loaded: object = json.loads(OVER_DEATH_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    samples = loaded["samples"]
    snapshots = tuple(
        parse_snapshot(sample["payload"], received_at=sample["ts"])
        for sample in samples
    )

    events = _detect(snapshots)

    assert [event.type for event in events] == ["death"]
    assert events[0].round_number == 2
    assert events[0].subject_steamid == "76561198000000904"


def test_same_recorded_sequence_is_deterministic(event_samples: dict[str, Any]) -> None:
    snapshots = _snapshots(event_samples, "multi_kill_after_dropped_push")

    first = _detect(snapshots)
    second = _detect(snapshots)

    assert first == second
    assert [event.model_dump() for event in first] == [
        event.model_dump() for event in second
    ]


def test_replay_tool_sorts_recorded_rows_and_formats_chinese_summary(
    event_samples: dict[str, Any], tmp_path: Path
) -> None:
    samples = event_samples["round_win_dedup"]["samples"]
    recording = tmp_path / "scrubbed-real-fragment.jsonl"
    recording.write_text(
        "\n".join(json.dumps(sample, ensure_ascii=False) for sample in reversed(samples)),
        encoding="utf-8",
    )

    first = replay_recording(recording, EventsConfig())
    second = replay_recording(recording, EventsConfig())
    rendered = format_replay(first, started_at=min(sample["ts"] for sample in samples))

    assert first == second
    assert [event.type for event in first.events] == ["round_win"]
    assert first.snapshot_count == 3
    assert first.rounds_covered == 1
    assert "事件时间线：" in rendered
    assert "回合胜利" in rendered
    assert "处理快照：3" in rendered
