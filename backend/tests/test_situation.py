"""Round-situation accumulation and single-snapshot derivation tests."""

from collections.abc import Callable
import logging

import pytest

from pet.gsi import GSI_SILENCE_SECONDS, GameSnapshot, parse_snapshot
from pet.session import GameSessionTracker, GameState, MatchLifecycleTracker
from pet.situation import (
    RoundSituation,
    SituationTracker,
    armor_status,
    held_weapon,
    is_carrying_bomb,
    is_currently_flashed,
    is_currently_smoked,
    is_eco_round,
    is_low_ammo,
    is_low_health,
)

SELF_ID = "76561198000000001"
TEAMMATE_ID = "76561198000000999"


def _snapshot(
    ts: float,
    *,
    map_round: int = 0,
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
    ammo_clip: int | None = 30,
    weapon_slots: tuple[tuple[str, str | None, str], ...] | None = None,
    bomb_state: str | None = None,
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
            "state": "active",
        }
        if ammo_clip is not None:
            weapon["ammo_clip"] = ammo_clip
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
            "phase": "live",
            **({"bomb": bomb_state} if bomb_state is not None else {}),
        },
        "player": {
            "steamid": player_id,
            "activity": "playing",
            "team": "CT",
            "state": state,
            "weapons": weapons,
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
        )
    )

    assert result.burn_count == 2


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
    assert armor_status(snapshot) == "有甲无头"
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
