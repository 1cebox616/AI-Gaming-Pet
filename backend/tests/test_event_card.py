"""GSI-event-card rendering tests based on an existing scrubbed GSI sample."""

import json
from pathlib import Path
from typing import Any

from pet.events import GameEvent
from pet.gsi import GameSnapshot, WeaponSlot, parse_snapshot
from pet.session import GameState
from pet.situation import RoundSituation, TimelineEntry
from pet.event_card import render_event_card

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
        self_team="CT",
        timeline=(
            TimelineEntry(10.9, "round_live", None),
            TimelineEntry(12.345, "flash_start", None),
            TimelineEntry(13.049, "flash_end", "持续0.7秒"),
            TimelineEntry(20.0, "grenade_used", "扔了烟雾弹"),
            TimelineEntry(30.0, "ammo_low", "弹匣仅剩1发 M4A1-S"),
            TimelineEntry(33.149, "kill", "M4A1-S 爆头 弹匣仅剩1发"),
            TimelineEntry(45.222, "smoke_start", None),
            TimelineEntry(53.444, "smoke_end", "持续8.2秒"),
            TimelineEntry(54.0, "bomb_pickup", None),
        ),
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
            "team_consecutive_round_losses": 2,
            "score_ct": 2,
            "score_t": 3,
        },
    )


def test_all_five_sections_are_present_in_fixed_order_with_full_facts() -> None:
    snapshot = _real_snapshot().model_copy(update={"health": 0, "bomb_state": "planted"})

    card = render_event_card(snapshot, _game(snapshot), _situation(), _event(snapshot))

    headings = [
        "【对局】",
        "【我】",
        "【本回合】",
        "【全场】",
        "【刚刚】",
    ]
    assert all(heading in card for heading in headings)
    assert [card.index(heading) for heading in headings] == sorted(
        card.index(heading) for heading in headings
    )
    assert "休闲 de_anubis 第6回合 CT 2:3 T 我在CT方 我方连败2轮" in card
    assert "已阵亡 倒地前12血 有甲无头 1750块 装备4100" in card
    assert "手持M4A1-S 弹匣12/20 备弹40" in card
    assert "  10.9s 正式开打" in card
    assert "  12.3s 被闪" in card
    assert "  20.0s 扔了烟雾弹" in card
    assert "  30.0s 弹匣仅剩1发 M4A1-S" in card
    assert "  33.1s 击杀 M4A1-S 爆头 弹匣仅剩1发" in card
    assert "  53.4s 出烟 持续8.2秒" in card
    assert "  54.0s 拿到炸弹" in card
    assert "12杀 4助攻 5死 MVP1次" in card
    assert "普通死亡 存活89.8秒 本回合1杀" in card
    assert card.count("【本回合】") == 1
    for removed_summary in (
        "【本回合时间线】",
        "被闪累计",
        "在烟中累计",
        "用过",
        "本回合买过装备",
        "峰值",
    ):
        assert removed_summary not in card
    for redundant in (
        "CT比分",
        "T比分",
        "比分态势",
        "我方连败2轮",
        "连续增加",
    ):
        assert redundant not in card.split("【刚刚】", 1)[1]


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

    card = render_event_card(snapshot, _game(snapshot), _situation(), _event(snapshot))

    assert "未知" not in card
    assert "块" not in card
    assert "装备价值4100" in card  # The event explicitly carries this independent fact.
    assert "有甲" not in card and "无甲" not in card
    assert "手持" not in card
    assert "助" not in card


def test_zero_and_no_occurrence_facts_are_omitted() -> None:
    snapshot = _real_snapshot().model_copy(
        update={
            "has_defusekit": False,
            "flashed": 0,
            "smoked": 0,
            "round_kills": 0,
            "round_killhs": 0,
            "match_kills": 0,
            "match_assists": 0,
            "match_deaths": 0,
            "match_mvps": 0,
            "weapons": (),
        }
    )
    situation = RoundSituation(
        round_number=6,
        flash_count=0,
        flashed_seconds_total=0.0,
        longest_flash_seconds=0.0,
        smoked_seconds_total=0.0,
        max_smoke_intensity=0,
        burn_count=0,
        total_damage_taken=0,
        lowest_health_while_alive=100,
        health_before_death=None,
        primary_weapons_used=(),
        bought_equipment=False,
        bomb_planted_at_ts=None,
        seconds_since_bomb_planted=None,
        self_team="CT",
        timeline=(),
    )

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))

    for omitted in (
        "被闪0次",
        "被闪累计0.0秒",
        "最长0.0秒",
        "在烟中累计0.0秒",
        "最高烟雾强度0",
        "燃烧0次",
        "累计掉血0",
        "0个击杀",
        "0个爆头击杀",
        "存活时最低100血",
        "没带包",
        "没带拆弹器",
        "未被闪",
        "不在烟中",
        "本回合未买装备",
    ):
        assert omitted not in card
    assert "【本回合】" not in card
    assert "【全场】" not in card


def test_card_contains_no_unavailable_position_enemy_or_attribution_facts() -> None:
    snapshot = _real_snapshot()

    card = render_event_card(snapshot, _game(snapshot), _situation(), _event(snapshot))

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

    card = render_event_card(snapshot, _game(snapshot), _situation(), _event(snapshot))

    assert "0.7秒" in card
    assert "8.2秒" in card
    assert "89.8秒" in card
    assert "1.4002890586853" not in card


def test_stale_round_situation_is_omitted_while_other_sections_remain() -> None:
    snapshot = _real_snapshot()

    card = render_event_card(
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


def test_spectating_keeps_match_round_timeline_and_self_team_but_not_player_data() -> None:
    snapshot = _real_snapshot().model_copy(
        update={
            "player_steamid": "76561198000000999",
            "team": "T",
            "health": 5,
            "money": 9999,
            "round_kills": 99,
            "round_killhs": 99,
        }
    )
    game = _game(snapshot).model_copy(
        update={
            "state": "spectating",
            "subject_steamid": snapshot.player_steamid,
            "subject_is_self": False,
        }
    )

    card = render_event_card(snapshot, game, _situation(), _event(snapshot))

    assert "【对局】休闲 de_anubis 第6回合 CT 2:3 T 我在CT方" in card
    assert "【我】" not in card
    assert "【全场】" not in card
    assert "【本回合】" in card
    assert "【刚刚】" in card
    assert "9999块" not in card
    assert "5血" not in card
    assert "99个击杀" not in card
    assert "99个爆头击杀" not in card
