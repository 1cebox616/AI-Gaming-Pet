"""Render existing CS2 facts into the GSI event card shown to a model."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
import logging
import re
from typing import Any

from pet.games.cs2.template_rules import WIN_METHOD_LABELS
from pet.games.cs2.events import EventType, GameEvent
from pet.games.cs2.gsi import GameSnapshot, WeaponSlot, human_round_number
from pet.games.cs2.session import GameState
from pet.games.cs2.situation import (
    RoundSituation,
    BURN_BAD_LUCK_DAMAGE,
    DEATH_COMBAT_WINDOW_SECONDS,
    FLASH_DEATH_AFTERGLOW_SECONDS,
    FLASH_BAD_LUCK_COUNT,
    FLASH_BAD_LUCK_SECONDS,
    RESIDUAL_KILL_HEALTH,
    MISFIRE_DEATH_MIN_AMMO,
    SCENE_TAGS,
    SMOKE_DEATH_WINDOW_SECONDS,
    WEAPON_SWITCH_KILL_WINDOW_SECONDS,
    TimelineEntry,
    armor_status,
    economy_tier,
    held_weapon,
    is_carrying_bomb,
    is_currently_flashed,
    is_currently_smoked,
    round_stage_label,
    weapon_display_name,
)

_MODE_LABELS = {
    "casual": "休闲",
    "competitive": "竞技",
    "scrimcomp2v2": "搭档",
    "deathmatch": "死亡竞赛",
}
_EVENT_LABELS: dict[EventType, str] = {
    "kill": "普通击杀",
    "kill_headshot": "爆头击杀",
    "multi_kill": "多杀",
    "death": "普通死亡",
    "death_after_kill": "击杀后被补枪",
    "death_thrown_away": "白给",
    "round_win": "回合胜利",
    "round_loss": "回合失败",
}
_FACT_EVENT_LABELS: dict[EventType, str] = {
    "kill": "击杀",
    "kill_headshot": "爆头击杀",
    "multi_kill": "多杀",
    "death": "阵亡",
    "death_after_kill": "被补",
    "death_thrown_away": "白给",
    "round_win": "回合胜利",
    "round_loss": "回合失败",
}
FACT_EVENT_NAMES: frozenset[str] = frozenset(_FACT_EVENT_LABELS.values())
_FACT_ORDER = (
    "round_kill_index",
    "weapon",
    "count",
    "survival_seconds",
    "round_kills",
    "seconds_since_last_kill",
    "equip_value",
    "method",
)
_ZERO_SIGNAL_EVENT_FACTS = {
    "round_kill_index",
    "count",
    "round_kills",
}
_STAGE_ANNOTATED_TIMELINE_KINDS = frozenset({"kill", "death"})
_CARD_HIDDEN_TIMELINE_KINDS = frozenset({"reload"})
_NEARBY_COMBAT_SECONDS = 1.0
_CONTINUOUS_EVENT_MAX_SECONDS = 3.0
_CONTINUOUS_EVENT_KINDS = frozenset(
    {
        "flash_start",
        "flash_end",
        "smoke_start",
        "smoke_end",
        "burn_start",
        "burn_end",
        "kill",
        "damage",
        "primary_weapon",
        "ammo_low",
        "grenade_used",
        "bomb",
        "bomb_pickup",
        "bomb_drop",
        "assist",
        "mvp",
        "death",
        "round_result",
    }
)

logger = logging.getLogger(__name__)

_KILL_SPECIAL_SCENE_TAGS = frozenset(
    {"狙击击杀", "白着打", "踩火杀", "摸烟击杀", "换枪后立刻杀"}
)
_KILL_STREAK_SCENE_TAGS = frozenset({"连续双杀", "连续三杀", "连续四杀", "连续五杀"})
_KILL_MULTI_SCENE_TAGS = frozenset({"多杀2", "多杀3", "多杀4", "多杀5+"})
_KILL_GUN_SCENE_TAGS = frozenset({"颗秒", "秒杀", "有些吃力", "马完了"})
_KILL_LOWEST_PRIORITY_SCENE_TAGS = frozenset({"普通击杀"})
_DEATH_NATURE_SCENE_TAGS = frozenset({"白给", "击杀后被补枪", "马枪死", "送狙", "切雷时被打死", "切刀时被打死"})
_DEATH_RETALIATION_SCENE_TAGS = frozenset(
    {"对枪输了", "一枪没开就没了", "打空了还是没打过"}
)
_DEATH_UTILITY_SCENE_TAGS = frozenset({"白着被打死", "烟里死", "出烟就没了"})


@dataclass(frozen=True, slots=True)
class SceneTagSelection:
    """Scene labels visible to the style layer and lower-priority omissions."""

    selected: tuple[str, ...]
    discarded: tuple[str, ...]
_CONTINUOUS_EVENT_ANCHORS = frozenset({"kill", "death", "round_result"})
_AMMO_SCENE_WEAPONS = frozenset(
    {
        "ak47",
        "m4a1",
        "m4a1_silencer",
        "m4a4",
        "aug",
        "famas",
        "galilar",
        "sg556",
        "deagle",
        "沙鹰",
        "ak47",
        "m4a1-s",
        "m4a4",
        "famas",
        "galil ar",
        "aug",
    }
)
_KILL_INCREASE_PATTERN = re.compile(r"(?:^| )增加(\d+)杀(?: |$)")
# Real casual recordings (45 completed live rounds) have a median observed
# live-to-settlement duration of 83.9 seconds; 64 seconds is its final 20.
# No other mode has enough observed complete rounds, so it is intentionally
# not inferred for them.
CASUAL_ROUND_MEDIAN_SECONDS = 83.9
LATE_ROUND_WINDOW_SECONDS = 20.0


def render_event_card(
    snapshot: GameSnapshot,
    game: GameState,
    round_situation: RoundSituation,
    event: GameEvent,
    *,
    death_after_kill_max_seconds: float = 8.0,
) -> str:
    """Render one policy-selected event as a GSI event card."""
    sections: list[str | None] = [
        *_match_sections(snapshot, round_situation, event),
        _section("我", _player_facts(snapshot, game, round_situation)),
        _section("全场", _match_statistics(snapshot, game)),
    ]
    current_round = human_round_number(snapshot)
    focus_rendered = False
    if round_situation.round_number == current_round:
        event_self_team = event.facts.get("self_team")
        self_team = round_situation.self_team or (
            event_self_team if isinstance(event_self_team, str) else None
        )
        sections.append(_bomb_timer(round_situation.seconds_since_bomb_planted))
        sections.append(_grenade_summary(round_situation.grenades_used))
        timeline_history, focus_line = _timeline_sections(
            round_situation.timeline,
            self_team=self_team,
            death_after_kill_max_seconds=death_after_kill_max_seconds,
            event=event,
            snapshot=snapshot,
            round_situation=round_situation,
        )
        sections.append(timeline_history)
        state_tags = _round_state_tags(snapshot, round_situation, game)
        if state_tags:
            sections.append(f"【本回合状态】{'｜'.join(state_tags)}")
        if focus_line is not None:
            sections.append(f"【刚刚】（唯一回应范围）\n{focus_line}")
            focus_rendered = True
    if not focus_rendered:
        # Incomplete or stale timelines cannot host a truthful timeline focus.
        fallback = " ".join(_event_facts(event))
        sections.append(f"【刚刚】（唯一回应范围）\n  {fallback}")
    return "\n".join(section for section in sections if section is not None)


def render_model_event_card(
    snapshot: GameSnapshot,
    game: GameState,
    round_situation: RoundSituation,
    event: GameEvent,
    *,
    death_after_kill_max_seconds: float = 8.0,
) -> str:
    """Render the compact header and sole current-event section sent to a model."""
    current_round = human_round_number(snapshot)
    focus_line: str | None = None
    if round_situation.round_number == current_round:
        event_self_team = event.facts.get("self_team")
        self_team = round_situation.self_team or (
            event_self_team if isinstance(event_self_team, str) else None
        )
        _, focus_line = _timeline_sections(
            round_situation.timeline,
            self_team=self_team,
            death_after_kill_max_seconds=death_after_kill_max_seconds,
            event=event,
            snapshot=snapshot,
            round_situation=round_situation,
        )
    if focus_line is None:
        focus_line = "  " + " ".join(_event_facts(event))

    header = _model_header(snapshot, event, round_situation)
    state_tags = _round_state_tags(snapshot, round_situation, game)
    direct_focus = _direct_focus_section(focus_line, event, state_tags=state_tags)
    return "\n".join(part for part in (header, direct_focus) if part)


def render_fact_sentence(
    snapshot: GameSnapshot,
    game: GameState,
    round_situation: RoundSituation,
    event: GameEvent,
    *,
    death_after_kill_max_seconds: float = 8.0,
) -> str:
    """Render the focused event as plain Chinese prose for the style layer.

    This is deliberately a rendering pass over the same deterministic focus used
    by the compact event card.  It does not infer a new event, scene, player, or
    opponent fact; the style layer receives prose instead of having to decode
    card labels.
    """
    current_round = human_round_number(snapshot)
    focus_line: str | None = None
    if round_situation.round_number == current_round:
        event_self_team = event.facts.get("self_team")
        self_team = round_situation.self_team or (
            event_self_team if isinstance(event_self_team, str) else None
        )
        _, focus_line = _timeline_sections(
            round_situation.timeline,
            self_team=self_team,
            death_after_kill_max_seconds=death_after_kill_max_seconds,
            event=event,
            snapshot=snapshot,
            round_situation=round_situation,
        )
    if focus_line is None:
        focus_line = " ".join(_event_facts(event))

    tag_selection = fact_sentence_scene_tag_selection(
        snapshot,
        game,
        round_situation,
        event,
        focus_line=focus_line,
    )
    process = (
        _fact_multikill_process(snapshot, round_situation, event)
        if event.type == "multi_kill"
        else _fact_process(focus_line, event, tag_selection.selected + tag_selection.discarded, snapshot)
    )
    nearby = _focused_nearby_clause(round_situation, event)
    if nearby is not None and "玩家" in process:
        process = process.replace("玩家", f"玩家{nearby}", 1)
    return "\n".join(
        (
            _model_header(snapshot, event, round_situation),
            f"【事件】{_fact_event_label(event.type)}",
            f"【过程】{process}",
            f"【场景标签】{'、'.join(tag_selection.selected) if tag_selection.selected else '无'}",
        )
    )


def _focused_nearby_clause(
    round_situation: RoundSituation, event: GameEvent
) -> str | None:
    """Keep only timeline facts adjacent to the selected event, never old history."""
    entries = round_situation.timeline
    focus_index = _focus_entry_index(entries, event)
    if focus_index is None:
        return None
    focus_seconds = entries[focus_index].seconds
    nearby = tuple(
        entry for entry in entries[:focus_index]
        if 0 <= focus_seconds - entry.seconds <= 3.0
    )
    kinds = tuple(entry.kind for entry in nearby)
    if "bomb_drop" in kinds and "bomb_pickup" in kinds:
        return "丢包又捡回后"
    if "bomb_pickup" in kinds:
        return "拿包后"
    if sum(entry.kind == "grenade_used" for entry in nearby) >= 4:
        return "连扔四颗道具后"
    return None


def fact_sentence_scene_tag_selection(
    snapshot: GameSnapshot,
    game: GameState,
    round_situation: RoundSituation,
    event: GameEvent,
    *,
    focus_line: str | None = None,
) -> SceneTagSelection:
    """Return the three most useful scene cues and explicitly omitted labels."""
    if focus_line is None:
        current_round = human_round_number(snapshot)
        if round_situation.round_number == current_round:
            event_self_team = event.facts.get("self_team")
            self_team = round_situation.self_team or (
                event_self_team if isinstance(event_self_team, str) else None
            )
            _, focus_line = _timeline_sections(
                round_situation.timeline,
                self_team=self_team,
                death_after_kill_max_seconds=8.0,
                event=event,
                snapshot=snapshot,
                round_situation=round_situation,
            )
    # "普通击杀" is also the human event label, so it must come from the
    # measured ammo band below rather than a substring match on the focus name.
    all_tags = [
        label
        for label in sorted(SCENE_TAGS)
        if label != "普通击杀"
        and label not in (_KILL_GUN_SCENE_TAGS if event.type == "multi_kill" else frozenset())
        and focus_line
        and label in focus_line
    ]
    for label in _round_state_tags(snapshot, round_situation, game):
        if label not in all_tags:
            all_tags.append(label)
    if event.type == "multi_kill":
        ammo_drops: list[int] = []
        focus_index = _focus_entry_index(round_situation.timeline, event)
        entries_before_focus = (
            round_situation.timeline[: focus_index + 1]
            if focus_index is not None
            else ()
        )
        for index, entry in enumerate(entries_before_focus):
            if entry.kind != "kill":
                continue
            ammo_drop = _ammo_drop(entry.detail)
            if ammo_drop is not None:
                ammo_drops.append(ammo_drop)
            for label in _action_scene_tags(
                entries_before_focus,
                index,
                event=event,
                snapshot=snapshot,
                round_situation=round_situation,
            ):
                if label not in _KILL_GUN_SCENE_TAGS and label not in all_tags:
                    all_tags.append(label)
        kill_count = event.facts.get("count")
        divisor = kill_count if isinstance(kill_count, int) and kill_count > 0 else len(ammo_drops)
        if ammo_drops and divisor:
            weapons = {
                _kill_weapon_name(entry.detail)
                for entry in entries_before_focus
                if entry.kind == "kill" and _kill_weapon_name(entry.detail) is not None
            }
            ammo_tag = _ammo_evaluation_tag(
                sum(ammo_drops) / divisor,
                next(iter(weapons)) if len(weapons) == 1 else None,
            )
            if ammo_tag is not None and ammo_tag not in all_tags:
                all_tags.append(ammo_tag)
    else:
        focus_index = _focus_entry_index(round_situation.timeline, event)
        if focus_index is not None:
            for label in _action_scene_tags(
                round_situation.timeline,
                focus_index,
                event=event,
                snapshot=snapshot,
                round_situation=round_situation,
            ):
                if label not in all_tags:
                    all_tags.append(label)
    focus_index = _focus_entry_index(round_situation.timeline, event)
    if event.type in {"kill", "kill_headshot", "multi_kill"} and focus_index is not None:
        kill_progress_tag = _kill_progress_scene_tag(
            round_situation.timeline, focus_index, snapshot.map_mode
        )
        if kill_progress_tag is not None and kill_progress_tag not in all_tags:
            all_tags.append(kill_progress_tag)
        gun_evaluation_entries = (
            round_situation.timeline[: focus_index + 1]
            if event.type == "multi_kill"
            else (round_situation.timeline[focus_index],)
        )
        if any(
            _is_sniper_weapon_name(_kill_weapon(entry.detail))
            for entry in gun_evaluation_entries
            if entry.kind == "kill"
        ):
            all_tags = [tag for tag in all_tags if tag not in _KILL_GUN_SCENE_TAGS]
    all_tags = _resolve_fact_sentence_scene_tag_conflicts(all_tags)
    ordered = _ordered_fact_scene_tags(event.type, all_tags)
    return SceneTagSelection(selected=tuple(ordered[:3]), discarded=tuple(ordered[3:]))


def _resolve_fact_sentence_scene_tag_conflicts(tags: Iterable[str]) -> list[str]:
    """Remove redundant cues before the three-label product-priority selection."""
    resolved = list(dict.fromkeys(tags))
    utility_death_tags = {"白着被打死", "烟里死", "出烟就没了"}
    if utility_death_tags.intersection(resolved):
        resolved = [tag for tag in resolved if tag != "对枪输了"]
    # AWP misses are a stronger round-level spectator cue than the narrower
    # final-three-seconds no-fire observation.  Keep that signal unambiguous.
    if {"大狙空枪", "连续空枪"}.intersection(resolved):
        resolved = [tag for tag in resolved if tag != "一枪没开就没了"]
    if any(tag in _KILL_STREAK_SCENE_TAGS for tag in resolved):
        resolved = [tag for tag in resolved if tag not in _KILL_MULTI_SCENE_TAGS]
    return resolved


def _ordered_fact_scene_tags(event_type: EventType, tags: Iterable[str]) -> list[str]:
    """Order labels by the product's fixed observer-interest priority."""
    unique = list(dict.fromkeys(tags))
    if event_type in {"kill", "kill_headshot", "multi_kill"}:
        def rank(label: str) -> tuple[int, str]:
            if label in _KILL_STREAK_SCENE_TAGS:
                return 1, label
            if label in _KILL_SPECIAL_SCENE_TAGS:
                return 2, label
            if label == "对枪胜利":
                return 3, label
            if label in _KILL_GUN_SCENE_TAGS:
                return 4, label
            if label in _KILL_MULTI_SCENE_TAGS:
                return 5, label
            if label in _KILL_LOWEST_PRIORITY_SCENE_TAGS:
                return 6, label
            return 5, label
    elif event_type in {"death", "death_after_kill", "death_thrown_away"}:
        def rank(label: str) -> tuple[int, str]:
            if label in _DEATH_NATURE_SCENE_TAGS:
                return 1, label
            if label in _DEATH_RETALIATION_SCENE_TAGS:
                return 2, label
            if label in _DEATH_UTILITY_SCENE_TAGS:
                return 3, label
            return 4, label
    else:
        def rank(label: str) -> tuple[int, str]:
            return 1, label
    return sorted(unique, key=rank)


