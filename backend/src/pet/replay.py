"""Replay CS2 recordings through production fact, policy, and commentary layers."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import random
import sys

from pet.commentary import CommentaryGenerator
from pet.config import EventsConfig, PersonalityStyle, PolicyConfig, load_config
from pet.events import EVENT_TYPES, EventDetector, EventType, GameEvent
from pet.gsi import (
    GSI_SILENCE_SECONDS,
    GameSnapshot,
    human_round_number,
    parse_snapshot,
)
from pet.lines import Utterance
from pet.policy import DecisionReason, PolicyDecision, SpeechPolicy
from pet.session import GameSessionTracker, MatchLifecycleTracker

REPLAY_RANDOM_SEED = 20260809

_EVENT_LABELS: dict[EventType, str] = {
    "kill": "击杀",
    "kill_headshot": "爆头击杀",
    "multi_kill": "多杀",
    "death": "死亡",
    "death_after_kill": "击杀后被补枪",
    "death_thrown_away": "白给",
    "round_win": "回合胜利",
    "round_loss": "回合失利",
}
_POLICY_EVENT_LABELS: dict[EventType, str] = {
    "kill": "普通击杀",
    "kill_headshot": "爆头击杀",
    "multi_kill": "多杀",
    "death": "死亡",
    "death_after_kill": "击杀后被补枪",
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


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Events and aggregate counts produced by one recording replay."""

    events: tuple[GameEvent, ...]
    started_at: float
    snapshot_count: int
    rounds_covered: int


@dataclass(frozen=True, slots=True)
class PolicyReplayResult:
    """Policy decisions and aggregate recording metadata from one replay."""

    decisions: tuple[PolicyDecision, ...]
    started_at: float
    snapshot_count: int


@dataclass(frozen=True, slots=True)
class CommentaryDisposition:
    """One policy decision and its generated utterance when selected."""

    decision: PolicyDecision
    utterance: Utterance | None


@dataclass(frozen=True, slots=True)
class CommentaryReplayResult:
    """Commentary decisions and aggregate metadata from one recording replay."""

    dispositions: tuple[CommentaryDisposition, ...]
    started_at: float
    snapshot_count: int


def load_recording(path: Path) -> tuple[GameSnapshot, ...]:
    """Parse a raw JSONL recording and return snapshots in timestamp order."""
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


def replay_recording(path: Path, config: EventsConfig) -> ReplayResult:
    """Replay one raw JSONL recording through the production event detector."""
    snapshots = load_recording(path)
    detector = EventDetector(config)
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    lifecycle = MatchLifecycleTracker()
    events: list[GameEvent] = []
    covered_rounds: set[tuple[str | None, int]] = set()
    for snapshot in snapshots:
        game = session.observe(snapshot)
        if lifecycle.observe(game):
            detector.reset()
        events.extend(detector.observe(snapshot, game))
        if snapshot.map_phase == "live" and snapshot.round_number is not None:
            round_number = human_round_number(snapshot)
            if round_number is not None:
                covered_rounds.add((snapshot.map_name, round_number))
    return ReplayResult(
        events=tuple(events),
        started_at=snapshots[0].ts if snapshots else 0.0,
        snapshot_count=len(snapshots),
        rounds_covered=len(covered_rounds),
    )


def replay_policy(
    snapshots: Sequence[GameSnapshot],
    events_config: EventsConfig,
    policy_config: PolicyConfig,
    *,
    muted: bool = False,
) -> PolicyReplayResult:
    """Replay snapshots through production event detection and policy."""
    detector = EventDetector(events_config)
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    lifecycle = MatchLifecycleTracker()
    policy = SpeechPolicy(policy_config)
    decisions: list[PolicyDecision] = []
    for snapshot in snapshots:
        game = session.observe(snapshot)
        if lifecycle.observe(game):
            detector.reset()
            policy.reset()
        policy.observe_snapshot(snapshot)
        events = detector.observe(snapshot, game)
        batch = policy.decide(events, game, now=snapshot.ts, muted=muted)
        decisions.extend(batch.decisions)
    return PolicyReplayResult(
        decisions=tuple(decisions),
        started_at=snapshots[0].ts if snapshots else 0.0,
        snapshot_count=len(snapshots),
    )


