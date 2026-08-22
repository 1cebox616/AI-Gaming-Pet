"""Session-state tests built from scrubbed fragments of the M2-T1 recording."""

import json
from pathlib import Path
from typing import Any

import pytest

from pet.games.cs2.gsi import GSI_SILENCE_SECONDS, parse_snapshot
from pet.games.cs2.session import GameSessionTracker, GameState

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_session_samples.json"


@pytest.fixture(scope="module")
def recorded_samples() -> dict[str, dict[str, Any]]:
    """Load representative real payload fragments with identities scrubbed."""
    loaded: object = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("sample_name", "expected"),
    (
        (
            "menu",
            GameState(
                state="menu",
                subject_steamid="76561198000000001",
                subject_is_self=True,
            ),
        ),
        (
            "warmup",
            GameState(
                state="warmup",
                mode="casual",
                map="de_nuke",
                round=1,
                score_ct=0,
                score_t=0,
                subject_steamid="76561198000000001",
                subject_is_self=True,
            ),
        ),
        (
            "playing",
            GameState(
                state="playing",
                mode="casual",
                map="de_anubis",
                round=6,
                score_ct=2,
                score_t=3,
                subject_steamid="76561198000000001",
                subject_is_self=True,
            ),
        ),
        (
            "spectating",
            GameState(
                state="spectating",
                mode="casual",
                map="de_anubis",
                round=6,
                score_ct=2,
                score_t=3,
                subject_steamid="76561198000000999",
                subject_is_self=False,
            ),
        ),
        (
            "round_over",
            GameState(
                state="round_over",
                mode="casual",
                map="de_anubis",
                round=6,
                score_ct=3,
                score_t=3,
                subject_steamid="76561198000000999",
                subject_is_self=False,
            ),
        ),
        (
            "match_over",
            GameState(
                state="match_over",
                mode="casual",
                map="de_anubis",
                round=9,
                score_ct=1,
                score_t=8,
                subject_steamid="76561198000000001",
                subject_is_self=True,
            ),
        ),
    ),
)
def test_recorded_session_states(
    recorded_samples: dict[str, dict[str, Any]],
    sample_name: str,
    expected: GameState,
) -> None:
    sample = recorded_samples[sample_name]
    snapshot = parse_snapshot(sample["payload"], received_at=sample["ts"])
    tracker = GameSessionTracker(offline_timeout_seconds=GSI_SILENCE_SECONDS)

    assert tracker.observe(snapshot) == expected


def test_spectating_subject_is_the_teammate_not_the_provider(
    recorded_samples: dict[str, dict[str, Any]],
) -> None:
    sample = recorded_samples["spectating"]
    snapshot = parse_snapshot(sample["payload"], received_at=sample["ts"])
    tracker = GameSessionTracker(offline_timeout_seconds=GSI_SILENCE_SECONDS)

    state = tracker.observe(snapshot)

    assert state.state == "spectating"
    assert state.subject_steamid == "76561198000000999"
    assert state.subject_steamid != "76561198000000001"
    assert state.subject_is_self is False


def test_snapshot_silence_transitions_to_offline(
    recorded_samples: dict[str, dict[str, Any]],
) -> None:
    sample = recorded_samples["playing"]
    snapshot = parse_snapshot(sample["payload"], received_at=sample["ts"])
    tracker = GameSessionTracker(offline_timeout_seconds=GSI_SILENCE_SECONDS)
    tracker.observe(snapshot)

    state = tracker.current(now=snapshot.ts + GSI_SILENCE_SECONDS + 0.001)

    assert state == GameState.offline()
    assert tracker.observe(
        parse_snapshot({}, received_at=snapshot.ts + GSI_SILENCE_SECONDS + 1.0)
    ).state == "offline"


def test_missing_fields_are_safe_and_have_specific_states() -> None:
    tracker = GameSessionTracker(offline_timeout_seconds=GSI_SILENCE_SECONDS)

    assert tracker.observe(parse_snapshot({}, received_at=1.0)) == GameState.offline()
    assert tracker.observe(
        parse_snapshot({"provider": {"steamid": "76561198000000001"}}, received_at=2.0)
    ) == GameState(
        state="menu",
        subject_steamid="76561198000000001",
        subject_is_self=True,
    )

    assert tracker.observe(parse_snapshot({}, received_at=3.0)).state == "menu"