def _fact_event_label(event_type: EventType) -> str:
    return _FACT_EVENT_LABELS[event_type]


def _fact_process(
    focus_line: str,
    event: GameEvent,
    tags: tuple[str, ...],
    snapshot: GameSnapshot,
) -> str:
    """Render a plain-language event process without exposing measurements."""
    match = _FOCUS_LINE_PATTERN.fullmatch(focus_line.strip())
    if match is None or (match.group("marker") is None and match.group("seconds") is None):
        return _plain_event_fallback(event).removeprefix("你")
    marker = match.group("marker") or ""
    stage = next((fact for fact in marker.removeprefix("【").removesuffix("】").split("｜") if fact and not fact.startswith("连续事件")), None)
    expression = match.group("expression").strip()
    stage_prefix = f"{stage}，" if stage and stage != "阶段不可判断" else ""
    if event.type in {"kill", "kill_headshot"}:
        return stage_prefix + _fact_kill_process(expression, event, tags, snapshot)
    if event.type in {"death", "death_after_kill", "death_thrown_away"}:
        return stage_prefix + _fact_death_process(event, tags)
    return stage_prefix + _plain_event_fallback(event).removeprefix("你")


def _fact_kill_process(
    expression: str,
    event: GameEvent,
    tags: tuple[str, ...],
    snapshot: GameSnapshot,
) -> str:
    weapon = _kill_weapon_name(expression)
    weapon_phrase = f"用{weapon}" if weapon is not None else ""
    headshot = "爆头" if event.type == "kill_headshot" or "爆头" in expression else ""
    health = _kill_health_value(expression)
    if health is None:
        health = snapshot.health
    tier = _health_tier(health)
    contexts = _kill_context_clauses(tags)
    context_phrase = "、".join(contexts)
    action = f"{weapon_phrase}{headshot}完成击杀" or "完成击杀"
    if tier in {"丝血", "残血"} and "对枪胜利" not in tags:
        sentence = f"玩家{tier}还{context_phrase}{action}"
    elif "对枪胜利" in tags:
        outcome = f"打成{tier}" if tier in {"丝血", "残血"} else ""
        sentence = "玩家赢下对枪"
        if outcome:
            sentence += f"，{outcome}"
        sentence += f"，{context_phrase}{action}" if context_phrase else f"，{action}"
    elif tier is not None:
        sentence = f"玩家{tier}{context_phrase}{action}"
    else:
        sentence = f"玩家{context_phrase}{action}" if context_phrase else f"玩家{action}"
    evaluation = _gun_evaluation_clause(tags)
    return f"{sentence}，{evaluation}" if evaluation else sentence