def replay_commentary(
    snapshots: Sequence[GameSnapshot],
    events_config: EventsConfig,
    policy_config: PolicyConfig,
    *,
    muted: bool = False,
    random_seed: int = REPLAY_RANDOM_SEED,
    personality_style: PersonalityStyle = "brother",
) -> CommentaryReplayResult:
    """Replay the complete production chain with a fixed random seed."""
    detector = EventDetector(events_config)
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    lifecycle = MatchLifecycleTracker()
    policy = SpeechPolicy(policy_config)
    generator = CommentaryGenerator(
        random.Random(random_seed),
        personality_style=personality_style,
    )
    dispositions: list[CommentaryDisposition] = []
    for snapshot in snapshots:
        game = session.observe(snapshot)
        if lifecycle.observe(game):
            detector.reset()
            policy.reset()
        policy.observe_snapshot(snapshot)
        events = detector.observe(snapshot, game)
        batch = policy.decide(events, game, now=snapshot.ts, muted=muted)
        utterance = (
            generator.generate(batch.selected_event, map_name=snapshot.map_name)
            if batch.selected_event is not None
            else None
        )
        dispositions.extend(
            CommentaryDisposition(
                decision=decision,
                utterance=utterance if decision.selected else None,
            )
            for decision in batch.decisions
        )
    return CommentaryReplayResult(
        dispositions=tuple(dispositions),
        started_at=snapshots[0].ts if snapshots else 0.0,
        snapshot_count=len(snapshots),
    )


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
        event_label = _POLICY_EVENT_LABELS[event.type]
        if event.type == "multi_kill":
            event_label = f"{event.facts.get('count', '?')} 杀"
        lines.append(
            f"+{relative:8.3f}s | {round_label} | {event_label:<8} | "
            f"{action} | {format_decision_reason(decision)}"
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
    _append_rejection_summary(lines, rejected_counts)
    lines.append(f"- 处理快照：{result.snapshot_count}")
    return "\n".join(lines)


def format_commentary_replay(result: CommentaryReplayResult) -> str:
    """Format policy decisions and generated lines as a readable Chinese timeline."""
    lines = ["游戏解说决策时间线："]
    if not result.dispositions:
        lines.append("（未检测到事件）")
    for disposition in result.dispositions:
        decision = disposition.decision
        event = decision.event
        relative = event.ts - result.started_at
        round_label = (
            f"第 {event.round_number} 回合"
            if event.round_number is not None
            else "回合未知"
        )
        event_label = _POLICY_EVENT_LABELS[event.type]
        if event.type == "multi_kill":
            count = event.facts.get("count")
            event_label = f"{count} 杀" if isinstance(count, int) else "多杀"
        action = "开口" if decision.selected else "丢弃"
        if disposition.utterance is None:
            spoken = "话术：—"
        else:
            spoken = (
                f"话术[{disposition.utterance.emotion}]："
                f"{disposition.utterance.text}"
            )
        lines.append(
            f"+{relative:8.3f}s | {round_label} | {event_label:<8} | "
            f"{action} | {format_decision_reason(decision)} | {spoken}"
        )

    selected_count = sum(
        disposition.decision.selected for disposition in result.dispositions
    )
    generated_count = sum(
        disposition.utterance is not None for disposition in result.dispositions
    )
    rejected_counts = Counter(
        disposition.decision.reason_code
        for disposition in result.dispositions
        if not disposition.decision.selected
    )
    lines.extend(
        (
            "",
            "策略汇总：",
            f"- 检测到事件：{len(result.dispositions)}",
            f"- 实际开口：{selected_count}",
            f"- 实际生成话术：{generated_count}",
        )
    )
    _append_rejection_summary(lines, rejected_counts)
    lines.append(f"- 处理快照：{result.snapshot_count}")
    return "\n".join(lines)


def format_decision_reason(decision: PolicyDecision) -> str:
    """Turn one structured policy result into the legacy Chinese diagnostic text."""
    if decision.reason_code == "selected":
        return f"优先级 {decision.priority}"
    if decision.reason_code == "teammate_event":
        return "队友事件，本里程碑不解说"
    if decision.reason_code == "muted":
        return "自动说话已关闭"
    if decision.reason_code == "alive_threshold":
        return f"交火中，优先级 {decision.priority} 未达门槛 {_format_limit(decision)}"
    if decision.reason_code == "round_limit":
        return f"本回合发言已达上限 {_format_limit(decision)}"
    if decision.reason_code == "cooldown":
        return (
            f"距上次发言 {_format_elapsed(decision)} 秒，冷却 "
            f"{_format_limit(decision)} 秒未过"
        )
    if decision.reason_code == "minimum_gap":
        return (
            f"距上次发言 {_format_elapsed(decision)} 秒，最小间隔 "
            f"{_format_limit(decision)} 秒未过"
        )
    return "已有更高优先级事件"


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
    if event.type in {"death", "death_after_kill", "death_thrown_away"}:
        survival = facts["survival_seconds"]
        survival_label = "未知" if survival is None else f"{survival:.3f} 秒"
        equip_value = facts["equip_value"]
        equip_label = "未知" if equip_value is None else str(equip_value)
        last_kill = facts["seconds_since_last_kill"]
        last_kill_label = "无" if last_kill is None else f"{last_kill:.3f} 秒前"
        situation = facts["score_situation"] or "未知"
        losses = facts["team_consecutive_round_losses"]
        losses_label = "未知" if losses is None else str(losses)
        return (
            f"存活 {survival_label}，本回合 {facts['round_kills']} 杀，"
            f"距上次击杀 {last_kill_label}，装备值 {equip_label}，"
            f"态势 {situation}，本方连败 {losses_label}"
        )
    method = facts["method"] or "未知"
    score_ct = facts["score_ct"] if facts["score_ct"] is not None else "未知"
    score_t = facts["score_t"] if facts["score_t"] is not None else "未知"
    situation = facts["score_situation"] or "未知"
    losses = facts["team_consecutive_round_losses"]
    losses_label = "未知" if losses is None else str(losses)
    return (
        f"方式 {method}，比分 CT {score_ct} : {score_t} T，态势 {situation}，"
        f"本方连败 {losses_label}"
    )


def _append_rejection_summary(
    lines: list[str], rejected_counts: Counter[DecisionReason]
) -> None:
    if rejected_counts:
        reason_summary = "、".join(
            f"{_REASON_LABELS[reason]} {rejected_counts[reason]}"
            for reason in _REASON_LABELS
            if reason != "selected" and rejected_counts[reason]
        )
        lines.append(f"- 丢弃原因：{reason_summary}")
    else:
        lines.append("- 丢弃原因：无")


def _format_elapsed(decision: PolicyDecision) -> str:
    if decision.elapsed_seconds is None:
        raise ValueError(f"policy decision {decision.reason_code} has no elapsed time")
    return f"{decision.elapsed_seconds:.3f}"


def _format_limit(decision: PolicyDecision) -> str:
    if decision.limit is None:
        raise ValueError(f"policy decision {decision.reason_code} has no limit")
    return f"{decision.limit:g}"


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
        snapshots = load_recording(args.replay)
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
