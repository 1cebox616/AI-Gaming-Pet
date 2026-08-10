"""Cross-match reset tests driven by existing scrubbed real GSI fixtures."""

import json
from pathlib import Path
from typing import Any

from pet.config import EventsConfig, PolicyConfig
from pet.events import EventDetector, GameEvent
from pet.gsi import GSI_SILENCE_SECONDS, GameSnapshot, parse_snapshot
from pet.policy import PolicyDecision, SpeechPolicy
from pet.session import GameSessionTracker, MatchLifecycleTracker

EVENT_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_event_samples.json"
SESSION_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_session_samples.json"


def _fixtures() -> tuple[dict[str, Any], dict[str, Any]]:
    event_data: object = json.loads(EVENT_FIXTURE_PATH.read_text(encoding="utf-8"))
    session_data: object = json.loads(SESSION_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(event_data, dict)
    assert isinstance(session_data, dict)
    return event_data, session_data


def _snapshot(entry: dict[str, Any]) -> GameSnapshot:
    return parse_snapshot(entry["payload"], received_at=entry["ts"])


def _process(
    snapshot: GameSnapshot,
    *,
    session: GameSessionTracker,
    lifecycle: MatchLifecycleTracker,
    detector: EventDetector,
    policy: SpeechPolicy,
) -> tuple[GameEvent, ...]:
    game = session.observe(snapshot)
    if lifecycle.observe(game):
        detector.reset()
        policy.reset()
    policy.observe_snapshot(snapshot)
    events = detector.observe(snapshot, game)
    return events


def test_menu_resets_round_limit_before_same_numbered_round_in_new_match() -> None:
    event_data, session_data = _fixtures()
    round_samples = tuple(
        _snapshot(entry)
        for entry in event_data["multi_kill_after_dropped_push"]["samples"]
    )
    menu = _snapshot(session_data["menu"])
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    lifecycle = MatchLifecycleTracker()
    detector = EventDetector(EventsConfig())
    policy = SpeechPolicy(
        PolicyConfig(
            cooldown_seconds=0,
            max_lines_per_round=1,
            alive_priority_threshold=0,
        )
    )

    selected: list[PolicyDecision] = []
    for snapshot in round_samples:
        events = _process(
            snapshot,
            session=session,
            lifecycle=lifecycle,
            detector=detector,
            policy=policy,
        )
        game = session.current(now=snapshot.ts)
        selected.extend(
            decision
            for decision in policy.decide(
                events, game, now=snapshot.ts, muted=False
            ).decisions
            if decision.selected
        )
    assert len(selected) == 1

    _process(
        menu,
        session=session,
        lifecycle=lifecycle,
        detector=detector,
        policy=policy,
    )
    new_match_selected: list[PolicyDecision] = []
    for snapshot in round_samples:
        events = _process(
            snapshot,
            session=session,
            lifecycle=lifecycle,
            detector=detector,
            policy=policy,
        )
        game = session.current(now=snapshot.ts)
        new_match_selected.extend(
            decision
            for decision in policy.decide(
                events, game, now=snapshot.ts, muted=False
            ).decisions
            if decision.selected
        )

    assert len(new_match_selected) == 1
    assert new_match_selected[0].event.round_number == selected[0].event.round_number == 6
    assert new_match_selected[0].reason_code == "selected"


def test_same_teammate_id_is_not_differenced_across_menu_boundary() -> None:
    event_data, session_data = _fixtures()
    teammate_samples = event_data["spectating_teammate_headshot"]["samples"]
    previous_match = _snapshot(teammate_samples[0])
    new_match = _snapshot(teammate_samples[1])
    menu = _snapshot(session_data["menu"])
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    lifecycle = MatchLifecycleTracker()
    detector = EventDetector(EventsConfig())
    policy = SpeechPolicy(PolicyConfig())

    assert _process(
        previous_match,
        session=session,
        lifecycle=lifecycle,
        detector=detector,
        policy=policy,
    ) == ()
    _process(
        menu,
        session=session,
        lifecycle=lifecycle,
        detector=detector,
        policy=policy,
    )
    events = _process(
        new_match,
        session=session,
        lifecycle=lifecycle,
        detector=detector,
        policy=policy,
    )

    assert previous_match.player_steamid == new_match.player_steamid
    assert previous_match.round_kills == 2
    assert new_match.round_kills == 3
    assert events == ()


def test_spectating_updates_do_not_reset_teammate_baseline() -> None:
    event_data, _ = _fixtures()
    snapshots = tuple(
        _snapshot(entry)
        for entry in event_data["spectating_teammate_headshot"]["samples"]
    )
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    lifecycle = MatchLifecycleTracker()
    detector = EventDetector(EventsConfig())
    policy = SpeechPolicy(PolicyConfig())

    events = tuple(
        event
        for snapshot in snapshots
        for event in _process(
            snapshot,
            session=session,
            lifecycle=lifecycle,
            detector=detector,
            policy=policy,
        )
    )

    assert [event.type for event in events] == ["kill_headshot", "multi_kill"]
    assert all(event.subject_is_self is False for event in events)


def test_explicit_resets_clear_all_detector_subjects_and_policy_state() -> None:
    event_data, _ = _fixtures()
    snapshots = tuple(
        _snapshot(entry)
        for entry in event_data["spectating_subject_switch"]["samples"]
    )
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    detector = EventDetector(EventsConfig())
    policy = SpeechPolicy(PolicyConfig(cooldown_seconds=0))
    for snapshot in snapshots:
        game = session.observe(snapshot)
        policy.observe_snapshot(snapshot)
        detector.observe(snapshot, game)

    assert len(detector._subjects) == 2
    policy._last_selected_at = snapshots[-1].ts
    policy._counted_round_number = 5
    policy._lines_this_round = 3

    detector.reset()
    policy.reset()

    assert len(detector._subjects) == 0
    assert policy._self_steamid is None
    assert policy._self_health is None
    assert policy._last_selected_at is None
    assert policy._counted_round_number is None
    assert policy._lines_this_round == 0