def _kill_context_clauses(tags: Iterable[str]) -> tuple[str, ...]:
    labels = {
        "白着打": "被闪时",
        "踩火杀": "踩火时",
        "摸烟击杀": "出烟后不久",
        "换枪后立刻杀": "换枪后",
    }
    return tuple(labels[tag] for tag in tags if tag in labels)


def _gun_evaluation_clause(tags: Iterable[str]) -> str | None:
    if "颗秒" in tags:
        return "一发就带走"
    if "秒杀" in tags:
        return "几枪就解决"
    if "有些吃力" in tags:
        return "打了不少发"
    if "马完了" in tags:
        return "开了很多枪"
    return None


def _fact_death_process(event: GameEvent, tags: Iterable[str]) -> str:
    tag_set = set(tags)
    if event.type == "death_after_kill":
        parts = ["玩家刚完成击杀就被补掉"]
    elif event.type == "death_thrown_away":
        parts = ["玩家白给"]
    else:
        parts = ["玩家阵亡"]
    if "马枪死" in tag_set:
        parts.append("开了这么多枪没打死")
    elif "对枪输了" in tag_set:
        parts.append("开火后没打过")
    elif "打空了还是没打过" in tag_set:
        parts.append("弹匣打空还是没打过")
    elif "一枪没开就没了" in tag_set:
        parts.append("一枪没开就没了")
    utility = {
        "白着被打死": "被闪时阵亡",
        "烟里死": "烟里阵亡",
        "出烟就没了": "出烟后不久阵亡",
    }
    parts.extend(utility[tag] for tag in tags if tag in utility)
    return "，".join(dict.fromkeys(parts))


def _health_tier(health: int | None) -> str | None:
    """Use product health bands, never expose the raw value to the style model."""
    if health is None or health <= 0:
        return None
    if health < 25:
        return "丝血"
    if health < 50:
        return "残血"
    if health == 100:
        return "满血"
    return None


def _fact_multikill_process(
    snapshot: GameSnapshot,
    round_situation: RoundSituation,
    event: GameEvent,
) -> str:
    """Summarize every kill represented by a multi-kill event exactly once."""
    entries = round_situation.timeline
    focus_index = _focus_entry_index(entries, event)
    if focus_index is None:
        return _plain_event_fallback(event).removeprefix("你")
    kill_indexes = tuple(
        index
        for index, entry in enumerate(entries[: focus_index + 1])
        if entry.kind == "kill"
    )
    if not kill_indexes:
        return _plain_event_fallback(event).removeprefix("你")

    observed_live = any(entry.kind == "round_live" for entry in entries)
    stages = _timeline_stages(
        entries,
        self_team=round_situation.self_team,
        observed_live=observed_live,
    )
    count_fact = event.facts.get("count")
    counted_kills = sum(_timeline_kill_increase(entries[index]) for index in kill_indexes)
    kill_count = (
        count_fact
        if isinstance(count_fact, int) and count_fact >= 2
        else max(counted_kills, 2)
    )
    segment_indexes = next(
        (
            group
            for group in _continuous_event_groups(entries)
            if all(index in group for index in kill_indexes)
        ),
        (),
    )
    connected = bool(segment_indexes)
    stage_values = tuple(
        dict.fromkeys(
            stage
            for index in kill_indexes
            if (stage := stages[index]) is not None and stage != "阶段不可判断"
        )
    )
    weapons = tuple(
        dict.fromkeys(
            weapon
            for index in kill_indexes
            if (weapon := _kill_weapon(entries[index].detail)) is not None
        )
    )
    context = _multikill_stage_context(stage_values)
    action = _multikill_action_phrase(weapons, kill_count, connected)
    health_clause = _multikill_health_clause(entries, kill_indexes, snapshot)
    if health_clause is not None and health_clause.endswith("还"):
        action = health_clause + action.removeprefix("玩家")
        health_clause = None
    parts = [part for part in (context, health_clause, action) if part]
    parts.extend(
        _multikill_finish_clauses(
            entries,
            kill_indexes,
            event,
            snapshot,
            round_situation,
        )
    )
    return "，".join(parts)


def _multikill_stage_context(stages: tuple[str, ...]) -> str | None:
    """Describe one stage once, or make a cross-stage sequence explicit."""
    if not stages:
        return None
    if len(stages) == 1:
        return stages[0]
    return f"从{stages[0]}打到{stages[-1]}"


