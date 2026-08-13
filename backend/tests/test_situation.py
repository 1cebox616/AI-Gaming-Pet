"""Round-situation accumulation and single-snapshot derivation tests."""

from collections.abc import Callable
import logging
from typing import get_args

import pytest

from pet.gsi import GSI_SILENCE_SECONDS, GameSnapshot, WeaponSlot, parse_snapshot
from pet.session import GameSessionTracker, GameState, MatchLifecycleTracker
from pet.situation import (
    RoundSituation,
    SituationTracker,
    TimelineEntry,
    TimelineKind,
    armor_status,
    held_weapon,
    is_carrying_bomb,
    is_currently_flashed,
    is_currently_smoked,
    is_eco_round,
    is_low_ammo,
    is_low_health,
    round_stage_label,
)

SELF_ID = "76561198000000001"
TEAMMATE_ID = "76561198000000999"


def _snapshot(
    ts: float,
    *,
    map_round: int = 0,
    round_phase: str | None = "live",
    player_id: str = SELF_ID,
    health: int | None = 100,
    flashed: int | None = 0,
    smoked: int | None = 0,
    burning: int | None = 0,
    armor: int | None = 100,
    helmet: bool | None = True,
    money: int | None = 4000,
    equip_value: int | None = 2000,
    active_weapon: str | None = "weapon_ak47",
    weapon_state: str = "active",
    ammo_clip: int | None = 30,
    ammo_reserve: int | None = 90,
    weapon_slots: tuple[tuple[str, str | None, str], ...] | None = None,
    bomb_state: str | None = None,
    round_kills: int | None = 0,
    round_killhs: int | None = 0,
    match_assists: int | None = 0,
    match_mvps: int | None = 0,
    team: str = "CT",
    has_defusekit: bool | None = None,
) -> GameSnapshot:
    state: dict[str, object] = {}
    for name, value in (
        ("health", health),
        ("flashed", flashed),
        ("smoked", smoked),
        ("burning", burning),
        ("armor", armor),
        ("helmet", helmet),
        ("money", money),
        ("equip_value", equip_value),
        ("round_kills", round_kills),
        ("round_killhs", round_killhs),
        ("defusekit", has_defusekit),
    ):
        if value is not None:
            state[name] = value

    weapons: dict[str, object] = {}
    if weapon_slots is not None:
        for index, (name, weapon_type, state_name) in enumerate(weapon_slots):
            weapons[f"weapon_{index}"] = {
                "name": name,
                "type": weapon_type,
                "state": state_name,
            }
    elif active_weapon is not None:
        weapon: dict[str, object] = {
            "name": active_weapon,
            "type": "Rifle",
            "state": weapon_state,
        }
        if ammo_clip is not None:
            weapon["ammo_clip"] = ammo_clip
        if ammo_reserve is not None:
            weapon["ammo_reserve"] = ammo_reserve
        weapons["weapon_0"] = weapon

    payload = {
        "provider": {"steamid": SELF_ID},
        "map": {
            "mode": "casual",
            "name": "de_anubis",
            "phase": "live",
            "round": map_round,
            "team_ct": {"score": 0},
            "team_t": {"score": 0},
        },
        "round": {
            **({"phase": round_phase} if round_phase is not None else {}),
            **({"bomb": bomb_state} if bomb_state is not None else {}),
        },
        "player": {
            "steamid": player_id,
            "activity": "playing",
            "team": team,
            "state": state,
            "weapons": weapons,
            "match_stats": {
                **({"assists": match_assists} if match_assists is not None else {}),
                **({"mvps": match_mvps} if match_mvps is not None else {}),
            },
        },
    }
    return parse_snapshot(payload, received_at=ts)


def _game(snapshot: GameSnapshot) -> GameState:
    return GameSessionTracker(GSI_SILENCE_SECONDS).observe(snapshot)


def _observe_all(snapshots: tuple[GameSnapshot, ...]) -> RoundSituation:
    tracker = SituationTracker()
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    current: RoundSituation | None = None
    for snapshot in snapshots:
        current = tracker.observe(snapshot, session.observe(snapshot))
    assert current is not None
    return current


def test_timeline_kind_contract_contains_exactly_twenty_three_values() -> None:
    assert get_args(TimelineKind) == (
        "bought",
        "round_live",
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
        "grenade_pickup",
        "bomb",
        "bomb_pickup",
        "bomb_drop",
        "assist",
        "mvp",
            "death",
            "round_result",
            "awp_miss",
    )


