"""Accumulate current-round CS2 facts without detecting discrete events."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging

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

logger = logging.getLogger(__name__)

PRIMARY_WEAPON_TYPES: frozenset[str] = frozenset(
    {"Rifle", "SniperRifle", "Submachine Gun", "Shotgun", "Machine Gun"}
)
_KNOWN_NON_PRIMARY_WEAPON_TYPES: frozenset[str] = frozenset(
    {"Knife", "Pistol", "Grenade", "C4"}
)
_WARNED_WEAPON_TYPES: set[str] = set()


@dataclass(frozen=True, slots=True)
class RoundSituation:
    """Per-round accumulations that a single snapshot cannot express."""

    round_number: int | None
    flash_count: int
    flashed_seconds_total: float
    longest_flash_seconds: float
    smoked_seconds_total: float
    max_smoke_intensity: int | None
    burn_count: int
    total_damage_taken: int
    lowest_health_while_alive: int | None
    health_before_death: int | None
    primary_weapons_used: tuple[str, ...]
    bought_equipment: bool
    bomb_planted_at_ts: float | None
    seconds_since_bomb_planted: float | None


class SituationTracker:
    """Fold ordered self-owned snapshots into one current-round situation."""

    def __init__(self) -> None:
        self.reset()

    def observe(self, snapshot: GameSnapshot, game: GameState) -> RoundSituation:
        """Fold one snapshot into the running per-round accumulations."""
        if game.subject_is_self is not True:
            self._close_effect_intervals()
            return self._current

        round_number = human_round_number(snapshot)
        if round_number is not None and round_number != self._current.round_number:
            self._close_effect_intervals()
            self._start_round(round_number)

        flash_count, flashed_seconds_total, longest_flash_seconds = (
            self._observe_flash(snapshot)
        )
        smoked_seconds_total = self._observe_smoke(snapshot)
        max_smoke_intensity = self._current.max_smoke_intensity
        if snapshot.smoked is not None:
            max_smoke_intensity = (
                snapshot.smoked
                if max_smoke_intensity is None
                else max(max_smoke_intensity, snapshot.smoked)
            )

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

        lowest_health_while_alive = self._current.lowest_health_while_alive
        if snapshot.health is not None and snapshot.health > 0 and (
            lowest_health_while_alive is None
            or snapshot.health < lowest_health_while_alive
        ):
            lowest_health_while_alive = snapshot.health

        health_before_death = self._current.health_before_death
        if snapshot.health == 0 and self._last_nonzero_health is not None:
            health_before_death = self._last_nonzero_health

        primary_weapons_used = self._observe_primary_weapons(snapshot)

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

        bomb_planted_at_ts = self._current.bomb_planted_at_ts
        if bomb_planted_at_ts is None and snapshot.bomb_state == "planted":
            bomb_planted_at_ts = snapshot.ts
        seconds_since_bomb_planted = (
            max(0.0, snapshot.ts - bomb_planted_at_ts)
            if bomb_planted_at_ts is not None
            else None
        )

        self._current = replace(
            self._current,
            flash_count=flash_count,
            flashed_seconds_total=flashed_seconds_total,
            longest_flash_seconds=longest_flash_seconds,
            smoked_seconds_total=smoked_seconds_total,
            max_smoke_intensity=max_smoke_intensity,
            burn_count=burn_count,
            total_damage_taken=total_damage_taken,
            lowest_health_while_alive=lowest_health_while_alive,
            health_before_death=health_before_death,
            primary_weapons_used=primary_weapons_used,
            bought_equipment=bought_equipment,
            bomb_planted_at_ts=bomb_planted_at_ts,
            seconds_since_bomb_planted=seconds_since_bomb_planted,
        )
        self._previous_burning = snapshot.burning
        self._previous_health = snapshot.health
        if snapshot.health is not None and snapshot.health > 0:
            self._last_nonzero_health = snapshot.health
        self._previous_money = snapshot.money
        self._previous_equip_value = snapshot.equip_value
        self._last_self_ts = snapshot.ts
        return self._current

    def finish(self) -> RoundSituation:
        """Close active intervals at the final self snapshot of a recording."""
        self._close_effect_intervals()
        return self._current

    def reset(self) -> None:
        """Clear all accumulations at a match boundary."""
        self._current = RoundSituation(
            round_number=None,
            flash_count=0,
            flashed_seconds_total=0.0,
            longest_flash_seconds=0.0,
            smoked_seconds_total=0.0,
            max_smoke_intensity=None,
            burn_count=0,
            total_damage_taken=0,
            lowest_health_while_alive=None,
            health_before_death=None,
            primary_weapons_used=(),
            bought_equipment=False,
            bomb_planted_at_ts=None,
            seconds_since_bomb_planted=None,
        )
        self._reset_baselines()

    def _start_round(self, round_number: int) -> None:
        self.reset()
        self._current = replace(self._current, round_number=round_number)

    def _reset_baselines(self) -> None:
        self._flash_active = False
        self._active_flash_seconds = 0.0
        self._smoke_active = False
        self._previous_burning: int | None = None
        self._previous_health: int | None = None
        self._last_nonzero_health: int | None = None
        self._previous_money: int | None = None
        self._previous_equip_value: int | None = None
        self._last_self_ts: float | None = None

    def _observe_flash(self, snapshot: GameSnapshot) -> tuple[int, float, float]:
        count = self._current.flash_count
        total = self._current.flashed_seconds_total
        longest = self._current.longest_flash_seconds
        if self._flash_active:
            if snapshot.flashed is None:
                self._flash_active = False
                self._active_flash_seconds = 0.0
            else:
                elapsed = self._elapsed_since_last_self(snapshot.ts)
                total += elapsed
                self._active_flash_seconds += elapsed
                longest = max(longest, self._active_flash_seconds)
                if snapshot.flashed <= 0:
                    self._flash_active = False
                    self._active_flash_seconds = 0.0
        elif snapshot.flashed is not None and snapshot.flashed > 0:
            count += 1
            self._flash_active = True
            self._active_flash_seconds = 0.0
        return count, total, longest

    def _observe_smoke(self, snapshot: GameSnapshot) -> float:
        total = self._current.smoked_seconds_total
        if self._smoke_active:
            if snapshot.smoked is None:
                self._smoke_active = False
            else:
                total += self._elapsed_since_last_self(snapshot.ts)
                if snapshot.smoked <= 0:
                    self._smoke_active = False
        elif snapshot.smoked is not None and snapshot.smoked > 0:
            self._smoke_active = True
        return total

    def _observe_primary_weapons(self, snapshot: GameSnapshot) -> tuple[str, ...]:
        names = list(self._current.primary_weapons_used)
        if snapshot.weapons is None:
            return tuple(names)
        for weapon in snapshot.weapons:
            if weapon.type in PRIMARY_WEAPON_TYPES:
                if weapon.name not in names:
                    names.append(weapon.name)
                continue
            if weapon.type is None or weapon.type in _KNOWN_NON_PRIMARY_WEAPON_TYPES:
                continue
            _warn_unknown_weapon_type_once(weapon.type)
        return tuple(names)

    def _elapsed_since_last_self(self, ts: float) -> float:
        if self._last_self_ts is None:
            return 0.0
        return max(0.0, ts - self._last_self_ts)

    def _close_effect_intervals(self) -> None:
        self._flash_active = False
        self._active_flash_seconds = 0.0
        self._smoke_active = False


def _warn_unknown_weapon_type_once(weapon_type: str) -> None:
    if weapon_type in _WARNED_WEAPON_TYPES:
        return
    _WARNED_WEAPON_TYPES.add(weapon_type)
    logger.warning(
        "unknown CS2 WeaponSlot.type %r; treating it as non-primary",
        weapon_type,
    )


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
    """Return whether the current flash presence flag is positive."""
    if snapshot.flashed is None:
        return None
    return snapshot.flashed > 0


def is_currently_smoked(snapshot: GameSnapshot) -> bool | None:
    """Return whether the current smoke intensity is positive."""
    if snapshot.smoked is None:
        return None
    return snapshot.smoked > 0


def is_carrying_bomb(snapshot: GameSnapshot) -> bool | None:
    """Return whether a known weapon list contains the C4."""
    if snapshot.weapons is None:
        return None
    return any(weapon.type == "C4" for weapon in snapshot.weapons)
