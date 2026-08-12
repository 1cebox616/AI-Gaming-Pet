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
        "reload",
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
_KILL_INCREASE_PATTERN = re.compile(r"(?:^| )增加(\d+)杀(?: |$)")


def render_event_card(
    snapshot: GameSnapshot,
    game: GameState,
    round_situation: RoundSituation,
    event: GameEvent,
    *,
    death_after_kill_max_seconds: float = 8.0,
) -> str:
    """Render everything the model may know about this moment as Chinese text."""
    sections: list[str | None] = [
        *_match_sections(snapshot, round_situation, event),
        _section("我", _player_facts(snapshot, game, round_situation)),
        _section("全场", _match_statistics(snapshot, game)),
    ]
    current_round = human_round_number(snapshot)
    focus_integrated = False
    if round_situation.round_number == current_round:
        sections.append(_bomb_timer(round_situation.seconds_since_bomb_planted))
        sections.append(_grenade_summary(round_situation.grenades_used))
        timeline, focus_integrated = _timeline_section(
            round_situation.timeline,
            self_team=round_situation.self_team,
            death_after_kill_max_seconds=death_after_kill_max_seconds,
            event=event,
        )
        sections.append(timeline)
    if not focus_integrated:
        # Incomplete or stale timelines cannot host a truthful focus marker.
        sections.append(_section("刚刚", _event_facts(event)))
    return "\n".join(section for section in sections if section is not None)


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


def _timeline_section(
    entries: tuple[TimelineEntry, ...],
    *,
    self_team: str | None,
    death_after_kill_max_seconds: float,
    event: GameEvent,
) -> tuple[str | None, bool]:
    if not entries:
        return None, False
    observed_live = any(entry.kind == "round_live" for entry in entries)
    lines = [
        "【本回合】（秒数从正式开打算起）"
        if observed_live
        else "【本回合】（未观测到开打时刻，秒数从回合起点算起）"
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
                annotations.append(f"对枪胜利，间隔{_seconds(damage_gap)}秒")
            smoke_gap = _previous_entry_gap(entries, index, kind="smoke_end")
            if smoke_gap is not None and smoke_gap <= _NEARBY_COMBAT_SECONDS:
                annotations.append(f"摸烟击杀，出烟后{_seconds(smoke_gap)}秒")
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
                    "击杀后被补枪，距击杀"
                    f"{_seconds(entry.seconds - last_kill_seconds)}秒"
                )
        annotations_by_index.append(tuple(annotations))

    focus_index = _focus_entry_index(entries, event)
    for indexes in _continuous_event_groups(entries):
        lines.append(
            _timeline_group_line(
                entries,
                indexes,
                stages,
                annotations_by_index,
                event=event,
                focus_index=focus_index,
            )
        )
    return "\n".join(lines), focus_index is not None


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
    if focused:
        marker_facts.append("刚刚")
    if len(indexes) >= 2:
        duration = max(0.0, last.seconds - entries[indexes[0]].seconds)
        if duration >= 0.05:
            marker_facts.append(f"连续事件{_seconds(duration)}秒")
    stage_index = (
        focus_index
        if focused
        else next(
            (
                index
                for index in reversed(indexes)
                if entries[index].kind in _CONTINUOUS_EVENT_ANCHORS
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
    return (f"本次焦点：{_EVENT_LABELS[event.type]}", *annotations)


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
        if detail is not None and detail.startswith("未结束 "):
            return "玩家受闪光影响" + detail
        return "玩家闪光影响结束" + (f" {detail}" if detail else "")
    if entry.kind == "smoke_start":
        return "玩家进烟"
    if entry.kind == "smoke_end":
        if detail is not None and detail.startswith("未结束 "):
            return "玩家仍在烟中 " + detail.removeprefix("未结束 ")
        return "玩家出烟" + (f" {detail}" if detail else "")
    if entry.kind == "burn_start":
        return "玩家开始燃烧"
    if entry.kind == "burn_end":
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
    if entry.kind in {"ammo_low", "reload", "grenade_used", "grenade_pickup"}:
        return "玩家" + (detail or "状态变化")
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