@pytest.mark.parametrize(
    ("seconds", "bomb_planted", "team", "expected"),
    (
        (0.0, False, "T", "开局"),
        (14.9, False, "CT", "开局"),
        (15.0, False, "T", "前期"),
        (29.9, False, "CT", "前期"),
        (30.0, False, "T", "中期"),
        (69.9, False, "CT", "中期"),
        (70.0, False, "T", "后期"),
        (50.0, True, "T", "守包"),
        (50.0, True, "CT", "反攻包点"),
        (50.0, True, None, "下包后"),
    ),
)
def test_round_stage_labels_follow_product_clock_and_postplant_roles(
    seconds: float,
    bomb_planted: bool,
    team: str | None,
    expected: str,
) -> None:
    assert (
        round_stage_label(
            seconds,
            bomb_planted=bomb_planted,
            self_team=team,
            observed_live=True,
        )
        == expected
    )


def test_round_stage_is_unknown_without_live_origin_or_before_live() -> None:
    assert (
        round_stage_label(
            20.0,
            bomb_planted=False,
            self_team="T",
            observed_live=False,
        )
        is None
    )
    assert (
        round_stage_label(
            -0.1,
            bomb_planted=False,
            self_team="T",
            observed_live=True,
        )
        is None
    )


def test_flash_count_tracks_zero_or_missing_to_positive_transitions() -> None:
    result = _observe_all(
        (
            _snapshot(1.0, flashed=0),
            _snapshot(2.0, flashed=90),
            _snapshot(3.0, flashed=20),
            _snapshot(4.0, flashed=None),
            _snapshot(5.0, flashed=70),
        )
    )

    assert result.flash_count == 2


def test_effect_durations_close_normally_from_adjacent_timestamps() -> None:
    result = _observe_all(
        (
            _snapshot(1.0, flashed=0, smoked=0),
            _snapshot(2.0, flashed=1, smoked=80),
            _snapshot(5.0, flashed=1, smoked=255),
            _snapshot(7.0, flashed=0, smoked=0),
        )
    )

    assert result.flashed_seconds_total == 5.0
    assert result.longest_flash_seconds == 5.0
    assert result.smoked_seconds_total == 5.0
    assert result.max_smoke_intensity == 255


def test_effect_durations_close_at_round_boundary_without_counting_gap() -> None:
    tracker = SituationTracker()
    first = _snapshot(1.0, map_round=0, flashed=1, smoked=100)
    tracker.observe(first, _game(first))
    last_self = _snapshot(4.0, map_round=0, flashed=1, smoked=100)
    before_boundary = tracker.observe(last_self, _game(last_self))
    next_round = _snapshot(20.0, map_round=1, flashed=1, smoked=100)

    after_boundary = tracker.observe(next_round, _game(next_round))

    assert before_boundary.flashed_seconds_total == 3.0
    assert before_boundary.smoked_seconds_total == 3.0
    assert after_boundary.round_number == 2
    assert after_boundary.flashed_seconds_total == 0.0
    assert after_boundary.smoked_seconds_total == 0.0


def test_effect_durations_close_when_subject_switches_to_teammate() -> None:
    tracker = SituationTracker()
    first = _snapshot(1.0, flashed=1, smoked=200)
    tracker.observe(first, _game(first))
    own = _snapshot(4.0, flashed=1, smoked=200)
    tracker.observe(own, _game(own))
    teammate = _snapshot(
        20.0,
        player_id=TEAMMATE_ID,
        flashed=1,
        smoked=255,
    )

    result = tracker.observe(teammate, _game(teammate))

    assert result.flashed_seconds_total == 3.0
    assert result.longest_flash_seconds == 3.0
    assert result.smoked_seconds_total == 3.0


def test_effect_durations_close_at_recording_end() -> None:
    tracker = SituationTracker()
    first = _snapshot(1.0, flashed=1, smoked=150)
    tracker.observe(first, _game(first))
    last = _snapshot(4.5, flashed=1, smoked=150)
    tracker.observe(last, _game(last))

    result = tracker.finish()

    assert result.flashed_seconds_total == 3.5
    assert result.longest_flash_seconds == 3.5
    assert result.smoked_seconds_total == 3.5


def test_missing_effect_fields_neither_open_nor_extend_intervals() -> None:
    result = _observe_all(
        (
            _snapshot(1.0, flashed=1, smoked=100),
            _snapshot(4.0, flashed=None, smoked=None),
            _snapshot(6.0, flashed=1, smoked=100),
            _snapshot(8.0, flashed=0, smoked=0),
        )
    )

    assert result.flash_count == 2
    assert result.flashed_seconds_total == 2.0
    assert result.longest_flash_seconds == 2.0
    assert result.smoked_seconds_total == 2.0