def _multikill_action_phrase(
    weapons: tuple[str, ...], kill_count: int, connected: bool
) -> str:
    """State the kill total and whether it was one continuous run."""
    kill_label = _kill_count_label(kill_count)
    verb = f"连拿{kill_label}" if connected else f"陆续拿到{kill_label}"
    if not weapons:
        return "玩家" + verb
    if len(weapons) == 1:
        return f"玩家用{weapons[0]}{verb}"
    return f"玩家用{weapons[0]}，换成{weapons[-1]}接着{verb}"


def _multikill_finish_clauses(
    entries: tuple[TimelineEntry, ...],
    kill_indexes: tuple[int, ...],
    event: GameEvent,
    snapshot: GameSnapshot,
    round_situation: RoundSituation,
) -> tuple[str, ...]:
    """Keep exceptional kill facts without replaying every ordinary kill."""
    rare_tags = _KILL_SPECIAL_SCENE_TAGS
    clauses: list[str] = []
    for ordinal, index in enumerate(kill_indexes, 1):
        tags = _action_scene_tags(
            entries,
            index,
            event=event,
            snapshot=snapshot,
            round_situation=round_situation,
        )
        found = tuple(tag for tag in tags if tag in rare_tags)
        if found:
            subject = "最后一杀" if ordinal == len(kill_indexes) else f"第{ordinal}杀"
            labels = {
                "狙击击杀": "使用狙击枪完成击杀",
                "白着打": "被闪时完成击杀",
                "踩火杀": "踩火时完成击杀",
                "摸烟击杀": "出烟后不久完成击杀",
                "换枪后立刻杀": "换枪后完成击杀",
            }
            clauses.append(f"{subject}{'、'.join(labels[tag] for tag in found)}")
    last_detail = entries[kill_indexes[-1]].detail or ""
    if "爆头" in last_detail:
        clauses.append("最后一杀爆头")
    if "弹匣仅剩1发" in last_detail:
        clauses.append("最后一杀弹匣见底")
    return tuple(dict.fromkeys(clauses))


def _multikill_health_clause(
    entries: tuple[TimelineEntry, ...],
    kill_indexes: tuple[int, ...],
    snapshot: GameSnapshot,
) -> str | None:
    """Describe health timing without exposing raw health or damage numbers."""
    first_index = kill_indexes[0]
    start_health = _latest_observed_health(entries[: first_index + 1])
    end_health = _latest_observed_health(entries[: kill_indexes[-1] + 1])
    if end_health is None:
        end_health = snapshot.health
    start_tier = _health_tier(start_health)
    end_tier = _health_tier(end_health)
    if start_tier in {"丝血", "残血"}:
        return f"{start_tier}还"
    if end_tier in {"丝血", "残血"}:
        return f"连着赢下几波对枪，打成{end_tier}"
    if start_tier == "满血" and end_tier == "满血":
        return "满血"
    return None


def _latest_observed_health(entries: Iterable[TimelineEntry]) -> int | None:
    """Read the most recent explicit remaining-health observation, if any."""
    health: int | None = None
    for entry in entries:
        if entry.kind == "damage":
            match = re.search(r"掉了\d+血\s*剩(\d+)血", entry.detail or "")
            if match is not None:
                health = int(match.group(1))
        elif entry.kind == "kill":
            observed = _kill_health_value(entry.detail)
            if observed is not None:
                health = observed
    return health


def _plain_event_fallback(event: GameEvent) -> str:
    """Keep an incomplete timeline truthful rather than manufacturing detail."""
    labels = {
        "kill": "你完成击杀",
        "kill_headshot": "你爆头完成击杀",
        "multi_kill": "你完成多杀",
        "death": "你阵亡",
        "death_after_kill": "你完成击杀后不久阵亡",
        "death_thrown_away": "你在本回合早段阵亡",
        "round_win": "本回合获胜",
        "round_loss": "本回合失利",
    }
    return labels[event.type]


def _model_header(
    snapshot: GameSnapshot, event: GameEvent, round_situation: RoundSituation
) -> str:
    """Render the product-approved one-line static match context."""
    parts: list[str] = []
    if snapshot.map_name is not None:
        parts.append(snapshot.map_name)
    self_team = event.facts.get("self_team")
    if self_team in {"CT", "T"}:
        parts.append(str(self_team))
    self_score = event.facts.get("self_score")
    opponent_score = event.facts.get("opponent_score")
    if isinstance(self_score, int) and isinstance(opponent_score, int):
        parts.append(f"{self_score}:{opponent_score}")
    score_situation = event.facts.get("score_situation")
    if isinstance(score_situation, str) and score_situation:
        parts.append(score_situation)
    losses = event.facts.get("team_consecutive_round_losses")
    if isinstance(losses, int) and losses > 0:
        parts.append(f"连败{losses}")
    economic_tier = economy_tier(snapshot)
    if economic_tier is not None:
        parts.append(economic_tier)
    if snapshot.map_mode == "casual":
        if round_situation.seconds_since_bomb_planted is not None:
            is_late = round_situation.seconds_since_bomb_planted >= LATE_ROUND_WINDOW_SECONDS
        else:
            elapsed = max((entry.seconds for entry in round_situation.timeline), default=None)
            is_late = elapsed is not None and elapsed >= CASUAL_ROUND_MEDIAN_SECONDS - LATE_ROUND_WINDOW_SECONDS
        if is_late:
            parts.append("大后期")
    return " ".join(parts)


_FOCUS_LINE_PATTERN = re.compile(
    r"^\s*(?:(?P<seconds>-?\d+(?:\.\d+)?)s)?"
    r"(?P<marker>【[^】]+】)?\s*(?P<expression>.*)$"
)


def _direct_focus_section(
    focus_line: str,
    event: GameEvent,
    *,
    state_tags: tuple[str, ...],
) -> str:
    """Turn the lossless timeline expression into model-readable Chinese prose."""
    match = _FOCUS_LINE_PATTERN.fullmatch(focus_line.strip())
    if match is None:
        return f"【刚刚】\n焦点：{_EVENT_LABELS[event.type]}\n经过：{focus_line.strip()}"

    marker = match.group("marker") or ""
    marker_facts = tuple(
        fact
        for fact in marker.removeprefix("【").removesuffix("】").split("｜")
        if fact
    )
    duration = next(
        (
            fact.removeprefix("连续事件").removesuffix("秒")
            for fact in marker_facts
            if fact.startswith("连续事件")
        ),
        None,
    )
    stage = next(
        (fact for fact in marker_facts if not fact.startswith("连续事件")),
        None,
    )
    expression = match.group("expression").strip()
    expression = re.sub(r"本次焦点：[^｜）]+(?:｜)?", "", expression)
    expression = expression.replace("（）", "")
    expression = expression.replace(" > ", "，随后")
    expression = expression.replace(" + ", "；同一快照还观察到")
    expression = expression.replace("｜", "、")

    scene_tags = [label for label in sorted(SCENE_TAGS) if label in focus_line]
    scene_tags.extend(label for label in state_tags if label not in scene_tags)

    context: list[str] = []
    if stage is not None:
        context.append(f"阶段：{stage}")
    seconds = match.group("seconds")
    if seconds is not None:
        context.append(f"回合时刻：{float(seconds):.1f}秒")
    if duration is not None:
        context.append(f"连续事件跨度：{float(duration):.1f}秒")

    lines = ["【刚刚】", f"焦点：{_EVENT_LABELS[event.type]}"]
    if scene_tags:
        lines.append(f"场景标签：{'、'.join(scene_tags)}")
    if context:
        lines.append("；".join(context))
    lines.append(f"经过：{expression}")
    return "\n".join(lines)


def _grenade_summary(grenades_used: tuple[tuple[str, int], ...]) -> str | None:
    counts: Counter[str] = Counter()
    for name, count in grenades_used:
        if count > 0:
            counts[_grenade_card_label(name)] += count
    if not counts:
        return None
    ordered_labels = ("闪光弹", "烟雾弹", "手雷", "燃烧弹", "诱饵弹")
    facts = [
        f"{label}×{counts[label]}" for label in ordered_labels if counts[label] > 0
    ]
    facts.extend(
        f"{label}×{count}"
        for label, count in sorted(counts.items())
        if label not in ordered_labels
    )
    return f"【本回合投掷物】{'｜'.join(facts)}"


def _bomb_timer(seconds_since_bomb_planted: float | None) -> str | None:
    if seconds_since_bomb_planted is None:
        return None
    return f"【下包后】{_seconds(seconds_since_bomb_planted)}秒"


