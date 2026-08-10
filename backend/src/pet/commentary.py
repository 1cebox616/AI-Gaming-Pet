"""Turn selected CS2 events into Chinese template commentary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import random
from typing import Any

from pydantic import BaseModel, ConfigDict

from pet.commentary_templates import (
    COMMENTARY_TEMPLATES,
    CommentaryCategory,
    CommentaryTemplate,
    templates_for_map,
)
from pet.config import EventsConfig, PersonalityStyle, PolicyConfig
from pet.events import EventDetector, EventType, GameEvent
from pet.gsi import GSI_SILENCE_SECONDS, GameSnapshot
from pet.lines import Utterance
from pet.policy import DecisionReason, PolicyDecision, SpeechPolicy
from pet.session import GameSessionTracker, GameState

REPLAY_RANDOM_SEED = 20260809

WIN_METHOD_LABELS: dict[str, str] = {
    "elimination": "灭队",
    "bomb": "炸弹引爆",
    "defuse": "炸弹拆除",
    "time": "时间耗尽",
    "ct_win_elimination": "灭队",
    "t_win_elimination": "灭队",
    "ct_win_bomb": "炸弹引爆",
    "t_win_bomb": "炸弹引爆",
    "ct_win_defuse": "炸弹拆除",
    "t_win_defuse": "炸弹拆除",
    "ct_win_time": "时间耗尽",
    "t_win_time": "时间耗尽",
}

_METHOD_CATEGORY_BY_LABEL = {
    "灭队": "elimination",
    "炸弹引爆": "bomb",
    "炸弹拆除": "defuse",
    "时间耗尽": "time",
}
_MULTI_CATEGORIES: dict[int | None, CommentaryCategory] = {
    2: "multi_2",
    3: "multi_3",
    4: "multi_4",
    5: "multi_5",
    None: "multi_general",
}
_ROUND_CATEGORIES: dict[tuple[EventType, str | None], CommentaryCategory] = {
    ("round_win", "elimination"): "round_win_elimination",
    ("round_win", "bomb"): "round_win_bomb",
    ("round_win", "defuse"): "round_win_defuse",
    ("round_win", "time"): "round_win_time",
    ("round_win", None): "round_win_general",
    ("round_loss", "elimination"): "round_loss_elimination",
    ("round_loss", "bomb"): "round_loss_bomb",
    ("round_loss", "defuse"): "round_loss_defuse",
    ("round_loss", "time"): "round_loss_time",
    ("round_loss", None): "round_loss_general",
}
_DIRECT_CATEGORIES: dict[EventType, CommentaryCategory] = {
    "kill": "kill",
    "kill_headshot": "kill_headshot",
    "death": "death",
    "death_after_kill": "death_after_kill",
    "death_thrown_away": "death_thrown_away",
}
_EVENT_LABELS: dict[EventType, str] = {
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


class CommentaryDisposition(BaseModel):
    """One policy decision and its generated utterance when selected."""

    model_config = ConfigDict(frozen=True)

    decision: PolicyDecision
    utterance: Utterance | None


class CommentaryBatch(BaseModel):
    """Every disposition produced while processing one snapshot."""

    model_config = ConfigDict(frozen=True)

    dispositions: tuple[CommentaryDisposition, ...]
    utterance: Utterance | None


@dataclass(frozen=True, slots=True)
class CommentaryReplayResult:
    """Commentary decisions and aggregate metadata from one recording replay."""

    dispositions: tuple[CommentaryDisposition, ...]
    started_at: float
    snapshot_count: int


class CommentaryGenerator:
    """Choose and safely fill templates without repeating one category consecutively."""

    def __init__(
        self,
        rng: random.Random | None = None,
        *,
        personality_style: PersonalityStyle = "brother",
    ) -> None:
        self._rng = rng or random.Random()
        self._templates = COMMENTARY_TEMPLATES[personality_style]
        self._last_templates: dict[CommentaryCategory, CommentaryTemplate] = {}

    def generate(self, event: GameEvent, *, map_name: str | None = None) -> Utterance:
        """Generate one valid utterance without exposing missing or raw method values."""
        category = commentary_category(event)
        templates = templates_for_map(self._templates[category], map_name)
        if not templates:
            raise ValueError(
                f"no commentary template for category {category!r} on map {map_name!r}"
            )
        template_index = self._choose_template_index(category, templates)
        template = templates[template_index]
        text = template.text.format_map(_template_context(event))
        return Utterance(
            id=f"game-{event.id}",
            text=text,
            emotion=template.emotion,
        )

    def _choose_template_index(
        self,
        category: CommentaryCategory,
        templates: tuple[CommentaryTemplate, ...],
    ) -> int:
        previous = self._last_templates.get(category)
        if previous is None or len(templates) == 1:
            selected = self._rng.randrange(len(templates))
        else:
            alternatives = tuple(
                index for index, template in enumerate(templates) if template != previous
            )
            selected = self._rng.choice(alternatives)
        self._last_templates[category] = templates[selected]
        return selected


class GameCommentaryEngine:
    """Run event detection, policy, and template generation for live snapshots."""

    def __init__(
        self,
        events_config: EventsConfig,
        policy_config: PolicyConfig,
        generator: CommentaryGenerator | None = None,
        *,
        personality_style: PersonalityStyle = "brother",
    ) -> None:
        self._detector = EventDetector(events_config)
        self._policy = SpeechPolicy(policy_config)
        self._generator = generator or CommentaryGenerator(
            personality_style=personality_style
        )

    def observe(
        self,
        snapshot: GameSnapshot,
        game: GameState,
        *,
        muted: bool,
    ) -> CommentaryBatch:
        """Process one snapshot in the required health-before-decision order."""
        self._policy.observe_snapshot(snapshot)
        events = self._detector.observe(snapshot)
        policy_batch = self._policy.decide(
            events,
            game,
            now=snapshot.ts,
            muted=muted,
        )
        utterance = (
            self._generator.generate(
                policy_batch.selected_event,
                map_name=snapshot.map_name,
            )
            if policy_batch.selected_event is not None
            else None
        )
        dispositions = tuple(
            CommentaryDisposition(
                decision=decision,
                utterance=(utterance if decision.selected else None),
            )
            for decision in policy_batch.decisions
        )
        return CommentaryBatch(dispositions=dispositions, utterance=utterance)


def commentary_category(event: GameEvent) -> CommentaryCategory:
    """Map structured event facts to one independently editable template group."""
    if event.type == "multi_kill":
        count = _integer_fact(event.facts, "count")
        return _MULTI_CATEGORIES.get(count, "multi_general")
    if event.type in {"round_win", "round_loss"}:
        method = _method_suffix(event.facts.get("method"))
        return _ROUND_CATEGORIES[(event.type, method)]
    return _DIRECT_CATEGORIES[event.type]


def replay_commentary(
    snapshots: Sequence[GameSnapshot],
    events_config: EventsConfig,
    policy_config: PolicyConfig,
    *,
    muted: bool = False,
    random_seed: int = REPLAY_RANDOM_SEED,
    personality_style: PersonalityStyle = "brother",
) -> CommentaryReplayResult:
    """Replay the complete session-to-commentary chain with a fixed random seed."""
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    engine = GameCommentaryEngine(
        events_config,
        policy_config,
        CommentaryGenerator(
            random.Random(random_seed),
            personality_style=personality_style,
        ),
    )
    dispositions: list[CommentaryDisposition] = []
    for snapshot in snapshots:
        game = session.observe(snapshot)
        batch = engine.observe(snapshot, game, muted=muted)
        dispositions.extend(batch.dispositions)
    return CommentaryReplayResult(
        dispositions=tuple(dispositions),
        started_at=snapshots[0].ts if snapshots else 0.0,
        snapshot_count=len(snapshots),
    )


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
        event_label = _EVENT_LABELS[event.type]
        if event.type == "multi_kill":
            count = _integer_fact(event.facts, "count")
            event_label = f"{count} 杀" if count is not None else "多杀"
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
            f"{action} | {decision.reason} | {spoken}"
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


def _template_context(event: GameEvent) -> dict[str, str]:
    kill_index = _integer_fact(event.facts, "round_kill_index")
    survival_seconds = _numeric_fact(event.facts, "survival_seconds")
    equip_value = _integer_fact(event.facts, "equip_value")
    score_ct = _integer_fact(event.facts, "score_ct")
    score_t = _integer_fact(event.facts, "score_t")
    return {
        "kill_detail": (
            f"本回合第 {kill_index} 杀，" if kill_index is not None else ""
        ),
        "survival_detail": (
            f"存活了 {_display_seconds(survival_seconds)} 秒，"
            if survival_seconds is not None
            else ""
        ),
        "equip_detail": (
            f"还带着 {equip_value} 的装备，" if equip_value is not None else ""
        ),
        "score_detail": (
            f"比分来到 {score_ct}:{score_t}，"
            if score_ct is not None and score_t is not None
            else ""
        ),
    }


def _integer_fact(facts: Mapping[str, Any], key: str) -> int | None:
    value = facts.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _numeric_fact(facts: Mapping[str, Any], key: str) -> float | None:
    value = facts.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _display_seconds(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _method_suffix(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    label = WIN_METHOD_LABELS.get(value)
    if label is None:
        suffix = value.partition("_win_")[2] or value
        label = WIN_METHOD_LABELS.get(suffix)
    return _METHOD_CATEGORY_BY_LABEL.get(label) if label is not None else None
