"""Choose at most one detected CS2 event that is worth speaking about."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pet.config import EventsConfig, PolicyConfig
from pet.events import EventDetector, EventType, GameEvent
from pet.gsi import GSI_SILENCE_SECONDS, GameSnapshot
from pet.session import GameSessionTracker, GameState

DecisionReason = Literal[
    "selected",
    "teammate_event",
    "muted",
    "alive_threshold",
    "round_limit",
    "cooldown",
    "minimum_gap",
    "higher_priority",
]

_STATIC_PRIORITIES: dict[EventType, int] = {
    "kill": 20,
    "kill_headshot": 30,
    "death": 45,
    "death_thrown_away": 60,
    "round_win": 70,
    "round_loss": 70,
    "multi_kill": 0,
}
_MULTI_KILL_PRIORITIES = {2: 50, 3: 80, 4: 90, 5: 100}
_EVENT_LABELS: dict[EventType, str] = {
    "kill": "普通击杀",
    "kill_headshot": "爆头击杀",
    "multi_kill": "多杀",
    "death": "死亡",
    "death_thrown_away": "白给",
    "round_win": "回合胜利",
    "round_loss": "回合失败",
}
_REASON_LABELS: dict[DecisionReason, str] = {
    "selected": "开口",
    "teammate_event": "队友事件",
    "muted": "自动说话关闭",
    "alive_threshold": "交火中未达门槛",
    "round_limit": "每回合上限",
    "cooldown": "冷却未过",
    "minimum_gap": "最小间隔未过",
    "higher_priority": "已有更高优先级事件",
}


class PolicyDecision(BaseModel):
    """The disposition and concrete reason for one detected event."""

    model_config = ConfigDict(frozen=True)

    event: GameEvent
    selected: bool
    priority: int
    reason_code: DecisionReason
    reason: str


class PolicyBatchDecision(BaseModel):
    """All dispositions for one snapshot's event batch and its single winner."""

    model_config = ConfigDict(frozen=True)

    selected_event: GameEvent | None
    decisions: tuple[PolicyDecision, ...]


@dataclass(frozen=True, slots=True)
class PolicyReplayResult:
    """Policy decisions and aggregate recording metadata from one replay."""

    decisions: tuple[PolicyDecision, ...]
    started_at: float
    snapshot_count: int


class SpeechPolicy:
    """Apply ordered filters, cooldown, and per-round limits to event batches."""

    def __init__(self, config: PolicyConfig) -> None:
        self._config = config
        self._self_steamid: str | None = None
        self._self_health: int | None = None
        self._last_selected_at: float | None = None
        self._counted_round_number: int | None = None
        self._lines_this_round = 0

    def observe_snapshot(self, snapshot: GameSnapshot) -> None:
        """Remember health only when the snapshot describes the local player."""
        if snapshot.provider_steamid is not None:
            self._self_steamid = snapshot.provider_steamid
        if (
            snapshot.player_steamid is not None
            and snapshot.player_steamid == self._self_steamid
            and snapshot.health is not None
        ):
            self._self_health = snapshot.health

    def decide(
        self,
        events: Sequence[GameEvent],
        game: GameState,
        *,
        now: float,
        muted: bool,
    ) -> PolicyBatchDecision:
        """Select no more than one event and explain every disposition."""
        if not events:
            return PolicyBatchDecision(selected_event=None, decisions=())

        batch_round = next(
            (event.round_number for event in events if event.round_number is not None),
            game.round,
        )
        if batch_round != self._counted_round_number:
            self._counted_round_number = batch_round
            self._lines_this_round = 0

        decisions_by_id: dict[str, PolicyDecision] = {}
        candidates: list[tuple[int, int, GameEvent]] = []
        for order, event in enumerate(events):
            priority = event_priority(event)
            rejection = self._filter_event(
                event,
                game,
                priority=priority,
                now=now,
                muted=muted,
            )
            if rejection is None:
                candidates.append((priority, order, event))
            else:
                decisions_by_id[event.id] = rejection

        selected_event: GameEvent | None = None
        if candidates:
            priority, _, selected_event = max(
                candidates,
                key=lambda candidate: (candidate[0], -candidate[1]),
            )
            for candidate_priority, _, event in candidates:
                if event.id == selected_event.id:
                    decisions_by_id[event.id] = PolicyDecision(
                        event=event,
                        selected=True,
                        priority=candidate_priority,
                        reason_code="selected",
                        reason=f"优先级 {candidate_priority}",
                    )
                else:
                    decisions_by_id[event.id] = PolicyDecision(
                        event=event,
                        selected=False,
                        priority=candidate_priority,
                        reason_code="higher_priority",
                        reason="已有更高优先级事件",
                    )
            self._last_selected_at = now
            self._lines_this_round += 1

        return PolicyBatchDecision(
            selected_event=selected_event,
            decisions=tuple(decisions_by_id[event.id] for event in events),
        )

    def _filter_event(
        self,
        event: GameEvent,
        game: GameState,
        *,
        priority: int,
        now: float,
        muted: bool,
    ) -> PolicyDecision | None:
        if not event.subject_is_self:
            return _rejected(
                event,
                priority,
                "teammate_event",
                "队友事件，本里程碑不解说",
            )
        if muted:
            return _rejected(event, priority, "muted", "自动说话已关闭")
        if (
            game.state == "playing"
            and self._self_health is not None
            and self._self_health > 0
            and priority < self._config.alive_priority_threshold
        ):
            return _rejected(
                event,
                priority,
                "alive_threshold",
                f"交火中，优先级 {priority} 未达门槛 "
                f"{self._config.alive_priority_threshold}",
            )
        if self._lines_this_round >= self._config.max_lines_per_round:
            return _rejected(
                event,
                priority,
                "round_limit",
                f"本回合发言已达上限 {self._config.max_lines_per_round}",
            )
        if self._last_selected_at is not None:
            elapsed = max(0.0, now - self._last_selected_at)
            if elapsed < self._config.cooldown_seconds:
                if priority >= self._config.cooldown_override_priority:
                    if elapsed < self._config.minimum_gap_seconds:
                        return _rejected(
                            event,
                            priority,
                            "minimum_gap",
                            f"距上次发言 {elapsed:.3f} 秒，最小间隔 "
                            f"{self._config.minimum_gap_seconds:g} 秒未过",
                        )
                    return None
                return _rejected(
                    event,
                    priority,
                    "cooldown",
                    f"距上次发言 {elapsed:.3f} 秒，冷却 "
                    f"{self._config.cooldown_seconds:g} 秒未过",
                )
        return None


