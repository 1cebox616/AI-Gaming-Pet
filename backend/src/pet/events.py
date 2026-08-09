"""Detect CS2 game events from ordered GSI snapshots and replay recordings."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from pet.config import EventsConfig, load_config
from pet.gsi import GSI_SILENCE_SECONDS, GameSnapshot, RoundWin, parse_snapshot
from pet.session import GameSessionTracker

EventType = Literal[
    "kill",
    "kill_headshot",
    "multi_kill",
    "death",
    "death_thrown_away",
    "round_win",
    "round_loss",
]

EVENT_TYPES: tuple[EventType, ...] = (
    "kill",
    "kill_headshot",
    "multi_kill",
    "death",
    "death_thrown_away",
    "round_win",
    "round_loss",
)
MULTI_KILL_THRESHOLDS = (2, 3, 4, 5)
_EVENT_LABELS: dict[EventType, str] = {
    "kill": "击杀",
    "kill_headshot": "爆头击杀",
    "multi_kill": "多杀",
    "death": "死亡",
    "death_thrown_away": "白给",
    "round_win": "回合胜利",
    "round_loss": "回合失利",
}
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


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Events and aggregate counts produced by one recording replay."""

    events: tuple[GameEvent, ...]
    started_at: float
    snapshot_count: int
    rounds_covered: int