def test_burn_count_tracks_zero_or_missing_to_positive_transitions() -> None:
    result = _observe_all(
        (
            _snapshot(1.0, burning=0),
            _snapshot(2.0, burning=45),
            _snapshot(3.0, burning=0),
            _snapshot(4.0, burning=80),
            _snapshot(6.0, burning=0),
        )
    )

    assert result.burn_count == 2
    assert tuple(
        entry for entry in result.timeline if entry.kind in {"burn_start", "burn_end"}
    ) == (
        TimelineEntry(1.0, "burn_start", None),
        TimelineEntry(2.0, "burn_end", "持续1.0秒"),
        TimelineEntry(3.0, "burn_start", None),
        TimelineEntry(5.0, "burn_end", "持续2.0秒"),
    )


def test_match_mvp_increase_is_recorded_in_the_timeline() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, match_mvps=1),
            _snapshot(12.0, match_mvps=2),
        )
    )

    assert tuple(entry for entry in result.timeline if entry.kind == "mvp") == (
        TimelineEntry(2.0, "mvp", "MVP+1"),
    )


def test_mvp_update_published_in_next_freezetime_is_kept_as_previous_round_fact() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, map_round=0, round_phase="live", match_mvps=0),
            _snapshot(18.0, map_round=1, round_phase="over", match_mvps=None),
            _snapshot(20.0, map_round=1, round_phase="freezetime", match_mvps=1),
        )
    )

    assert result.round_number == 2
    assert tuple(entry for entry in result.timeline if entry.kind == "mvp") == (
        TimelineEntry(0.0, "mvp", "上回合 MVP+1"),
    )


def test_total_damage_taken_sums_only_health_drops() -> None:
    result = _observe_all(
        (
            _snapshot(1.0, health=100),
            _snapshot(2.0, health=73),
            _snapshot(3.0, health=80),
            _snapshot(4.0, health=40),
        )
    )

    assert result.total_damage_taken == 67


def test_lowest_health_while_alive_stays_at_full_health() -> None:
    result = _observe_all((_snapshot(1.0, health=100), _snapshot(2.0, health=100)))

    assert result.lowest_health_while_alive == 100


def test_lowest_health_while_alive_excludes_death_zero() -> None:
    result = _observe_all(
        (
            _snapshot(1.0, health=100),
            _snapshot(2.0, health=12),
            _snapshot(3.0, health=0),
        )
    )

    assert result.lowest_health_while_alive == 12


def test_lowest_health_while_alive_is_unknown_without_health_data() -> None:
    result = _observe_all((_snapshot(1.0, health=None), _snapshot(2.0, health=None)))

    assert result.lowest_health_while_alive is None


def test_health_before_death_keeps_last_nonzero_value() -> None:
    result = _observe_all(
        (
            _snapshot(1.0, health=100),
            _snapshot(2.0, health=31),
            _snapshot(3.0, health=None),
            _snapshot(4.0, health=0),
        )
    )

    assert result.health_before_death == 31


def test_primary_weapons_used_filters_deduplicates_and_preserves_first_order() -> None:
    result = _observe_all(
        (
            _snapshot(
                1.0,
                weapon_slots=(
                    ("weapon_knife", "Knife", "active"),
                    ("weapon_ak47", "Rifle", "holstered"),
                    ("weapon_flashbang", "Grenade", "holstered"),
                ),
            ),
            _snapshot(
                2.0,
                weapon_slots=(
                    ("weapon_ak47", "Rifle", "holstered"),
                    ("weapon_awp", "SniperRifle", "active"),
                    ("weapon_deagle", "Pistol", "holstered"),
                ),
            ),
        )
    )

    assert result.primary_weapons_used == ("weapon_ak47", "weapon_awp")
    assert result.timeline[0] == TimelineEntry(0.0, "primary_weapon", "AK47")
    assert result.timeline[1] == TimelineEntry(
        1.0, "primary_weapon", "换枪 AWP"
    )


def test_unknown_weapon_type_warns_once_and_is_not_primary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="pet.situation")
    snapshots = (
        _snapshot(
            1.0,
            weapon_slots=(("weapon_future", "FutureWeapon", "active"),),
        ),
        _snapshot(
            2.0,
            weapon_slots=(("weapon_future", "FutureWeapon", "active"),),
        ),
    )

    result = _observe_all(snapshots)

    assert result.primary_weapons_used == ()
    assert caplog.text.count("FutureWeapon") == 1


def test_bought_equipment_requires_money_down_and_value_up_together() -> None:
    result = _observe_all(
        (
            _snapshot(1.0, money=5000, equip_value=1000),
            _snapshot(2.0, money=4500, equip_value=1000),
            _snapshot(3.0, money=3000, equip_value=2500),
        )
    )

    assert result.bought_equipment is True


