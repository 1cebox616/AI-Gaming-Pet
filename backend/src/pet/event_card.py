"""Render existing CS2 facts into the GSI event card shown to a model."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pet.commentary_templates import WIN_METHOD_LABELS
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


def render_event_card(
    snapshot: GameSnapshot,
    game: GameState,
    round_situation: RoundSituation,
    event: GameEvent,
) -> str:
    """Render everything the model may know about this moment as Chinese text."""
    sections = [
        _section("对局", _match_facts(snapshot, round_situation)),
        _section("我", _player_facts(snapshot, game, round_situation)),
    ]
    current_round = human_round_number(snapshot)
    if round_situation.round_number == current_round:
        sections.append(_timeline_section(round_situation.timeline))
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


def _match_facts(
    snapshot: GameSnapshot, round_situation: RoundSituation
) -> list[str]:
    facts: list[str] = []
    if snapshot.map_mode is not None:
        facts.append(_MODE_LABELS.get(snapshot.map_mode.lower(), snapshot.map_mode))
    if snapshot.map_name is not None:
        facts.append(snapshot.map_name)
    round_number = human_round_number(snapshot)
    if round_number is not None:
        facts.append(f"第{round_number}回合")
    if snapshot.score_ct is not None and snapshot.score_t is not None:
        facts.append(f"CT {snapshot.score_ct}:{snapshot.score_t} T")
    if round_situation.self_team in {"CT", "T"}:
        facts.append(f"我在{round_situation.self_team}方")
        losses = (
            snapshot.ct_consecutive_round_losses
            if round_situation.self_team == "CT"
            else snapshot.t_consecutive_round_losses
        )
        if losses is not None and losses > 0:
            facts.append(f"我方连败{losses}轮")
    return facts


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
        facts.append(f"{snapshot.money}块")
    if snapshot.equip_value is not None:
        facts.append(f"装备{snapshot.equip_value}")
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
        ):
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


def _timeline_section(entries: tuple[TimelineEntry, ...]) -> str | None:
    if not entries:
        return None
    lines = ["【本回合】"]
    lines.extend(
        f"  {_seconds(entry.seconds)}s {_timeline_entry_text(entry)}"
        for entry in entries
    )
    return "\n".join(lines)


def _timeline_entry_text(entry: TimelineEntry) -> str:
    detail = entry.detail
    if entry.kind == "bought":
        return "买了装备"
    if entry.kind == "round_live":
        return "正式开打"
    if entry.kind == "flash_start":
        return "被闪"
    if entry.kind == "flash_end":
        return "闪光结束" + (f" {detail}" if detail else "")
    if entry.kind == "smoke_start":
        return "进烟"
    if entry.kind == "smoke_end":
        return "出烟" + (f" {detail}" if detail else "")
    if entry.kind == "kill":
        return "击杀" + (f" {detail}" if detail else "")
    if entry.kind == "damage":
        return detail or "受到伤害"
    if entry.kind == "primary_weapon":
        return "主武器" + (f" {detail}" if detail else "")
    if entry.kind in {"ammo_low", "grenade_used"}:
        return detail or "状态变化"
    if entry.kind == "bomb":
        return "炸弹" + (detail or "状态变化")
    if entry.kind == "bomb_pickup":
        return "拿到炸弹"
    return "阵亡"


def _seconds(value: Any) -> str:
    return f"{float(value):.1f}"
