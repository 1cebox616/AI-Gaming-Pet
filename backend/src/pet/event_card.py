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
    ]
    current_round = human_round_number(snapshot)
    if round_situation.round_number == current_round:
        sections.append(
            _timeline_section(
                round_situation.timeline,
                self_team=round_situation.self_team,
                death_after_kill_max_seconds=death_after_kill_max_seconds,
            )
        )
    sections.extend(
        (
            _section("全场", _match_statistics(snapshot, game)),
            _section("刚刚", _event_facts(event)),
        )
    )
    return "\n".join(section for section in sections if section is not None)


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
    if isinstance(losses, int) and losses > 0:
        sections.append(f"【连败】{losses}轮")
    return sections


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
) -> str | None:
    if not entries:
        return None
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
    for index, (entry, stage) in enumerate(zip(entries, stages)):
        annotations: list[str] = []
        if stage is not None:
            annotations.append(stage)
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
                annotations.append(f"近同时掉血，间隔{_seconds(damage_gap)}秒")
            smoke_gap = _previous_entry_gap(entries, index, kind="smoke_end")
            if smoke_gap is not None and smoke_gap <= _NEARBY_COMBAT_SECONDS:
                annotations.append(f"出烟后{_seconds(smoke_gap)}秒")
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
                    "击杀后很快阵亡，间隔"
                    f"{_seconds(entry.seconds - last_kill_seconds)}秒"
                )
        lines.append(
            _timeline_line(entry, indent="  ", annotations=tuple(annotations))
        )
    return "\n".join(lines)


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


def _timeline_line(
    entry: TimelineEntry,
    *,
    indent: str,
    annotations: tuple[str, ...] = (),
) -> str:
    annotation_text = f"〔{'｜'.join(annotations)}〕" if annotations else ""
    return (
        f"{indent}{_seconds(entry.seconds)}s{annotation_text} "
        f"{_timeline_entry_text(entry)}"
    )


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


def _timeline_entry_text(entry: TimelineEntry) -> str:
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
    if entry.kind == "kill":
        return _timeline_kill_text(detail)
    if entry.kind == "damage":
        return "玩家" + (detail or "受到伤害")
    if entry.kind == "primary_weapon":
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
    return "玩家阵亡"


def _timeline_kill_text(detail: str | None) -> str:
    if not detail:
        return "玩家完成击杀"
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
