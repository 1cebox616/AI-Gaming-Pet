"""Accumulate current-round CS2 facts without detecting discrete events."""

from __future__ import annotations

from dataclasses import dataclass, replace

from pet.gsi import GameSnapshot, WeaponSlot, human_round_number
from pet.session import GameState

# Thirty health is the common CS threshold where one more solid hit is lethal.
LOW_HEALTH_THRESHOLD = 30
# Below 1500 cash leaves few meaningful full-buy options in the next purchase.
ECO_MONEY_THRESHOLD = 1500
# Below 2000 equipment value represents a materially light current loadout.
ECO_EQUIP_THRESHOLD = 2000
# One chambered round is the last-shot warning point before an empty magazine.
LOW_AMMO_THRESHOLD = 1


@dataclass(frozen=True, slots=True)
class RoundSituation:
    """Per-round accumulations that a single snapshot cannot express."""

    round_number: int | None
    flash_count: int
    burn_count: int
    total_damage_taken: int
    lowest_health: int | None
    health_before_death: int | None
    weapon_switch_count: int
    bought_equipment: bool


class SituationTracker:
    """Fold ordered self-owned snapshots into one current-round situation."""

    def __init__(self) -> None:
        self.reset()

    def observe(self, snapshot: GameSnapshot, game: GameState) -> RoundSituation:
        """Fold one snapshot into the running per-round accumulations."""
        if game.subject_is_self is not True:
            return self._current

        round_number = human_round_number(snapshot)
        if round_number is not None and round_number != self._current.round_number:
            self._start_round(round_number)

        flash_count = self._current.flash_count
        if (self._previous_flashed is None or self._previous_flashed == 0) and (
            snapshot.flashed is not None and snapshot.flashed > 0
        ):
            flash_count += 1

        burn_count = self._current.burn_count
        if (self._previous_burning is None or self._previous_burning == 0) and (
            snapshot.burning is not None and snapshot.burning > 0
        ):
            burn_count += 1

        total_damage_taken = self._current.total_damage_taken
        if (
            self._previous_health is not None
            and snapshot.health is not None
            and snapshot.health < self._previous_health
        ):
            total_damage_taken += self._previous_health - snapshot.health

        lowest_health = self._current.lowest_health
        if snapshot.health is not None and (
            lowest_health is None or snapshot.health < lowest_health
        ):
            lowest_health = snapshot.health

        health_before_death = self._current.health_before_death
        if snapshot.health == 0 and self._last_nonzero_health is not None:
            health_before_death = self._last_nonzero_health

        weapon_switch_count = self._current.weapon_switch_count
        if (
            snapshot.active_weapon is not None
            and self._previous_active_weapon is not None
            and snapshot.active_weapon != self._previous_active_weapon
        ):
            weapon_switch_count += 1

        bought_equipment = self._current.bought_equipment
        if (
            self._previous_money is not None
            and snapshot.money is not None
            and self._previous_equip_value is not None
            and snapshot.equip_value is not None
            and snapshot.money < self._previous_money
            and snapshot.equip_value > self._previous_equip_value
        ):
            bought_equipment = True

        self._current = replace(
            self._current,
            flash_count=flash_count,
            burn_count=burn_count,
            total_damage_taken=total_damage_taken,
            lowest_health=lowest_health,
            health_before_death=health_before_death,
            weapon_switch_count=weapon_switch_count,
            bought_equipment=bought_equipment,
        )
        self._previous_flashed = snapshot.flashed
        self._previous_burning = snapshot.burning
        self._previous_health = snapshot.health
        if snapshot.health is not None and snapshot.health > 0:
            self._last_nonzero_health = snapshot.health
        if snapshot.active_weapon is not None:
            self._previous_active_weapon = snapshot.active_weapon
        self._previous_money = snapshot.money
        self._previous_equip_value = snapshot.equip_value
        return self._current

    def reset(self) -> None:
        """Clear all accumulations at a match boundary."""
        self._current = RoundSituation(
            round_number=None,
            flash_count=0,
            burn_count=0,
            total_damage_taken=0,
            lowest_health=None,
            health_before_death=None,
            weapon_switch_count=0,
            bought_equipment=False,
        )
        self._reset_baselines()

    def _start_round(self, round_number: int) -> None:
        self.reset()
        self._current = replace(self._current, round_number=round_number)

    def _reset_baselines(self) -> None:
        self._previous_flashed: int | None = None
        self._previous_burning: int | None = None
        self._previous_health: int | None = None
        self._last_nonzero_health: int | None = None
        self._previous_active_weapon: str | None = None
        self._previous_money: int | None = None
        self._previous_equip_value: int | None = None


def is_low_health(snapshot: GameSnapshot) -> bool | None:
    """Return whether a known, living player is at the low-health threshold."""
    if snapshot.health is None:
        return None
    return 0 < snapshot.health <= LOW_HEALTH_THRESHOLD


def is_eco_round(snapshot: GameSnapshot) -> bool | None:
    """Return whether both known economy signals are below their thresholds."""
    if snapshot.money is None or snapshot.equip_value is None:
        return None
    return (
        snapshot.money < ECO_MONEY_THRESHOLD
        and snapshot.equip_value < ECO_EQUIP_THRESHOLD
    )


def held_weapon(snapshot: GameSnapshot) -> WeaponSlot | None:
    """Return the weapon whose GSI state is active, when the list is known."""
    if snapshot.weapons is None:
        return None
    return next((weapon for weapon in snapshot.weapons if weapon.state == "active"), None)


def is_low_ammo(snapshot: GameSnapshot) -> bool | None:
    """Return whether the held weapon has at most one known chambered round."""
    weapon = held_weapon(snapshot)
    if weapon is None or weapon.ammo_clip is None:
        return None
    return weapon.ammo_clip <= LOW_AMMO_THRESHOLD


def armor_status(snapshot: GameSnapshot) -> str | None:
    """Describe known armor and helmet coverage without inventing missing values."""
    if snapshot.armor is None or snapshot.helmet is None:
        return None
    if snapshot.armor <= 0:
        return "无甲"
    if not snapshot.helmet:
        return "有甲无头"
    return "满甲"


def is_currently_flashed(snapshot: GameSnapshot) -> bool | None:
    """Return whether the current flash intensity is positive."""
    if snapshot.flashed is None:
        return None
    return snapshot.flashed > 0


def is_currently_smoked(snapshot: GameSnapshot) -> bool | None:
    """Return whether the current smoke intensity is positive."""
    if snapshot.smoked is None:
        return None
    return snapshot.smoked > 0
