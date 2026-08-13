"""Accumulate current-round CS2 facts without detecting discrete events."""

from __future__ import annotations

from collections import Counter
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
# These thresholds are product-level scene definitions, not user settings.
RESIDUAL_KILL_HEALTH = 30
SMOKE_DEATH_WINDOW_SECONDS = 2.0
WEAPON_SWITCH_KILL_WINDOW_SECONDS = 3.0
FLASH_BAD_LUCK_SECONDS = 1.5
FLASH_BAD_LUCK_COUNT = 3
BURN_BAD_LUCK_DAMAGE = 30
TIMELINE_MAX_ENTRIES = 25
# Product-defined phase buckets for the roughly 110-second live round clock.
ROUND_OPENING_END_SECONDS = 15.0
ROUND_EARLY_END_SECONDS = 30.0
ROUND_LATE_START_SECONDS = 70.0

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
_GRENADE_LABELS = {
    "weapon_flashbang": "闪光弹",
    "weapon_smokegrenade": "烟雾弹",
    "weapon_hegrenade": "手雷",
    "weapon_molotov": "燃烧弹",
    "weapon_incgrenade": "燃烧弹",
    "weapon_decoy": "诱饵弹",
}

SCENE_TAGS: frozenset[str] = frozenset(
    {
        "对枪胜利",
        "摸烟击杀",
        "击杀后被补枪",
        "白给",
        "残血击杀",
        "白着打",
        "踩火杀",
        "换枪后立刻杀",
        "白着被打死",
        "出烟就没了",
        "一发命中",
        "干净解决",
        "打了半天",
        "大狙空枪",
        "连续空枪",
        "白惨了",
        "烧惨了",
        "血皮撑住了",
    }
)


def _scene_tag(label: str) -> str:
    if label not in SCENE_TAGS:
        raise AssertionError(f"scene label is not enumerated: {label}")
    return label

