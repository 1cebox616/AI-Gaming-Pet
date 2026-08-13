"""GSI-event-card rendering tests based on an existing scrubbed GSI sample."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pet.events import GameEvent
from pet.gsi import GameSnapshot, WeaponSlot, parse_snapshot
from pet.session import GameState
from pet.situation import RoundSituation, TimelineEntry
from pet.event_card import render_event_card, render_model_event_card

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
            "t_consecutive_round_losses": 0,
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
        grenades_used=(
            ("weapon_flashbang", 2),
            ("weapon_smokegrenade", 1),
        ),
        self_team="CT",
        timeline=(
            TimelineEntry(-3.9, "bought", None),
            TimelineEntry(0.0, "round_live", None),
            TimelineEntry(1.445, "flash_start", None),
            TimelineEntry(2.149, "flash_end", "持续0.7秒"),
            TimelineEntry(9.1, "grenade_used", "扔了烟雾弹"),
            TimelineEntry(19.1, "ammo_low", "弹匣仅剩1发 M4A1-S"),
            TimelineEntry(22.249, "kill", "M4A1-S 爆头 弹匣仅剩1发"),
            TimelineEntry(34.322, "smoke_start", None),
            TimelineEntry(42.544, "smoke_end", "持续8.2秒"),
            TimelineEntry(43.1, "bomb_pickup", None),
            TimelineEntry(89.81234, "death", None),
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
            "self_team": "CT",
            "self_score": 2,
            "opponent_score": 3,
            "score_situation": "落后",
            "team_consecutive_round_losses": 2,
            "score_ct": 2,
            "score_t": 3,
        },
    )


def test_top_labels_separate_forbidden_history_from_the_only_response_scope() -> None:
    snapshot = _real_snapshot().model_copy(update={"health": 0, "bomb_state": "planted"})

    card = render_event_card(snapshot, _game(snapshot), _situation(), _event(snapshot))

    headings = [
        "【游戏模式】",
        "【地图】",
        "【回合】",
        "【比分】",
        "【连败】",
        "【我】",
        "【全场】",
        "【下包后】",
        "【本回合投掷物】",
        "【本回合历史】",
        "【刚刚】",
    ]
    assert all(heading in card for heading in headings)
    assert [card.index(heading) for heading in headings] == sorted(
        card.index(heading) for heading in headings
    )
    assert "【游戏模式】休闲" in card
    assert "【地图】de_anubis" in card
    assert "【回合】第6回合" in card
    assert "【比分】我方CT 2:3（落后）" in card
    assert "【连败】2轮" in card
    assert "已阵亡 倒地前12血 有甲 余额1750 装备价值4100" in card
    assert "手持M4A1-S 弹匣12/20 备弹40" in card
    assert "【本回合投掷物】闪光弹×2｜烟雾弹×1" in card
    assert "【下包后】8.7秒" in card
    assert "【本回合历史】（仅供校验，禁止回应；秒数从正式开打算起）" in card
    assert "  -3.9s 玩家购买装备（仅检测到金额与价值变化）" in card
    assert "  0.0s 正式开打" in card
    assert "  1.4s 玩家被闪" in card
    assert "  9.1s 玩家扔了烟雾弹" in card
    assert "  19.1s 玩家弹匣仅剩1发 M4A1-S" in card
    assert (
        "  22.2s【前期】 玩家使用M4A1-S完成击杀 爆头 弹匣仅剩1发"
        "（本回合第1杀）"
    ) in card
    assert "  42.5s 玩家出烟 持续8.2秒" in card
    assert "  43.1s 玩家拿到包" in card
    assert "12杀 4助攻 5死 MVP1次" in card
    assert "【刚刚】（唯一回应范围）" in card
    assert "89.8s【后期】" in card
    assert "玩家阵亡（本次焦点：普通死亡｜本回合1杀）" in card
    assert "存活89.8秒" not in card
    assert card.count("【刚刚】") == 1
    assert "\n【刚刚】（唯一回应范围）\n" in card
    assert card.count("【本回合历史】") == 1
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


def test_model_card_contains_only_header_and_readable_current_event() -> None:
    snapshot = _real_snapshot()
    situation = _situation()
    event = _event(snapshot)

    full_card = render_event_card(snapshot, _game(snapshot), situation, event)
    model_card = render_model_event_card(snapshot, _game(snapshot), situation, event)

    assert full_card.startswith("【游戏模式】")
    assert model_card.startswith("de_anubis CT 2:3 落后 连败2\n【刚刚】")
    assert "焦点：普通死亡" in model_card
    assert "场景标签：" in model_card
    assert "回合时刻：89.8秒" in model_card
    assert "【本回合历史】" not in model_card
    assert "【我】" not in model_card
    assert " > " not in model_card
    assert " + " not in model_card
    assert "｜" not in model_card


def test_model_header_omits_zero_losses_and_missing_fields() -> None:
    snapshot = _real_snapshot().model_copy(update={"map_name": None})
    event = _event(snapshot).model_copy(
        update={
            "facts": {
                "self_team": "CT",
                "self_score": 4,
                "opponent_score": 4,
                "score_situation": "追平",
                "team_consecutive_round_losses": 0,
            }
        }
    )

    model_card = render_model_event_card(snapshot, _game(snapshot), _situation(), event)

    assert model_card.splitlines()[0] == "CT 4:4 追平"
    assert "连败" not in model_card.splitlines()[0]


def test_interrupted_smoke_observation_is_not_treated_as_smoke_exit() -> None:
    snapshot = _real_snapshot()
    situation = replace(
        _situation(),
        timeline=(
            TimelineEntry(0.0, "round_live", None),
            TimelineEntry(8.0, "smoke_start", None),
            TimelineEntry(9.0, "smoke_end", "观测中断 已确认持续0.5秒"),
            TimelineEntry(9.5, "death", None),
        ),
        fire_seconds=(),
        last_readable_held_ammo_at_seconds=9.5,
    )

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))

    assert "出烟就没了" not in card
    assert "烟雾状态观测中断" in card


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
    assert "装备价值4100" not in card
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
    assert "【本回合历史】" not in card
    assert "【全场】" not in card


def test_grenade_summary_aggregates_incendiary_aliases_and_omits_zeroes() -> None:
    snapshot = _real_snapshot()
    situation = replace(
        _situation(),
        grenades_used=(
            ("weapon_flashbang", 2),
            ("weapon_molotov", 1),
            ("weapon_incgrenade", 2),
            ("weapon_decoy", 0),
        ),
    )

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))

    assert "【本回合投掷物】闪光弹×2｜燃烧弹×3" in card
    assert "诱饵弹" not in card


def test_opponent_loss_streak_is_rendered_as_our_win_streak() -> None:
    snapshot = _real_snapshot().model_copy(
        update={"ct_consecutive_round_losses": 0, "t_consecutive_round_losses": 3}
    )
    event = _event(snapshot).model_copy(
        update={"facts": {**_event(snapshot).facts, "team_consecutive_round_losses": 0}}
    )

    card = render_event_card(snapshot, _game(snapshot), _situation(), event)

    assert "【连胜】3轮" in card
    assert "【连败】" not in card


def test_round_result_uses_result_direction_when_gsi_streak_resets_arrive_together() -> None:
    snapshot = _real_snapshot().model_copy(
        update={"ct_consecutive_round_losses": 3, "t_consecutive_round_losses": 1}
    )
    base = _event(snapshot)
    win = base.model_copy(update={"type": "round_win"})
    loss = base.model_copy(update={"type": "round_loss"})

    win_card = render_event_card(snapshot, _game(snapshot), _situation(), win)
    loss_card = render_event_card(snapshot, _game(snapshot), _situation(), loss)

    assert "【连胜】1轮" in win_card and "【连败】" not in win_card
    assert "【连败】2轮" in loss_card and "【连胜】" not in loss_card


def test_burn_transitions_and_mvp_increase_render_as_timeline_facts() -> None:
    snapshot = _real_snapshot()
    situation = replace(
        _situation(),
        timeline=(
            TimelineEntry(0.0, "round_live", None),
            TimelineEntry(20.0, "burn_start", None),
            TimelineEntry(22.4, "burn_end", "持续2.4秒"),
            TimelineEntry(80.0, "mvp", "MVP+1"),
            TimelineEntry(80.0, "round_result", "CT"),
        ),
    )
    event = _event(snapshot).model_copy(
        update={"type": "round_win", "facts": {**_event(snapshot).facts, "method": "elimination"}}
    )

    card = render_event_card(snapshot, _game(snapshot), situation, event)

    assert "20.0s 玩家开始燃烧" in card
    assert "22.4s 玩家燃烧结束 持续2.4秒" in card
    assert (
        "80.0s 玩家获得MVP（MVP+1） + "
        "灭队（本次焦点：回合胜利｜结算：灭队）"
    ) in card


def test_late_mvp_update_is_labeled_as_previous_round() -> None:
    snapshot = _real_snapshot()
    situation = replace(
        _situation(),
        timeline=(
            TimelineEntry(-3.0, "mvp", "上回合 MVP+1"),
            TimelineEntry(0.0, "round_live", None),
            TimelineEntry(20.0, "death", None),
        ),
    )

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))

    assert "-3.0s 玩家上回合获得MVP（MVP+1）" in card


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
    assert "89.8s" in card
    assert "存活89.8秒" not in card
    assert "1.4002890586853" not in card


def test_stale_round_situation_is_omitted_while_other_sections_remain() -> None:
    snapshot = _real_snapshot()

    card = render_event_card(
        snapshot,
        _game(snapshot),
        _situation(round_number=5),
        _event(snapshot),
    )

    assert "【本回合历史】" not in card
    assert "【游戏模式】" in card
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

    assert "【游戏模式】休闲" in card
    assert "【地图】de_anubis" in card
    assert "【回合】第6回合" in card
    assert "【比分】我方CT 2:3（落后）" in card
    assert "【我】" not in card
    assert "【全场】" not in card
    assert "【本回合历史】" in card
    assert "【刚刚】（唯一回应范围）" in card
    assert "余额9999" not in card
    assert "5血" not in card
    assert "99个击杀" not in card
    assert "99个爆头击杀" not in card


def test_existing_timeline_kinds_render_declared_text_except_hidden_reload() -> None:
    entries = (
        TimelineEntry(-2.0, "bought", None),
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(5.0, "flash_start", None),
        TimelineEntry(10.0, "flash_end", "持续0.8秒"),
        TimelineEntry(15.0, "smoke_start", None),
        TimelineEntry(20.0, "smoke_end", "持续6.8秒"),
        TimelineEntry(25.0, "kill", "AK47 爆头 弹匣仅剩1发"),
        TimelineEntry(30.0, "damage", "掉了35血 剩65血"),
        TimelineEntry(35.0, "primary_weapon", "换枪 AK47"),
        TimelineEntry(40.0, "ammo_low", "弹匣仅剩1发 AK47"),
        TimelineEntry(45.0, "reload", "换弹 AK47"),
        TimelineEntry(50.0, "grenade_used", "扔了闪光弹"),
        TimelineEntry(55.0, "grenade_pickup", "捡到烟雾弹"),
        TimelineEntry(60.0, "bomb", "已安放"),
        TimelineEntry(65.0, "bomb_pickup", None),
        TimelineEntry(70.0, "bomb_drop", None),
        TimelineEntry(75.0, "assist", None),
        TimelineEntry(80.0, "death", None),
    )
    snapshot = _real_snapshot()

    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(_situation(), timeline=entries),
        _event(snapshot),
    )

    expected = (
        "-2.0s 玩家购买装备（仅检测到金额与价值变化）",
        "0.0s 正式开打",
        "5.0s 玩家被闪",
        "10.0s 玩家闪光影响结束 持续0.8秒",
        "15.0s 玩家进烟",
        "20.0s 玩家出烟 持续6.8秒",
        "25.0s【前期】 玩家使用AK47完成击杀 爆头 弹匣仅剩1发（本回合第1杀）",
        "30.0s 玩家掉了35血 剩65血",
        "35.0s 玩家换枪 AK47",
        "40.0s 玩家弹匣仅剩1发 AK47",
        "50.0s 玩家扔了闪光弹",
        "55.0s 玩家捡到烟雾弹",
        "60.0s 炸弹已安放",
        "65.0s 玩家拿到包",
        "70.0s 玩家丢了包",
        "75.0s 玩家助攻",
        "80.0s【反攻包点】 "
        "玩家阵亡（本次焦点：普通死亡｜本回合1杀）",
    )
    assert all(text in card for text in expected)
    assert "换弹" not in card


def test_round_result_is_rendered_only_in_the_separate_focus_section() -> None:
    snapshot = _real_snapshot().model_copy(
        update={"round_phase": "over", "round_win_team": "T"}
    )
    event = _event(snapshot).model_copy(
        update={"type": "round_loss", "facts": {**_event(snapshot).facts, "method": "elimination"}}
    )
    situation = replace(
        _situation(),
        round_number=5,
        timeline=(
            TimelineEntry(0.0, "round_live", None),
            TimelineEntry(42.0, "death", None),
            TimelineEntry(57.4, "round_result", "T"),
        ),
    )

    card = render_event_card(snapshot, _game(snapshot), situation, event)

    assert (
        "57.4s 灭队"
        "（本次焦点：回合失败｜结算：灭队）"
    ) in card
    assert card.count("回合失败") == 1
    assert "\n【刚刚】（唯一回应范围）\n" in card


def test_round_result_deduplicates_matching_global_bomb_transition() -> None:
    snapshot = _real_snapshot().model_copy(
        update={"round_phase": "over", "round_win_team": "T"}
    )
    event = _event(snapshot).model_copy(
        update={"type": "round_loss", "facts": {**_event(snapshot).facts, "method": "defuse"}}
    )
    situation = replace(
        _situation(),
        round_number=5,
        timeline=(
            TimelineEntry(0.0, "round_live", None),
            TimelineEntry(57.4, "bomb", "已拆除"),
            TimelineEntry(57.4, "round_result", "T"),
        ),
    )

    card = render_event_card(snapshot, _game(snapshot), situation, event)

    assert (
        "57.4s 炸弹拆除"
        "（本次焦点：回合失败｜结算：炸弹拆除）"
    ) in card
    assert "炸弹已拆除" not in card
    assert card.count("炸弹拆除") == 2


def test_same_snapshot_uses_plus_without_a_redundant_zero_second_marker() -> None:
    snapshot = _real_snapshot()
    situation = replace(
        _situation(),
        timeline=(
            TimelineEntry(0.0, "round_live", None),
            TimelineEntry(18.0, "damage", "掉了100血 剩0血"),
            TimelineEntry(18.0, "death", None),
        ),
    )

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))

    assert (
        "18.0s【前期】 玩家掉了100血 剩0血 + "
        "阵亡（本次焦点：普通死亡）"
    ) in card
    assert "连续事件0.0秒" not in card


def test_unclosed_flash_and_smoke_render_as_still_active_not_normal_end() -> None:
    snapshot = _real_snapshot()
    situation = replace(
        _situation(),
        timeline=(
            TimelineEntry(0.0, "round_live", None),
            TimelineEntry(55.0, "flash_start", None),
            TimelineEntry(57.4, "flash_end", "未结束 已持续1.2秒"),
            TimelineEntry(58.0, "smoke_start", None),
            TimelineEntry(64.8, "smoke_end", "未结束 已持续6.8秒"),
        ),
    )

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))

    assert "57.4s 玩家受闪光影响未结束 已持续1.2秒" in card
    assert "64.8s 玩家仍在烟中 已持续6.8秒" in card
    assert "57.4s 闪光结束" not in card
    assert "64.8s 出烟" not in card


def test_timeline_without_round_live_declares_fallback_origin() -> None:
    snapshot = _real_snapshot()
    situation = replace(
        _situation(), timeline=(TimelineEntry(3.0, "damage", "掉了10血 剩90血"),)
    )

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))

    assert "【本回合历史】（仅供校验，禁止回应；未观测到开打时刻，秒数从回合起点算起）" in card
    timeline = card.split("【本回合历史】", 1)[1]
    assert "〔" not in timeline


def test_kill_and_death_timeline_entries_use_code_derived_stage_labels() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(14.9, "kill", "AK47"),
        TimelineEntry(15.0, "kill", "AK47"),
        TimelineEntry(30.0, "kill", "AK47"),
        TimelineEntry(70.0, "death", None),
        TimelineEntry(75.0, "bomb", "已安放"),
        TimelineEntry(76.0, "kill", "AK47"),
        TimelineEntry(77.0, "death", None),
    )
    situation = replace(_situation(), self_team="T", timeline=entries)

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))

    assert "15.0s【连续事件0.1秒｜前期】" in card
    assert "玩家使用AK47完成击杀（本回合第1杀）" in card
    assert "使用AK47完成击杀（本回合第2杀）" in card
    assert "30.0s【中期】 玩家使用AK47完成击杀（本回合第3杀）" in card
    assert "70.0s【后期】 玩家阵亡（本回合3杀）" in card
    assert "77.0s【连续事件2.0秒｜守包】" in card
    assert "使用AK47完成击杀（本回合第4杀）" in card
    assert (
        "阵亡（本次焦点：普通死亡｜本回合4杀｜"
        "击杀后被补枪，距击杀1.0秒）"
    ) in card


def test_close_timeline_entries_remain_complete_without_a_dense_marker() -> None:
    snapshot = _real_snapshot()
    entries = tuple(
        TimelineEntry(value, "assist", None) for value in (1.0, 2.0, 5.0)
    )

    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(_situation(), timeline=entries),
        _event(snapshot).model_copy(update={"type": "kill"}),
    )

    assert "密集" not in card
    assert "  1.0s 玩家助攻\n  2.0s 玩家助攻\n  5.0s 玩家助攻" in card


def test_continuous_group_uses_only_total_three_second_window_without_gap_limit() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(20.0, "damage", "掉了40血 剩60血"),
        TimelineEntry(22.5, "kill", "AK47 击杀时剩60血"),
    )

    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(_situation(), timeline=entries),
        _event(snapshot).model_copy(update={"type": "kill"}),
    )

    assert "22.5s【连续事件2.5秒｜前期】" in card
    assert (
        "玩家掉了40血 剩60血 > 使用AK47完成击杀"
        "（本次焦点：普通击杀｜本回合第1杀）"
    ) in card
    assert "击杀时剩60血" not in card
    assert "对枪胜利" not in card


def test_timeline_kill_labels_the_players_weapon_without_victim_ambiguity() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(1.0, "kill", "Galil AR"),
        TimelineEntry(2.0, "kill", "SSG 08 爆头 弹匣仅剩1发"),
        TimelineEntry(3.0, "kill", "增加2杀"),
    )

    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(_situation(), timeline=entries),
        _event(snapshot),
    )

    assert "3.0s【连续事件2.0秒｜阶段不可判断】" in card
    assert "玩家使用Galil AR完成击杀（本回合第1杀）" in card
    assert (
        "使用SSG 08完成击杀 爆头 弹匣仅剩1发"
        "（本回合第2杀）"
    ) in card
    assert (
        "完成击杀 增加2杀（本回合累计4杀｜该阶段四杀）"
    ) in card


def test_timeline_relations_are_calculated_before_the_model_reads_the_card() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(21.1, "damage", "掉了70血 剩30血"),
        TimelineEntry(21.3, "kill", "AK47 爆头 击杀时剩30血"),
        TimelineEntry(25.2, "smoke_end", "持续6.8秒"),
        TimelineEntry(25.7, "kill", "M4A1-S"),
        TimelineEntry(28.5, "kill", "M4A1-S"),
        TimelineEntry(29.0, "death", None),
    )

    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(_situation(), timeline=entries),
        _event(snapshot),
    )

    assert "21.3s【连续事件0.2秒｜前期】" in card
    assert "玩家掉了70血 剩30血 > 使用AK47完成击杀 爆头" in card
    assert "击杀时剩30血" not in card
    assert "对枪胜利，间隔0.2秒" in card
    assert "25.7s【连续事件0.5秒｜前期】" in card
    assert "玩家出烟 持续6.8秒 > 使用M4A1-S完成击杀" in card
    assert "摸烟击杀，出烟后0.5秒" in card
    assert "29.0s【连续事件0.5秒｜前期】" in card
    assert "使用M4A1-S完成击杀（本回合第3杀｜该阶段三杀）" in card
    assert (
        "阵亡（本次焦点：普通死亡｜本回合3杀｜"
        "击杀后被补枪，距击杀0.5秒）"
    ) in card


def test_event_card_has_no_required_facts_and_separates_focus_from_history() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(22.0, "kill", "AK47 爆头 击杀时满血"),
        TimelineEntry(40.0, "damage", "掉了62血 剩38血"),
        TimelineEntry(40.2, "kill", None),
        TimelineEntry(41.0, "death", None),
    )
    event = _event(snapshot).model_copy(
        update={"type": "multi_kill", "facts": {"count": 2}}
    )

    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(_situation(), timeline=entries),
        event,
    )

    assert "【事件必答】" not in card
    assert "〔必答" not in card
    assert "〔因字数丢弃〕" not in card
    assert card.count("【刚刚】") == 1
    assert "本次焦点：多杀" in card


def test_round_result_focus_does_not_add_a_historical_contribution_section() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(22.0, "kill", "AK47 爆头 击杀时满血"),
        TimelineEntry(40.0, "damage", "掉了62血 剩38血"),
        TimelineEntry(40.2, "kill", None),
        TimelineEntry(41.0, "death", None),
        TimelineEntry(42.0, "round_result", "T"),
    )
    event = _event(snapshot).model_copy(
        update={
            "type": "round_loss",
            "facts": {"method": "t_win_elimination"},
        }
    )

    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(_situation(), self_team="CT", timeline=entries),
        event,
    )

    assert "【事件必答】" not in card
    assert "【刚刚】（唯一回应范围）" in card
    assert "本次焦点：回合失败" in card
    assert "结算：灭队" in card


def test_round_result_focus_omits_adjacent_death_to_avoid_invented_causality() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(40.0, "death", None),
        TimelineEntry(40.0, "round_result", "T"),
    )
    event = _event(snapshot).model_copy(
        update={"type": "round_loss", "facts": {"method": "t_win_elimination"}}
    )

    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(_situation(), self_team="CT", timeline=entries),
        event,
    )
    focus = card.split("【刚刚】", 1)[1]

    assert "灭队（本次焦点：回合失败｜结算：灭队）" in focus
    assert "阵亡" not in focus


def test_death_focus_omits_grenade_to_avoid_guessing_damage_source() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(19.0, "grenade_used", "扔了手雷"),
        TimelineEntry(20.0, "damage", "掉了100血 剩0血"),
        TimelineEntry(20.0, "death", None),
    )
    event = _event(snapshot).model_copy(update={"type": "death_thrown_away"})

    card = render_event_card(
        snapshot, _game(snapshot), replace(_situation(), timeline=entries), event
    )
    focus = card.split("【刚刚】", 1)[1]

    assert "白给" in focus
    assert "扔了手雷" not in focus


def test_death_focus_labels_an_observed_lost_duel_and_empty_magazine() -> None:
    snapshot = _real_snapshot().model_copy(
        update={
            "health": 0,
            "weapons": (
                WeaponSlot("weapon_ak47", "Rifle", 0, 30, 60, "active"),
            ),
        }
    )
    situation = replace(
        _situation(),
        timeline=(
            TimelineEntry(0.0, "round_live", None),
            TimelineEntry(18.5, "damage", "掉了55血 剩0血"),
            TimelineEntry(20.0, "death", None),
        ),
        fire_seconds=(18.0,),
        last_readable_held_ammo_at_seconds=20.0,
        weapons_fired_this_round=frozenset({"weapon_ak47"}),
    )

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))
    focus = card.split("【刚刚】", 1)[1]

    assert "对枪输了" in focus
    assert "打空了还是没打过" in focus
    assert "一枪没开就没了" not in focus


def test_death_focus_labels_no_shot_only_with_readable_ammunition() -> None:
    snapshot = _real_snapshot().model_copy(update={"health": 0})
    entries = (TimelineEntry(0.0, "round_live", None), TimelineEntry(20.0, "death", None))
    readable = replace(
        _situation(),
        timeline=entries,
        fire_seconds=(),
        last_readable_held_ammo_at_seconds=19.5,
    )
    unreadable = replace(readable, last_readable_held_ammo_at_seconds=None)

    readable_card = render_event_card(
        snapshot, _game(snapshot), readable, _event(snapshot)
    )
    unreadable_card = render_event_card(
        snapshot, _game(snapshot), unreadable, _event(snapshot)
    )

    assert "一枪没开就没了" in readable_card
    assert "一枪没开就没了" not in unreadable_card


def test_death_focus_does_not_call_blind_fire_a_duel_or_no_shot() -> None:
    snapshot = _real_snapshot().model_copy(update={"health": 0})
    situation = replace(
        _situation(),
        timeline=(TimelineEntry(0.0, "round_live", None), TimelineEntry(20.0, "death", None)),
        fire_seconds=(19.0,),
        last_readable_held_ammo_at_seconds=20.0,
    )

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))

    assert "对枪输了" not in card
    assert "一枪没开就没了" not in card


def test_empty_magazine_death_requires_that_weapon_to_have_fired_this_round() -> None:
    snapshot = _real_snapshot().model_copy(
        update={
            "health": 0,
            "weapons": (WeaponSlot("weapon_ak47", "Rifle", 0, 30, 60, "active"),),
        }
    )
    situation = replace(
        _situation(),
        timeline=(TimelineEntry(20.0, "death", None),),
        last_readable_held_ammo_at_seconds=20.0,
        weapons_fired_this_round=frozenset(),
    )

    card = render_event_card(snapshot, _game(snapshot), situation, _event(snapshot))

    assert "打空了还是没打过" not in card


@pytest.mark.parametrize("death_type, existing_tag", [("death_after_kill", "击杀后被补枪"), ("death_thrown_away", "白给")])
def test_existing_death_tags_remain_when_combat_tags_also_apply(
    death_type: str, existing_tag: str
) -> None:
    snapshot = _real_snapshot().model_copy(update={"health": 0})
    situation = replace(
        _situation(),
        timeline=(
            TimelineEntry(18.0, "kill", "AK47"),
            TimelineEntry(19.0, "damage", "掉了100血 剩0血"),
            TimelineEntry(20.0, "death", None),
        ),
        fire_seconds=(19.5,),
        last_readable_held_ammo_at_seconds=20.0,
    )
    event = _event(snapshot).model_copy(update={"type": death_type})

    card = render_event_card(snapshot, _game(snapshot), situation, event)
    focus = card.split("【刚刚】", 1)[1]

    assert existing_tag in focus
    assert "对枪输了" in focus


def test_reload_detection_is_hidden_from_full_and_model_cards() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(20.0, "flash_start", None),
        TimelineEntry(20.5, "reload", "换弹 AK47 用时约2.1秒"),
        TimelineEntry(21.0, "ammo_low", "弹匣仅剩1发 AK47"),
        TimelineEntry(21.0, "damage", "掉了55血 剩45血"),
        TimelineEntry(21.0, "kill", "AK47 击杀时剩45血"),
        TimelineEntry(22.0, "flash_end", "持续2.0秒"),
    )

    situation = replace(_situation(), flash_count=1, timeline=entries)
    event = _event(snapshot).model_copy(update={"type": "kill"})
    card = render_event_card(
        snapshot,
        _game(snapshot),
        situation,
        event,
    )
    model_card = render_model_event_card(
        snapshot,
        _game(snapshot),
        situation,
        event,
    )

    assert "【事件必答】" not in card
    assert "玩家被闪" in card
    assert "换弹" not in card
    assert "换弹" not in model_card
    assert "弹匣仅剩1发 AK47" in card
    assert "本次焦点：普通击杀" in card


def test_round_result_keeps_only_one_focus_marker_without_required_facts() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(45.0, "bomb", "已安放"),
        TimelineEntry(52.0, "death", None),
        TimelineEntry(60.0, "round_result", "T"),
    )
    event = _event(snapshot).model_copy(
        update={"type": "round_loss", "facts": {"method": "t_win_elimination"}}
    )

    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(_situation(), self_team="CT", timeline=entries),
        event,
    )

    assert "【事件必答】" not in card
    assert card.count("【刚刚】") == 1
    assert "本次焦点：回合失败" in card


def test_scene_tags_cover_positive_and_negative_action_cases() -> None:
    snapshot = _real_snapshot().model_copy(update={"health": 20})
    kill = _event(snapshot).model_copy(update={"type": "kill"})
    positive_entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(1.0, "primary_weapon", "换枪 AK47"),
        TimelineEntry(1.5, "flash_start", None),
        TimelineEntry(1.8, "burn_start", None),
        TimelineEntry(2.0, "kill", "AK47 用弹1 击杀时剩20血"),
    )
    positive = render_event_card(
        snapshot, _game(snapshot), replace(_situation(), timeline=positive_entries), kill
    )
    assert all(
        tag in positive
        for tag in ("残血击杀", "白着打", "踩火杀", "一发命中", "换枪后立刻杀")
    )

    ended_flash = render_event_card(
        snapshot,
        _game(snapshot),
        replace(
            _situation(),
            timeline=(
                TimelineEntry(0.0, "round_live", None),
                TimelineEntry(1.0, "flash_start", None),
                TimelineEntry(2.0, "flash_end", "持续1.0秒"),
                TimelineEntry(3.0, "kill", "AK47 用弹0 击杀时剩20血"),
            ),
        ),
        kill,
    )
    assert "白着打" not in ended_flash
    assert "一发命中" not in ended_flash


@pytest.mark.parametrize(
    ("ammo_drop", "expected", "unexpected"),
    (
        (6, "打了半天", "一梭子扫死"),
        (9, "打了半天", "一梭子扫死"),
        (10, "一梭子扫死", "打了半天"),
    ),
)
def test_ammo_scene_tag_uses_the_four_product_tiers(
    ammo_drop: int, expected: str, unexpected: str
) -> None:
    snapshot = _real_snapshot()
    event = _event(snapshot).model_copy(update={"type": "kill"})
    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(
            _situation(),
            timeline=(
                TimelineEntry(0.0, "round_live", None),
                TimelineEntry(2.0, "kill", f"AK47 用弹{ammo_drop}"),
            ),
        ),
        event,
    )

    assert expected in card
    assert unexpected not in card


def test_scene_state_tags_require_thresholds_and_survival() -> None:
    snapshot = _real_snapshot().model_copy(update={"health": 20})
    event = _event(snapshot).model_copy(update={"type": "kill"})
    state = replace(
        _situation(),
        flash_count=3,
        longest_flash_seconds=1.5,
        burn_damage_taken=30,
        awp_miss_count=2,
        lowest_health_while_alive=20,
    )
    card = render_event_card(snapshot, _game(snapshot), state, event)
    assert all(tag in card for tag in ("白惨了", "烧惨了", "血皮撑住了", "连续空枪"))
    dead = render_event_card(snapshot.model_copy(update={"health": 0}), _game(snapshot), state, event)
    assert "血皮撑住了" not in dead


def test_timeline_code_labels_split_stage_multi_kills_without_model_arithmetic() -> None:
    snapshot = _real_snapshot()
    entries = (
        TimelineEntry(0.0, "round_live", None),
        TimelineEntry(21.0, "kill", "AK47"),
        TimelineEntry(45.0, "bomb", "已安放"),
        TimelineEntry(52.0, "kill", "AK47"),
        TimelineEntry(57.0, "kill", "AK47"),
    )

    card = render_event_card(
        snapshot,
        _game(snapshot),
        replace(_situation(), self_team="CT", timeline=entries),
        _event(snapshot),
    )

    assert "21.0s【前期】 玩家使用AK47完成击杀（本回合第1杀）" in card
    assert "52.0s【反攻包点】 玩家使用AK47完成击杀（本回合第2杀）" in card
    assert "57.0s【反攻包点】 玩家使用AK47完成击杀（本回合第3杀｜该阶段双杀）" in card
