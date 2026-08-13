"""Replay CS2 recordings through production fact, policy, and commentary layers."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
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
    WeaponSlot,
    human_round_number,
    parse_snapshot,
)
from pet.lines import Utterance
from pet.policy import DecisionReason, PolicyDecision, SpeechPolicy
from pet.session import GameSessionTracker, GameState, MatchLifecycleTracker
from pet.situation import (
    RoundSituation,
    SituationTracker,
    armor_status,
    held_weapon,
    is_carrying_bomb,
    is_currently_flashed,
    is_currently_smoked,
    is_eco_round,
    is_low_ammo,
    is_low_health,
)

REPLAY_RANDOM_SEED = 20260809

# Public inventory reports must never expose a real player's display name or
# SteamID. Match complete paths so weapon, map, and provider program names keep
# their diagnostic value.
_REDACTED_RAW_PATHS: frozenset[str] = frozenset(
    {
        "player.name",
        "player.steamid",
        "provider.steamid",
        "previously.player.name",
        "previously.player.steamid",
        "added.player.name",
        "added.player.steamid",
    }
)

_PARSED_RAW_PATHS: frozenset[str] = frozenset(
    {
        "map.mode",
        "map.name",
        "map.phase",
        "map.round",
        "map.team_ct.consecutive_round_losses",
        "map.team_ct.score",
        "map.team_t.consecutive_round_losses",
        "map.team_t.score",
        "player.activity",
        "player.match_stats.assists",
        "player.match_stats.deaths",
        "player.match_stats.kills",
        "player.match_stats.mvps",
        "player.match_stats.score",
        "player.state.armor",
        "player.state.burning",
        "player.state.equip_value",
        "player.state.flashed",
        "player.state.health",
        "player.state.helmet",
        "player.state.defusekit",
        "player.state.money",
        "player.state.round_killhs",
        "player.state.round_kills",
        "player.state.smoked",
        "player.steamid",
        "player.team",
        "player.weapons.*.ammo_clip",
        "player.weapons.*.ammo_clip_max",
        "player.weapons.*.ammo_reserve",
        "player.weapons.*.name",
        "player.weapons.*.state",
        "player.weapons.*.type",
        "provider.steamid",
        "round.bomb",
        "round.phase",
        "round.win_team",
    }
)

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
    "round_event": "回合结算留给长记忆",
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
    snapshot: GameSnapshot
    game: GameState
    round_situation: RoundSituation


@dataclass(frozen=True, slots=True)
class CommentaryReplayResult:
    """Commentary decisions and aggregate metadata from one recording replay."""

    dispositions: tuple[CommentaryDisposition, ...]
    started_at: float
    snapshot_count: int


@dataclass(frozen=True, slots=True)
class _RecordingRow:
    ts: float
    line_number: int
    payload: object


@dataclass(slots=True)
class _RawPathStats:
    occurrences: int = 0
    values: list[object] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _DerivedFact:
    name: str
    dependencies: str
    rule: str


@dataclass(frozen=True, slots=True)
class _InventoryAnalysis:
    rounds: tuple[RoundSituation, ...]
    pure_values: tuple[tuple[str, tuple[object | None, ...]], ...]


_ACCUMULATED_FACTS: tuple[_DerivedFact, ...] = (
    _DerivedFact("flash_count", "player.state.flashed", "被闪值从 0 或缺失变为大于 0 时加一"),
    _DerivedFact(
        "flashed_seconds_total",
        "player.state.flashed + ts",
        "按相邻本人快照时间差累计被闪时长",
    ),
    _DerivedFact(
        "longest_flash_seconds",
        "player.state.flashed + ts",
        "取本回合单次连续被闪的最长时长",
    ),
    _DerivedFact(
        "smoked_seconds_total",
        "player.state.smoked + ts",
        "按相邻本人快照时间差累计处于烟雾中的时长",
    ),
    _DerivedFact(
        "max_smoke_intensity",
        "player.state.smoked",
        "取本回合观测到的最大烟雾强度",
    ),
    _DerivedFact("burn_count", "player.state.burning", "燃烧值从 0 或缺失变为大于 0 时加一"),
    _DerivedFact("total_damage_taken", "player.state.health", "累加相邻快照中血量的下降量"),
    _DerivedFact(
        "lowest_health_while_alive",
        "player.state.health",
        "取本回合出现过的大于 0 的最低血量",
    ),
    _DerivedFact("health_before_death", "player.state.health", "记录血量归零前最后一个非零值"),
    _DerivedFact(
        "primary_weapons_used",
        "player.weapons.*.name + player.weapons.*.type",
        "按首次出现顺序记录本回合不同主武器",
    ),
    _DerivedFact(
        "bought_equipment",
        "player.state.money + player.state.equip_value",
        "同次更新中金钱下降且装备价值上升即为真",
    ),
    _DerivedFact(
        "bomb_planted_at_ts",
        "round.bomb + ts",
        "记录本回合 bomb 首次变为 planted 的时间",
    ),
    _DerivedFact(
        "seconds_since_bomb_planted",
        "round.bomb + ts",
        "当前本人快照时间减去首次安放时间",
    ),
)
_PURE_FACTS: tuple[
    tuple[_DerivedFact, Callable[[GameSnapshot], object | None]], ...
] = (
    (
        _DerivedFact("is_low_health", "player.state.health", "血量大于 0 且不高于 30"),
        is_low_health,
    ),
    (
        _DerivedFact(
            "is_eco_round",
            "player.state.money + player.state.equip_value",
            "金钱低于 1500 且装备价值低于 2000",
        ),
        is_eco_round,
    ),
    (
        _DerivedFact("is_low_ammo", "player.weapons.*", "手持武器弹夹余弹不高于 1"),
        is_low_ammo,
    ),
    (
        _DerivedFact(
            "armor_status",
            "player.state.armor",
            "按护甲值返回无甲或有甲，不保留无关的具体甲量与头盔细分",
        ),
        armor_status,
    ),
    (
        _DerivedFact("held_weapon", "player.weapons.*.state", "返回状态为 active 的武器"),
        held_weapon,
    ),
    (
        _DerivedFact("is_currently_flashed", "player.state.flashed", "被闪标记大于 0"),
        is_currently_flashed,
    ),
    (
        _DerivedFact("is_currently_smoked", "player.state.smoked", "烟雾强度大于 0"),
        is_currently_smoked,
    ),
    (
        _DerivedFact(
            "is_carrying_bomb",
            "player.weapons.*.type",
            "武器列表已知且含 C4 类型",
        ),
        is_carrying_bomb,
    ),
)


def load_recording(path: Path) -> tuple[GameSnapshot, ...]:
    """Parse a raw JSONL recording and return snapshots in timestamp order."""
    rows = _load_recording_rows(path)
    return tuple(parse_snapshot(row.payload, received_at=row.ts) for row in rows)


def _load_recording_rows(path: Path) -> tuple[_RecordingRow, ...]:
    rows: list[_RecordingRow] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        decoded: object = json.loads(line)
        if not isinstance(decoded, Mapping):
            raise ValueError(f"recording line {line_number} must be a JSON object")
        ts = decoded.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise ValueError(f"recording line {line_number} has no numeric ts")
        rows.append(
            _RecordingRow(
                ts=float(ts),
                line_number=line_number,
                payload=decoded.get("payload"),
            )
        )
    rows.sort(key=lambda row: (row.ts, row.line_number))
    return tuple(rows)


def replay_recording(path: Path, config: EventsConfig) -> ReplayResult:
    """Replay one raw JSONL recording through the production event detector."""
    snapshots = load_recording(path)
    detector = EventDetector(config)
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    lifecycle = MatchLifecycleTracker()
    situation = SituationTracker()
    events: list[GameEvent] = []
    covered_rounds: set[tuple[str | None, int]] = set()
    for snapshot in snapshots:
        game = session.observe(snapshot)
        if lifecycle.observe(game):
            detector.reset()
            situation.reset()
        situation.observe(snapshot, game)
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
    situation = SituationTracker()
    policy = SpeechPolicy(policy_config)
    decisions: list[PolicyDecision] = []
    for snapshot in snapshots:
        game = session.observe(snapshot)
        if lifecycle.observe(game):
            detector.reset()
            situation.reset()
            policy.reset()
        situation.observe(snapshot, game)
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
    situation = SituationTracker()
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
            situation.reset()
            policy.reset()
        round_situation = situation.observe(snapshot, game)
        policy.observe_snapshot(snapshot)
        events = detector.observe(snapshot, game)
        batch = policy.decide(events, game, now=snapshot.ts, muted=muted)
        dispositions.extend(
            CommentaryDisposition(
                decision=decision,
                utterance=None,
                snapshot=snapshot,
                game=game,
                round_situation=round_situation,
            )
            for decision in batch.decisions
            if not decision.selected
        )
        if batch.selected_event is not None:
            utterance = generator.generate(
                batch.selected_event,
                map_name=snapshot.map_name,
            )
            selected_id = batch.selected_event.id
            dispositions.extend(
                CommentaryDisposition(
                    decision=decision,
                    utterance=utterance if decision.event.id == selected_id else None,
                    snapshot=snapshot,
                    game=game,
                    round_situation=round_situation,
                )
                for decision in batch.decisions
                if decision.selected
            )
    return CommentaryReplayResult(
        dispositions=tuple(dispositions),
        started_at=snapshots[0].ts if snapshots else 0.0,
        snapshot_count=len(snapshots),
    )


def generate_data_inventory(
    path: Path, *, generated_at: datetime | None = None
) -> str:
    """Generate a Markdown inventory entirely from one raw recording."""
    rows = _load_recording_rows(path)
    snapshots = tuple(
        parse_snapshot(row.payload, received_at=row.ts) for row in rows
    )
    raw_stats = _collect_raw_path_stats(rows)
    analysis = _analyze_inventory(snapshots)
    timestamp = generated_at or datetime.now().astimezone()

    lines = [
        "# CS2 GSI 数据清单",
        "",
        f"- 录制文件：`{path.name}`",
        f"- payload 条数：{len(rows)}",
        f"- 覆盖回合数：{len(analysis.rounds)}",
        f"- 生成时间：{timestamp.isoformat(timespec='seconds')}",
        "",
        "> “是否已解析”依据源码中的人工维护路径清单判断，可能随解析器改动而滞后。",
        "",
        "## 表一：原始字段清单",
        "",
        "| 字段路径 | 出现次数 | 取值样例 | 是否已解析 |",
        "|---|---:|---|:---:|",
    ]
    for path_name in sorted(raw_stats):
        stats = raw_stats[path_name]
        lines.append(
            f"| `{_escape_markdown(path_name)}` | {stats.occurrences} | "
            f"{_escape_markdown(_summarize_raw_values(path_name, stats.values))} | "
            f"{'是' if _is_parsed_raw_path(path_name) else '否'} |"
        )

    pure_values = dict(analysis.pure_values)
    lines.extend(
        (
            "",
            "## 表二：推导事实清单",
            "",
            "| 事实名 | 依赖字段 | 推导规则 | 本录制中的表现 |",
            "|---|---|---|---|",
        )
    )
    for fact in _ACCUMULATED_FACTS:
        values = tuple(getattr(round_state, fact.name) for round_state in analysis.rounds)
        lines.append(
            _format_derived_row(
                fact,
                _summarize_accumulated_values(
                    fact.name, values, round_count=len(analysis.rounds)
                ),
            )
        )
    for fact, _ in _PURE_FACTS:
        lines.append(
            _format_derived_row(
                fact,
                _summarize_pure_values(fact.name, pure_values[fact.name]),
            )
        )
    return "\n".join(lines) + "\n"


def _collect_raw_path_stats(
    rows: Sequence[_RecordingRow],
) -> dict[str, _RawPathStats]:
    collected: dict[str, _RawPathStats] = {}
    for row in rows:
        per_payload: dict[str, list[object]] = {}
        _walk_raw_leaves(row.payload, (), per_payload)
        for path_name, values in per_payload.items():
            stats = collected.setdefault(path_name, _RawPathStats())
            stats.occurrences += 1
            stats.values.extend(values)
    return collected


def _walk_raw_leaves(
    value: object,
    path: tuple[str, ...],
    found: dict[str, list[object]],
) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if path and path[-1] == "weapons" and key.startswith("weapon_"):
                suffix = key.removeprefix("weapon_")
                key = "*" if suffix.isdecimal() else key
            _walk_raw_leaves(child, (*path, key), found)
        return
    if isinstance(value, list):
        for child in value:
            _walk_raw_leaves(child, (*path, "*"), found)
        return
    if path:
        found.setdefault(".".join(path), []).append(value)


def _analyze_inventory(snapshots: Sequence[GameSnapshot]) -> _InventoryAnalysis:
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    lifecycle = MatchLifecycleTracker()
    tracker = SituationTracker()
    match_sequence = 0
    rounds: dict[tuple[int, str | None, int], RoundSituation] = {}
    pure_values: dict[str, list[object | None]] = {
        fact.name: [] for fact, _ in _PURE_FACTS
    }

    for snapshot in snapshots:
        game = session.observe(snapshot)
        if lifecycle.observe(game):
            tracker.reset()
            match_sequence += 1
        current = tracker.observe(snapshot, game)
        if (
            game.subject_is_self is True
            and snapshot.map_phase == "live"
            and current.round_number is not None
        ):
            rounds[(match_sequence, snapshot.map_name, current.round_number)] = current

        for fact, derive in _PURE_FACTS:
            value = derive(snapshot) if game.subject_is_self is True else None
            pure_values[fact.name].append(value)

    tracker.finish()

    return _InventoryAnalysis(
        rounds=tuple(rounds.values()),
        pure_values=tuple(
            (fact.name, tuple(pure_values[fact.name])) for fact, _ in _PURE_FACTS
        ),
    )


def _is_parsed_raw_path(path: str) -> bool:
    return path in _PARSED_RAW_PATHS or (
        path.startswith("map.round_wins.")
        and path.removeprefix("map.round_wins.").isdecimal()
    )


def _summarize_raw_values(path: str, values: Sequence[object]) -> str:
    if not values:
        return "—"
    if path in _REDACTED_RAW_PATHS:
        return "<已脱敏>"
    if all(isinstance(value, Mapping) for value in values):
        return "对象（字段见子项）"
    if all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in values
    ):
        numeric = tuple(
            value
            for value in values
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        )
        return f"最小 {_format_number(min(numeric))} / 最大 {_format_number(max(numeric))}"
    if all(isinstance(value, str) for value in values):
        unique = _unique_json_values(values)
        return "、".join(unique[:10])
    if all(isinstance(value, bool) for value in values):
        unique = _unique_json_values(values)
        return "、".join(unique[:5])
    return "、".join(_unique_json_values(values)[:5])


def _unique_json_values(values: Sequence[object]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if encoded in seen:
            continue
        seen.add(encoded)
        unique.append(encoded)
    return unique


def _format_number(value: int | float) -> str:
    if isinstance(value, int) or value.is_integer():
        return str(int(value))
    return f"{value:.15g}"


def _format_derived_row(fact: _DerivedFact, performance: str) -> str:
    return (
        f"| `{fact.name}` | `{fact.dependencies}` | {fact.rule} | {performance} |"
    )


def _summarize_round_values(
    values: Sequence[object | None], *, round_count: int
) -> str:
    if not values:
        return "在 0 个回合中无数据"
    if all(value is None for value in values):
        return f"在 {round_count} 个回合中均无法判断"
    if all(value is None or isinstance(value, bool) for value in values):
        return _format_boolean_counts(values)
    known = [
        value
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not known:
        return f"在 {round_count} 个回合中均无法判断"
    summary = (
        f"在 {round_count} 个回合中取值范围 "
        f"{_format_number(min(known))}–{_format_number(max(known))}"
    )
    unknown = sum(value is None for value in values)
    if unknown:
        summary += f"（无法判断 {unknown} 回合）"
    return summary


def _summarize_accumulated_values(
    name: str, values: Sequence[object | None], *, round_count: int
) -> str:
    if name != "primary_weapons_used":
        return _summarize_round_values(values, round_count=round_count)
    sequences = [value for value in values if isinstance(value, tuple)]
    unique = list(dict.fromkeys(sequences))
    samples = "、".join(
        "(" + ", ".join(f"`{weapon}`" for weapon in weapons) + ")"
        if weapons
        else "()"
        for weapons in unique[:10]
    )
    return f"在 {round_count} 个回合中：{samples or '无数据'}"


def _summarize_pure_values(name: str, values: Sequence[object | None]) -> str:
    if name == "armor_status":
        counts = Counter(values)
        return (
            f"无甲 {counts['无甲']} 次 / 有甲 {counts['有甲']} 次 / "
            f"无法判断 {counts[None]} 次"
        )
    if name == "held_weapon":
        names = [value.name for value in values if isinstance(value, WeaponSlot)]
        unique_names = list(dict.fromkeys(names))
        samples = "、".join(f"`{weapon}`" for weapon in unique_names[:5]) or "无"
        unknown = sum(value is None for value in values)
        return f"手持武器样例 {samples} / 无法判断 {unknown} 次"
    return _format_boolean_counts(values)


def _format_boolean_counts(values: Sequence[object | None]) -> str:
    true_count = sum(value is True for value in values)
    false_count = sum(value is False for value in values)
    unknown_count = sum(value is None for value in values)
    return f"真 {true_count} 次 / 假 {false_count} 次 / 无法判断 {unknown_count} 次"


def _escape_markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


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
    if decision.reason_code == "round_event":
        return "回合结算事件，留给后续长记忆"
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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--replay", type=Path, help="GSI JSONL 录制文件")
    source.add_argument(
        "--data-inventory",
        type=Path,
        help="从 GSI JSONL 录制生成字段与推导事实清单",
    )
    parser.add_argument("--out", type=Path, help="数据清单 Markdown 输出路径")
    parser.add_argument(
        "--with-policy",
        action="store_true",
        help="展示发言策略决定、丢弃原因与实际模板话术",
    )
    parser.add_argument(
        "--with-commentary",
        action="store_true",
        help="展示发言策略决定、丢弃原因与实际模板话术",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the recording replay CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.data_inventory is not None:
        if args.out is None:
            parser.error("--data-inventory requires --out")
        if args.with_policy or args.with_commentary:
            parser.error("commentary flags can only be used with --replay")
        report = generate_data_inventory(args.data_inventory)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8", newline="\n")
        print(f"数据清单已写入：{args.out}")
        return 0

    if args.out is not None:
        parser.error("--out can only be used with --data-inventory")
    if args.replay is None:
        parser.error("--replay is required")
    configuration = load_config()
    if args.with_policy or args.with_commentary:
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