def _grenade_card_label(name: str) -> str:
    return {
        "weapon_flashbang": "闪光弹",
        "weapon_smokegrenade": "烟雾弹",
        "weapon_hegrenade": "手雷",
        "weapon_molotov": "燃烧弹",
        "weapon_incgrenade": "燃烧弹",
        "weapon_decoy": "诱饵弹",
    }.get(name.lower(), weapon_display_name(name))


def _section(name: str, facts: Iterable[str]) -> str | None:
    content = " ".join(fact for fact in facts if fact)
    return f"【{name}】{content}" if content else None


def _match_sections(
    snapshot: GameSnapshot,
    round_situation: RoundSituation,
    event: GameEvent,
) -> list[str]:
    sections: list[str] = []
    if snapshot.map_mode is not None:
        mode = _MODE_LABELS.get(snapshot.map_mode.lower(), snapshot.map_mode)
        sections.append(f"【游戏模式】{mode}")
    if snapshot.map_name is not None:
        sections.append(f"【地图】{snapshot.map_name}")
    round_number = human_round_number(snapshot)
    if round_number is not None:
        sections.append(f"【回合】第{round_number}回合")
    self_team = event.facts.get("self_team")
    self_score = event.facts.get("self_score")
    opponent_score = event.facts.get("opponent_score")
    score_situation = event.facts.get("score_situation")
    if (
        self_team in {"CT", "T"}
        and isinstance(self_score, int)
        and isinstance(opponent_score, int)
    ):
        situation_suffix = (
            f"（{score_situation}）"
            if isinstance(score_situation, str) and score_situation
            else ""
        )
        sections.append(
            f"【比分】我方{self_team} {self_score}:{opponent_score}{situation_suffix}"
        )
    losses = event.facts.get("team_consecutive_round_losses")
    has_loss_streak = isinstance(losses, int) and losses > 0
    if event.type == "round_win":
        show_losses, show_wins = False, True
    elif event.type == "round_loss":
        show_losses, show_wins = True, False
    else:
        show_losses, show_wins = has_loss_streak, not has_loss_streak
    if show_losses and isinstance(losses, int) and losses > 0:
        sections.append(f"【连败】{losses}轮")
    opponent_losses = _opponent_consecutive_losses(snapshot, self_team)
    if show_wins and opponent_losses is not None and opponent_losses > 0:
        sections.append(f"【连胜】{opponent_losses}轮")
    return sections


def _opponent_consecutive_losses(
    snapshot: GameSnapshot, self_team: object
) -> int | None:
    if self_team == "CT":
        return snapshot.t_consecutive_round_losses
    if self_team == "T":
        return snapshot.ct_consecutive_round_losses
    return None


def _player_facts(
    snapshot: GameSnapshot,
    game: GameState,
    round_situation: RoundSituation,
) -> list[str]:
    if game.subject_is_self is not True:
        return []
    facts: list[str] = []
    if snapshot.health is not None:
        if snapshot.health <= 0:
            facts.append("已阵亡")
            if round_situation.health_before_death is not None:
                facts.append(f"倒地前{round_situation.health_before_death}血")
        else:
            facts.extend(("存活", f"{snapshot.health}血"))
    armor = armor_status(snapshot)
    if armor is not None:
        facts.append(armor)
    if snapshot.money is not None:
        facts.append(f"余额{snapshot.money}")
    if snapshot.equip_value is not None:
        facts.append(f"装备价值{snapshot.equip_value}")
    weapon = held_weapon(snapshot)
    if weapon is not None:
        facts.append(_held_weapon_fact(weapon))
    elif snapshot.active_weapon is not None:
        facts.append(f"手持{weapon_display_name(snapshot.active_weapon)}")
    if is_carrying_bomb(snapshot) is True:
        facts.append("带包")
    if snapshot.has_defusekit is True:
        facts.append("带拆弹器")
    if is_currently_flashed(snapshot) is True:
        facts.append("正被闪")
    if is_currently_smoked(snapshot) is True:
        facts.append("正在烟中")
    return facts


def _match_statistics(snapshot: GameSnapshot, game: GameState) -> list[str]:
    if game.subject_is_self is not True:
        return []
    facts: list[str] = []
    for value, suffix in (
        (snapshot.match_kills, "杀"),
        (snapshot.match_assists, "助攻"),
        (snapshot.match_deaths, "死"),
    ):
        if value is not None and value > 0:
            facts.append(f"{value}{suffix}")
    if snapshot.match_mvps is not None and snapshot.match_mvps > 0:
        facts.append(f"MVP{snapshot.match_mvps}次")
    return facts


def _event_facts(event: GameEvent) -> list[str]:
    facts = [_EVENT_LABELS[event.type]]
    renderers = {
        "round_kill_index": lambda value: f"本回合第{value}杀",
        "weapon": lambda value: f"武器{weapon_display_name(str(value))}",
        "count": lambda value: f"本回合累计{value}杀",
        "survival_seconds": lambda value: f"存活{_seconds(value)}秒",
        "round_kills": lambda value: f"本回合{value}杀",
        "seconds_since_last_kill": lambda value: f"距上次击杀{_seconds(value)}秒",
        "equip_value": lambda value: f"装备价值{value}",
        "method": lambda value: f"获胜方式{WIN_METHOD_LABELS.get(str(value), '其他')}",
    }
    for key in _FACT_ORDER:
        value = event.facts.get(key)
        if value is not None and not (
            key in _ZERO_SIGNAL_EVENT_FACTS and value == 0
        ) and not (key == "round_kill_index" and value == 1):
            facts.append(renderers[key](value))
    return facts


def _held_weapon_fact(weapon: WeaponSlot) -> str:
    fact = f"手持{weapon_display_name(weapon.name)}"
    if weapon.ammo_clip is not None:
        clip = str(weapon.ammo_clip)
        if weapon.ammo_clip_max is not None:
            clip += f"/{weapon.ammo_clip_max}"
        fact += f" 弹匣{clip}"
    if weapon.ammo_reserve is not None:
        fact += f" 备弹{weapon.ammo_reserve}"
    return fact


def _timeline_sections(
    entries: tuple[TimelineEntry, ...],
    *,
    self_team: str | None,
    death_after_kill_max_seconds: float,
    event: GameEvent,
    snapshot: GameSnapshot,
    round_situation: RoundSituation,
) -> tuple[str | None, str | None]:
    entries = tuple(
        entry for entry in entries if entry.kind not in _CARD_HIDDEN_TIMELINE_KINDS
    )
    if not entries:
        return None, None
    observed_live = any(entry.kind == "round_live" for entry in entries)
    history_lines = [
        "【本回合历史】（仅供校验，禁止回应；秒数从正式开打算起）"
        if observed_live
        else "【本回合历史】（仅供校验，禁止回应；未观测到开打时刻，秒数从回合起点算起）"
    ]
    stages = _timeline_stages(
        entries,
        self_team=self_team,
        observed_live=observed_live,
    )
    stage_kill_totals: Counter[str] = Counter()
    for entry, stage in zip(entries, stages):
        if entry.kind == "kill" and stage is not None:
            stage_kill_totals[stage] += _timeline_kill_increase(entry)
    stage_kill_counts: Counter[str] = Counter()
    round_kills = 0
    last_kill_seconds: float | None = None
    annotations_by_index: list[tuple[str, ...]] = []
    for index, (entry, stage) in enumerate(zip(entries, stages)):
        annotations: list[str] = []
        if entry.kind == "kill":
            increase = _timeline_kill_increase(entry)
            round_kills += increase
            annotations.append(
                f"本回合第{round_kills}杀"
                if increase == 1
                else f"本回合累计{round_kills}杀"
            )
            if stage is not None:
                stage_kill_counts[stage] += increase
                stage_total = stage_kill_totals[stage]
                if stage_total >= 2 and stage_kill_counts[stage] >= stage_total:
                    annotations.append(f"该阶段{_kill_count_label(stage_total)}")
            damage_gap = _nearest_entry_gap(
                entries,
                index,
                kind="damage",
                maximum_seconds=_NEARBY_COMBAT_SECONDS,
            )
            if damage_gap is not None:
                annotations.append(f"{_scene('对枪胜利')}，间隔{_seconds(damage_gap)}秒")
            smoke_gap = _previous_completed_effect_gap(
                entries, index, kind="smoke_end"
            )
            if smoke_gap is not None and smoke_gap <= _NEARBY_COMBAT_SECONDS:
                annotations.append(f"{_scene('摸烟击杀')}，出烟后{_seconds(smoke_gap)}秒")
            annotations.extend(
                _action_scene_tags(
                    entries,
                    index,
                    event=event,
                    snapshot=snapshot,
                    round_situation=round_situation,
                )
            )
            last_kill_seconds = entry.seconds
        elif entry.kind == "death":
            if round_kills > 0:
                annotations.append(f"本回合{round_kills}杀")
            if (
                last_kill_seconds is not None
                and entry.seconds - last_kill_seconds
                <= death_after_kill_max_seconds
            ):
                annotations.append(
                    f"{_scene('击杀后被补枪')}，距击杀"
                    f"{_seconds(entry.seconds - last_kill_seconds)}秒"
                )
            annotations.extend(
                _action_scene_tags(
                    entries,
                    index,
                    event=event,
                    snapshot=snapshot,
                    round_situation=round_situation,
                )
            )
            if event.type == "death_thrown_away":
                annotations.append(_scene("白给"))
        annotations_by_index.append(tuple(annotations))

    focus_index = _focus_entry_index(entries, event)
    focus_line: str | None = None
    for indexes in _continuous_event_groups(entries):
        line = _timeline_group_line(
            entries,
            indexes,
            stages,
            annotations_by_index,
            event=event,
            focus_index=focus_index,
        )
        if focus_index is not None and focus_index in indexes:
            focus_line = line
        else:
            history_lines.append(line)
    history = "\n".join(history_lines) if len(history_lines) > 1 else None
    return history, focus_line