def test_spectated_teammate_does_not_change_current_situation() -> None:
    tracker = SituationTracker()
    own = _snapshot(1.0, health=64, flashed=0)
    before = tracker.observe(own, _game(own))
    teammate = _snapshot(
        2.0,
        player_id=TEAMMATE_ID,
        health=5,
        flashed=200,
        burning=100,
        active_weapon="weapon_deagle",
    )

    after = tracker.observe(teammate, _game(teammate))

    assert after == before


def test_human_round_change_resets_all_accumulations() -> None:
    tracker = SituationTracker()
    first_round = _snapshot(1.0, map_round=0, health=20, flashed=90)
    tracker.observe(first_round, _game(first_round))
    next_round = _snapshot(2.0, map_round=1, health=100, flashed=0)

    result = tracker.observe(next_round, _game(next_round))

    assert result.round_number == 2
    assert result.flash_count == 0
    assert result.total_damage_taken == 0
    assert result.lowest_health_while_alive == 100
    assert result.primary_weapons_used == ("weapon_ak47",)


def test_match_lifecycle_signal_resets_tracker_before_new_match() -> None:
    tracker = SituationTracker()
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    lifecycle = MatchLifecycleTracker()
    playing = _snapshot(1.0, health=18, flashed=100)
    game = session.observe(playing)
    assert lifecycle.observe(game) is False
    tracker.observe(playing, game)

    menu = parse_snapshot(
        {
            "provider": {"steamid": SELF_ID},
            "player": {"steamid": SELF_ID, "activity": "menu"},
        },
        received_at=2.0,
    )
    menu_game = session.observe(menu)
    assert lifecycle.observe(menu_game) is True
    tracker.reset()
    tracker.observe(menu, menu_game)

    new_match = _snapshot(3.0, health=100, flashed=0)
    new_game = session.observe(new_match)
    if lifecycle.observe(new_game):
        tracker.reset()
    result = tracker.observe(new_match, new_game)

    assert result.round_number == 1
    assert result.flash_count == 0
    assert result.total_damage_taken == 0
    assert result.lowest_health_while_alive == 100


def test_bomb_plant_time_and_elapsed_seconds_use_first_planted_snapshot() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, bomb_state=None),
            _snapshot(12.0, bomb_state="planted"),
            _snapshot(17.5, bomb_state="planted"),
            _snapshot(20.0, bomb_state="defused"),
        )
    )

    assert result.bomb_planted_at_ts == 12.0
    assert result.seconds_since_bomb_planted == 8.0


def test_bomb_clock_continues_during_teammate_spectating() -> None:
    tracker = SituationTracker()
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    own = _snapshot(10.0, bomb_state=None)
    tracker.observe(own, session.observe(own))
    planted_while_spectating = _snapshot(
        20.0,
        player_id=TEAMMATE_ID,
        health=80,
        bomb_state="planted",
    )
    tracker.observe(
        planted_while_spectating,
        session.observe(planted_while_spectating),
    )
    later = _snapshot(
        27.5,
        player_id=TEAMMATE_ID,
        health=80,
        bomb_state="planted",
    )

    result = tracker.observe(later, session.observe(later))

    assert result.bomb_planted_at_ts == 20.0
    assert result.seconds_since_bomb_planted == 7.5
    assert TimelineEntry(10.0, "bomb", "已安放") in result.timeline


def test_timeline_records_state_changes_in_time_order_with_human_details() -> None:
    result = _observe_all(
        (
            _snapshot(
                10.0,
                money=5000,
                equip_value=1000,
                health=100,
                round_kills=0,
                round_killhs=0,
            ),
            _snapshot(
                11.0,
                money=3000,
                equip_value=3000,
                health=90,
                flashed=1,
                smoked=255,
                round_kills=0,
                round_killhs=0,
            ),
            _snapshot(
                11.5,
                money=3000,
                equip_value=3000,
                health=80,
                flashed=1,
                smoked=255,
                round_kills=0,
                round_killhs=0,
            ),
            _snapshot(
                12.0,
                money=3000,
                equip_value=3000,
                health=80,
                flashed=0,
                smoked=0,
                round_kills=1,
                round_killhs=1,
                active_weapon="weapon_awp",
                bomb_state="planted",
            ),
            _snapshot(
                13.0,
                money=3000,
                equip_value=3000,
                health=0,
                round_kills=1,
                round_killhs=1,
                active_weapon="weapon_awp",
                bomb_state="defused",
            ),
        )
    )

    assert tuple(entry.kind for entry in result.timeline) == (
        "primary_weapon",
        "bought",
        "flash_start",
        "smoke_start",
        "damage",
        "primary_weapon",
        "flash_end",
        "smoke_end",
        "kill",
        "bomb",
        "damage",
        "bomb",
        "death",
    )
    assert result.timeline[0] == TimelineEntry(0.0, "primary_weapon", "AK47")
    assert result.timeline[5] == TimelineEntry(
        2.0, "primary_weapon", "换枪 AWP"
    )
    assert result.timeline[4] == TimelineEntry(1.5, "damage", "掉了20血 剩80血")
    assert result.timeline[6] == TimelineEntry(2.0, "flash_end", "持续1.0秒")
    assert result.timeline[7] == TimelineEntry(2.0, "smoke_end", "持续1.0秒")
    assert result.timeline[8] == TimelineEntry(2.0, "kill", "AWP 爆头")
    assert result.timeline[-1] == TimelineEntry(3.0, "death", None)


