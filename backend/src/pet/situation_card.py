"""Render existing CS2 facts into the complete context shown to a model."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pet.commentary_templates import WIN_METHOD_LABELS
from pet.events import EventType, GameEvent
from pet.gsi import GameSnapshot, WeaponSlot, human_round_number
from pet.session import GameState
from pet.situation import (
    RoundSituation,
    armor_status,
    held_weapon,
    is_carrying_bomb,
    is_currently_flashed,
    is_currently_smoked,
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
_BOMB_STATE_LABELS = {
    "planted": "炸弹已安放",
    "defused": "炸弹已拆除",
    "exploded": "炸弹已爆炸",
}
_WEAPON_LABELS = {
    "ak47": "AK47",
    "aug": "AUG",
    "awp": "AWP",
    "bizon": "PP-Bizon",
    "c4": "C4",
    "deagle": "沙鹰",
    "famas": "FAMAS",
    "galilar": "Galil AR",
    "glock": "Glock",
    "hkp2000": "P2000",
    "m4a1": "M4A4",
    "m4a1_silencer": "M4A1-S",
    "mac10": "MAC-10",
    "mp9": "MP9",
    "p250": "P250",
    "ssg08": "SSG 08",
    "usp_silencer": "USP-S",
}
_FACT_ORDER = (
    "round_kill_index",
    "delta",
    "weapon",
    "count",
    "survival_seconds",
    "round_kills",
    "seconds_since_last_kill",
    "equip_value",
    "score_situation",
    "team_consecutive_round_losses",
    "method",
    "score_ct",
    "score_t",
)


def render_situation_card(
    snapshot: GameSnapshot,
    game: GameState,
    round_situation: RoundSituation,
    event: GameEvent,
) -> str:
    """Render everything the model may know about this moment as Chinese text."""
    sections = [
        _section("对局", _match_facts(snapshot, game)),
        _section("我", _player_facts(snapshot, game, round_situation)),
    ]
    current_round = human_round_number(snapshot)
    if (
        game.subject_is_self is True
        and round_situation.round_number == current_round
    ):
        sections.append(_section("本回合", _round_facts(snapshot, round_situation)))
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


def _match_facts(snapshot: GameSnapshot, game: GameState) -> list[str]:
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
    if game.subject_is_self is True and snapshot.team in {"CT", "T"}:
        facts.append(f"我在{snapshot.team}方")
        losses = (
            snapshot.ct_consecutive_round_losses
            if snapshot.team == "CT"
            else snapshot.t_consecutive_round_losses
        )
        if losses is not None:
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
        facts.append(f"装备价值{snapshot.equip_value}")
    weapon = held_weapon(snapshot)
    if weapon is not None:
        facts.append(_held_weapon_fact(weapon))
    elif snapshot.active_weapon is not None:
        facts.append(f"手持{_weapon_label(snapshot.active_weapon)}")
    carrying_bomb = is_carrying_bomb(snapshot)
    if carrying_bomb is not None:
        facts.append("带包" if carrying_bomb else "没带包")
    if snapshot.has_defusekit is not None:
        facts.append("带拆弹器" if snapshot.has_defusekit else "没带拆弹器")
    flashed = is_currently_flashed(snapshot)
    if flashed is not None:
        facts.append("正被闪" if flashed else "未被闪")
    smoked = is_currently_smoked(snapshot)
    if smoked is not None:
        facts.append("正在烟中" if smoked else "不在烟中")
    return facts


def _round_facts(
    snapshot: GameSnapshot,
    situation: RoundSituation,
) -> list[str]:
    facts: list[str] = []
    if snapshot.round_kills is not None:
        facts.append(f"{snapshot.round_kills}个击杀")
    if snapshot.round_killhs is not None:
        facts.append(f"{snapshot.round_killhs}个爆头击杀")
    facts.extend(
        (
            f"被闪{situation.flash_count}次",
            f"被闪累计{_seconds(situation.flashed_seconds_total)}秒",
            f"最长{_seconds(situation.longest_flash_seconds)}秒",
            f"在烟中累计{_seconds(situation.smoked_seconds_total)}秒",
        )
    )
    if situation.max_smoke_intensity is not None:
        facts.append(f"最高烟雾强度{situation.max_smoke_intensity}")
    facts.extend(
        (
            f"燃烧{situation.burn_count}次",
            f"累计掉血{situation.total_damage_taken}",
        )
    )
    if situation.lowest_health_while_alive is not None:
        facts.append(f"存活时最低{situation.lowest_health_while_alive}血")
    if situation.primary_weapons_used:
        facts.append(
            "用过" + "、".join(_weapon_label(name) for name in situation.primary_weapons_used)
        )
    facts.append("本回合买过装备" if situation.bought_equipment else "本回合未买装备")
    if snapshot.bomb_state is not None:
        facts.append(_BOMB_STATE_LABELS.get(snapshot.bomb_state, snapshot.bomb_state))
    if situation.seconds_since_bomb_planted is not None:
        facts.append(f"安放后{_seconds(situation.seconds_since_bomb_planted)}秒")
    return facts


def _match_statistics(snapshot: GameSnapshot, game: GameState) -> list[str]:
    if game.subject_is_self is not True:
        return []
    facts: list[str] = []
    for value, suffix in (
        (snapshot.match_kills, "杀"),
        (snapshot.match_assists, "助"),
        (snapshot.match_deaths, "死"),
    ):
        if value is not None:
            facts.append(f"{value}{suffix}")
    if snapshot.match_mvps is not None:
        facts.append(f"MVP{snapshot.match_mvps}次")
    return facts


def _event_facts(event: GameEvent) -> list[str]:
    facts = [_EVENT_LABELS[event.type]]
    renderers = {
        "round_kill_index": lambda value: f"本回合第{value}杀",
        "delta": lambda value: f"连续增加{value}杀",
        "weapon": lambda value: f"武器{_weapon_label(str(value))}",
        "count": lambda value: f"本回合累计{value}杀",
        "survival_seconds": lambda value: f"存活{_seconds(value)}秒",
        "round_kills": lambda value: f"本回合{value}杀",
        "seconds_since_last_kill": lambda value: f"距上次击杀{_seconds(value)}秒",
        "equip_value": lambda value: f"装备价值{value}",
        "score_situation": lambda value: f"比分态势{value}",
        "team_consecutive_round_losses": lambda value: f"我方连败{value}轮",
        "method": lambda value: f"获胜方式{WIN_METHOD_LABELS.get(str(value), '其他')}",
        "score_ct": lambda value: f"CT比分{value}",
        "score_t": lambda value: f"T比分{value}",
    }
    for key in _FACT_ORDER:
        value = event.facts.get(key)
        if value is not None:
            facts.append(renderers[key](value))
    return facts


def _held_weapon_fact(weapon: WeaponSlot) -> str:
    fact = f"手持{_weapon_label(weapon.name)}"
    if weapon.ammo_clip is not None:
        clip = str(weapon.ammo_clip)
        if weapon.ammo_clip_max is not None:
            clip += f"/{weapon.ammo_clip_max}"
        fact += f" 弹匣{clip}"
    if weapon.ammo_reserve is not None:
        fact += f" 备弹{weapon.ammo_reserve}"
    return fact


def _weapon_label(name: str) -> str:
    normalized = name.removeprefix("weapon_").lower()
    return _WEAPON_LABELS.get(normalized, normalized)


def _seconds(value: Any) -> str:
    return f"{float(value):.1f}"