def _scene(label: str) -> str:
    if label not in SCENE_TAGS:
        raise AssertionError(f"scene label is not enumerated: {label}")
    return label


def _round_state_tags(
    snapshot: GameSnapshot, round_situation: RoundSituation, game: GameState
) -> tuple[str, ...]:
    if game.subject_is_self is not True:
        return ()
    tags: list[str] = []
    if (
        round_situation.longest_flash_seconds >= FLASH_BAD_LUCK_SECONDS
        or round_situation.flash_count >= FLASH_BAD_LUCK_COUNT
    ):
        tags.append(_scene("白惨了"))
    if round_situation.burn_damage_taken >= BURN_BAD_LUCK_DAMAGE:
        tags.append(_scene("烧惨了"))
    if (
        snapshot.health is not None
        and snapshot.health > 0
        and round_situation.lowest_health_while_alive is not None
        and round_situation.lowest_health_while_alive <= RESIDUAL_KILL_HEALTH
    ):
        tags.append(_scene("血皮撑住了"))
    if round_situation.awp_miss_count >= 1:
        tags.append(_scene("大狙空枪"))
    if round_situation.awp_miss_count >= 2:
        tags.append(_scene("连续空枪"))
    return tuple(tags)


def _action_scene_tags(
    entries: tuple[TimelineEntry, ...],
    index: int,
    *,
    event: GameEvent,
    snapshot: GameSnapshot,
    round_situation: RoundSituation,
) -> tuple[str, ...]:
    entry = entries[index]
    tags: list[str] = []
    if entry.kind == "kill":
        if _interval_active_at(entries, index, "flash_start", "flash_end"):
            tags.append(_scene("白着打"))
        if _interval_active_at(entries, index, "burn_start", "burn_end"):
            tags.append(_scene("踩火杀"))
        previous_weapon = _previous_primary_weapon_entry(entries, index)
        if previous_weapon is not None:
            gap = entry.seconds - previous_weapon.seconds
            if gap is not None and gap <= WEAPON_SWITCH_KILL_WINDOW_SECONDS:
                tags.append(_scene("换枪后立刻杀"))
        ammo_drop = _ammo_drop(entry.detail)
        weapon = _kill_weapon_name(entry.detail)
        if _is_sniper_weapon_name(weapon):
            tags.append(_scene("狙击击杀"))
        if (
            ammo_drop is not None
            and weapon is not None
            and weapon.removeprefix("weapon_").lower() in _AMMO_SCENE_WEAPONS
            and not _is_sniper_weapon_name(weapon)
        ):
            # Weapon lethality varies (AK one shot, M4 two, SMG three), and
            # players commonly keep spraying after a headshot. GSI therefore
            # observes more rounds than the lethal burst; these are product
            # evaluation bands, not a claim about the exact fatal bullet.
            ammo_tag = _ammo_evaluation_tag(ammo_drop, weapon)
            if ammo_tag is not None:
                tags.append(_scene(ammo_tag))
    elif entry.kind == "death":
        flash_afterglow = _previous_completed_effect_gap(
            entries, index, kind="flash_end"
        )
        if (
            _interval_active_at(entries, index, "flash_start", "flash_end")
            or (
                flash_afterglow is not None
                and 0 <= flash_afterglow <= FLASH_DEATH_AFTERGLOW_SECONDS
            )
        ):
            tags.append(_scene("白着被打死"))
        if snapshot.smoked is not None and snapshot.smoked > 0:
            tags.append(_scene("烟里死"))
        else:
            smoke_gap = _previous_completed_effect_gap(
                entries, index, kind="smoke_end"
            )
            if smoke_gap is not None and 0 <= smoke_gap <= SMOKE_DEATH_WINDOW_SECONDS:
                tags.append(_scene("出烟就没了"))
        if event.type in {"death", "death_after_kill", "death_thrown_away"}:
            held = held_weapon(snapshot)
            if held is not None and held.type == "Grenade":
                tags.append(_scene("切雷时被打死"))
            elif held is not None and held.type == "Knife":
                tags.append(_scene("切刀时被打死"))
            if (
                round_situation.awp_seen_this_round
                and event.facts.get("round_kills") == 0
            ):
                tags.append(_scene("送狙"))
            tags.extend(
                _death_combat_scene_tags(
                    entries,
                    index,
                    snapshot=snapshot,
                    round_situation=round_situation,
                )
            )
    return tuple(tags)


def _ammo_evaluation_tag(
    ammo_per_kill: float, weapon_name: str | None = None
) -> str | None:
    """Map observed shots per kill to the product's coarse gunplay bands."""
    if ammo_per_kill == 1:
        normalized = (weapon_name or "").removeprefix("weapon_").lower()
        return "颗秒" if normalized in {"ak47", "deagle", "沙鹰"} else "秒杀"
    if 2 <= ammo_per_kill <= 5:
        return "秒杀"
    if 6 <= ammo_per_kill <= 9:
        return "普通击杀"
    if 10 <= ammo_per_kill <= 14:
        return "有些吃力"
    if ammo_per_kill >= 15:
        return "马完了"
    return None


def _is_sniper_weapon_name(weapon_name: str | None) -> bool:
    normalized = (weapon_name or "").removeprefix("weapon_").lower()
    return normalized in {"awp", "ssg08", "ssg 08"}


def _kill_progress_scene_tag(
    entries: tuple[TimelineEntry, ...], index: int, map_mode: str | None
) -> str | None:
    """Prefer a five-second burst label over the round's cumulative kill count."""
    kill_indexes = [
        item for item in range(index + 1) if entries[item].kind == "kill"
    ]
    if not kill_indexes:
        return None
    total = sum(_timeline_kill_increase(entries[item]) for item in kill_indexes)
    streak = _timeline_kill_increase(entries[kill_indexes[-1]])
    following = kill_indexes[-1]
    for item in reversed(kill_indexes[:-1]):
        if entries[following].seconds - entries[item].seconds > 5.0:
            break
        streak += _timeline_kill_increase(entries[item])
        following = item
    if streak >= 2:
        return {
            2: "连续双杀", 3: "连续三杀", 4: "连续四杀"
        }.get(streak, "连续五杀")
    if total < 2:
        return None
    if total >= 5:
        if map_mode != "casual":
            logger.warning("observed %s round kills outside casual mode", total)
        return "多杀5+"
    return {2: "多杀2", 3: "多杀3", 4: "多杀4"}[total]