def test_round_live_records_only_freezetime_to_live_transition() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, round_phase="freezetime"),
            _snapshot(11.0, round_phase="freezetime"),
            _snapshot(12.0, round_phase="live"),
            _snapshot(13.0, round_phase="live"),
        )
    )

    live_entries = tuple(
        entry for entry in result.timeline if entry.kind == "round_live"
    )
    assert live_entries == (TimelineEntry(0.0, "round_live", None),)


def test_round_live_rebases_purchase_phase_entries_to_negative_seconds() -> None:
    result = _observe_all(
        (
            _snapshot(
                10.0,
                round_phase="freezetime",
                money=5000,
                equip_value=1000,
            ),
            _snapshot(
                11.0,
                round_phase="freezetime",
                money=3000,
                equip_value=3000,
            ),
            _snapshot(
                12.0,
                round_phase="live",
                money=3000,
                equip_value=3000,
            ),
        )
    )

    purchase = next(entry for entry in result.timeline if entry.kind == "bought")
    live = next(entry for entry in result.timeline if entry.kind == "round_live")
    assert purchase == TimelineEntry(-1.0, "bought", None)
    assert live == TimelineEntry(0.0, "round_live", None)


def test_warmup_combat_does_not_pollute_the_first_real_round_timeline() -> None:
    tracker = SituationTracker()
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    warmup = _snapshot(
        10.0,
        map_round=0,
        round_phase="live",
        round_kills=3,
        health=36,
    ).model_copy(update={"map_phase": "warmup"})
    freeze = _snapshot(
        80.0,
        map_round=0,
        round_phase="freezetime",
        round_kills=0,
        health=100,
    )
    live = _snapshot(
        85.0,
        map_round=0,
        round_phase="live",
        round_kills=0,
        health=100,
    )

    warmup_result = tracker.observe(warmup, session.observe(warmup))
    tracker.observe(freeze, session.observe(freeze))
    result = tracker.observe(live, session.observe(live))

    assert warmup_result.timeline == ()
    assert TimelineEntry(0.0, "round_live", None) in result.timeline
    assert all(entry.seconds >= -5.0 for entry in result.timeline)
    assert all(entry.kind not in {"kill", "damage", "death"} for entry in result.timeline)


def test_grenade_disappearance_records_only_after_live_and_not_on_death() -> None:
    rifle_and_grenades = (
        ("weapon_ak47", "Rifle", "active"),
        ("weapon_flashbang", "Grenade", "holstered"),
        ("weapon_smokegrenade", "Grenade", "holstered"),
    )
    result = _observe_all(
        (
            _snapshot(
                10.0,
                round_phase="freezetime",
                weapon_slots=rifle_and_grenades,
            ),
            _snapshot(
                11.0,
                round_phase="freezetime",
                weapon_slots=rifle_and_grenades[:-1],
            ),
            _snapshot(12.0, round_phase="live", weapon_slots=rifle_and_grenades),
            _snapshot(
                13.0,
                round_phase="live",
                weapon_slots=rifle_and_grenades[:-1],
            ),
            _snapshot(14.0, round_phase="live", health=0, weapon_slots=()),
        )
    )

    grenades = tuple(entry for entry in result.timeline if entry.kind == "grenade_used")
    assert grenades == (TimelineEntry(1.0, "grenade_used", "扔了烟雾弹"),)
    assert result.grenades_used == (("weapon_smokegrenade", 1),)


def test_grenade_pickup_records_only_after_round_is_live() -> None:
    rifle = (("weapon_ak47", "Rifle", "active"),)
    with_flash = rifle + (("weapon_flashbang", "Grenade", "holstered"),)
    with_two_grenades = with_flash + (
        ("weapon_smokegrenade", "Grenade", "holstered"),
    )
    result = _observe_all(
        (
            _snapshot(10.0, round_phase="freezetime", weapon_slots=rifle),
            _snapshot(11.0, round_phase="freezetime", weapon_slots=with_flash),
            _snapshot(12.0, round_phase="live", weapon_slots=with_flash),
            _snapshot(13.0, round_phase="live", weapon_slots=with_two_grenades),
        )
    )

    pickups = tuple(
        entry for entry in result.timeline if entry.kind == "grenade_pickup"
    )
    assert pickups == (TimelineEntry(1.0, "grenade_pickup", "捡到烟雾弹"),)