def event_priority(event: GameEvent) -> int:
    """Return the policy-owned priority for one detected fact."""
    if event.type == "multi_kill":
        count = event.facts.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            return _MULTI_KILL_PRIORITIES.get(count, 0)
        return 0
    return _STATIC_PRIORITIES[event.type]


def replay_policy(
    snapshots: Sequence[GameSnapshot],
    events_config: EventsConfig,
    policy_config: PolicyConfig,
    *,
    muted: bool = False,
) -> PolicyReplayResult:
    """Replay snapshots through event detection, session state, and policy."""
    detector = EventDetector(events_config)
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    policy = SpeechPolicy(policy_config)
    decisions: list[PolicyDecision] = []
    for snapshot in snapshots:
        game = session.observe(snapshot)
        policy.observe_snapshot(snapshot)
        events = detector.observe(snapshot)
        batch = policy.decide(events, game, now=snapshot.ts, muted=muted)
        decisions.extend(batch.decisions)
    return PolicyReplayResult(
        decisions=tuple(decisions),
        started_at=snapshots[0].ts if snapshots else 0.0,
        snapshot_count=len(snapshots),
    )


def format_policy_replay(result: PolicyReplayResult) -> str:
    """Format every policy disposition and aggregate reason counts in Chinese."""
    lines = ["发言策略决策时间线："]
    if not result.decisions:
        lines.append("（未检测到事件）")
    for decision in result.decisions:
        event = decision.event
        relative = event.ts - result.started_at
        round_label = (
            f"第 {event.round_number} 回合"
            if event.round_number is not None
            else "回合未知"
        )
        action = "开口" if decision.selected else "丢弃"
        event_label = _EVENT_LABELS[event.type]
        if event.type == "multi_kill":
            event_label = f"{event.facts.get('count', '?')} 杀"
        lines.append(
            f"+{relative:8.3f}s | {round_label} | {event_label:<8} | "
            f"{action} | {decision.reason}"
        )

    selected_count = sum(decision.selected for decision in result.decisions)
    rejected_counts = Counter(
        decision.reason_code for decision in result.decisions if not decision.selected
    )
    lines.extend(
        (
            "",
            "策略汇总：",
            f"- 检测到事件：{len(result.decisions)}",
            f"- 实际开口：{selected_count}",
        )
    )
    if rejected_counts:
        reason_summary = "、".join(
            f"{_REASON_LABELS[reason]} {rejected_counts[reason]}"
            for reason in _REASON_LABELS
            if reason != "selected" and rejected_counts[reason]
        )
        lines.append(f"- 丢弃原因：{reason_summary}")
    else:
        lines.append("- 丢弃原因：无")
    lines.append(f"- 处理快照：{result.snapshot_count}")
    return "\n".join(lines)


def _rejected(
    event: GameEvent,
    priority: int,
    reason_code: DecisionReason,
    reason: str,
) -> PolicyDecision:
    return PolicyDecision(
        event=event,
        selected=False,
        priority=priority,
        reason_code=reason_code,
        reason=reason,
    )