def _death_combat_scene_tags(
    entries: tuple[TimelineEntry, ...],
    index: int,
    *,
    snapshot: GameSnapshot,
    round_situation: RoundSituation,
) -> tuple[str, ...]:
    """Derive only observable, death-specific combat outcomes."""
    death_seconds = entries[index].seconds
    window_start = death_seconds - DEATH_COMBAT_WINDOW_SECONDS
    fired_in_window = any(
        window_start <= fired_at <= death_seconds
        for fired_at in round_situation.fire_seconds
    )
    damaged_in_window = any(
        entry.kind == "damage" and window_start <= entry.seconds <= death_seconds
        for entry in entries[: index + 1]
    )
    tags: list[str] = []
    lost_duel = fired_in_window and damaged_in_window
    if lost_duel:
        tags.append(_scene("对枪输了"))
        if (
            round_situation.last_firing_ammo_drop is not None
            and round_situation.last_firing_ammo_drop >= MISFIRE_DEATH_MIN_AMMO
            and round_situation.last_firing_ammo_at_seconds is not None
            and window_start
            <= round_situation.last_firing_ammo_at_seconds
            <= death_seconds
        ):
            tags.append(_scene("马枪死"))
    elif (
        not fired_in_window
        and round_situation.last_readable_held_ammo_at_seconds is not None
        and round_situation.last_readable_held_ammo_at_seconds >= window_start
    ):
        tags.append(_scene("一枪没开就没了"))

    weapon = held_weapon(snapshot)
    if (
        weapon is not None
        and weapon.ammo_clip == 0
        and weapon.name in round_situation.weapons_fired_this_round
    ):
        tags.append(_scene("打空了还是没打过"))
    return tuple(tags)


def _previous_primary_weapon_entry(
    entries: tuple[TimelineEntry, ...], index: int
) -> TimelineEntry | None:
    return next(
        (
            entry
            for entry in reversed(entries[:index])
            if entry.kind == "primary_weapon"
            and (entry.detail or "").startswith("换枪 ")
        ),
        None,
    )


def _kill_health_value(detail: str | None) -> int | None:
    match = re.search(r"击杀时剩(\d+)血", detail or "")
    return int(match.group(1)) if match is not None else None


def _ammo_drop(detail: str | None) -> int | None:
    match = re.search(r"用弹(\d+)", detail or "")
    return int(match.group(1)) if match is not None else None


def _kill_weapon_name(detail: str | None) -> str | None:
    if not detail:
        return None
    rendered = re.search(r"(?:使用|用)([^，；\s]+)完成击杀", detail)
    if rendered is not None:
        return rendered.group(1)
    first = detail.split(" ", 1)[0]
    return None if first == "玩家" or first.startswith("掉了") else first


def _interval_active_at(
    entries: tuple[TimelineEntry, ...], index: int, start_kind: str, end_kind: str
) -> bool:
    current = entries[index].seconds
    active = False
    for entry in entries[: index + 1]:
        if entry.seconds > current:
            break
        if entry.kind == start_kind:
            active = True
        elif entry.kind == end_kind:
            # An interval closed by death/round end is represented as an
            # unfinished end marker; the focus event still happened inside it.
            active = (entry.detail or "").startswith("未结束 ")
    return active


def _kill_weapon(detail: str | None) -> str | None:
    if not detail:
        return None
    marker = re.search(
        r"(?:^| )(?=(?:爆头|\d+个爆头|增加\d+杀|用弹\d+|击杀时满血|"
        r"击杀时剩\d+血|弹匣仅剩\d+发))",
        detail,
    )
    weapon = detail[: marker.start()].strip() if marker is not None else detail.strip()
    return _timeline_weapon_display(weapon) if weapon else None


def _focus_entry_index(
    entries: tuple[TimelineEntry, ...], event: GameEvent
) -> int | None:
    expected_kind = {
        "kill": "kill",
        "kill_headshot": "kill",
        "multi_kill": "kill",
        "death": "death",
        "death_after_kill": "death",
        "death_thrown_away": "death",
        "round_win": "round_result",
        "round_loss": "round_result",
    }[event.type]
    return next(
        (
            index
            for index in range(len(entries) - 1, -1, -1)
            if entries[index].kind == expected_kind
        ),
        None,
    )


def _continuous_event_groups(
    entries: tuple[TimelineEntry, ...],
) -> tuple[tuple[int, ...], ...]:
    groups: list[tuple[int, ...]] = []
    index = 0
    while index < len(entries):
        if entries[index].kind not in _CONTINUOUS_EVENT_KINDS:
            groups.append((index,))
            index += 1
            continue
        end = index + 1
        while end < len(entries):
            current = entries[end]
            if current.kind not in _CONTINUOUS_EVENT_KINDS:
                break
            if current.seconds - entries[index].seconds > _CONTINUOUS_EVENT_MAX_SECONDS:
                break
            end += 1
        candidate = tuple(range(index, end))
        if len(candidate) >= 2 and any(
            entries[item].kind in _CONTINUOUS_EVENT_ANCHORS for item in candidate
        ):
            groups.append(candidate)
            index = end
        else:
            # Keep advancing one entry at a time so an early unanchored change
            # cannot prevent a later <=3-second window from reaching its anchor.
            groups.append((index,))
            index += 1
    return tuple(groups)


def _timeline_group_line(
    entries: tuple[TimelineEntry, ...],
    indexes: tuple[int, ...],
    stages: tuple[str | None, ...],
    annotations_by_index: list[tuple[str, ...]],
    *,
    event: GameEvent,
    focus_index: int | None,
) -> str:
    indexes = _deduplicated_group_indexes(
        entries,
        indexes,
        event=event,
        focus_index=focus_index,
    )
    last = entries[indexes[-1]]
    focused = focus_index is not None and focus_index in indexes
    marker_facts: list[str] = []
    if len(indexes) >= 2:
        duration = max(0.0, last.seconds - entries[indexes[0]].seconds)
        if duration >= 0.05:
            marker_facts.append(f"连续事件{_seconds(duration)}秒")
    stage_index = (
        focus_index
        if focused
        and focus_index is not None
        and stages[focus_index] is not None
        else next(
            (
                index
                for index in reversed(indexes)
                if entries[index].kind in _CONTINUOUS_EVENT_ANCHORS
                and stages[index] is not None
            ),
            None,
        )
    )
    if stage_index is not None and stages[stage_index] is not None:
        marker_facts.append(stages[stage_index])
    marker = f"【{'｜'.join(marker_facts)}】" if marker_facts else ""
    suppress_kill_health = any(entries[index].kind == "damage" for index in indexes)
    fragments = [
        _timeline_fragment(
            entries[index],
            _focused_annotations(
                annotations_by_index[index],
                event=event if index == focus_index else None,
                entry=entries[index],
            ),
            event=event if index == focus_index else None,
            suppress_kill_health=suppress_kill_health,
        )
        for index in indexes
    ]
    if len(fragments) >= 2:
        fragments = [
            fragments[0],
            *(fragment.removeprefix("玩家") for fragment in fragments[1:]),
        ]
    expression = fragments[0]
    for previous_index, current_index, fragment in zip(
        indexes,
        indexes[1:],
        fragments[1:],
    ):
        separator = (
            " + "
            if abs(entries[current_index].seconds - entries[previous_index].seconds)
            < 0.05
            else " > "
        )
        expression += separator + fragment
    return f"  {_seconds(last.seconds)}s{marker} {expression}"


def _deduplicated_group_indexes(
    entries: tuple[TimelineEntry, ...],
    indexes: tuple[int, ...],
    *,
    event: GameEvent,
    focus_index: int | None,
) -> tuple[int, ...]:
    if focus_index is None or focus_index not in indexes:
        return indexes
    method_label = WIN_METHOD_LABELS.get(str(event.facts.get("method")))
    if event.type in {"round_win", "round_loss"}:
        allowed_kinds = {"mvp", "round_result"}
        if method_label is None:
            allowed_kinds.add("bomb")
        focused_result = tuple(
            index
            for index in indexes
            if entries[index].kind in allowed_kinds
        )
        return focused_result or (focus_index,)
    if event.type in {"death", "death_after_kill", "death_thrown_away"}:
        without_ambiguous_grenade = tuple(
            index for index in indexes if entries[index].kind != "grenade_used"
        )
        if without_ambiguous_grenade:
            indexes = without_ambiguous_grenade
    duplicate_bomb_detail = {
        "炸弹拆除": "已拆除",
        "炸弹引爆": "已爆炸",
    }.get(method_label)
    if duplicate_bomb_detail is None:
        return indexes
    filtered = tuple(
        index
        for index in indexes
        if not (
            entries[index].kind == "bomb"
            and entries[index].detail == duplicate_bomb_detail
        )
    )
    return filtered or indexes


