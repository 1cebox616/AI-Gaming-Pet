"""Detect CS2 game events from ordered GSI snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from pet.config import EventsConfig
from pet.gsi import GameSnapshot, RoundWin, human_round_number
from pet.session import GameState

EventType = Literal[
    "kill",
    "kill_headshot",
    "multi_kill",
    "death",
    "death_after_kill",
    "death_thrown_away",
    "round_win",
    "round_loss",
]

EVENT_TYPES: tuple[EventType, ...] = (
    "kill",
    "kill_headshot",
    "multi_kill",
    "death",
    "death_after_kill",
    "death_thrown_away",
    "round_win",
    "round_loss",
)
MULTI_KILL_THRESHOLDS = (2, 3, 4, 5)
LARGE_SCORE_GAP = 4
_EMITTING_STATES = {"playing", "spectating", "round_over"}


class GameEvent(BaseModel):
    """One fact inferred from the difference between ordered snapshots."""

    model_config = ConfigDict(frozen=True)

    id: str
    type: EventType
    ts: float
    subject_steamid: str | None
    subject_is_self: bool
    round_number: int | None
    facts: dict[str, Any]


@dataclass(slots=True)
class _SubjectBaseline:
    snapshot: GameSnapshot
    last_kill_at: float | None = None
    last_kill_round_number: int | None = None


class EventDetector:
    """Compare snapshots while keeping independent baselines for each subject."""

    def __init__(self, config: EventsConfig) -> None:
        self._config = config
        self._subjects: dict[str | None, _SubjectBaseline] = {}
        self._has_observed_snapshot = False
        self._event_index = 0
        self._self_steamid: str | None = None
        self._self_team: Literal["CT", "T"] | None = None
        self._last_round_phase: str | None = None
        self._live_started_at: float | None = None
        self._reported_results: set[tuple[int | None, str]] = set()
        self._result_map_name: str | None = None

    def reset(self) -> None:
        """Discard every per-match baseline while keeping event IDs process-unique."""
        self._subjects.clear()
        self._has_observed_snapshot = False
        self._self_steamid = None
        self._self_team = None
        self._last_round_phase = None
        self._live_started_at = None
        self._reported_results.clear()
        self._result_map_name = None

    def observe(
        self, snapshot: GameSnapshot, game: GameState
    ) -> tuple[GameEvent, ...]:
        """Consume one ordered snapshot and return newly inferred events."""
        self._update_match_context(snapshot, game.state)
        self._update_round_clock(snapshot, game.state)

        baseline = self._subjects.get(snapshot.player_steamid)
        events: list[GameEvent] = []
        if baseline is not None and game.state in _EMITTING_STATES:
            events.extend(self._detect_subject_events(baseline, snapshot))
        if self._has_observed_snapshot and game.state in _EMITTING_STATES:
            result = self._detect_round_result(snapshot)
            if result is not None:
                events.append(result)

        if game.state in _EMITTING_STATES or game.state == "warmup":
            if baseline is None:
                self._subjects[snapshot.player_steamid] = _SubjectBaseline(
                    snapshot=snapshot
                )
            else:
                baseline.snapshot = snapshot
        self._has_observed_snapshot = True
        return tuple(events)

    def _update_match_context(self, snapshot: GameSnapshot, state: str) -> None:
        if state == "menu":
            self._self_steamid = snapshot.provider_steamid
            self._self_team = None
            self._reported_results.clear()
            self._result_map_name = None
        elif state in {"warmup", "match_over"}:
            self._reported_results.clear()

        if snapshot.map_name is not None and snapshot.map_name != self._result_map_name:
            self._reported_results.clear()
            self._result_map_name = snapshot.map_name

        if snapshot.provider_steamid is not None:
            self._self_steamid = snapshot.provider_steamid
        if (
            snapshot.player_steamid is not None
            and snapshot.player_steamid == snapshot.provider_steamid
            and snapshot.team in {"CT", "T"}
        ):
            self._self_team = snapshot.team

    def _update_round_clock(self, snapshot: GameSnapshot, state: str) -> None:
        if state in {"menu", "warmup", "match_over"}:
            self._live_started_at = None
            self._last_round_phase = snapshot.round_phase
            return

        if snapshot.round_phase == "freezetime":
            self._live_started_at = None
        elif (
            snapshot.round_phase == "live"
            and self._last_round_phase is not None
            and self._last_round_phase != "live"
        ):
            self._live_started_at = snapshot.ts

        if snapshot.round_phase is not None:
            self._last_round_phase = snapshot.round_phase

    def _detect_subject_events(
        self, baseline: _SubjectBaseline, current: GameSnapshot
    ) -> list[GameEvent]:
        previous = baseline.snapshot
        self_steamid = current.provider_steamid or self._self_steamid
        subject_is_self = (
            current.player_steamid is not None
            and current.player_steamid == self_steamid
        )
        round_number = human_round_number(current)
        events: list[GameEvent] = []

        round_boundary = (
            current.round_phase == "freezetime"
            or previous.map_name != current.map_name
            or (
                previous.round_number is not None
                and current.round_number is not None
                and previous.round_number != current.round_number
            )
        )
        if round_boundary:
            baseline.last_kill_at = None
            baseline.last_kill_round_number = None
        if not round_boundary:
            kill_events = self._detect_kills(
                previous,
                current,
                subject_is_self=subject_is_self,
                round_number=round_number,
            )
            if kill_events:
                baseline.last_kill_at = current.ts
                baseline.last_kill_round_number = round_number
            events.extend(kill_events)

        if (
            current.round_phase != "freezetime"
            and _is_death(previous, current)
        ):
            survival_seconds = (
                max(0.0, current.ts - self._live_started_at)
                if self._live_started_at is not None
                else None
            )
            round_kills = current.round_kills
            seconds_since_last_kill = _seconds_since_last_kill(
                baseline,
                current,
                round_number,
            )
            death_after_kill = (
                round_kills is not None
                and round_kills >= 1
                and seconds_since_last_kill is not None
                and seconds_since_last_kill
                <= self._config.death_after_kill_max_seconds
            )
            thrown_away = (
                not death_after_kill
                and round_kills == 0
                and survival_seconds is not None
                and survival_seconds < self._config.thrown_away_max_survival_seconds
                and current.equip_value is not None
                and current.equip_value >= self._config.thrown_away_min_equip_value
            )
            events.append(
                self._make_event(
                    event_type=(
                        "death_after_kill"
                        if death_after_kill
                        else "death_thrown_away" if thrown_away else "death"
                    ),
                    snapshot=current,
                    subject_steamid=current.player_steamid,
                    subject_is_self=subject_is_self,
                    round_number=round_number,
                    facts={
                        "survival_seconds": survival_seconds,
                        "round_kills": round_kills,
                        "seconds_since_last_kill": seconds_since_last_kill,
                        "equip_value": current.equip_value,
                        **_score_situation_facts(current, current.team),
                    },
                )
            )
        return events

    def _detect_kills(
        self,
        previous: GameSnapshot,
        current: GameSnapshot,
        *,
        subject_is_self: bool,
        round_number: int | None,
    ) -> list[GameEvent]:
        if (
            previous.round_kills is None
            or current.round_kills is None
            or current.round_kills <= previous.round_kills
        ):
            return []

        kill_delta = current.round_kills - previous.round_kills
        headshot_delta = 0
        if previous.round_killhs is not None and current.round_killhs is not None:
            headshot_delta = max(0, current.round_killhs - previous.round_killhs)
        headshot_delta = min(headshot_delta, kill_delta)
        first_headshot_index = current.round_kills - headshot_delta + 1

        events: list[GameEvent] = []
        for kill_index in range(previous.round_kills + 1, current.round_kills + 1):
            event_type: EventType = (
                "kill_headshot" if kill_index >= first_headshot_index else "kill"
            )
            events.append(
                self._make_event(
                    event_type=event_type,
                    snapshot=current,
                    subject_steamid=current.player_steamid,
                    subject_is_self=subject_is_self,
                    round_number=round_number,
                    facts={
                        "round_kill_index": kill_index,
                        "delta": kill_delta,
                        "weapon": current.active_weapon,
                        **_score_situation_facts(current, current.team),
                    },
                )
            )

        for threshold in MULTI_KILL_THRESHOLDS:
            if previous.round_kills < threshold <= current.round_kills:
                events.append(
                    self._make_event(
                        event_type="multi_kill",
                        snapshot=current,
                        subject_steamid=current.player_steamid,
                        subject_is_self=subject_is_self,
                        round_number=round_number,
                        facts={
                            "count": threshold,
                            **_score_situation_facts(current, current.team),
                        },
                    )
                )
        return events

    def _detect_round_result(self, snapshot: GameSnapshot) -> GameEvent | None:
        if snapshot.round_win_team not in {"CT", "T"} or self._self_team is None:
            return None
        result_key = (snapshot.round_number, snapshot.round_win_team)
        if result_key in self._reported_results:
            return None
        self._reported_results.add(result_key)

        event_type: EventType = (
            "round_win" if snapshot.round_win_team == self._self_team else "round_loss"
        )
        return self._make_event(
            event_type=event_type,
            snapshot=snapshot,
            subject_steamid=snapshot.provider_steamid or self._self_steamid,
            subject_is_self=True,
            round_number=human_round_number(snapshot),
            facts={
                "method": _round_win_method(snapshot),
                "score_ct": snapshot.score_ct,
                "score_t": snapshot.score_t,
                **_score_situation_facts(snapshot, self._self_team),
            },
        )

    def _make_event(
        self,
        *,
        event_type: EventType,
        snapshot: GameSnapshot,
        subject_steamid: str | None,
        subject_is_self: bool,
        round_number: int | None,
        facts: dict[str, Any],
    ) -> GameEvent:
        self._event_index += 1
        return GameEvent(
            id=f"event-{self._event_index:08d}",
            type=event_type,
            ts=snapshot.ts,
            subject_steamid=subject_steamid,
            subject_is_self=subject_is_self,
            round_number=round_number,
            facts=facts,
        )


def _is_death(previous: GameSnapshot, current: GameSnapshot) -> bool:
    health_reached_zero = (
        previous.health is not None
        and previous.health > 0
        and current.health == 0
    )
    deaths_increased = (
        previous.match_deaths is not None
        and current.match_deaths is not None
        and current.match_deaths > previous.match_deaths
        and current.health in {0, None}
    )
    return health_reached_zero or deaths_increased


def _seconds_since_last_kill(
    baseline: _SubjectBaseline,
    current: GameSnapshot,
    round_number: int | None,
) -> float | None:
    """Return a same-round kill interval without borrowing another round's kill."""
    if (
        baseline.last_kill_at is None
        or baseline.last_kill_round_number != round_number
    ):
        return None
    return max(0.0, current.ts - baseline.last_kill_at)


