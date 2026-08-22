"""Speech-policy tests driven by scrubbed fragments of real GSI recordings."""

import json
from pathlib import Path
from typing import Any

import pytest

from pet.config import EventsConfig, PolicyConfig
from pet.events import EventDetector, EventType, GameEvent
from pet.gsi import GSI_SILENCE_SECONDS, GameSnapshot, parse_snapshot
from pet.policy import PolicyDecision, SpeechPolicy
from pet.replay import format_decision_reason, replay_policy
from pet.session import GameSessionTracker, GameState

EVENT_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_event_samples.json"
SESSION_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_session_samples.json"


@pytest.fixture(scope="module")
def recorded_fixtures() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the existing identity-scrubbed real recording fragments."""
    event_data: object = json.loads(EVENT_FIXTURE_PATH.read_text(encoding="utf-8"))
    session_data: object = json.loads(SESSION_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(event_data, dict)
    assert isinstance(session_data, dict)
    return event_data, session_data


def _snapshot(entry: dict[str, Any]) -> GameSnapshot:
    return parse_snapshot(entry["payload"], received_at=entry["ts"])


def _event_sequence(
    fixtures: tuple[dict[str, Any], dict[str, Any]], name: str
) -> tuple[GameSnapshot, ...]:
    event_data, _ = fixtures
    return tuple(_snapshot(entry) for entry in event_data[name]["samples"])


def _three_kill_sequence(
    fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> tuple[GameSnapshot, ...]:
    event_data, _ = fixtures
    baseline = event_data["multi_kill_after_dropped_push"]["samples"][0]
    three_kills = event_data["cold_start_existing_stats"]["samples"][0]
    return (_snapshot(baseline), _snapshot(three_kills))


def _same_round_timeline(
    fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> tuple[GameSnapshot, ...]:
    event_data, session_data = fixtures
    multi_samples = event_data["multi_kill_after_dropped_push"]["samples"]
    three_kills = event_data["cold_start_existing_stats"]["samples"][0]
    round_over = session_data["round_over"]
    return tuple(
        _snapshot(entry)
        for entry in (*multi_samples, three_kills, round_over)
    )


def _timeline_with_next_round(
    fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> tuple[GameSnapshot, ...]:
    event_data, _ = fixtures
    next_round = event_data["round_win_dedup"]["samples"][:2]
    return (*_same_round_timeline(fixtures), *(_snapshot(entry) for entry in next_round))


def _decisions(
    snapshots: tuple[GameSnapshot, ...],
    config: PolicyConfig | None = None,
    *,
    muted: bool = False,
) -> tuple[PolicyDecision, ...]:
    return replay_policy(
        snapshots,
        EventsConfig(),
        config or PolicyConfig(),
        muted=muted,
    ).decisions


def _cooldown_override_decisions(
    fixtures: tuple[dict[str, Any], dict[str, Any]],
    config: PolicyConfig,
    *,
    last_now: float | None = None,
) -> tuple[PolicyDecision, ...]:
    """Run three ordered real snapshots, optionally compressing the final interval."""
    snapshots = _same_round_timeline(fixtures)[:3]
    detector = EventDetector(EventsConfig())
    tracker = GameSessionTracker(GSI_SILENCE_SECONDS)
    policy = SpeechPolicy(config)
    decisions: list[PolicyDecision] = []
    for index, snapshot in enumerate(snapshots):
        game = tracker.observe(snapshot)
        policy.observe_snapshot(snapshot)
        events = detector.observe(snapshot, game)
        now = last_now if index == len(snapshots) - 1 and last_now is not None else snapshot.ts
        decisions.extend(policy.decide(events, game, now=now, muted=False).decisions)
    return tuple(decisions)


def _policy_event(
    event_id: str,
    event_type: EventType,
    *,
    ts: float,
    round_number: int = 1,
    count: int | None = None,
) -> GameEvent:
    """Create a minimal self event for direct policy behavior tests."""
    facts: dict[str, object] = {}
    if count is not None:
        facts["count"] = count
    return GameEvent(
        id=event_id,
        type=event_type,
        ts=ts,
        subject_steamid="self",
        subject_is_self=True,
        round_number=round_number,
        facts=facts,
    )


def _playing_game(round_number: int = 1) -> GameState:
    """Build a self-owned round state without live-combat threshold filtering."""
    return GameState(state="spectating", round=round_number, subject_is_self=True)


def test_alive_combat_drops_normal_kills_but_allows_three_kill(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    decisions = _decisions(
        _three_kill_sequence(recorded_fixtures),
        PolicyConfig(alive_priority_threshold=75),
    )
    normal_kills = [decision for decision in decisions if decision.event.type == "kill"]
    three_kill = next(
        decision
        for decision in decisions
        if decision.event.type == "multi_kill" and decision.event.facts["count"] == 3
    )

    assert normal_kills
    assert all(decision.selected is False for decision in normal_kills)
    assert all(decision.reason_code == "alive_threshold" for decision in normal_kills)
    assert format_decision_reason(normal_kills[0]) == "交火中，优先级 20 未达门槛 75"
    assert three_kill.selected is True
    assert three_kill.priority == 80
    assert format_decision_reason(three_kill) == "优先级 80"


def test_round_result_is_detected_but_excluded_from_speech_candidates(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    decisions = _decisions(
        _event_sequence(recorded_fixtures, "round_win_dedup"),
        PolicyConfig(cooldown_seconds=0, alive_priority_threshold=0),
    )

    round_decisions = [
        decision
        for decision in decisions
        if decision.event.type == "round_win"
    ]
    assert round_decisions
    assert all(not decision.selected for decision in round_decisions)
    assert all(decision.reason_code == "round_event" for decision in round_decisions)


def test_death_after_kill_is_not_blocked_by_alive_combat_threshold(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    snapshots = _event_sequence(recorded_fixtures, "ordinary_death_with_trade_kill")
    tracker = GameSessionTracker(GSI_SILENCE_SECONDS)
    game = None
    for snapshot in snapshots:
        game = tracker.observe(snapshot)
    decisions = _decisions(snapshots)
    death = next(
        decision for decision in decisions if decision.event.type == "death_after_kill"
    )

    assert game is not None
    assert game.state == "playing"
    assert snapshots[-1].health == 0
    assert death.selected is True
    assert death.priority == 50
    assert format_decision_reason(death) == "优先级 50"
    assert all(decision.reason_code != "alive_threshold" for decision in decisions)


def test_teammate_events_are_dropped_with_milestone_reason(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    decisions = _decisions(
        _event_sequence(recorded_fixtures, "spectating_teammate_headshot")
    )

    assert decisions
    assert all(decision.selected is False for decision in decisions)
    assert all(decision.reason_code == "teammate_event" for decision in decisions)
    assert all(
        format_decision_reason(decision) == "队友事件，本里程碑不解说"
        for decision in decisions
    )


def test_muted_switch_drops_every_game_event(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    decisions = _decisions(
        _three_kill_sequence(recorded_fixtures),
        PolicyConfig(alive_priority_threshold=0),
        muted=True,
    )

    assert decisions
    assert all(decision.selected is False for decision in decisions)
    assert all(decision.reason_code == "muted" for decision in decisions)
    assert all(
        format_decision_reason(decision) == "自动说话已关闭"
        for decision in decisions
    )


def test_cooldown_drops_normal_events_but_not_multi_kills(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    decisions = _decisions(
        _same_round_timeline(recorded_fixtures),
        PolicyConfig(
            cooldown_seconds=20,
            max_lines_per_round=20,
            alive_priority_threshold=0,
            cooldown_override_priority=101,
        ),
    )
    selected = [decision for decision in decisions if decision.selected]
    cooldown_rejections = [
        decision for decision in decisions if decision.reason_code == "cooldown"
    ]

    assert [decision.event.type for decision in selected] == ["multi_kill", "multi_kill"]
    assert any(
        decision.event.type == "round_win"
        and decision.reason_code == "round_event"
        for decision in decisions
    )
    assert cooldown_rejections
    assert all(
        format_decision_reason(decision)
        == "距上次发言 12.791 秒，冷却 20 秒未过"
        for decision in cooldown_rejections
    )


def test_round_limit_drops_normal_events_but_not_multi_kills(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    decisions = _decisions(
        _timeline_with_next_round(recorded_fixtures),
        PolicyConfig(
            cooldown_seconds=0,
            max_lines_per_round=1,
            alive_priority_threshold=0,
        ),
    )
    selected = [decision for decision in decisions if decision.selected]
    limit_rejections = [
        decision for decision in decisions if decision.reason_code == "round_limit"
    ]

    assert [(decision.event.round_number, decision.event.type) for decision in selected] == [
        (6, "multi_kill"),
        (6, "multi_kill"),
    ]
    assert limit_rejections
    assert all(
        format_decision_reason(decision) == "本回合发言已达上限 1"
        for decision in limit_rejections
    )


def test_same_batch_selects_highest_priority_and_marks_every_other_event(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    decisions = _decisions(
        _three_kill_sequence(recorded_fixtures),
        PolicyConfig(alive_priority_threshold=0),
    )
    selected = [decision for decision in decisions if decision.selected]
    rejected = [decision for decision in decisions if not decision.selected]

    assert len(selected) == 1
    assert selected[0].event.type == "multi_kill"
    assert selected[0].event.facts["count"] == 3
    assert selected[0].priority == 80
    assert rejected
    assert all(decision.reason_code == "higher_priority" for decision in rejected)
    assert all(
        format_decision_reason(decision) == "已有更高优先级事件"
        for decision in rejected
    )


def test_alive_threshold_config_changes_same_real_batch(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    snapshots = _event_sequence(recorded_fixtures, "multi_kill_after_dropped_push")
    quiet = _decisions(snapshots, PolicyConfig(alive_priority_threshold=75))
    permissive = _decisions(snapshots, PolicyConfig(alive_priority_threshold=0))

    assert not any(decision.selected for decision in quiet)
    assert [decision.event.type for decision in permissive if decision.selected] == [
        "multi_kill"
    ]


def test_cooldown_config_does_not_block_multi_kill_timeline(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    snapshots = _same_round_timeline(recorded_fixtures)
    cooled = _decisions(
        snapshots,
        PolicyConfig(
            cooldown_seconds=20,
            max_lines_per_round=20,
            alive_priority_threshold=0,
            cooldown_override_priority=101,
        ),
    )
    immediate = _decisions(
        snapshots,
        PolicyConfig(
            cooldown_seconds=0,
            max_lines_per_round=20,
            alive_priority_threshold=0,
        ),
    )

    assert sum(decision.selected for decision in cooled) == 2
    assert sum(decision.selected for decision in immediate) == 2


def test_low_priority_event_is_still_dropped_during_cooldown(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    decisions = _cooldown_override_decisions(
        recorded_fixtures,
        PolicyConfig(
            cooldown_seconds=20,
            max_lines_per_round=20,
            cooldown_override_priority=70,
            minimum_gap_seconds=2,
        ),
    )
    third_ts = _same_round_timeline(recorded_fixtures)[2].ts
    normal_kill = next(
        decision
        for decision in decisions
        if decision.event.ts == third_ts and decision.event.type == "kill"
    )

    assert normal_kill.priority == 20
    assert normal_kill.selected is False
    assert normal_kill.reason_code == "cooldown"


def test_high_priority_event_overrides_regular_cooldown(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    decisions = _cooldown_override_decisions(
        recorded_fixtures,
        PolicyConfig(
            cooldown_seconds=20,
            max_lines_per_round=20,
            cooldown_override_priority=70,
            minimum_gap_seconds=2,
        ),
    )
    three_kill = next(
        decision
        for decision in decisions
        if decision.event.type == "multi_kill" and decision.event.facts["count"] == 3
    )

    assert three_kill.priority == 80
    assert three_kill.selected is True
    assert three_kill.reason_code == "selected"


def test_multi_kill_is_deferred_inside_minimum_gap(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    snapshots = _same_round_timeline(recorded_fixtures)
    decisions = _cooldown_override_decisions(
        recorded_fixtures,
        PolicyConfig(
            cooldown_seconds=20,
            max_lines_per_round=20,
            cooldown_override_priority=70,
            minimum_gap_seconds=2,
        ),
        last_now=snapshots[1].ts + 1,
    )
    three_kill = next(
        decision
        for decision in decisions
        if decision.event.type == "multi_kill" and decision.event.facts["count"] == 3
    )

    assert three_kill.selected is False
    assert three_kill.reason_code == "deferred"
    assert (
        format_decision_reason(three_kill)
        == "距上次发言 1.000 秒，最小间隔 2 秒未过，暂存追补"
    )


def test_multi_kill_ignores_round_limit(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    decisions = _cooldown_override_decisions(
        recorded_fixtures,
        PolicyConfig(
            cooldown_seconds=20,
            max_lines_per_round=1,
            cooldown_override_priority=70,
            minimum_gap_seconds=2,
        ),
    )
    three_kill = next(
        decision
        for decision in decisions
        if decision.event.type == "multi_kill" and decision.event.facts["count"] == 3
    )

    assert three_kill.selected is True
    assert three_kill.reason_code == "selected"


def test_round_limit_config_does_not_block_multi_kill_timeline(
    recorded_fixtures: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    snapshots = _timeline_with_next_round(recorded_fixtures)
    one_line = _decisions(
        snapshots,
        PolicyConfig(
            cooldown_seconds=0,
            max_lines_per_round=1,
            alive_priority_threshold=0,
        ),
    )
    three_lines = _decisions(
        snapshots,
        PolicyConfig(
            cooldown_seconds=0,
            max_lines_per_round=3,
            alive_priority_threshold=0,
        ),
    )

    assert sum(decision.selected for decision in one_line) == 2
    assert sum(decision.selected for decision in three_lines) == 2


def test_multi_kill_ignores_cooldown_but_respects_minimum_gap() -> None:
    policy = SpeechPolicy(
        PolicyConfig(cooldown_seconds=6, minimum_gap_seconds=2, max_lines_per_round=4)
    )
    game = _playing_game()
    first = _policy_event("kill", "kill", ts=10)
    double_kill = _policy_event("double", "multi_kill", ts=13, count=2)

    assert policy.decide((first,), game, now=10, muted=False).selected_event == first
    outcome = policy.decide((double_kill,), game, now=13, muted=False)

    assert outcome.selected_event == double_kill
    assert outcome.decisions[0].reason_code == "selected"


def test_multi_kill_deferred_for_minimum_gap_is_selected_on_empty_batch() -> None:
    policy = SpeechPolicy(
        PolicyConfig(cooldown_seconds=6, minimum_gap_seconds=2, max_lines_per_round=4)
    )
    game = _playing_game()
    first = _policy_event("kill", "kill", ts=10)
    triple_kill = _policy_event("triple", "multi_kill", ts=11, count=3)

    policy.decide((first,), game, now=10, muted=False)
    deferred = policy.decide((triple_kill,), game, now=11, muted=False)
    followed_up = policy.decide((), game, now=12, muted=False)

    assert deferred.selected_event is None
    assert deferred.decisions[0].reason_code == "deferred"
    assert followed_up.selected_event == triple_kill
    assert followed_up.decisions[0].selected is True


def test_higher_multi_kill_replaces_pending_follow_up() -> None:
    policy = SpeechPolicy(PolicyConfig(minimum_gap_seconds=2))
    game = _playing_game()
    first = _policy_event("kill", "kill", ts=10)
    triple_kill = _policy_event("triple", "multi_kill", ts=11, count=3)
    four_kill = _policy_event("four", "multi_kill", ts=11.5, count=4)

    policy.decide((first,), game, now=10, muted=False)
    policy.decide((triple_kill,), game, now=11, muted=False)
    replacement = policy.decide((four_kill,), game, now=11.5, muted=False)
    followed_up = policy.decide((), game, now=12, muted=False)

    assert replacement.decisions[0].reason_code == "deferred"
    assert followed_up.selected_event == four_kill
    assert [decision.event.id for decision in followed_up.decisions] == ["four"]


def test_lower_multi_kill_keeps_existing_pending_follow_up() -> None:
    policy = SpeechPolicy(PolicyConfig(minimum_gap_seconds=2))
    game = _playing_game()
    first = _policy_event("kill", "kill", ts=10)
    four_kill = _policy_event("four", "multi_kill", ts=11, count=4)
    triple_kill = _policy_event("triple", "multi_kill", ts=11.5, count=3)

    policy.decide((first,), game, now=10, muted=False)
    policy.decide((four_kill,), game, now=11, muted=False)
    policy.decide((triple_kill,), game, now=11.5, muted=False)
    followed_up = policy.decide((), game, now=12, muted=False)

    assert followed_up.selected_event == four_kill


def test_higher_priority_new_event_discards_pending_follow_up() -> None:
    policy = SpeechPolicy(PolicyConfig(cooldown_seconds=0, minimum_gap_seconds=2))
    game = _playing_game()
    first = _policy_event("kill", "kill", ts=10)
    double_kill = _policy_event("double", "multi_kill", ts=11, count=2)
    death = _policy_event("death", "death_thrown_away", ts=12.1)

    policy.decide((first,), game, now=10, muted=False)
    policy.decide((double_kill,), game, now=11, muted=False)
    contested = policy.decide((death,), game, now=12.1, muted=False)
    after = policy.decide((), game, now=12.2, muted=False)

    assert contested.selected_event == death
    assert any(
        decision.event == double_kill and decision.reason_code == "higher_priority"
        for decision in contested.decisions
    )
    assert after.selected_event is None
    assert after.decisions == ()


def test_expired_pending_follow_up_is_silently_discarded() -> None:
    policy = SpeechPolicy(
        PolicyConfig(minimum_gap_seconds=2, follow_up_max_age_seconds=2)
    )
    game = _playing_game()
    first = _policy_event("kill", "kill", ts=10)
    double_kill = _policy_event("double", "multi_kill", ts=11, count=2)

    policy.decide((first,), game, now=10, muted=False)
    policy.decide((double_kill,), game, now=11, muted=False)
    expired = policy.decide((), game, now=13.1, muted=False)

    assert expired.selected_event is None
    assert expired.decisions == ()


def test_multi_kill_ignores_full_round_quota_and_still_counts() -> None:
    policy = SpeechPolicy(
        PolicyConfig(cooldown_seconds=6, minimum_gap_seconds=2, max_lines_per_round=1)
    )
    game = _playing_game()
    first = _policy_event("kill", "kill", ts=10)
    double_kill = _policy_event("double", "multi_kill", ts=13, count=2)

    policy.decide((first,), game, now=10, muted=False)
    selected = policy.decide((double_kill,), game, now=13, muted=False)
    ordinary = policy.decide(
        (_policy_event("later-kill", "kill", ts=20),), game, now=20, muted=False
    )

    assert selected.selected_event == double_kill
    assert ordinary.decisions[0].reason_code == "round_limit"


@pytest.mark.parametrize("clear_action", ("round_change", "reset"))
def test_round_change_or_reset_clears_pending_follow_up(clear_action: str) -> None:
    policy = SpeechPolicy(PolicyConfig(minimum_gap_seconds=2))
    first_round = _playing_game(1)
    first = _policy_event("kill", "kill", ts=10, round_number=1)
    double_kill = _policy_event("double", "multi_kill", ts=11, round_number=1, count=2)

    policy.decide((first,), first_round, now=10, muted=False)
    policy.decide((double_kill,), first_round, now=11, muted=False)
    if clear_action == "round_change":
        outcome = policy.decide((), _playing_game(2), now=13, muted=False)
    else:
        policy.reset()
        outcome = policy.decide((), first_round, now=13, muted=False)

    assert outcome.selected_event is None
    assert outcome.decisions == ()