def test_ammo_low_rearms_after_weapon_switch_and_reload() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, active_weapon="weapon_ak47", ammo_clip=30),
            _snapshot(11.0, active_weapon="weapon_ak47", ammo_clip=1),
            _snapshot(12.0, active_weapon="weapon_ak47", ammo_clip=0),
            _snapshot(13.0, active_weapon="weapon_m4a1_silencer", ammo_clip=20),
            _snapshot(14.0, active_weapon="weapon_m4a1_silencer", ammo_clip=1),
            _snapshot(15.0, active_weapon="weapon_m4a1_silencer", ammo_clip=20),
            _snapshot(16.0, active_weapon="weapon_m4a1_silencer", ammo_clip=0),
        )
    )

    low_ammo = tuple(entry for entry in result.timeline if entry.kind == "ammo_low")
    assert low_ammo == (
        TimelineEntry(1.0, "ammo_low", "弹匣仅剩1发 AK47"),
        TimelineEntry(4.0, "ammo_low", "弹匣仅剩1发 M4A1-S"),
        TimelineEntry(6.0, "ammo_low", "弹匣打空 M4A1-S"),
    )


def test_reload_duration_uses_reloading_state_transitions() -> None:
    completed = _observe_all(
        (
            _snapshot(10.0, ammo_clip=3, ammo_reserve=60),
            _snapshot(
                11.0,
                weapon_state="reloading",
                ammo_clip=3,
                ammo_reserve=60,
            ),
            _snapshot(
                12.4,
                weapon_state="reloading",
                ammo_clip=30,
                ammo_reserve=33,
            ),
            _snapshot(13.0, ammo_clip=30, ammo_reserve=33),
        )
    )
    interrupted = _observe_all(
        (
            _snapshot(10.0, ammo_clip=3, ammo_reserve=60),
            _snapshot(
                11.0,
                weapon_state="reloading",
                ammo_clip=3,
                ammo_reserve=60,
            ),
            _snapshot(12.0, active_weapon="weapon_knife", ammo_clip=None),
        )
    )

    completed_reloads = tuple(
        entry for entry in completed.timeline if entry.kind == "reload"
    )
    interrupted_reloads = tuple(
        entry for entry in interrupted.timeline if entry.kind == "reload"
    )
    assert completed_reloads == (
        TimelineEntry(3.0, "reload", "换弹 AK47 用时约2.0秒"),
    )
    assert interrupted_reloads == (
        TimelineEntry(2.0, "reload", "换弹 AK47 未完成 已持续1.0秒"),
    )


def test_kill_detail_includes_low_ammo_at_that_snapshot() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, round_kills=0, ammo_clip=10),
            _snapshot(11.0, round_kills=1, ammo_clip=2),
        )
    )

    kills = tuple(entry for entry in result.timeline if entry.kind == "kill")
    assert kills == (TimelineEntry(1.0, "kill", "AK47 用弹8 击杀时满血 弹匣仅剩2发"),)


def test_damage_detail_omits_armor_and_merges_health_loss() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, health=100, armor=100),
            _snapshot(11.0, health=65, armor=88),
            _snapshot(11.5, health=50, armor=85),
        )
    )

    damage = tuple(entry for entry in result.timeline if entry.kind == "damage")
    assert damage == (
        TimelineEntry(1.5, "damage", "掉了50血 剩50血"),
    )


def test_bomb_pickup_and_alive_drop_are_recorded_but_death_is_not_a_drop() -> None:
    rifle = (("weapon_ak47", "Rifle", "active"),)
    rifle_and_bomb = rifle + (("weapon_c4", "C4", "holstered"),)
    alive_drop = _observe_all(
        (
            _snapshot(10.0, weapon_slots=rifle),
            _snapshot(11.0, weapon_slots=rifle_and_bomb),
            _snapshot(12.0, health=100, weapon_slots=rifle),
        )
    )
    death = _observe_all(
        (
            _snapshot(10.0, weapon_slots=rifle_and_bomb),
            _snapshot(11.0, health=0, weapon_slots=()),
        )
    )

    bomb_entries = tuple(
        entry
        for entry in alive_drop.timeline
        if entry.kind in {"bomb_pickup", "bomb_drop"}
    )
    assert bomb_entries == (
        TimelineEntry(1.0, "bomb_pickup", None),
        TimelineEntry(2.0, "bomb_drop", None),
    )
    assert all(entry.kind != "bomb_drop" for entry in death.timeline)