TimelineKind = Literal[
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
]
RoundStage = Literal["开局", "前期", "中期", "后期", "守包", "反攻包点", "下包后"]


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
    grenades_used: tuple[tuple[str, int], ...] = ()
    awp_miss_count: int = 0
    burn_damage_taken: int = 0
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
        if game.state == "warmup":
            # Warmup and the first real round both commonly report map.round == 0.
            # Keeping warmup baselines would therefore rebase warmup combat to large
            # negative timestamps when the first real round becomes live.
            self.reset()
            return self._current
        if game.subject_is_self is not True:
            self._close_effect_intervals()
            self._record_bomb_state(snapshot)
            self._record_round_result(snapshot)
            return self._current

        round_number = human_round_number(snapshot)
        if round_number is not None and round_number != self._current.round_number:
            self._close_effect_intervals()
            self._start_round(round_number, snapshot.ts)
        elif self._round_started_at is None:
            self._round_started_at = snapshot.ts

        timeline = list(self._current.timeline)
        relative_seconds = self._relative_seconds(snapshot.ts)

        if (
            self._previous_round_phase == "freezetime"
            and snapshot.round_phase == "live"
        ):
            timeline = [
                replace(entry, seconds=entry.seconds - relative_seconds)
                for entry in timeline
            ]
            if self._last_bought_at_seconds is not None:
                self._last_bought_at_seconds -= relative_seconds
            self._round_live_at = snapshot.ts
            relative_seconds = 0.0
            timeline = _append_timeline(
                timeline,
                TimelineEntry(relative_seconds, "round_live", None),
            )
        if snapshot.round_phase == "live":
            self._round_is_live = True

        primary_weapons_used, new_primary_weapons = self._observe_primary_weapons(
            snapshot
        )
        had_primary_weapon = bool(self._current.primary_weapons_used)
        for index, weapon_name in enumerate(new_primary_weapons):
            label = weapon_display_name(weapon_name)
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    seconds=relative_seconds,
                    kind="primary_weapon",
                    detail=(
                        f"换枪 {label}"
                        if had_primary_weapon or index > 0
                        else label
                    ),
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
            if (
                self._last_bought_at_seconds is None
                or relative_seconds - self._last_bought_at_seconds >= 3.0 - 1e-9
            ):
                timeline = _append_timeline(
                    timeline,
                    TimelineEntry(relative_seconds, "bought", None),
                )
            self._last_bought_at_seconds = relative_seconds

        used_grenades, picked_up_grenades = self._observe_grenades(snapshot)
        grenades_used = Counter(dict(self._current.grenades_used))
        for grenade_name in used_grenades:
            grenades_used[grenade_name] += 1
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    "grenade_used",
                    f"扔了{_grenade_display_name(grenade_name)}",
                ),
            )
        for grenade_name in picked_up_grenades:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    "grenade_pickup",
                    f"捡到{_grenade_display_name(grenade_name)}",
                ),
            )

        round_kills_increase = _positive_increase(
            self._previous_round_kills, snapshot.round_kills
        )
        ammo_detail, ammo_drop, awp_miss = self._observe_ammo(
            snapshot, kill_count=round_kills_increase
        )
        if ammo_detail is not None:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(relative_seconds, "ammo_low", ammo_detail),
            )
        awp_miss_count = self._current.awp_miss_count
        if awp_miss:
            awp_miss_count += 1
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    "awp_miss",
                    _scene_tag("大狙空枪")
                    if awp_miss_count == 1
                    else f"{_scene_tag('大狙空枪')}又空了（累计{awp_miss_count}次）",
                ),
            )
        for reload_detail in self._observe_reload(snapshot, relative_seconds):
            timeline = _append_timeline(
                timeline,
                TimelineEntry(relative_seconds, "reload", reload_detail),
            )

        (
            flash_count,
            flashed_seconds_total,
            longest_flash_seconds,
            flash_transition,
            flash_duration,
            flash_interrupted,
        ) = self._observe_flash(snapshot)
        if flash_transition is not None:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    flash_transition,
                    (
                        _unfinished_duration_detail(flash_duration)
                        if flash_interrupted
                        else _duration_detail(flash_duration)
                    )
                    if flash_transition == "flash_end"
                    else None,
                ),
            )

        (
            smoked_seconds_total,
            smoke_transition,
            smoke_duration,
            smoke_interrupted,
        ) = self._observe_smoke(snapshot)
        if smoke_transition is not None:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    smoke_transition,
                    (
                        _unfinished_duration_detail(smoke_duration)
                        if smoke_interrupted
                        else _duration_detail(smoke_duration)
                    )
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

        burn_count, burn_transition, burn_duration, burn_interrupted = (
            self._observe_burn(snapshot)
        )
        if burn_transition is not None:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    burn_transition,
                    (
                        _unfinished_duration_detail(burn_duration)
                        if burn_interrupted
                        else _duration_detail(burn_duration)
                    )
                    if burn_transition == "burn_end"
                    else None,
                ),
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
                        ammo_drop=ammo_drop,
                    ),
                ),
            )

        total_damage_taken = self._current.total_damage_taken
        burn_damage_taken = self._current.burn_damage_taken
        damage_taken = 0
        if (
            self._previous_health is not None
            and snapshot.health is not None
            and snapshot.health < self._previous_health
        ):
            damage_taken = self._previous_health - snapshot.health
            total_damage_taken += damage_taken
            if (
                self._previous_burning is not None
                and self._previous_burning > 0
                and snapshot.burning is not None
                and snapshot.burning > 0
            ):
                burn_damage_taken += damage_taken
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

        (
            timeline,
            bomb_planted_at_ts,
            seconds_since_bomb_planted,
        ) = self._bomb_state_update(snapshot, timeline, relative_seconds)

        carrying_bomb = is_carrying_bomb(snapshot)
        if self._previous_carrying_bomb is False and carrying_bomb is True:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(relative_seconds, "bomb_pickup", None),
            )
        if (
            self._previous_carrying_bomb is True
            and carrying_bomb is False
            and snapshot.health is not None
            and snapshot.health > 0
        ):
            timeline = _append_timeline(
                timeline,
                TimelineEntry(relative_seconds, "bomb_drop", None),
            )

        assists_increase = _positive_increase(
            self._previous_match_assists, snapshot.match_assists
        )
        if assists_increase > 0:
            timeline = _append_timeline(
                timeline,
                TimelineEntry(relative_seconds, "assist", None),
            )

        mvps_increase = _positive_increase(
            self._previous_match_mvps, snapshot.match_mvps
        )
        if mvps_increase > 0:
            previous_round_prefix = (
                "上回合 " if snapshot.round_phase == "freezetime" else ""
            )
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    "mvp",
                    f"{previous_round_prefix}MVP+{mvps_increase}",
                ),
            )

        if (
            self._previous_health is not None
            and self._previous_health > 0
            and snapshot.health == 0
        ):
            timeline = self._close_effect_intervals(
                timeline=timeline,
                relative_seconds=relative_seconds,
            )
            timeline = _append_timeline(
                timeline,
                TimelineEntry(relative_seconds, "death", None),
            )
        elif snapshot.round_phase == "over" or snapshot.round_win_team is not None:
            timeline = self._close_effect_intervals(
                timeline=timeline,
                relative_seconds=relative_seconds,
            )

        if (
            snapshot.round_win_team is not None
            and snapshot.round_win_team != self._previous_round_win_team
        ):
            timeline = _append_timeline(
                timeline,
                TimelineEntry(
                    relative_seconds,
                    "round_result",
                    snapshot.round_win_team,
                ),
            )
            self._previous_round_win_team = snapshot.round_win_team

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
            grenades_used=tuple(sorted(grenades_used.items())),
            awp_miss_count=awp_miss_count,
            burn_damage_taken=burn_damage_taken,
            self_team=self_team,
            timeline=tuple(timeline),
        )
        self._previous_health = snapshot.health
        if snapshot.health is not None and snapshot.health > 0:
            self._last_nonzero_health = snapshot.health
        self._previous_money = snapshot.money
        self._previous_equip_value = snapshot.equip_value
        self._previous_round_kills = snapshot.round_kills
        self._previous_burning = snapshot.burning
        self._previous_round_killhs = snapshot.round_killhs
        self._previous_match_assists = snapshot.match_assists
        if snapshot.match_mvps is not None:
            self._previous_match_mvps = snapshot.match_mvps
        if carrying_bomb is not None:
            self._previous_carrying_bomb = carrying_bomb
        self._previous_round_phase = snapshot.round_phase
        self._last_self_ts = snapshot.ts
        return self._current

    def finish(self) -> RoundSituation:
        """Close active intervals at the final self snapshot of a recording."""
        timeline = self._close_effect_intervals(
            timeline=list(self._current.timeline),
            relative_seconds=self._relative_seconds(self._last_self_ts),
        )
        self._current = replace(self._current, timeline=tuple(timeline))
        return self._current

    def _record_round_result(self, snapshot: GameSnapshot) -> None:
        round_number = human_round_number(snapshot)
        if (
            round_number != self._current.round_number
            or snapshot.round_win_team is None
            or snapshot.round_win_team == self._previous_round_win_team
        ):
            return
        timeline = _append_timeline(
            list(self._current.timeline),
            TimelineEntry(
                self._relative_seconds(snapshot.ts),
                "round_result",
                snapshot.round_win_team,
            ),
        )
        self._current = replace(self._current, timeline=tuple(timeline))
        self._previous_round_win_team = snapshot.round_win_team

    def _record_bomb_state(self, snapshot: GameSnapshot) -> None:
        if human_round_number(snapshot) != self._current.round_number:
            return
        timeline, planted_at, elapsed = self._bomb_state_update(
            snapshot,
            list(self._current.timeline),
            self._relative_seconds(snapshot.ts),
        )
        self._current = replace(
            self._current,
            bomb_planted_at_ts=planted_at,
            seconds_since_bomb_planted=elapsed,
            timeline=tuple(timeline),
        )

    def _bomb_state_update(
        self,
        snapshot: GameSnapshot,
        timeline: list[TimelineEntry],
        relative_seconds: float,
    ) -> tuple[list[TimelineEntry], float | None, float | None]:
        planted_at = self._current.bomb_planted_at_ts
        if planted_at is None and snapshot.bomb_state == "planted":
            planted_at = snapshot.ts
        elapsed = (
            max(0.0, snapshot.ts - planted_at) if planted_at is not None else None
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
        if snapshot.bomb_state is not None:
            self._previous_bomb_state = snapshot.bomb_state
        return timeline, planted_at, elapsed

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
            grenades_used=(),
            awp_miss_count=0,
            burn_damage_taken=0,
            self_team=None,
            timeline=(),
        )
        self._reset_baselines()

    def _start_round(self, round_number: int, started_at: float) -> None:
        previous_match_mvps = self._previous_match_mvps
        self.reset()
        # GSI commonly publishes MVP+1 in the next round's freeze time. Keep the
        # match-level baseline across a round reset so that late update is visible.
        self._previous_match_mvps = previous_match_mvps
        self._current = replace(self._current, round_number=round_number)
        self._round_started_at = started_at

    def _reset_baselines(self) -> None:
        self._flash_active = False
        self._active_flash_seconds = 0.0
        self._smoke_active = False
        self._active_smoke_seconds = 0.0
        self._burn_active = False
        self._active_burn_seconds = 0.0
        self._previous_health: int | None = None
        self._last_nonzero_health: int | None = None
        self._previous_money: int | None = None
        self._previous_equip_value: int | None = None
        self._previous_round_kills: int | None = None
        self._previous_burning: int | None = None
        self._previous_round_killhs: int | None = None
        self._previous_match_assists: int | None = None
        self._previous_match_mvps: int | None = None
        self._previous_bomb_state: str | None = None
        self._previous_carrying_bomb: bool | None = None
        self._previous_grenades: Counter[str] | None = None
        self._previous_weapon_states: dict[str, str | None] | None = None
        self._previous_held_weapon_name: str | None = None
        self._previous_held_ammo: int | None = None
        self._reload_weapon_name: str | None = None
        self._reload_started_at_seconds: float | None = None
        self._previous_round_win_team: str | None = None
        self._previous_round_phase: str | None = None
        self._round_is_live = False
        self._last_bought_at_seconds: float | None = None
        self._last_self_ts: float | None = None
        self._round_started_at: float | None = None
        self._round_live_at: float | None = None

    def _observe_flash(
        self, snapshot: GameSnapshot
    ) -> tuple[
        int,
        float,
        float,
        Literal["flash_start", "flash_end"] | None,
        float | None,
        bool,
    ]:
        count = self._current.flash_count
        total = self._current.flashed_seconds_total
        longest = self._current.longest_flash_seconds
        transition: Literal["flash_start", "flash_end"] | None = None
        duration: float | None = None
        interrupted = False
        if self._flash_active:
            if snapshot.flashed is None:
                transition = "flash_end"
                duration = self._active_flash_seconds
                interrupted = True
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
        return count, total, longest, transition, duration, interrupted

    def _observe_smoke(
        self, snapshot: GameSnapshot
    ) -> tuple[
        float,
        Literal["smoke_start", "smoke_end"] | None,
        float | None,
        bool,
    ]:
        total = self._current.smoked_seconds_total
        transition: Literal["smoke_start", "smoke_end"] | None = None
        duration: float | None = None
        interrupted = False
        if self._smoke_active:
            if snapshot.smoked is None:
                transition = "smoke_end"
                duration = self._active_smoke_seconds
                interrupted = True
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
        return total, transition, duration, interrupted

    def _observe_burn(
        self, snapshot: GameSnapshot
    ) -> tuple[
        int,
        Literal["burn_start", "burn_end"] | None,
        float | None,
        bool,
    ]:
        count = self._current.burn_count
        transition: Literal["burn_start", "burn_end"] | None = None
        duration: float | None = None
        interrupted = False
        if self._burn_active:
            if snapshot.burning is None:
                transition = "burn_end"
                duration = self._active_burn_seconds
                interrupted = True
                self._burn_active = False
                self._active_burn_seconds = 0.0
            else:
                self._active_burn_seconds += self._elapsed_since_last_self(snapshot.ts)
                if snapshot.burning <= 0:
                    transition = "burn_end"
                    duration = self._active_burn_seconds
                    self._burn_active = False
                    self._active_burn_seconds = 0.0
        elif snapshot.burning is not None and snapshot.burning > 0:
            count += 1
            self._burn_active = True
            self._active_burn_seconds = 0.0
            transition = "burn_start"
        return count, transition, duration, interrupted

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

    def _observe_grenades(
        self, snapshot: GameSnapshot
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if snapshot.weapons is None:
            return (), ()
        current = Counter(
            weapon.name for weapon in snapshot.weapons if weapon.type == "Grenade"
        )
        used: list[str] = []
        picked_up: list[str] = []
        if (
            self._round_is_live
            and snapshot.health != 0
            and self._previous_grenades is not None
        ):
            for name, count in (self._previous_grenades - current).items():
                used.extend(name for _ in range(count))
            for name, count in (current - self._previous_grenades).items():
                picked_up.extend(name for _ in range(count))
        self._previous_grenades = current
        return tuple(used), tuple(picked_up)

    def _observe_ammo(
        self, snapshot: GameSnapshot, *, kill_count: int
    ) -> tuple[str | None, int | None, bool]:
        weapon = _operated_weapon(snapshot)
        if weapon is None or weapon.ammo_clip is None:
            self._previous_held_weapon_name = None
            self._previous_held_ammo = None
            return None, None, False

        detail: str | None = None
        ammo_drop: int | None = None
        awp_miss = False
        if (
            weapon.name == self._previous_held_weapon_name
            and self._previous_held_ammo is not None
            and weapon.ammo_clip < self._previous_held_ammo
        ):
            ammo_drop = self._previous_held_ammo - weapon.ammo_clip
            awp_miss = weapon.name.lower() == "weapon_awp" and kill_count == 0
            label = weapon_display_name(weapon.name)
            if self._previous_held_ammo > LOW_AMMO_THRESHOLD and weapon.ammo_clip <= LOW_AMMO_THRESHOLD:
                detail = (
                    f"弹匣打空 {label}"
                    if weapon.ammo_clip == 0
                    else f"弹匣仅剩{weapon.ammo_clip}发 {label}"
                )
        self._previous_held_weapon_name = weapon.name
        self._previous_held_ammo = weapon.ammo_clip
        return detail, ammo_drop, awp_miss

    def _observe_reload(
        self,
        snapshot: GameSnapshot,
        relative_seconds: float,
    ) -> tuple[str, ...]:
        current_states = {
            weapon.name: weapon.state for weapon in snapshot.weapons or ()
        }
        reloading = next(
            (
                weapon
                for weapon in snapshot.weapons or ()
                if weapon.state == "reloading"
            ),
            None,
        )
        details: list[str] = []

        if self._reload_weapon_name is not None and (
            reloading is None or reloading.name != self._reload_weapon_name
        ):
            if self._reload_started_at_seconds is not None:
                duration = max(
                    0.0,
                    relative_seconds - self._reload_started_at_seconds,
                )
                label = weapon_display_name(self._reload_weapon_name)
                if current_states.get(self._reload_weapon_name) == "active":
                    details.append(f"换弹 {label} 用时约{duration:.1f}秒")
                else:
                    details.append(
                        f"换弹 {label} 未完成 已持续{duration:.1f}秒"
                    )
            self._reload_weapon_name = None
            self._reload_started_at_seconds = None

        if reloading is not None and self._reload_weapon_name is None:
            previous_state = (
                self._previous_weapon_states.get(reloading.name)
                if self._previous_weapon_states is not None
                else None
            )
            self._reload_weapon_name = reloading.name
            self._reload_started_at_seconds = (
                relative_seconds
                if self._previous_weapon_states is not None
                and previous_state != "reloading"
                else None
            )

        self._previous_weapon_states = current_states
        return tuple(details)

    def _relative_seconds(self, ts: float | None) -> float:
        if ts is None:
            return 0.0
        origin = self._round_live_at or self._round_started_at
        if origin is None:
            return 0.0
        return ts - origin

    def _elapsed_since_last_self(self, ts: float) -> float:
        if self._last_self_ts is None:
            return 0.0
        return max(0.0, ts - self._last_self_ts)
    def _close_effect_intervals(
        self,
        *,
        timeline: list[TimelineEntry] | None = None,
        relative_seconds: float | None = None,
    ) -> list[TimelineEntry]:
        result = list(self._current.timeline) if timeline is None else list(timeline)
        seconds = (
            self._relative_seconds(self._last_self_ts)
            if relative_seconds is None
            else relative_seconds
        )
        if self._flash_active:
            result = _append_timeline(
                result,
                TimelineEntry(
                    seconds,
                    "flash_end",
                    f"未结束 已持续{self._active_flash_seconds:.1f}秒",
                ),
            )
        if self._smoke_active:
            result = _append_timeline(
                result,
                TimelineEntry(
                    seconds,
                    "smoke_end",
                    f"未结束 已持续{self._active_smoke_seconds:.1f}秒",
                ),
            )
        if self._burn_active:
            result = _append_timeline(
                result,
                TimelineEntry(
                    seconds,
                    "burn_end",
                    f"未结束 已持续{self._active_burn_seconds:.1f}秒",
                ),
            )
        self._flash_active = False
        self._active_flash_seconds = 0.0
        self._smoke_active = False
        self._active_smoke_seconds = 0.0
        self._burn_active = False
        self._active_burn_seconds = 0.0
        if timeline is None:
            self._current = replace(self._current, timeline=tuple(result))
        return result


def round_stage_label(
    seconds: float,
    *,
    bomb_planted: bool,
    self_team: str | None,
    observed_live: bool,
) -> RoundStage | None:
    """Classify a timeline moment without asking a model to do clock arithmetic."""
    if not observed_live or seconds < 0:
        return None
    if bomb_planted:
        if self_team == "T":
            return "守包"
        if self_team == "CT":
            return "反攻包点"
        return "下包后"
    if seconds < ROUND_OPENING_END_SECONDS:
        return "开局"
    if seconds < ROUND_EARLY_END_SECONDS:
        return "前期"
    if seconds >= ROUND_LATE_START_SECONDS:
        return "后期"
    return "中期"


def _positive_increase(previous: int | None, current: int | None) -> int:
    if previous is None or current is None or current <= previous:
        return 0
    return current - previous


def _kill_detail(
    snapshot: GameSnapshot,
    *,
    kill_count: int,
    headshot_count: int,
    ammo_drop: int | None = None,
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
    if ammo_drop is not None and ammo_drop > 0:
        if weapon_name is not None and weapon_name.lower() != "weapon_awp":
            details.append(f"用弹{ammo_drop}")
    if snapshot.health == 100:
        details.append("击杀时满血")
    elif snapshot.health is not None and 0 < snapshot.health <= LOW_HEALTH_THRESHOLD:
        details.append(f"击杀时剩{snapshot.health}血")
    if weapon is not None and weapon.ammo_clip is not None and weapon.ammo_clip <= 2:
        details.append(f"弹匣仅剩{weapon.ammo_clip}发")
    return " ".join(details) or None


def _damage_detail(
    damage: int,
    remaining_health: int,
) -> str:
    return f"掉了{damage}血 剩{remaining_health}血"


def _duration_detail(duration: float | None) -> str | None:
    return f"持续{duration:.1f}秒" if duration is not None else None


def _unfinished_duration_detail(duration: float | None) -> str | None:
    return f"未结束 已持续{duration:.1f}秒" if duration is not None else None


def _bomb_detail(state: str) -> str:
    return {
        "planted": "已安放",
        "defused": "已拆除",
        "exploded": "已爆炸",
    }.get(state, state)


def _grenade_display_name(name: str) -> str:
    return _GRENADE_LABELS.get(name.lower(), weapon_display_name(name))


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
        # A collapsed entry is timestamped at its final observed change.
        seconds=second.seconds,
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


def _operated_weapon(snapshot: GameSnapshot) -> WeaponSlot | None:
    """Return the weapon currently active or being reloaded."""
    if snapshot.weapons is None:
        return None
    return next(
        (
            weapon
            for weapon in snapshot.weapons
            if weapon.state in {"active", "reloading"}
        ),
        None,
    )


def is_low_ammo(snapshot: GameSnapshot) -> bool | None:
    """Return whether the held weapon has at most one known chambered round."""
    weapon = held_weapon(snapshot)
    if weapon is None or weapon.ammo_clip is None:
        return None
    return weapon.ammo_clip <= LOW_AMMO_THRESHOLD


def armor_status(snapshot: GameSnapshot) -> str | None:
    """Describe only whether known armor is present; its exact value is noise."""
    if snapshot.armor is None:
        return None
    if snapshot.armor <= 0:
        return "无甲"
    return "有甲"


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