def _timeline_fragment(
    entry: TimelineEntry,
    annotations: tuple[str, ...],
    *,
    event: GameEvent | None,
    suppress_kill_health: bool,
) -> str:
    text = _timeline_entry_text(
        entry,
        event=event,
        suppress_kill_health=suppress_kill_health,
    )
    return text + (f"（{'｜'.join(annotations)}）" if annotations else "")


def _focused_annotations(
    annotations: tuple[str, ...],
    *,
    event: GameEvent | None,
    entry: TimelineEntry,
) -> tuple[str, ...]:
    if event is None:
        return annotations
    # 【刚刚】 identifies the containing line. A <=3-second line may carry
    # several independently classified actions, so mark the exact action that
    # policy selected as well. This is factual routing metadata, not emphasis.
    focus_facts = [f"本次焦点：{_EVENT_LABELS[event.type]}"]
    if entry.kind == "round_result":
        method = event.facts.get("method")
        method_label = WIN_METHOD_LABELS.get(str(method)) if method else None
        if method_label is not None:
            focus_facts.append(f"结算：{method_label}")
    return (*focus_facts, *annotations)


def _timeline_stages(
    entries: tuple[TimelineEntry, ...],
    *,
    self_team: str | None,
    observed_live: bool,
) -> tuple[str | None, ...]:
    stages: list[str | None] = []
    bomb_planted = False
    for entry in entries:
        stage = (
            round_stage_label(
                entry.seconds,
                bomb_planted=bomb_planted,
                self_team=self_team,
                observed_live=observed_live,
            )
            if entry.kind in _STAGE_ANNOTATED_TIMELINE_KINDS
            else None
        )
        if (
            stage is None
            and entry.kind in _STAGE_ANNOTATED_TIMELINE_KINDS
            and not observed_live
        ):
            stage = "阶段不可判断"
        stages.append(stage)
        if entry.kind == "bomb" and entry.detail == "已安放":
            bomb_planted = True
    return tuple(stages)


def _kill_count_label(count: int) -> str:
    return {2: "双杀", 3: "三杀", 4: "四杀", 5: "五杀"}.get(count, f"{count}杀")


def _timeline_kill_increase(entry: TimelineEntry) -> int:
    match = _KILL_INCREASE_PATTERN.search(entry.detail or "")
    return int(match.group(1)) if match is not None else 1


def _nearest_entry_gap(
    entries: tuple[TimelineEntry, ...],
    index: int,
    *,
    kind: str,
    maximum_seconds: float,
) -> float | None:
    gaps = (
        abs(entry.seconds - entries[index].seconds)
        for candidate_index, entry in enumerate(entries)
        if candidate_index != index and entry.kind == kind
    )
    nearby = tuple(gap for gap in gaps if gap <= maximum_seconds)
    return min(nearby) if nearby else None


def _previous_completed_effect_gap(
    entries: tuple[TimelineEntry, ...], index: int, *, kind: str
) -> float | None:
    """Return a gap only from an observed effect end, never a missing-data marker."""
    current_seconds = entries[index].seconds
    previous = next(
        (
            entry
            for entry in reversed(entries[:index])
            if entry.kind == kind
            and entry.seconds <= current_seconds
            and not (entry.detail or "").startswith("观测中断 ")
            and not (entry.detail or "").startswith("未结束 ")
        ),
        None,
    )
    return current_seconds - previous.seconds if previous is not None else None


def _timeline_entry_text(
    entry: TimelineEntry,
    *,
    event: GameEvent | None = None,
    suppress_kill_health: bool = False,
) -> str:
    detail = entry.detail
    if entry.kind == "bought":
        return "玩家购买装备（仅检测到金额与价值变化）"
    if entry.kind == "round_live":
        return "正式开打"
    if entry.kind == "flash_start":
        return "玩家被闪"
    if entry.kind == "flash_end":
        if detail is not None and detail.startswith("观测中断"):
            return "玩家闪光状态" + detail
        if detail is not None and detail.startswith("未结束 "):
            return "玩家受闪光影响" + detail
        return "玩家闪光影响结束" + (f" {detail}" if detail else "")
    if entry.kind == "smoke_start":
        return "玩家进烟"
    if entry.kind == "smoke_end":
        if detail is not None and detail.startswith("观测中断"):
            return "玩家烟雾状态" + detail
        if detail is not None and detail.startswith("未结束 "):
            return "玩家仍在烟中 " + detail.removeprefix("未结束 ")
        return "玩家出烟" + (f" {detail}" if detail else "")
    if entry.kind == "burn_start":
        return "玩家开始燃烧"
    if entry.kind == "burn_end":
        if detail is not None and detail.startswith("观测中断"):
            return "玩家燃烧状态" + detail
        if detail is not None and detail.startswith("未结束 "):
            return "玩家仍在燃烧 " + detail.removeprefix("未结束 ")
        return "玩家燃烧结束" + (f" {detail}" if detail else "")
    if entry.kind == "kill":
        return _timeline_kill_text(
            detail,
            suppress_health=suppress_kill_health,
        )
    if entry.kind == "damage":
        return "玩家" + (detail or "受到伤害")
    if entry.kind == "primary_weapon":
        if detail is not None and detail.startswith("换枪 "):
            return "玩家" + detail
        return "玩家主武器" + (f" {detail}" if detail else "")
    if entry.kind in {"ammo_low", "grenade_used", "grenade_pickup"}:
        return "玩家" + (detail or "状态变化")
    if entry.kind == "awp_miss":
        return "玩家" + (detail or _scene("大狙空枪"))
    if entry.kind == "bomb":
        return "炸弹" + (detail or "状态变化")
    if entry.kind == "bomb_pickup":
        return "玩家拿到包"
    if entry.kind == "bomb_drop":
        return "玩家丢了包"
    if entry.kind == "assist":
        return "玩家助攻"
    if entry.kind == "mvp":
        previous_round = detail is not None and detail.startswith("上回合 ")
        counter = detail.removeprefix("上回合 ") if detail else None
        prefix = "玩家上回合获得MVP" if previous_round else "玩家获得MVP"
        return prefix + (f"（{counter}）" if counter else "")
    if entry.kind == "death":
        return "玩家阵亡"
    if entry.kind == "round_result":
        if event is not None and event.type in {"round_win", "round_loss"}:
            result = _EVENT_LABELS[event.type]
            method = event.facts.get("method")
            method_label = WIN_METHOD_LABELS.get(str(method)) if method else None
            return method_label or result
        return f"{detail}方获胜" if detail else "回合结算"
    raise AssertionError(f"unhandled timeline kind: {entry.kind}")


def _timeline_kill_text(
    detail: str | None,
    *,
    suppress_health: bool = False,
) -> str:
    if not detail:
        return "玩家完成击杀"
    if suppress_health:
        detail = re.sub(
            r"(?:^| )击杀时(?:满血|剩\d+血)(?= |$)",
            "",
            detail,
        ).strip()
    marker = re.search(
        r"(?:^| )(?=(?:爆头|\d+个爆头|增加\d+杀|用弹\d+|击杀时满血|"
        r"击杀时剩\d+血|弹匣仅剩\d+发))",
        detail,
    )
    if marker is None:
        return f"玩家使用{detail}完成击杀"
    weapon = detail[: marker.start()].strip()
    attributes = detail[marker.start() :].strip()
    action = (
        f"玩家使用{_timeline_weapon_display(weapon)}完成击杀"
        if weapon
        else "玩家完成击杀"
    )
    return action + (f" {attributes}" if attributes else "")


def _timeline_weapon_display(weapon: str) -> str:
    """Normalize the few lowercase timeline names without altering display names."""
    return {"mp7": "MP7"}.get(weapon.lower(), weapon)


def _seconds(value: Any) -> str:
    return f"{float(value):.1f}"