class EventDetector:
    """Compare snapshots while keeping independent baselines for each subject."""

    def __init__(self, config: EventsConfig) -> None:
        self._session = GameSessionTracker(GSI_SILENCE_SECONDS)
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

    def observe(self, snapshot: GameSnapshot) -> tuple[GameEvent, ...]:
        """Consume one ordered snapshot and return newly inferred events."""
        game = self._session.observe(snapshot)
        self._update_match_context(snapshot, game.state)
        self._update_round_clock(snapshot, game.state)

        baseline = self._subjects.get(snapshot.player_steamid)
        events: list[GameEvent] = []
        if baseline is not None and game.state in _EMITTING_STATES:
            events.extend(self._detect_subject_events(baseline.snapshot, snapshot))
        if self._has_observed_snapshot and game.state in _EMITTING_STATES:
            result = self._detect_round_result(snapshot)
            if result is not None:
                events.append(result)

        self._subjects[snapshot.player_steamid] = _SubjectBaseline(snapshot=snapshot)
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
        self, previous: GameSnapshot, current: GameSnapshot
    ) -> list[GameEvent]:
        self_steamid = current.provider_steamid or self._self_steamid
        subject_is_self = (
            current.player_steamid is not None
            and current.player_steamid == self_steamid
        )
        round_number = _human_round_number(current)
        events: list[GameEvent] = []

        round_boundary = (
            current.round_phase == "freezetime"
            or (
                previous.round_number is not None
                and current.round_number is not None
                and previous.round_number != current.round_number
            )
        )
        if not round_boundary:
            events.extend(
                self._detect_kills(
                    previous,
                    current,
                    subject_is_self=subject_is_self,
                    round_number=round_number,
                )
            )

        if current.round_phase != "freezetime" and _is_death(previous, current):
            survival_seconds = (
                max(0.0, current.ts - self._live_started_at)
                if self._live_started_at is not None
                else None
            )
            round_kills = current.round_kills if current.round_kills is not None else 0
            thrown_away = (
                round_kills == 0
                and survival_seconds is not None
                and survival_seconds < self._config.thrown_away_max_survival_seconds
                and current.equip_value is not None
                and current.equip_value >= self._config.thrown_away_min_equip_value
            )
            events.append(
                self._make_event(
                    event_type="death_thrown_away" if thrown_away else "death",
                    snapshot=current,
                    subject_steamid=current.player_steamid,
                    subject_is_self=subject_is_self,
                    round_number=round_number,
                    facts={
                        "survival_seconds": survival_seconds,
                        "round_kills": round_kills,
                        "equip_value": current.equip_value,
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
                        facts={"count": threshold},
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
            round_number=_human_round_number(snapshot),
            facts={
                "method": _round_win_method(snapshot),
                "score_ct": snapshot.score_ct,
                "score_t": snapshot.score_t,
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


def _human_round_number(snapshot: GameSnapshot) -> int | None:
    if snapshot.round_number is None:
        return None
    if snapshot.round_phase == "over" or snapshot.round_win_team is not None:
        return snapshot.round_number
    return snapshot.round_number + 1


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


def replay_recording(path: Path, config: EventsConfig) -> ReplayResult:
    """Replay one raw JSONL recording in timestamp order."""
    snapshots = _load_recording(path)
    detector = EventDetector(config)
    events: list[GameEvent] = []
    covered_rounds: set[tuple[str | None, int]] = set()
    for snapshot in snapshots:
        events.extend(detector.observe(snapshot))
        if snapshot.map_phase == "live" and snapshot.round_number is not None:
            round_number = _human_round_number(snapshot)
            if round_number is not None:
                covered_rounds.add((snapshot.map_name, round_number))
    return ReplayResult(
        events=tuple(events),
        started_at=snapshots[0].ts if snapshots else 0.0,
        snapshot_count=len(snapshots),
        rounds_covered=len(covered_rounds),
    )


def _load_recording(path: Path) -> tuple[GameSnapshot, ...]:
    rows: list[tuple[float, int, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        decoded: object = json.loads(line)
        if not isinstance(decoded, Mapping):
            raise ValueError(f"recording line {line_number} must be a JSON object")
        ts = decoded.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise ValueError(f"recording line {line_number} has no numeric ts")
        rows.append((float(ts), line_number, decoded.get("payload")))
    rows.sort(key=lambda row: (row[0], row[1]))
    return tuple(parse_snapshot(payload, received_at=ts) for ts, _, payload in rows)


def format_replay(result: ReplayResult, *, started_at: float) -> str:
    """Format replay events and aggregates as a readable Chinese timeline."""
    lines: list[str] = ["事件时间线："]
    if not result.events:
        lines.append("（未检测到事件）")
    for event in result.events:
        relative = event.ts - started_at
        round_label = (
            f"第 {event.round_number} 回合"
            if event.round_number is not None
            else "回合未知"
        )
        subject = "本人" if event.subject_is_self else "队友"
        identity = event.subject_steamid or "未知主体"
        lines.append(
            f"+{relative:8.3f}s | {round_label} | {subject} {identity} | "
            f"{_EVENT_LABELS[event.type]} | {_format_facts(event)}"
        )

    counts = Counter(event.type for event in result.events)
    lines.append("")
    lines.append("汇总：")
    for event_type in EVENT_TYPES:
        lines.append(f"- {_EVENT_LABELS[event_type]}：{counts[event_type]}")
    lines.append(f"- 覆盖回合：{result.rounds_covered}")
    lines.append(f"- 处理快照：{result.snapshot_count}")
    return "\n".join(lines)


def _format_facts(event: GameEvent) -> str:
    facts = event.facts
    if event.type in {"kill", "kill_headshot"}:
        weapon = facts["weapon"] or "未知"
        return (
            f"本回合第 {facts['round_kill_index']} 杀，"
            f"本次差值 +{facts['delta']}，武器 {weapon}"
        )
    if event.type == "multi_kill":
        return f"本回合累计达到 {facts['count']} 杀"
    if event.type in {"death", "death_thrown_away"}:
        survival = facts["survival_seconds"]
        survival_label = "未知" if survival is None else f"{survival:.3f} 秒"
        equip_value = facts["equip_value"]
        equip_label = "未知" if equip_value is None else str(equip_value)
        return (
            f"存活 {survival_label}，本回合 {facts['round_kills']} 杀，"
            f"装备值 {equip_label}"
        )
    method = facts["method"] or "未知"
    score_ct = facts["score_ct"] if facts["score_ct"] is not None else "未知"
    score_t = facts["score_t"] if facts["score_t"] is not None else "未知"
    return f"方式 {method}，比分 CT {score_ct} : {score_t} T"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="回放 CS2 GSI 录制并打印事件时间线")
    parser.add_argument("--replay", type=Path, required=True, help="GSI JSONL 录制文件")
    parser.add_argument(
        "--with-policy",
        action="store_true",
        help="展示发言策略决定、丢弃原因与实际模板话术",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the recording replay CLI."""
    args = _build_parser().parse_args(argv)
    configuration = load_config()
    if args.with_policy:
        from pet.commentary import format_commentary_replay, replay_commentary

        snapshots = _load_recording(args.replay)
        commentary_result = replay_commentary(
            snapshots,
            configuration.events,
            configuration.policy,
            personality_style=configuration.personality.style,
        )
        print(format_commentary_replay(commentary_result))
        return 0

    result = replay_recording(args.replay, configuration.events)
    print(format_replay(result, started_at=result.started_at))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