def test_spectating_teammate_still_records_global_round_result() -> None:
    tracker = SituationTracker()
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    live = _snapshot(10.0, map_round=0)
    tracker.observe(live, session.observe(live))
    result_snapshot = _snapshot(
        52.0,
        map_round=1,
        round_phase="over",
        player_id=TEAMMATE_ID,
        health=80,
    ).model_copy(update={"round_win_team": "T"})

    result = tracker.observe(result_snapshot, session.observe(result_snapshot))

    assert result.timeline[-1] == TimelineEntry(42.0, "round_result", "T")


def test_assist_records_each_match_assist_increase() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, match_assists=2),
            _snapshot(11.0, match_assists=3),
            _snapshot(12.0, match_assists=5),
        )
    )

    assists = tuple(entry for entry in result.timeline if entry.kind == "assist")
    assert assists == (
        TimelineEntry(1.0, "assist", None),
        TimelineEntry(2.0, "assist", None),
    )


def test_death_closes_active_flash_and_smoke_as_unfinished_intervals() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, flashed=0, smoked=0, health=100),
            _snapshot(11.0, flashed=1, smoked=200, health=100),
            _snapshot(12.0, flashed=1, smoked=200, health=0),
        )
    )

    assert TimelineEntry(
        2.0, "flash_end", "未结束 已持续1.0秒"
    ) in result.timeline
    assert TimelineEntry(
        2.0, "smoke_end", "未结束 已持续1.0秒"
    ) in result.timeline
    assert result.timeline[-1] == TimelineEntry(2.0, "death", None)


def test_purchase_merge_uses_consecutive_gap_and_exact_three_seconds_is_boundary() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, money=5000, equip_value=1000),
            _snapshot(11.0, money=4500, equip_value=1500),
            _snapshot(13.9, money=4000, equip_value=2000),
            _snapshot(16.9, money=3500, equip_value=2500),
        )
    )

    purchases = tuple(entry for entry in result.timeline if entry.kind == "bought")
    assert purchases[0] == TimelineEntry(1.0, "bought", None)
    assert purchases[1].kind == "bought"
    assert purchases[1].detail is None
    assert purchases[1].seconds == pytest.approx(6.9)


def test_damage_entries_at_least_one_second_apart_do_not_merge() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, health=100),
            _snapshot(11.0, health=90),
            _snapshot(12.0, health=80),
        )
    )

    damage = tuple(entry for entry in result.timeline if entry.kind == "damage")
    assert damage == (
        TimelineEntry(1.0, "damage", "掉了10血 剩90血"),
        TimelineEntry(2.0, "damage", "掉了10血 剩80血"),
    )


def test_awp_miss_is_recorded_only_without_a_kill() -> None:
    missed = _observe_all(
            (
                _snapshot(10.0, active_weapon="weapon_awp", ammo_clip=3),
                _snapshot(11.0, active_weapon="weapon_awp", ammo_clip=2),
                _snapshot(12.0, active_weapon="weapon_awp", ammo_clip=2),
            )
    )
    killed = _observe_all(
        (
            _snapshot(10.0, active_weapon="weapon_awp", ammo_clip=3),
            _snapshot(11.0, active_weapon="weapon_awp", ammo_clip=2, round_kills=1),
        )
    )

    assert tuple(entry.kind for entry in missed.timeline) == (
        "primary_weapon",
        "awp_miss",
    )
    assert missed.awp_miss_count == 1
    assert all(entry.kind != "awp_miss" for entry in killed.timeline)


def test_awp_miss_compares_inventory_clip_after_switching_away() -> None:
    first = _snapshot(10.0).model_copy(
        update={
            "weapons": (
                WeaponSlot("weapon_awp", "SniperRifle", 3, 5, 0, "active"),
                WeaponSlot("weapon_glock", "Pistol", 20, 20, 120, "holstered"),
            )
        }
    )
    shot = _snapshot(11.0).model_copy(
        update={
            "weapons": (
                WeaponSlot("weapon_awp", "SniperRifle", 2, 5, 0, "holstered"),
                WeaponSlot("weapon_glock", "Pistol", 20, 20, 120, "active"),
            )
        }
    )
    missed = _observe_all((first, shot, shot.model_copy(update={"ts": 12.0})))

    assert missed.awp_miss_count == 1
    assert any(entry.kind == "awp_miss" for entry in missed.timeline)

    killed = _observe_all(
        (
            first,
            shot,
            shot.model_copy(update={"ts": 12.0, "round_kills": 1}),
        )
    )
    assert killed.awp_miss_count == 0
    assert all(entry.kind != "awp_miss" for entry in killed.timeline)


