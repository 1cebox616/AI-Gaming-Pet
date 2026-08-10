"""Round-situation accumulation and single-snapshot derivation tests."""

from collections.abc import Callable

import pytest

from pet.gsi import GSI_SILENCE_SECONDS, GameSnapshot, parse_snapshot
from pet.session import GameSessionTracker, GameState, MatchLifecycleTracker
from pet.situation import (
    RoundSituation,
    SituationTracker,
    armor_status,
    held_weapon,
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
    if active_weapon is not None:
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
        "round": {"phase": "live"},
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


def test_lowest_health_includes_zero() -> None:
    result = _observe_all(
        (
            _snapshot(1.0, health=100),
            _snapshot(2.0, health=24),
            _snapshot(3.0, health=0),
        )
    )

    assert result.lowest_health == 0


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


def test_weapon_switch_count_ignores_first_appearance() -> None:
    result = _observe_all(
        (
            _snapshot(1.0, active_weapon=None),
            _snapshot(2.0, active_weapon="weapon_ak47"),
            _snapshot(3.0, active_weapon="weapon_knife"),
            _snapshot(4.0, active_weapon="weapon_knife"),
            _snapshot(5.0, active_weapon="weapon_deagle"),
        )
    )

    assert result.weapon_switch_count == 2


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

    assert result == RoundSituation(2, 0, 0, 0, 100, None, 0, False)


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

    assert result == RoundSituation(1, 0, 0, 0, 100, None, 0, False)


PURE_FUNCTIONS: tuple[Callable[[GameSnapshot], object | None], ...] = (
    is_low_health,
    is_eco_round,
    is_low_ammo,
    armor_status,
    held_weapon,
    is_currently_flashed,
    is_currently_smoked,
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
