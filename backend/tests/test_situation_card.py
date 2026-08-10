"""Situation-card rendering tests based on an existing scrubbed GSI sample."""

import json
from pathlib import Path
from typing import Any

from pet.events import GameEvent
from pet.gsi import GameSnapshot, WeaponSlot, parse_snapshot
from pet.session import GameState
from pet.situation import RoundSituation
from pet.situation_card import render_situation_card

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_event_samples.json"


def _real_snapshot() -> GameSnapshot:
    loaded: Any = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    row = loaded["cold_start_existing_stats"]["samples"][0]
    snapshot = parse_snapshot(row["payload"], received_at=row["ts"])
    return snapshot.model_copy(
        update={
            "armor": 60,
            "helmet": False,
            "money": 1750,
            "equip_value": 4100,
            "has_defusekit": True,
            "flashed": 80,
            "smoked": 120,
            "match_kills": 12,
            "match_assists": 4,
            "match_deaths": 5,
            "match_mvps": 1,
            "ct_consecutive_round_losses": 2,
            "weapons": (
                WeaponSlot(
                    name="weapon_m4a1_silencer",
                    type="Rifle",
                    ammo_clip=12,
                    ammo_clip_max=20,
                    ammo_reserve=40,
                    state="active",
                ),
            ),
        }
    )


def _game(snapshot: GameSnapshot) -> GameState:
    return GameState(
        state="playing",
        mode=snapshot.map_mode,
        map=snapshot.map_name,
        round=6,
        score_ct=snapshot.score_ct,
        score_t=snapshot.score_t,
        subject_steamid=snapshot.player_steamid,
        subject_is_self=True,
    )


def _situation(*, round_number: int | None = 6) -> RoundSituation:
    return RoundSituation(
        round_number=round_number,
        flash_count=2,
        flashed_seconds_total=1.4002890586853,
        longest_flash_seconds=0.7001823,
        smoked_seconds_total=4.24991,
        max_smoke_intensity=255,
        burn_count=1,
        total_damage_taken=73,
        lowest_health_while_alive=12,
        health_before_death=12,
        primary_weapons_used=("weapon_m4a1_silencer", "weapon_ak47"),
        bought_equipment=True,
        bomb_planted_at_ts=100.0,
        seconds_since_bomb_planted=8.67891,
    )


def _event(snapshot: GameSnapshot) -> GameEvent:
    return GameEvent(
        id="fixture-event",
        type="death",
        ts=snapshot.ts,
        subject_steamid=snapshot.player_steamid,
        subject_is_self=True,
        round_number=6,
        facts={
            "survival_seconds": 89.81234,
            "round_kills": 1,
            "equip_value": 4100,
            "score_situation": "落后",
        },
    )


def test_all_five_sections_are_present_in_fixed_order_with_full_facts() -> None:
    snapshot = _real_snapshot().model_copy(update={"health": 0, "bomb_state": "planted"})

    card = render_situation_card(snapshot, _game(snapshot), _situation(), _event(snapshot))

    headings = ["【对局】", "【我】", "【本回合】", "【全场】", "【刚刚】"]
    assert all(heading in card for heading in headings)
    assert [card.index(heading) for heading in headings] == sorted(
        card.index(heading) for heading in headings
    )
    assert "休闲 de_anubis 第6回合 CT 2:3 T 我在CT方 我方连败2轮" in card
    assert "已阵亡 倒地前12血 有甲无头 1750块 装备价值4100" in card
    assert "手持M4A1-S 弹匣12/20 备弹40" in card
    assert "用过M4A1-S、AK47" in card
    assert "炸弹已安放 安放后8.7秒" in card
    assert "12杀 4助 5死 MVP1次" in card
    assert "普通死亡 存活89.8秒 本回合1杀" in card


def test_missing_fields_are_omitted_without_unknown_or_zero_substitution() -> None:
    snapshot = _real_snapshot().model_copy(
        update={
            "map_name": None,
            "money": None,
            "equip_value": None,
            "armor": None,
            "helmet": None,
            "weapons": None,
            "active_weapon": None,
            "match_assists": None,
        }
    )

    card = render_situation_card(snapshot, _game(snapshot), _situation(), _event(snapshot))

    assert "未知" not in card
    assert "块" not in card
    assert "装备价值4100" in card  # The event explicitly carries this independent fact.
    assert "有甲" not in card and "无甲" not in card
    assert "手持" not in card
    assert "助" not in card


def test_card_contains_no_unavailable_position_enemy_or_attribution_facts() -> None:
    snapshot = _real_snapshot()

    card = render_situation_card(snapshot, _game(snapshot), _situation(), _event(snapshot))

    for forbidden in (
        "玩家位置",
        "点位",
        "场上剩",
        "敌方经济",
        "敌方装备",
        "伤害来源",
        "谁杀",
        "下包者",
    ):
        assert forbidden not in card


def test_seconds_use_one_decimal_and_never_expose_long_float() -> None:
    snapshot = _real_snapshot()

    card = render_situation_card(snapshot, _game(snapshot), _situation(), _event(snapshot))

    assert "1.4秒" in card
    assert "0.7秒" in card
    assert "4.2秒" in card
    assert "8.7秒" in card
    assert "89.8秒" in card
    assert "1.4002890586853" not in card


def test_stale_round_situation_is_omitted_while_other_sections_remain() -> None:
    snapshot = _real_snapshot()

    card = render_situation_card(
        snapshot,
        _game(snapshot),
        _situation(round_number=5),
        _event(snapshot),
    )

    assert "【本回合】" not in card
    assert "【对局】" in card
    assert "【我】" in card
    assert "【全场】" in card
    assert "【刚刚】" in card
