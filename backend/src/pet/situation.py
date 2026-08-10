"""Accumulate current-round CS2 facts without detecting discrete events."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import re
from typing import Literal

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
TIMELINE_MAX_ENTRIES = 25

logger = logging.getLogger(__name__)

PRIMARY_WEAPON_TYPES: frozenset[str] = frozenset(
    {"Rifle", "SniperRifle", "Submachine Gun", "Shotgun", "Machine Gun"}
)
_KNOWN_NON_PRIMARY_WEAPON_TYPES: frozenset[str] = frozenset(
    {"Knife", "Pistol", "Grenade", "C4"}
)
_WARNED_WEAPON_TYPES: set[str] = set()
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
_DAMAGE_DETAIL_PATTERN = re.compile(r"^掉了(\d+)血 剩(\d+)血$")
_TRUNCATION_DETAIL_PATTERN = re.compile(r"^较早的(\d+)条受伤记录已省略$")

TimelineKind = Literal[
    "bought",
    "flash_start",
    "flash_end",
    "smoke_start",
    "smoke_end",
    "kill",
    "damage",
    "primary_weapon",
    "bomb",
    "death",
]


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One observed state change inside the current round."""

    seconds: float
    kind: TimelineKind
    detail: str | None


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
    # Deliberate GameSnapshot duplication: spectating replaces snapshot.team with
    # the teammate's team, while round-result cards still need the player's team.
    self_team: str | None = None
    timeline: tuple[TimelineEntry, ...] = ()


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
            self._start_round(round_number, snapshot.ts)
        elif self._round_started_at is None:
            self._round_started_at = snapshot.ts

        timeline = list(self._current.timeline)
        relative_seconds = self._relative_seconds(snapshot.ts)

        primary_weapons_used, new_primary_weapons = self._observe_primary_weapons(
            snapshot
        )
        for weapon_name in new_primary_weapons:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    seconds=relative_seconds,
                    kind="primary_weapon",
                    detail=weapon_display_name(weapon_name),
                ),
            )

        bought_equipment = self._current.bought_equipment
        bought_now = (
            self._previous_money is not None
            and snapshot.money is not None
            and self._previous_equip_value is not None
            and snapshot.equip_value is not None
            and snapshot.money < self._previous_money
            and snapshot.equip_value > self._previous_equip_value
        )
        if bought_now:
            bought_equipment = True
            timeline = _append_timeline(
                timeline,
                TimelineEntry(relative_seconds, "bought", None),
            )

        (
            flash_count,
            flashed_seconds_total,
            longest_flash_seconds,
            flash_transition,
            flash_duration,
        ) = self._observe_flash(snapshot)
        if flash_transition is not None:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    flash_transition,
                    _duration_detail(flash_duration)
                    if flash_transition == "flash_end"
                    else None,
                ),
            )

        smoked_seconds_total, smoke_transition, smoke_duration = self._observe_smoke(
            snapshot
        )
        if smoke_transition is not None:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    smoke_transition,
                    _duration_detail(smoke_duration)
                    if smoke_transition == "smoke_end"
                    else None,
                ),
            )
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

        round_kills_increase = _positive_increase(
            self._previous_round_kills, snapshot.round_kills
        )
        round_headshots_increase = _positive_increase(
            self._previous_round_killhs, snapshot.round_killhs
        )
        if round_kills_increase > 0:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    "kill",
                    _kill_detail(
                        snapshot,
                        kill_count=round_kills_increase,
                        headshot_count=round_headshots_increase,
                    ),
                ),
            )

        total_damage_taken = self._current.total_damage_taken
        damage_taken = 0
        if (
            self._previous_health is not None
            and snapshot.health is not None
            and snapshot.health < self._previous_health
        ):
            damage_taken = self._previous_health - snapshot.health
            total_damage_taken += damage_taken
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    "damage",
                    _damage_detail(damage_taken, snapshot.health),
                ),
            )

        lowest_health_while_alive = self._current.lowest_health_while_alive
        if snapshot.health is not None and snapshot.health > 0 and (
            lowest_health_while_alive is None
            or snapshot.health < lowest_health_while_alive
        ):
            lowest_health_while_alive = snapshot.health

        health_before_death = self._current.health_before_death
        if snapshot.health == 0 and self._last_nonzero_health is not None:
            health_before_death = self._last_nonzero_health

        bomb_planted_at_ts = self._current.bomb_planted_at_ts
        if bomb_planted_at_ts is None and snapshot.bomb_state == "planted":
            bomb_planted_at_ts = snapshot.ts
        seconds_since_bomb_planted = (
            max(0.0, snapshot.ts - bomb_planted_at_ts)
            if bomb_planted_at_ts is not None
            else None
        )
        if (
            snapshot.bomb_state is not None
            and snapshot.bomb_state != self._previous_bomb_state
        ):
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    "bomb",
                    _bomb_detail(snapshot.bomb_state),
                ),
            )

        if (
            self._previous_health is not None
            and self._previous_health > 0
            and snapshot.health == 0
        ):
            timeline = _append_timeline(
                timeline,
                TimelineEntry(relative_seconds, "death", None),
            )

        self_team = (
            snapshot.team if snapshot.team is not None else self._current.self_team
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
            self_team=self_team,
            timeline=tuple(timeline),
        )
        self._previous_burning = snapshot.burning
        self._previous_health = snapshot.health
        if snapshot.health is not None and snapshot.health > 0:
            self._last_nonzero_health = snapshot.health
        self._previous_money = snapshot.money
        self._previous_equip_value = snapshot.equip_value
        self._previous_round_kills = snapshot.round_kills
        self._previous_round_killhs = snapshot.round_killhs
        self._previous_bomb_state = snapshot.bomb_state
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
            self_team=None,
            timeline=(),
        )
        self._reset_baselines()

    def _start_round(self, round_number: int, started_at: float) -> None:
        self.reset()
        self._current = replace(self._current, round_number=round_number)
        self._round_started_at = started_at

    def _reset_baselines(self) -> None:
        self._flash_active = False
        self._active_flash_seconds = 0.0
        self._smoke_active = False
        self._active_smoke_seconds = 0.0
        self._previous_burning: int | None = None
        self._previous_health: int | None = None
        self._last_nonzero_health: int | None = None
        self._previous_money: int | None = None
        self._previous_equip_value: int | None = None
        self._previous_round_kills: int | None = None
        self._previous_round_killhs: int | None = None
        self._previous_bomb_state: str | None = None
        self._last_self_ts: float | None = None
        self._round_started_at: float | None = None

    def _observe_flash(
        self, snapshot: GameSnapshot
    ) -> tuple[
        int,
        float,
        float,
        Literal["flash_start", "flash_end"] | None,
        float | None,
    ]:
        count = self._current.flash_count
        total = self._current.flashed_seconds_total
        longest = self._current.longest_flash_seconds
        transition: Literal["flash_start", "flash_end"] | None = None
        duration: float | None = None
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
                    transition = "flash_end"
                    duration = self._active_flash_seconds
                    self._flash_active = False
                    self._active_flash_seconds = 0.0
        elif snapshot.flashed is not None and snapshot.flashed > 0:
            count += 1
            self._flash_active = True
            self._active_flash_seconds = 0.0
            transition = "flash_start"
        return count, total, longest, transition, duration

    def _observe_smoke(
        self, snapshot: GameSnapshot
    ) -> tuple[
        float,
        Literal["smoke_start", "smoke_end"] | None,
        float | None,
    ]:
        total = self._current.smoked_seconds_total
        transition: Literal["smoke_start", "smoke_end"] | None = None
        duration: float | None = None
        if self._smoke_active:
            if snapshot.smoked is None:
                self._smoke_active = False
                self._active_smoke_seconds = 0.0
            else:
                elapsed = self._elapsed_since_last_self(snapshot.ts)
                total += elapsed
                self._active_smoke_seconds += elapsed
                if snapshot.smoked <= 0:
                    transition = "smoke_end"
                    duration = self._active_smoke_seconds
                    self._smoke_active = False
                    self._active_smoke_seconds = 0.0
        elif snapshot.smoked is not None and snapshot.smoked > 0:
            self._smoke_active = True
            self._active_smoke_seconds = 0.0
            transition = "smoke_start"
        return total, transition, duration

    def _observe_primary_weapons(
        self, snapshot: GameSnapshot
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        names = list(self._current.primary_weapons_used)
        new_names: list[str] = []
        if snapshot.weapons is None:
            return tuple(names), ()
        for weapon in snapshot.weapons:
            if weapon.type in PRIMARY_WEAPON_TYPES:
                if weapon.name not in names:
                    names.append(weapon.name)
                    new_names.append(weapon.name)
                continue
            if weapon.type is None or weapon.type in _KNOWN_NON_PRIMARY_WEAPON_TYPES:
                continue
            _warn_unknown_weapon_type_once(weapon.type)
        return tuple(names), tuple(new_names)

    def _relative_seconds(self, ts: float) -> float:
        if self._round_started_at is None:
            return 0.0
        return max(0.0, ts - self._round_started_at)

    def _elapsed_since_last_self(self, ts: float) -> float:
        if self._last_self_ts is None:
            return 0.0
        return max(0.0, ts - self._last_self_ts)

    def _close_effect_intervals(self) -> None:
        self._flash_active = False
        self._active_flash_seconds = 0.0
        self._smoke_active = False
        self._active_smoke_seconds = 0.0


def _positive_increase(previous: int | None, current: int | None) -> int:
    if previous is None or current is None or current <= previous:
        return 0
    return current - previous


def _kill_detail(
    snapshot: GameSnapshot,
    *,
    kill_count: int,
    headshot_count: int,
) -> str | None:
    weapon = held_weapon(snapshot)
    weapon_name = weapon.name if weapon is not None else snapshot.active_weapon
    details: list[str] = []
    if weapon_name is not None:
        details.append(weapon_display_name(weapon_name))
    if headshot_count > 0:
        details.append("爆头" if headshot_count == 1 else f"{headshot_count}个爆头")
    if kill_count > 1:
        details.append(f"增加{kill_count}杀")
    return " ".join(details) or None


def _damage_detail(damage: int, remaining_health: int) -> str:
    return f"掉了{damage}血 剩{remaining_health}血"


def _duration_detail(duration: float | None) -> str | None:
    return f"持续{duration:.1f}秒" if duration is not None else None


def _bomb_detail(state: str) -> str:
    return {
        "planted": "已安放",
        "defused": "已拆除",
        "exploded": "已爆炸",
    }.get(state, state)


def _append_timeline(
    entries: list[TimelineEntry], entry: TimelineEntry
) -> list[TimelineEntry]:
    timeline = list(entries)
    truncated_count = 0
    if timeline and _is_truncation_note(timeline[-1]):
        note = timeline.pop()
        match = _TRUNCATION_DETAIL_PATTERN.fullmatch(note.detail or "")
        truncated_count = int(match.group(1)) if match is not None else 0

    if entry.kind == "damage" and timeline and timeline[-1].kind == "damage":
        previous = timeline[-1]
        if entry.seconds - previous.seconds < 1.0:
            merged = _merge_damage_entries(previous, entry)
            if merged is not None:
                timeline[-1] = merged
            else:
                timeline.append(entry)
        else:
            timeline.append(entry)
    else:
        timeline.append(entry)

    while len(timeline) + (1 if truncated_count else 0) > TIMELINE_MAX_ENTRIES:
        damage_index = next(
            (index for index, item in enumerate(timeline) if item.kind == "damage"),
            None,
        )
        if damage_index is None:
            break
        timeline.pop(damage_index)
        truncated_count += 1

    if truncated_count:
        while len(timeline) + 1 > TIMELINE_MAX_ENTRIES:
            damage_index = next(
                (index for index, item in enumerate(timeline) if item.kind == "damage"),
                None,
            )
            if damage_index is None:
                break
            timeline.pop(damage_index)
            truncated_count += 1
        note_seconds = timeline[-1].seconds if timeline else entry.seconds
        timeline.append(
            TimelineEntry(
                seconds=note_seconds,
                kind="damage",
                detail=f"较早的{truncated_count}条受伤记录已省略",
            )
        )
    return timeline


def _merge_damage_entries(
    first: TimelineEntry, second: TimelineEntry
) -> TimelineEntry | None:
    first_match = _DAMAGE_DETAIL_PATTERN.fullmatch(first.detail or "")
    second_match = _DAMAGE_DETAIL_PATTERN.fullmatch(second.detail or "")
    if first_match is None or second_match is None:
        return None
    combined_damage = int(first_match.group(1)) + int(second_match.group(1))
    remaining_health = int(second_match.group(2))
    return TimelineEntry(
        seconds=first.seconds,
        kind="damage",
        detail=_damage_detail(combined_damage, remaining_health),
    )


def _is_truncation_note(entry: TimelineEntry) -> bool:
    return (
        entry.kind == "damage"
        and _TRUNCATION_DETAIL_PATTERN.fullmatch(entry.detail or "") is not None
    )


def weapon_display_name(name: str) -> str:
    """Return the stable human-readable label used in cards and timelines."""
    normalized = name.removeprefix("weapon_").lower()
    return _WEAPON_LABELS.get(normalized, normalized)


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