def _score_situation_facts(
    snapshot: GameSnapshot, team: str | None
) -> dict[str, str | int | None]:
    """Describe the subject team's score position from the authoritative map scores."""
    own_score, opposing_score, losses = _team_score_values(snapshot, team)
    if own_score is None or opposing_score is None:
        situation: str | None = None
    else:
        difference = own_score - opposing_score
        if difference >= LARGE_SCORE_GAP:
            situation = "大比分领先"
        elif difference > 0:
            situation = "领先"
        elif difference == 0:
            situation = "追平"
        elif difference <= -LARGE_SCORE_GAP:
            situation = "大比分落后"
        else:
            situation = "落后"
    return {
        "self_team": team if team in {"CT", "T"} else None,
        "self_score": own_score,
        "opponent_score": opposing_score,
        "score_situation": situation,
        "team_consecutive_round_losses": losses,
    }


def _team_score_values(
    snapshot: GameSnapshot, team: str | None
) -> tuple[int | None, int | None, int | None]:
    if team == "CT":
        return (
            snapshot.score_ct,
            snapshot.score_t,
            snapshot.ct_consecutive_round_losses,
        )
    if team == "T":
        return (
            snapshot.score_t,
            snapshot.score_ct,
            snapshot.t_consecutive_round_losses,
        )
    return None, None, None


def _round_win_method(snapshot: GameSnapshot) -> str | None:
    if not snapshot.round_wins:
        return None
    completed_round = snapshot.round_number
    if completed_round is not None:
        exact = next(
            (win.method for win in snapshot.round_wins if win.round == completed_round),
            None,
        )
        if exact is not None:
            return exact
    latest: RoundWin = max(snapshot.round_wins, key=lambda win: win.round)
    return latest.method
