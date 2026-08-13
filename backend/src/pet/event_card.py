"""Render existing CS2 facts into the GSI event card shown to a model."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
import re
from typing import Any

from pet.commentary_rules import WIN_METHOD_LABELS
from pet.events import EventType, GameEvent
from pet.gsi import GameSnapshot, WeaponSlot, human_round_number
from pet.session import GameState
from pet.situation import (
    RoundSituation,
    BURN_BAD_LUCK_DAMAGE,
    DEATH_COMBAT_WINDOW_SECONDS,
    FLASH_BAD_LUCK_COUNT,
    FLASH_BAD_LUCK_SECONDS,
    RESIDUAL_KILL_HEALTH,
    SCENE_TAGS,
    SMOKE_DEATH_WINDOW_SECONDS,
    WEAPON_SWITCH_KILL_WINDOW_SECONDS,
    TimelineEntry,
    armor_status,
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
_REQUIRED_FACT_CHAR_BUDGET = 30
_DROPPED_REQUIRED_PREFIX = "因字数丢弃："
_HAN_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
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

    header = _model_header(snapshot, event)
    state_tags = _round_state_tags(snapshot, round_situation, game)
    direct_focus = _direct_focus_section(focus_line, event, state_tags=state_tags)
    return "\n".join(part for part in (header, direct_focus) if part)


def _model_header(snapshot: GameSnapshot, event: GameEvent) -> str:
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
        same_group = _same_continuous_group_has_kind(entries, index, "damage")
        health = _kill_health_value(entry.detail)
        if health is not None and health <= RESIDUAL_KILL_HEALTH and not same_group:
            tags.append(_scene("残血击杀"))
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
        if (
            ammo_drop is not None
            and weapon is not None
            and weapon.removeprefix("weapon_").lower() in _AMMO_SCENE_WEAPONS
        ):
            if ammo_drop == 1:
                tags.append(_scene("一发命中"))
            elif 2 <= ammo_drop <= 5:
                tags.append(_scene("干净解决"))
            elif 6 <= ammo_drop <= 9:
                tags.append(_scene("打了半天"))
            elif ammo_drop >= 10:
                tags.append(_scene("一梭子扫死"))
    elif entry.kind == "death":
        if _interval_active_at(entries, index, "flash_start", "flash_end"):
            tags.append(_scene("白着被打死"))
        smoke_gap = _previous_completed_effect_gap(
            entries, index, kind="smoke_end"
        )
        if smoke_gap is not None and 0 <= smoke_gap <= SMOKE_DEATH_WINDOW_SECONDS:
            tags.append(_scene("出烟就没了"))
        if event.type in {"death", "death_after_kill", "death_thrown_away"}:
            tags.extend(
                _death_combat_scene_tags(
                    entries,
                    index,
                    snapshot=snapshot,
                    round_situation=round_situation,
                )
            )
    return tuple(tags)


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
    if fired_in_window and damaged_in_window:
        tags.append(_scene("对枪输了"))
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
    return detail.split(" ", 1)[0]


def _same_continuous_group_has_kind(
    entries: tuple[TimelineEntry, ...], index: int, kind: str
) -> bool:
    for group in _continuous_event_groups(entries):
        if index in group:
            return any(entries[item].kind == kind for item in group)
    return False


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


def _required_event_facts(
    round_situation: RoundSituation,
    event: GameEvent,
    *,
    self_team: str | None,
    death_after_kill_max_seconds: float,
) -> list[str]:
    """List mandatory existing atoms without composing commentary prose."""
    entries = round_situation.timeline
    if not entries:
        return _event_facts(event)
    observed_live = any(entry.kind == "round_live" for entry in entries)
    stages = _timeline_stages(entries, self_team=self_team, observed_live=observed_live)
    kill_facts: list[str] = []
    kill_seconds: list[float] = []
    round_kills = 0
    for index, (entry, stage) in enumerate(zip(entries, stages)):
        if entry.kind != "kill":
            continue
        round_kills += _timeline_kill_increase(entry)
        kill_seconds.append(entry.seconds)
        parts = [f"第{round_kills}杀", _required_stage(stage, observed_live)]
        health = _kill_health_fact(entries, index)
        if health is not None:
            parts.append(health)
        weapon = _kill_weapon(entry.detail)
        if weapon is not None:
            parts.append(weapon_display_name(weapon))
        parts.append("爆头击杀" if "爆头" in (entry.detail or "") else "普通击杀")
        if _nearest_entry_gap(
            entries,
            index,
            kind="damage",
            maximum_seconds=_NEARBY_COMBAT_SECONDS,
        ) is not None:
            parts.append("赢下对枪")
        smoke_gap = _previous_entry_gap(entries, index, kind="smoke_end")
        if smoke_gap is not None and smoke_gap <= _NEARBY_COMBAT_SECONDS:
            parts.append("摸烟击杀")
        kill_facts.append("、".join(parts))

    death_index = next(
        (i for i in range(len(entries) - 1, -1, -1) if entries[i].kind == "death"),
        None,
    )
    death_fact: str | None = None
    death_after_kill = False
    if death_index is not None:
        death = entries[death_index]
        death_after_kill = bool(
            kill_seconds
            and death.seconds - kill_seconds[-1] <= death_after_kill_max_seconds
        )
        death_label = "被补枪" if death_after_kill else "普通死亡"
        if event.type == "death_thrown_away":
            death_label = "白给"
        death_fact = (
            f"{_required_stage(stages[death_index], observed_live)}、{death_label}"
        )

    rare_facts = _focused_rare_facts(
        round_situation,
        event,
        stages=stages,
        observed_live=observed_live,
        self_team=self_team,
        kill_facts=kill_facts,
    )

    if event.type in {"round_win", "round_loss"}:
        result = _EVENT_LABELS[event.type]
        method = event.facts.get("method")
        method_label = WIN_METHOD_LABELS.get(str(method)) if method else None
        result_fact = f"{method_label}，{result}" if method_label else result
        bomb_result = method_label in {"炸弹拆除", "炸弹引爆"} or any(
            "炸弹已拆除" in fact or "下包" in fact for fact in rare_facts
        )
        if bomb_result and round_kills == 0:
            return [_compact_bomb_result_fact(rare_facts, result_fact)]
        if not observed_live and round_kills == 0:
            return [*rare_facts, f"{result_fact}；未观测开打时刻"]
        if round_kills == 0:
            required = [result_fact]
            if death_fact is not None:
                required.append(death_fact)
            return [_bundle_required(*rare_facts, *required)]
        special_multikill = _compact_special_multikill_fact(
            rare_facts,
            kill_facts,
            round_kills=round_kills,
            result_fact=result_fact,
            death_fact=death_fact,
        )
        if special_multikill is not None:
            return [special_multikill]
        # A type-1 line comments on the player, not on the scoreboard.  Keep
        # the settlement first for round events, then preserve the player's
        # own contribution before optional bomb context.
        required = [result_fact]
        if round_kills >= 2:
            required.append(_compact_player_kill_summary(kill_facts, round_kills))
            if death_fact is not None:
                required.append(death_fact)
            required.append("有显著贡献")
        else:
            required.append(_compact_player_kill_summary(kill_facts, round_kills))
            required.append(death_fact or "存活到结算")
        return _prioritize_required_facts(required, rare_facts)

    if event.type in {"kill", "kill_headshot"}:
        grenade_focus = next(
            (fact for fact in rare_facts if fact.startswith("投掷物：")), None
        )
        if grenade_focus is not None:
            weapon = _weapon_from_required_kill_fact(kill_facts[-1]) if kill_facts else None
            kill = f"{weapon}完成击杀" if weapon is not None else "完成击杀"
            return [_bundle_required(grenade_focus, kill)]
        return _prioritize_required_facts(
            [*(kill_facts[-1:] or _event_facts(event))], rare_facts
        )
    if event.type == "multi_kill":
        required = [_compact_multikill_fact(kill_facts, max(round_kills, 2))]
        if death_fact is not None:
            required.append(death_fact)
        return _prioritize_required_facts(required, rare_facts)
    if round_kills == 0:
        stage = (
            _required_stage(stages[death_index], observed_live)
            if death_index is not None
            else "未观测开打时刻"
        )
        if rare_facts:
            return _prioritize_required_facts(
                [f"{stage}、{_EVENT_LABELS[event.type]}"], rare_facts
            )
        return [f"{stage}、{_EVENT_LABELS[event.type]}"]
    required: list[str] = []
    required.append(
        _compact_player_kill_summary(
            kill_facts, round_kills, include_stage=False, include_health=False
        )
    )
    if death_fact is not None:
        required.append(death_fact)
    return _prioritize_required_facts(required, rare_facts)


def _compact_multikill_fact(kill_facts: list[str], kill_count: int) -> str:
    """Bundle a deterministic kill sequence without prescribing final wording."""
    sequence = "；".join(kill_facts)
    return f"击杀经过={sequence}；本回合累计{_kill_count_label(kill_count)}"


def _compact_player_kill_summary(
    kill_facts: list[str],
    kill_count: int,
    *,
    include_stage: bool = True,
    include_health: bool = True,
) -> str:
    """Keep the player-facing kill facts without replaying every timeline token."""
    if not kill_facts:
        return f"本回合{_kill_count_label(kill_count)}"
    first = kill_facts[0].split("、")
    stage = next(
        (
            atom
            for atom in first
            if atom
            in {
                "开局",
                "前期",
                "中期",
                "后期",
                "守包",
                "反攻包点",
                "未观测开打时刻",
            }
        ),
        "",
    )
    health = next(
        (atom for atom in first if atom == "满血" or atom.startswith("剩")),
        "",
    )
    weapon = _weapon_from_required_kill_fact(kill_facts[0]) or ""
    headshot = "爆头" if "爆头击杀" in kill_facts[0] else ""
    relation = "赢下对枪" if "赢下对枪" in kill_facts[0] else ""
    smoke = "摸烟" if "摸烟击杀" in kill_facts[0] else ""
    count = "一杀" if kill_count == 1 else _kill_count_label(kill_count)
    return "".join(
        (
            stage if include_stage else "",
            health if include_health else "",
            smoke,
            weapon,
            headshot,
            count,
            relation,
        )
    )


def _bundle_required(*facts: str) -> str:
    """Join mandatory atoms into one coverage unit while leaving wording free."""
    return "；".join(dict.fromkeys(fact for fact in facts if fact))


def _prioritize_required_facts(
    primary_facts: list[str], secondary_facts: list[str]
) -> list[str]:
    """Return player-first mandatory atoms in deterministic retention order."""
    selected = list(dict.fromkeys(fact for fact in primary_facts if fact))
    dropped: list[str] = []
    for fact in dict.fromkeys(fact for fact in secondary_facts if fact):
        projected = "；".join((*selected, fact))
        if _chinese_character_count(projected) <= _REQUIRED_FACT_CHAR_BUDGET:
            selected.append(fact)
        else:
            dropped.append(fact)
    if dropped:
        selected.append(_DROPPED_REQUIRED_PREFIX + "；".join(dropped))
    return selected


def _chinese_character_count(text: str) -> int:
    """Measure the same Chinese-character budget used by the event utterance."""
    return len(_HAN_CHARACTER.findall(text))


def _compact_bomb_result_fact(rare_facts: list[str], result_fact: str) -> str:
    """Compress the planted-to-result chain without claiming an executor."""
    team = next((fact for fact in rare_facts if fact.startswith("我方")), None)
    elapsed = next((fact for fact in rare_facts if "下包" in fact), None)
    planted = any("炸弹已安放" in fact for fact in rare_facts)
    parts: list[str] = []
    if team is not None:
        parts.append(team)
    if elapsed is not None:
        parts.append(f"炸弹已安放；{elapsed}" if planted else elapsed)
    elif planted:
        action = "拆除" if "炸弹拆除" in result_fact else "引爆"
        parts.append(f"炸弹已安放后{action}")
    parts.append(result_fact)
    return _bundle_required(*parts)


def _compact_special_multikill_fact(
    rare_facts: list[str],
    kill_facts: list[str],
    *,
    round_kills: int,
    result_fact: str,
    death_fact: str | None,
) -> str | None:
    """Compress rare multi-kill relations that otherwise lose to generic history."""
    if round_kills < 2:
        return None
    special = next(
        (
            fact
            for fact in rare_facts
            if any(
                term in fact
                for term in (
                    "弹匣仅剩",
                    "弹匣打空",
                    "连续事件",
                    "最后一次击杀为爆头",
                )
            )
        ),
        None,
    )
    if special is None:
        return None
    weapons = list(
        dict.fromkeys(
            weapon
            for fact in kill_facts
            if (weapon := _weapon_from_required_kill_fact(fact)) is not None
        )
    )
    weapon = "→".join(weapons)
    count = _kill_count_label(round_kills)
    if "弹匣仅剩" in special:
        relation = f"{weapon}最后1发完成第{round_kills}杀，本回合{count}"
    elif "弹匣打空" in special:
        relation = f"{weapon}{count}后打空弹匣并阵亡"
    elif "最后一次击杀为爆头" in special:
        relation = f"{weapon}{count}，最后一杀爆头"
    else:
        span = re.search(r"连续事件([0-9.]+)秒", special)
        duration = f"{span.group(1)}秒内" if span is not None else "连续"
        relation = f"{weapon}{duration}被闪{count}"
    parts = [result_fact, relation]
    if death_fact is not None and "阵亡" not in relation:
        parts.append(death_fact)
    parts.append("有显著贡献")
    return _bundle_required(*parts)


def _weapon_from_required_kill_fact(fact: str) -> str | None:
    """Read the already-rendered weapon atom from one deterministic kill fact."""
    ignored = {
        "普通击杀",
        "爆头击杀",
        "赢下对枪",
        "摸烟击杀",
        "满血",
    }
    for atom in fact.split("、"):
        if (
            atom in ignored
            or atom.startswith("第")
            or atom.startswith("剩")
            or atom in {"开局", "前期", "中期", "后期", "守包", "反攻包点", "未观测开打时刻"}
        ):
            continue
        return atom.upper() if atom.lower() == "m4a1-s" else atom
    return None


def _focused_rare_facts(
    round_situation: RoundSituation,
    event: GameEvent,
    *,
    stages: list[str | None],
    observed_live: bool,
    self_team: str | None,
    kill_facts: list[str],
) -> list[str]:
    """Keep one coherent rare-event chain instead of many overlapping atoms."""
    facts = _rare_required_facts(
        round_situation,
        event,
        stages=stages,
        observed_live=observed_live,
        self_team=self_team,
    )

    def matching(*terms: str) -> list[str]:
        return [fact for fact in facts if any(term in fact for term in terms)]

    method = str(event.facts.get("method") or "")
    timeline_bomb_result = matching("下包", "炸弹已拆除", "炸弹引爆")
    if method in {"ct_win_defuse", "t_win_bomb", "defuse", "bomb"} or any(
        "炸弹已拆除" in fact or "下包" in fact for fact in timeline_bomb_result
    ):
        bomb = matching("下包", "炸弹已安放", "炸弹已拆除", "我方")
        return list(dict.fromkeys(bomb))

    package = matching("拿到包", "丢了包")
    if package:
        ammo_before_kill = matching("弹匣仅剩") if event.type in {"kill", "kill_headshot"} else []
        return list(dict.fromkeys([*package, *ammo_before_kill]))

    for priority_term in ("MVP", "助攻"):
        selected = matching(priority_term)
        if selected:
            return selected[:1]

    grenade_pickups = matching("捡到")
    grenade_summary = matching("投掷物：")
    if event.type in {"kill", "kill_headshot"} and grenade_summary:
        return grenade_summary[:1]
    if grenade_pickups and event.type in {"round_win", "round_loss"}:
        return grenade_pickups[:1]

    interrupted_flash = matching("被闪状态未结束")
    if interrupted_flash:
        return interrupted_flash[:1]
    flash_chain = matching("连续事件")
    if flash_chain:
        return flash_chain[:1]
    double_flash = matching("玩家被闪两次")
    if double_flash:
        return double_flash[:1]
    flash = matching("被闪期间")
    smoke = matching("烟雾中", "出烟后")
    if flash:
        return list(dict.fromkeys([flash[-1], *smoke[:1]]))

    burning = matching("燃烧期间")
    if burning:
        return burning[:1]
    residual = matching("残血")
    if residual:
        return residual[:1]
    smoke = matching("烟雾中", "出烟后")
    if event.type in {"death", "death_after_kill", "death_thrown_away"} and smoke:
        return smoke[:1]
    planted = matching("炸弹已安放")
    if (
        planted
        and event.type in {"death", "death_after_kill", "death_thrown_away"}
        and not kill_facts
    ):
        return planted[:1]
    ammo_before_kill = matching("弹匣仅剩")
    if ammo_before_kill:
        return ammo_before_kill[:1]
    switch = matching("换枪")
    if switch:
        return switch[:1]
    if kill_facts and "爆头击杀" in kill_facts[-1]:
        return ["最后一次击杀为爆头"]
    ammo_after_kills = matching("完成", "弹匣打空")
    ammo_after_kills = [fact for fact in ammo_after_kills if "弹匣打空" in fact]
    if ammo_after_kills:
        return ammo_after_kills[:1]
    if smoke:
        return smoke[:1]
    planted = matching("炸弹已安放")
    if planted:
        return planted[:1]
    return []


def _rare_required_facts(
    round_situation: RoundSituation,
    event: GameEvent,
    *,
    stages: list[str | None],
    observed_live: bool,
    self_team: str | None,
) -> list[str]:
    """Extract uncommon, card-backed relations that generic event facts omit."""
    entries = round_situation.timeline
    focus_index = _focus_entry_index(entries, event)
    relevant_indexes: list[int] = []
    if event.type in {
        "round_win",
        "round_loss",
        "multi_kill",
        "death",
        "death_after_kill",
        "death_thrown_away",
    }:
        relevant_indexes.extend(
            index for index, entry in enumerate(entries) if entry.kind == "kill"
        )
    if focus_index is not None and focus_index not in relevant_indexes:
        relevant_indexes.append(focus_index)

    facts: list[str] = []
    for index in relevant_indexes:
        entry = entries[index]
        stage = _required_stage(stages[index], observed_live)
        if entry.kind == "kill":
            increase = _timeline_kill_increase(entry)
            kill_label = _kill_count_label(increase) if increase >= 2 else "击杀"
            health_fact = _kill_health_fact(entries, index)
            if health_fact is not None:
                health_match = re.fullmatch(r"剩(\d+)血", health_fact)
                if health_match is not None and int(health_match.group(1)) < 50:
                    facts.append(f"残血（{health_fact}）完成{kill_label}")
            if _effect_active_at(entries, index, "flash_start", "flash_end"):
                facts.append(f"{stage}、被闪期间完成{kill_label}")
            smoke_started = _effect_started_at(
                entries, index, "smoke_start", "smoke_end"
            )
            if smoke_started is not None:
                duration = max(0.0, entry.seconds - smoke_started)
                facts.append(
                    f"{stage}、烟雾中（进烟{_seconds(duration)}秒后）完成{kill_label}"
                )
            if _effect_active_at(entries, index, "burn_start", "burn_end"):
                facts.append(f"{stage}、燃烧期间完成{kill_label}")
            ammo = _previous_detail(entries, index, kind="ammo_low")
            if ammo is not None:
                facts.append(f"{ammo}时完成{kill_label}")
        elif entry.kind == "death":
            if _has_unclosed_effect(entries, index, "flash_end"):
                facts.append("被闪状态未结束便阵亡")
            elif _effect_active_at(entries, index, "flash_start", "flash_end"):
                facts.append("被闪期间阵亡")
            smoke_gap = _previous_entry_gap(entries, index, kind="smoke_end")
            if smoke_gap is not None and smoke_gap <= _CONTINUOUS_EVENT_MAX_SECONDS:
                facts.append(f"出烟后{_seconds(smoke_gap)}秒阵亡")
            kills_before_death = sum(
                _timeline_kill_increase(item)
                for item in entries[:index]
                if item.kind == "kill"
            )
            ammo = _previous_detail(entries, index, kind="ammo_low")
            if ammo is not None and kills_before_death == 0:
                facts.append(f"{ammo}后阵亡")

    active_flash_kills = sum(
        _timeline_kill_increase(entry)
        for index, entry in enumerate(entries)
        if entry.kind == "kill"
        and _effect_active_at(entries, index, "flash_start", "flash_end")
    )
    if active_flash_kills >= 2:
        facts.append(f"被闪期间完成{_kill_count_label(active_flash_kills)}")
        flash_kill_seconds = [
            entry.seconds
            for index, entry in enumerate(entries)
            if entry.kind == "kill"
            and _effect_active_at(entries, index, "flash_start", "flash_end")
        ]
        span = max(flash_kill_seconds) - min(flash_kill_seconds)
        if span <= _CONTINUOUS_EVENT_MAX_SECONDS:
            facts.append(f"连续事件{_seconds(span)}秒内完成被闪双杀")
    if round_situation.flash_count >= 2 and focus_index is not None:
        completed_flashes = sum(
            1
            for entry in entries[: focus_index + 1]
            if entry.kind == "flash_end" and "未结束" not in (entry.detail or "")
        )
        if completed_flashes >= 2 and entries[focus_index].kind == "kill":
            facts.append("玩家被闪两次，闪光影响均结束后完成击杀")

    if round_situation.flash_count >= 2:
        facts.append(f"本回合被闪{round_situation.flash_count}次")
    grenade_total = sum(count for _, count in round_situation.grenades_used)
    if grenade_total >= 4:
        grenade_summary = _grenade_summary(round_situation.grenades_used)
        if grenade_summary is not None:
            facts.append(grenade_summary.replace("【本回合投掷物】", "投掷物：", 1))

    primary_labels = [
        weapon_display_name(name) for name in round_situation.primary_weapons_used
    ]
    if len(primary_labels) >= 2:
        facts.append(f"换枪：{'→'.join(primary_labels)}")

    details_by_kind: dict[str, list[str]] = {}
    for entry in entries:
        if entry.detail:
            details_by_kind.setdefault(entry.kind, []).append(entry.detail)
    method = str(event.facts.get("method") or "")
    bomb_result = method in {"ct_win_defuse", "t_win_bomb", "defuse", "bomb"}
    total_round_kills = sum(
        _timeline_kill_increase(entry) for entry in entries if entry.kind == "kill"
    )
    if not bomb_result and total_round_kills < 2:
        if any(entry.kind == "mvp" for entry in entries):
            facts.append("本回合获得MVP")
        if any(entry.kind == "assist" for entry in entries):
            facts.append("本回合新增助攻")
        for detail in details_by_kind.get("grenade_pickup", []):
            facts.append(detail)

    has_pickup = any(entry.kind == "bomb_pickup" for entry in entries)
    has_drop = any(entry.kind == "bomb_drop" for entry in entries)
    has_death = any(entry.kind == "death" for entry in entries)
    has_kill = any(entry.kind == "kill" for entry in entries)
    if has_drop and has_pickup:
        facts.append("丢了包后重新拿到包")
    elif has_pickup and has_death:
        facts.append("拿到包后阵亡")
    elif has_pickup and has_kill:
        facts.append("拿到包后完成击杀")
    if has_pickup:
        facts = [fact for fact in facts if not fact.startswith("捡到")]

    if has_death:
        last_kill_index = next(
            (i for i in range(len(entries) - 1, -1, -1) if entries[i].kind == "kill"),
            None,
        )
        death_index = next(
            (i for i in range(len(entries) - 1, -1, -1) if entries[i].kind == "death"),
            None,
        )
        if last_kill_index is not None and death_index is not None:
            ammo_between = next(
                (
                    entry.detail
                    for entry in entries[last_kill_index + 1 : death_index + 1]
                    if entry.kind == "ammo_low" and entry.detail
                ),
                None,
            )
            total_kills = sum(
                _timeline_kill_increase(entry)
                for entry in entries[: last_kill_index + 1]
                if entry.kind == "kill"
            )
            if ammo_between is not None and total_kills >= 2:
                facts.append(
                    f"完成{_kill_count_label(total_kills)}后{ammo_between}并阵亡"
                )

    planted_entry = next(
        (
            entry
            for entry in entries
            if entry.kind == "bomb" and "安放" in (entry.detail or "")
        ),
        None,
    )
    result_bomb_entry = next(
        (
            entry
            for entry in reversed(entries)
            if entry.kind == "bomb"
            and any(label in (entry.detail or "") for label in ("拆除", "引爆"))
        ),
        None,
    )
    if planted_entry is not None and (
        event.type in {"round_win", "round_loss"} or has_death
    ):
        facts.append("炸弹已安放")
    if method in {"ct_win_defuse", "t_win_bomb", "defuse", "bomb"} and self_team:
        facts.append(f"我方{self_team}")
    if planted_entry is not None and result_bomb_entry is not None:
        elapsed = max(0.0, result_bomb_entry.seconds - planted_entry.seconds)
        if elapsed >= 30.0:
            result_label = "拆除" if "拆除" in (result_bomb_entry.detail or "") else "引爆"
            facts.append(f"下包{_seconds(elapsed)}秒后{result_label}")
            if result_label == "拆除":
                facts.append("炸弹已拆除")
    unique_facts = list(dict.fromkeys(facts))
    return sorted(unique_facts, key=_rare_fact_priority)


def _rare_fact_priority(fact: str) -> int:
    """Put the scenario-defining atom before incidental generic combat facts."""
    if any(term in fact for term in ("拿到包", "丢了包")):
        return -1
    if any(
        term in fact
        for term in (
            "MVP",
            "助攻",
            "被闪",
            "闪光",
            "烟雾",
            "进烟",
            "出烟",
            "燃烧",
            "弹匣",
            "残血",
            "连续事件",
            "捡到",
            "投掷物",
            "秒后拆除",
            "秒后引爆",
            "炸弹已拆除",
            "炸弹引爆",
        )
    ):
        return 0
    if any(term in fact for term in ("炸弹", "下包")):
        return 1
    if "换枪" in fact:
        return 2
    return 3


def _effect_started_at(
    entries: tuple[TimelineEntry, ...],
    index: int,
    start_kind: str,
    end_kind: str,
) -> float | None:
    started_at: float | None = None
    for entry in entries[: index + 1]:
        if entry.kind == start_kind:
            started_at = entry.seconds
        elif entry.kind == end_kind:
            started_at = None
    return started_at


def _effect_active_at(
    entries: tuple[TimelineEntry, ...],
    index: int,
    start_kind: str,
    end_kind: str,
) -> bool:
    return _effect_started_at(entries, index, start_kind, end_kind) is not None


def _has_unclosed_effect(
    entries: tuple[TimelineEntry, ...], index: int, end_kind: str
) -> bool:
    target_seconds = entries[index].seconds
    return any(
        entry.kind == end_kind
        and entry.seconds == target_seconds
        and "未结束" in (entry.detail or "")
        for entry in entries[: index + 1]
    )


def _previous_detail(
    entries: tuple[TimelineEntry, ...],
    index: int,
    *,
    kind: str,
) -> str | None:
    target = entries[index]
    nearest: tuple[float, str] | None = None
    for entry in entries[: index + 1]:
        if entry.kind != kind or not entry.detail:
            continue
        gap = target.seconds - entry.seconds
        if gap <= _CONTINUOUS_EVENT_MAX_SECONDS and (
            nearest is None or gap < nearest[0]
        ):
            nearest = (gap, entry.detail)
    return nearest[1] if nearest is not None else None


def _required_stage(stage: str | None, observed_live: bool) -> str:
    if stage is not None and stage != "阶段不可判断":
        return stage
    return "未观测开打时刻" if not observed_live else "阶段不可判断"


def _scoped_multikill_facts(kill_facts: list[str]) -> list[str]:
    scoped: list[str] = []
    for fact in kill_facts:
        kill_marker = fact.split("、", 1)[0]
        prefix = f"{kill_marker}记录必须单独写"
        scoped.append(f"{prefix}：{fact}")
    scoped.append("不得把后一次击杀写成该阶段双杀")
    return scoped


def _kill_health_fact(
    entries: tuple[TimelineEntry, ...], index: int
) -> str | None:
    detail = entries[index].detail or ""
    explicit = re.search(r"击杀时(满血|剩\d+血)", detail)
    if explicit is not None:
        return explicit.group(1)
    nearest: tuple[float, str] | None = None
    for other_index in range(index - 1, -1, -1):
        other = entries[other_index]
        if other.kind != "damage":
            continue
        gap = entries[index].seconds - other.seconds
        if gap > _NEARBY_COMBAT_SECONDS:
            break
        remaining = re.search(r"剩(\d+)血", other.detail or "")
        if remaining is not None and (nearest is None or gap < nearest[0]):
            nearest = (gap, f"剩{remaining.group(1)}血")
    if nearest is None:
        for other in entries[index + 1 :]:
            if other.seconds != entries[index].seconds:
                break
            if other.kind != "damage":
                continue
            remaining = re.search(r"剩(\d+)血", other.detail or "")
            if remaining is not None:
                nearest = (0.0, f"剩{remaining.group(1)}血")
                break
    return nearest[1] if nearest is not None else None


def _kill_weapon(detail: str | None) -> str | None:
    if not detail:
        return None
    marker = re.search(
        r"(?:^| )(?=(?:爆头|\d+个爆头|增加\d+杀|击杀时满血|"
        r"击杀时剩\d+血|弹匣仅剩\d+发))",
        detail,
    )
    weapon = detail[: marker.start()].strip() if marker is not None else detail.strip()
    return weapon or None


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


def _previous_entry_gap(
    entries: tuple[TimelineEntry, ...], index: int, *, kind: str
) -> float | None:
    current_seconds = entries[index].seconds
    previous = next(
        (
            entry
            for entry in reversed(entries[:index])
            if entry.kind == kind and entry.seconds <= current_seconds
        ),
        None,
    )
    return current_seconds - previous.seconds if previous is not None else None


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
        r"(?:^| )(?=(?:爆头|\d+个爆头|增加\d+杀|击杀时满血|"
        r"击杀时剩\d+血|弹匣仅剩\d+发))",
        detail,
    )
    if marker is None:
        return f"玩家使用{detail}完成击杀"
    weapon = detail[: marker.start()].strip()
    attributes = detail[marker.start() :].strip()
    action = f"玩家使用{weapon}完成击杀" if weapon else "玩家完成击杀"
    return action + (f" {attributes}" if attributes else "")


def _seconds(value: Any) -> str:
    return f"{float(value):.1f}"