def test_kill_ammo_count_accumulates_across_contiguous_clip_drops() -> None:
    result = _observe_all(
        (
            _snapshot(10.0, ammo_clip=30),
            _snapshot(11.0, ammo_clip=27),
            _snapshot(12.0, ammo_clip=25),
            _snapshot(13.0, ammo_clip=22, round_kills=1),
        )
    )

    kill = next(entry for entry in result.timeline if entry.kind == "kill")
    assert kill.detail is not None and "用弹8" in kill.detail


def test_burn_damage_is_conservatively_counted_only_inside_burning_interval() -> None:
    inside = _observe_all(
        (
            _snapshot(10.0, health=100, burning=100),
            _snapshot(11.0, health=70, burning=100),
            _snapshot(12.0, health=70, burning=0),
        )
    )
    boundary = _observe_all(
        (
            _snapshot(10.0, health=100, burning=100),
            _snapshot(11.0, health=70, burning=0),
        )
    )

    assert inside.burn_damage_taken == 30
    assert boundary.burn_damage_taken == 0


def test_timeline_caps_at_25_by_dropping_earliest_damage_and_adding_note() -> None:
    snapshots = tuple(
        _snapshot(10.0 + index * 1.1, health=100 - index)
        for index in range(30)
    )

    result = _observe_all(snapshots)

    assert len(result.timeline) == 25
    assert result.timeline[0] == TimelineEntry(0.0, "primary_weapon", "AK47")
    assert result.timeline[-1].kind == "damage"
    assert result.timeline[-1].detail == "较早的6条受伤记录已省略"


def test_self_team_updates_only_from_self_and_resets_with_round() -> None:
    tracker = SituationTracker()
    self_snapshot = _snapshot(1.0, team="CT")
    own = tracker.observe(self_snapshot, _game(self_snapshot))
    teammate = _snapshot(2.0, player_id=TEAMMATE_ID, team="T")

    spectating = tracker.observe(teammate, _game(teammate))
    next_round = _snapshot(3.0, map_round=1, team="T")
    reset = tracker.observe(next_round, _game(next_round))

    assert own.self_team == "CT"
    assert spectating.self_team == "CT"
    assert reset.self_team == "T"
    assert reset.timeline[0] == TimelineEntry(0.0, "primary_weapon", "AK47")


PURE_FUNCTIONS: tuple[Callable[[GameSnapshot], object | None], ...] = (
    is_low_health,
    is_eco_round,
    is_low_ammo,
    armor_status,
    held_weapon,
    is_currently_flashed,
    is_currently_smoked,
    is_carrying_bomb,
)


@pytest.mark.parametrize("derive", PURE_FUNCTIONS, ids=lambda derive: derive.__name__)
def test_pure_derivations_return_none_when_dependencies_are_missing(
    derive: Callable[[GameSnapshot], object | None],
) -> None:
    snapshot = parse_snapshot(
        {
            "provider": {"steamid": SELF_ID},
            "player": {"steamid": SELF_ID, "activity": "playing"},
        },
        received_at=1.0,
    )

    assert derive(snapshot) is None


def test_pure_derivations_apply_declared_thresholds_and_weapon_state() -> None:
    snapshot = _snapshot(
        1.0,
        health=30,
        money=1499,
        equip_value=1999,
        armor=50,
        helmet=False,
        flashed=1,
        smoked=1,
        active_weapon="weapon_deagle",
        ammo_clip=1,
    )

    assert is_low_health(snapshot) is True
    assert is_eco_round(snapshot) is True
    assert is_low_ammo(snapshot) is True
    assert armor_status(snapshot) == "有甲"
    assert held_weapon(snapshot) is not None
    assert held_weapon(snapshot).name == "weapon_deagle"
    assert is_currently_flashed(snapshot) is True
    assert is_currently_smoked(snapshot) is True


def test_is_carrying_bomb_distinguishes_missing_known_and_c4_lists() -> None:
    missing = parse_snapshot(
        {
            "provider": {"steamid": SELF_ID},
            "player": {"steamid": SELF_ID, "activity": "playing"},
        },
        received_at=1.0,
    )
    no_c4 = _snapshot(
        2.0,
        weapon_slots=(("weapon_ak47", "Rifle", "active"),),
    )
    with_c4 = _snapshot(
        3.0,
        weapon_slots=(
            ("weapon_ak47", "Rifle", "active"),
            ("weapon_c4", "C4", "holstered"),
        ),
    )

    assert is_carrying_bomb(missing) is None
    assert is_carrying_bomb(no_c4) is False
    assert is_carrying_bomb(with_c4) is True
