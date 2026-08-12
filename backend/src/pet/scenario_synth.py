"""Build offline synthetic GSI regression scenarios from real recordings."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Literal, TypeAlias, cast

from pet.bench import BenchEvent, run_bench
from pet.events import EventType
from pet.gsi import GSI_SILENCE_SECONDS
from pet.replay import load_recording
from pet.session import GameSessionTracker, MatchLifecycleTracker
from pet.situation import SituationTracker, TimelineKind

JsonScalar: TypeAlias = str | int | float | bool | None
ScenarioCategory = Literal["甲", "乙", "丙", "丁"]
MutationOperation = Literal["set", "delete"]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
RECORDINGS_DIRECTORY = BACKEND_ROOT / "recordings"
SCENARIOS_DIRECTORY = BACKEND_ROOT / "scenarios"
REPORTS_DIRECTORY = BACKEND_ROOT / "bench-reports"
OBSERVED_CONSTRAINTS_PATH = REPORTS_DIRECTORY / "m3-t6-observed-constraints.json"
INVENTORY_PATHS = (
    REPORTS_DIRECTORY / "m3-t2-data-inventory.md",
    REPORTS_DIRECTORY / "m3-t2-inventory2.md",
)
SYNTHETIC_SELF_STEAMID = "SYNTHETIC_SELF_STEAMID"
SYNTHETIC_OTHER_STEAMID = "SYNTHETIC_OTHER_STEAMID"
SYNTHETIC_SELF_NAME = "Synthetic Self"
SYNTHETIC_OTHER_NAME = "Synthetic Other"

# The task's eighteen action/status kinds. The current implementation also has
# round_live and bought anchors plus two burn kinds. Burn is intentionally
# excluded because every real inventory value is zero.
REPORTED_TIMELINE_KINDS: tuple[TimelineKind, ...] = (
    "flash_start",
    "flash_end",
    "smoke_start",
    "smoke_end",
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
)

_TABLE_ROW = re.compile(r"^\| `([^`]+)` \| (\d+) \| (.*?) \|")
_NUMBER_RANGE = re.compile(
    r"最小\s+(-?\d+(?:\.\d+)?)\s*/\s*最大\s+(-?\d+(?:\.\d+)?)"
)
_QUOTED_VALUE = re.compile(r'"([^"]+)"')
_WEAPON_KEY = re.compile(r"(?<=\.weapons\.)weapon_\d+(?=\.)")
_TEMPLATE_SOURCE = re.compile(r"^(.+\.jsonl) 第 (\d+)–(\d+) 行$")


@dataclass(frozen=True, slots=True)
class Mutation:
    """One scalar change at an absolute source line."""

    line_number: int
    path: str
    operation: MutationOperation
    value: JsonScalar = None


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    """One synthetic scenario and its independently declared answer key."""

    scenario_id: str
    category: ScenarioCategory
    description: str
    template_source: str
    mutations: tuple[Mutation, ...]
    expected_event_type: EventType
    required_facts: tuple[str, ...]
    forbidden_claims: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PathConstraint:
    """All values observed for one whitelisted raw field in real recordings."""

    minimum: float | None = None
    maximum: float | None = None
    values: frozenset[str] = frozenset()
    source_files: frozenset[str] = frozenset()

    def merge(self, other: "PathConstraint") -> "PathConstraint":
        minimums = tuple(
            value for value in (self.minimum, other.minimum) if value is not None
        )
        maximums = tuple(
            value for value in (self.maximum, other.maximum) if value is not None
        )
        return PathConstraint(
            minimum=min(minimums) if minimums else None,
            maximum=max(maximums) if maximums else None,
            values=self.values | other.values,
            source_files=self.source_files | other.source_files,
        )


def _set(line_number: int, path: str, value: JsonScalar) -> Mutation:
    return Mutation(line_number, path, "set", value)


def _delete(line_number: int, path: str) -> Mutation:
    return Mutation(line_number, path, "delete")


def _span(
    start: int, end: int, path: str, value: JsonScalar
) -> tuple[Mutation, ...]:
    return tuple(_set(line, path, value) for line in range(start, end + 1))


BASE_CT = "gsi-20260810-154052-044137.jsonl 第 18–73 行"
BASE_CT_SHORT = "gsi-20260810-154052-044137.jsonl 第 104–132 行"
BASE_CT_DEFUSE = "gsi-20260810-154052-044137.jsonl 第 3–16 行"
BASE_T_C4 = "gsi-20260810-114649-321103.jsonl 第 65–100 行"
BASE_BURN = "gsi-20260811-223119-169538.jsonl 第 452–476 行"
BASE_LATE_DEFUSE = "gsi-20260809-112213.jsonl 第 40–72 行"
BASE_EXPLOSION = "gsi-20260811-223119-169538.jsonl 第 1413–1493 行"
COMMON_FORBIDDEN = (
    "不得出现队友或敌人身份",
    "不得声称玩家所在位置",
    "不得编造伤害来源",
)
DEATH_WITHOUT_ROUND_RESULT = (
    # Keep the final death in the same human round.  The source's settlement
    # snapshot advances map.round, which would otherwise reset the timeline.
    _set(73, "map.round", 1),
    _set(73, "round.phase", "live"),
    _delete(73, "round.win_team"),
)

TRIPLE_SAME_STAGE = (
    *_span(18, 66, "player.state.round_kills", 0),
    *_span(67, 73, "player.state.round_kills", 3),
)
TRIPLE_CROSS_STAGE = (
    *_span(18, 40, "player.state.round_kills", 0),
    *_span(41, 66, "player.state.round_kills", 1),
    *_span(67, 73, "player.state.round_kills", 3),
)


def _ct_spec(
    scenario_id: str,
    category: ScenarioCategory,
    description: str,
    mutations: Sequence[Mutation],
    required_facts: Sequence[str],
    *,
    expected_event_type: EventType = "round_loss",
    source: str = BASE_CT,
    forbidden_claims: Sequence[str] = COMMON_FORBIDDEN,
) -> ScenarioSpec:
    return ScenarioSpec(
        scenario_id,
        category,
        description,
        source,
        tuple(mutations),
        expected_event_type,
        tuple(required_facts),
        tuple(forbidden_claims),
    )


# Every entry below is still declarative: helpers only expand repeated scalar
# assignments into the immutable mutation tuple stored on ScenarioSpec.
SCENARIO_SPECS: tuple[ScenarioSpec, ...] = (
    _ct_spec(
        "rare_reload_then_kill",
        "甲",
        "换弹完成后立即用 M4A1-S 击杀",
        (
            *_span(18, 40, "player.state.round_kills", 0),
            *_span(41, 73, "player.state.round_kills", 1),
            _set(39, "player.weapons.weapon_2.state", "reloading"),
            _set(40, "player.weapons.weapon_2.state", "reloading"),
            _set(41, "player.weapons.weapon_2.state", "active"),
        ),
        ("换弹", "M4A1-S", "击杀"),
        expected_event_type="kill",
    ),
    _ct_spec(
        "rare_ammo_low_death",
        "甲",
        "弹匣打空后阵亡",
        (
            *_span(18, 73, "player.state.round_kills", 0),
            *_span(18, 73, "player.state.round_killhs", 0),
            *(_delete(line, "round.bomb") for line in range(36, 73)),
            *DEATH_WITHOUT_ROUND_RESULT,
        ),
        ("弹匣打空", "阵亡"),
        expected_event_type="death",
    ),
    _ct_spec(
        "rare_mvp_round_win",
        "甲",
        "本回合取胜并获得 MVP",
        (_set(132, "player.match_stats.mvps", 1),),
        ("获得MVP", "回合胜利"),
        expected_event_type="round_win",
        source=BASE_CT_SHORT,
    ),
    _ct_spec(
        "rare_assist_round_win",
        "甲",
        "回合中新增一次助攻并获胜",
        (
            _set(131, "player.match_stats.assists", 1),
            _set(132, "player.match_stats.assists", 1),
        ),
        ("助攻", "回合胜利"),
        expected_event_type="round_win",
        source=BASE_CT_SHORT,
    ),
    _ct_spec(
        "rare_grenade_pickup",
        "甲",
        "中途捡到一颗闪光弹后完成回合",
        (
            _set(124, "player.weapons.weapon_5.name", "weapon_flashbang"),
            _set(124, "player.weapons.weapon_5.type", "Grenade"),
            _set(124, "player.weapons.weapon_5.state", "holstered"),
        ),
        ("捡到闪光弹", "回合胜利"),
        expected_event_type="round_win",
        source=BASE_CT_SHORT,
    ),
    _ct_spec(
        "rare_primary_switch",
        "甲",
        "从 M4A1-S 换到 AK47 后结束回合",
        (),
        ("换枪", "AK47", "回合胜利"),
        expected_event_type="round_win",
        source=BASE_CT_SHORT,
    ),
    _ct_spec(
        "rare_flash_interrupted_by_death",
        "甲",
        "被闪状态尚未结束便阵亡",
        (
            *_span(18, 73, "player.state.round_kills", 0),
            *_span(18, 73, "player.state.round_killhs", 0),
            *_span(71, 73, "player.state.flashed", 1),
            *DEATH_WITHOUT_ROUND_RESULT,
        ),
        ("被闪", "未结束", "阵亡"),
        expected_event_type="death",
    ),
    _ct_spec(
        "triple_kill_same_stage",
        "乙",
        "反攻包点阶段用 M4A1-S 完成三杀",
        TRIPLE_SAME_STAGE,
        ("反攻包点", "M4A1-S", "三杀"),
        expected_event_type="multi_kill",
    ),
    _ct_spec(
        "triple_kill_cross_stage",
        "乙",
        "前期先杀一人，反攻包点时再连杀两人完成三杀",
        TRIPLE_CROSS_STAGE,
        ("前期", "反攻包点", "M4A1-S", "三杀"),
        expected_event_type="multi_kill",
    ),
    _ct_spec(
        "triple_kill_headshot_finish",
        "乙",
        "三杀的最后一次击杀为爆头",
        (
            *_span(18, 66, "player.state.round_killhs", 0),
            *_span(67, 73, "player.state.round_killhs", 1),
        ),
        ("三杀", "爆头", "M4A1-S"),
        expected_event_type="round_loss",
    ),
    _ct_spec(
        "weapon_switch_double_kill",
        "乙",
        "先用 M4A1-S 击杀，换到 AK47 后再杀一人",
        (
            *_span(122, 123, "player.state.round_kills", 1),
            *_span(124, 132, "player.state.round_kills", 2),
        ),
        ("M4A1-S", "换枪", "AK47", "双杀"),
        expected_event_type="round_win",
        source=BASE_CT_SHORT,
    ),
    _ct_spec(
        "last_bullet_triple",
        "乙",
        "最后一发子弹完成第三次击杀",
        (*_span(67, 71, "player.weapons.weapon_2.ammo_clip", 1),),
        ("弹匣仅剩1发", "三杀", "M4A1-S"),
        expected_event_type="round_loss",
    ),
    _ct_spec(
        "empty_mag_after_triple",
        "乙",
        "完成三杀后把弹匣打空并阵亡",
        (),
        ("三杀", "弹匣打空", "阵亡", "回合失败"),
    ),
    _ct_spec(
        "low_health_triple",
        "乙",
        "残血状态下完成本回合三杀",
        (
            *_span(62, 73, "player.state.health", 41),
            _set(73, "player.state.health", 0),
        ),
        ("剩41血", "三杀", "M4A1-S", "阵亡", "回合失败"),
    ),
    _ct_spec(
        "four_kill",
        "乙",
        "用 M4A1-S 完成本回合四杀",
        (*_span(71, 73, "player.state.round_kills", 4),),
        ("M4A1-S", "四杀"),
        expected_event_type="multi_kill",
    ),
    _ct_spec(
        "ace",
        "乙",
        "用 M4A1-S 完成本回合五杀",
        (
            *_span(41, 54, "player.state.round_kills", 2),
            *_span(55, 66, "player.state.round_kills", 3),
            *_span(67, 70, "player.state.round_kills", 4),
            *_span(71, 73, "player.state.round_kills", 5),
        ),
        ("M4A1-S", "五杀"),
        expected_event_type="multi_kill",
    ),
    _ct_spec(
        "flash_kill",
        "丙",
        "被闪期间用 M4A1-S 击杀一人",
        (
            *_span(18, 40, "player.state.round_kills", 0),
            *_span(41, 73, "player.state.round_kills", 1),
            *_span(40, 41, "player.state.flashed", 1),
            _set(42, "player.state.flashed", 0),
        ),
        ("被闪", "M4A1-S", "击杀"),
        expected_event_type="kill",
    ),
    _ct_spec(
        "flash_death",
        "丙",
        "被闪期间阵亡",
        (
            *_span(18, 73, "player.state.round_kills", 0),
            *_span(18, 73, "player.state.round_killhs", 0),
            *_span(71, 73, "player.state.flashed", 1),
            *DEATH_WITHOUT_ROUND_RESULT,
        ),
        ("被闪", "阵亡"),
        expected_event_type="death",
    ),
    _ct_spec(
        "flash_double_kill",
        "丙",
        "被闪期间连续完成两次击杀",
        (
            *_span(62, 67, "player.state.flashed", 1),
            _set(68, "player.state.flashed", 0),
            *_span(18, 62, "player.state.round_kills", 0),
            *_span(63, 66, "player.state.round_kills", 1),
            *_span(67, 73, "player.state.round_kills", 2),
        ),
        ("被闪", "连续事件", "双杀", "M4A1-S", "回合失败"),
    ),
    _ct_spec(
        "long_smoke_then_kill",
        "丙",
        "在烟中停留较久后用 M4A1-S 击杀",
        (
            *_span(18, 40, "player.state.round_kills", 0),
            *_span(41, 73, "player.state.round_kills", 1),
            *_span(32, 41, "player.state.smoked", 255),
            _set(42, "player.state.smoked", 0),
        ),
        ("进烟", "M4A1-S", "击杀"),
        expected_event_type="kill",
    ),
    _ct_spec(
        "smoke_exit_death",
        "丙",
        "离开烟雾后很快阵亡",
        (
            *_span(18, 73, "player.state.round_kills", 0),
            *_span(18, 73, "player.state.round_killhs", 0),
            *_span(68, 71, "player.state.smoked", 255),
            _set(72, "player.state.smoked", 0),
            *DEATH_WITHOUT_ROUND_RESULT,
        ),
        ("出烟", "阵亡"),
        expected_event_type="death",
    ),
    _ct_spec(
        "four_grenades_then_kill",
        "丙",
        "一回合连续投出四颗道具后击杀",
        (
            _set(120, "player.weapons.weapon_5.name", "weapon_flashbang"),
            _set(120, "player.weapons.weapon_5.type", "Grenade"),
            _set(120, "player.weapons.weapon_5.state", "holstered"),
            _delete(121, "player.weapons.weapon_5.name"),
            _delete(121, "player.weapons.weapon_5.type"),
            _delete(121, "player.weapons.weapon_5.state"),
        ),
        ("闪光弹×2", "烟雾弹×1", "手雷×1", "M4A1-S", "击杀"),
        expected_event_type="kill_headshot",
        source=BASE_CT_SHORT,
    ),
    _ct_spec(
        "double_flash_then_kill",
        "丙",
        "连续被闪两次后完成击杀",
        (
            *_span(18, 40, "player.state.round_kills", 0),
            *_span(41, 73, "player.state.round_kills", 1),
            *_span(28, 29, "player.state.flashed", 1),
            _set(30, "player.state.flashed", 0),
            *_span(39, 40, "player.state.flashed", 1),
            _set(41, "player.state.flashed", 0),
        ),
        ("玩家被闪", "闪光影响结束", "M4A1-S", "击杀"),
        expected_event_type="kill",
    ),
    _ct_spec(
        "smoke_flash_kill",
        "丙",
        "烟中又被闪时完成击杀",
        (
            *_span(18, 40, "player.state.round_kills", 0),
            *_span(41, 73, "player.state.round_kills", 1),
            *_span(36, 41, "player.state.smoked", 255),
            *_span(40, 41, "player.state.flashed", 1),
            _set(42, "player.state.smoked", 0),
            _set(42, "player.state.flashed", 0),
        ),
        ("烟雾", "被闪", "M4A1-S", "击杀"),
        expected_event_type="kill",
    ),
    _ct_spec(
        "burning_kill",
        "丙",
        "踩火期间用 AK47 击杀一人，随后阵亡",
        (
            *_span(458, 467, "player.state.round_kills", 1),
            *_span(458, 467, "player.match_stats.kills", 6),
            *_span(458, 467, "player.weapons.weapon_2.state", "active"),
            *_span(458, 467, "player.weapons.weapon_3.state", "holstered"),
        ),
        ("燃烧", "AK47", "击杀", "阵亡"),
        expected_event_type="death",
        source=BASE_BURN,
    ),
    _ct_spec(
        "bomb_pickup_then_death",
        "丁",
        "拿到炸弹后阵亡，死亡不额外记作主动丢包",
        (),
        ("拿到包", "阵亡"),
        expected_event_type="death",
        source=BASE_T_C4,
    ),
    _ct_spec(
        "bomb_drop_repickup",
        "丁",
        "主动丢包后又重新捡回，随后阵亡",
        (
            _delete(90, "player.weapons.weapon_4.name"),
            _delete(90, "player.weapons.weapon_4.type"),
            _delete(90, "player.weapons.weapon_4.state"),
            _set(91, "player.weapons.weapon_4.name", "weapon_c4"),
            _set(91, "player.weapons.weapon_4.type", "C4"),
            _set(91, "player.weapons.weapon_4.state", "holstered"),
        ),
        ("丢了包", "拿到包", "阵亡"),
        expected_event_type="death",
        source=BASE_T_C4,
    ),
    _ct_spec(
        "postplant_defuse_win",
        "丁",
        "炸弹安放后由 CT 拆除并获胜",
        (),
        ("炸弹已安放", "拆除", "回合胜利"),
        expected_event_type="round_win",
        source=BASE_CT_DEFUSE,
    ),
    _ct_spec(
        "postplant_counterattack_loss",
        "丁",
        "下包后反攻阶段阵亡并输掉回合",
        (
            *_span(18, 73, "player.state.round_kills", 0),
            *_span(18, 73, "player.state.round_killhs", 0),
        ),
        ("反攻包点", "阵亡", "回合失败"),
    ),
    _ct_spec(
        "postplant_triple_loss",
        "丁",
        "下包后反攻阶段完成三杀但最终回合失败",
        (_set(72, "player.weapons.weapon_2.ammo_clip", 2),),
        ("反攻包点", "三杀", "回合失败"),
    ),
    _ct_spec(
        "bomb_pickup_kill",
        "丁",
        "拿到炸弹后用 AK47 最后一发击杀",
        (
            *_span(65, 96, "player.state.round_kills", 0),
            *_span(97, 100, "player.state.round_kills", 1),
        ),
        ("拿到包", "弹匣仅剩1发", "AK47", "击杀"),
        expected_event_type="kill",
        source=BASE_T_C4,
    ),
    _ct_spec(
        "bomb_planted_then_death",
        "丁",
        "炸弹安放后玩家阵亡",
        (
            *_span(18, 73, "player.state.round_kills", 0),
            *_span(18, 73, "player.state.round_killhs", 0),
            *DEATH_WITHOUT_ROUND_RESULT,
        ),
        ("炸弹已安放", "阵亡"),
        expected_event_type="death",
    ),
    _ct_spec(
        "late_defuse",
        "丁",
        "炸弹安放 33.4 秒后完成拆除",
        (),
        ("炸弹已安放", "33.4", "炸弹已拆除", "回合胜利"),
        expected_event_type="round_win",
        source=BASE_LATE_DEFUSE,
    ),
    _ct_spec(
        "bomb_explosion_win",
        "丁",
        "CT阵亡观战期间炸弹爆炸并输掉回合",
        (),
        ("我方CT", "炸弹已安放", "炸弹引爆", "回合失败"),
        expected_event_type="round_loss",
        source=BASE_EXPLOSION,
    ),
)

SKIPPED_SCENARIOS: tuple[tuple[str, str], ...] = ()


def load_inventory_constraints(
    paths: Sequence[Path] = INVENTORY_PATHS,
) -> dict[str, PathConstraint]:
    """Scan every real recording while restricting paths to the inventory whitelist."""
    allowed_paths: set[str] = set()
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _TABLE_ROW.match(line)
            if match is None or int(match.group(2)) <= 0:
                continue
            allowed_paths.add(match.group(1))

    constraints: dict[str, PathConstraint] = {}
    for recording_path in RECORDINGS_DIRECTORY.glob("*.jsonl"):
        if recording_path.stat().st_size == 0:
            continue
        for raw_line in recording_path.read_text(encoding="utf-8").splitlines():
            wrapper = json.loads(raw_line)
            payload = wrapper.get("payload")
            if isinstance(payload, dict):
                _merge_observed_payload_values(
                    cast(dict[str, Any], payload),
                    constraints,
                    allowed_paths=allowed_paths,
                    source_file=recording_path.name,
                )
    return constraints


def write_observed_constraints(
    constraints: Mapping[str, PathConstraint],
    *,
    output_path: Path = OBSERVED_CONSTRAINTS_PATH,
) -> None:
    """Persist reproducible all-recording evidence without player identities."""
    recording_paths = tuple(
        sorted(
            (
                path
                for path in RECORDINGS_DIRECTORY.glob("*.jsonl")
                if path.stat().st_size > 0
            ),
            key=lambda path: path.name,
        )
    )
    payload_count = sum(
        len(path.read_text(encoding="utf-8").splitlines()) for path in recording_paths
    )
    data: dict[str, dict[str, object]] = {}
    for raw_path, constraint in sorted(constraints.items()):
        sensitive = raw_path.endswith("player.name") or raw_path.endswith("steamid")
        data[raw_path] = {
            "minimum": constraint.minimum,
            "maximum": constraint.maximum,
            "values": [] if sensitive else sorted(constraint.values),
            "source_files": sorted(constraint.source_files),
        }
    if any(
        not entry["values"]
        and entry["minimum"] is not None
        and entry["maximum"] not in (None, 0)
        for entry in data.values()
    ):
        raise ValueError("观测约束含有非零范围却缺少实际取值")
    payload = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_files": [path.name for path in recording_paths],
            "payload_count": payload_count,
        },
        "constraints": data,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _merge_observed_payload_values(
    payload: Mapping[str, Any],
    constraints: dict[str, PathConstraint],
    *,
    allowed_paths: set[str],
    source_file: str,
) -> None:
    for raw_path, value in _iter_scalar_paths(payload):
        normalized = normalize_inventory_path(raw_path)
        if normalized not in allowed_paths:
            continue
        if isinstance(value, bool):
            observed = PathConstraint(
                values=frozenset({str(value).lower()}),
                source_files=frozenset({source_file}),
            )
        elif isinstance(value, (int, float)):
            observed = PathConstraint(
                minimum=float(value),
                maximum=float(value),
                values=frozenset({str(value)}),
                source_files=frozenset({source_file}),
            )
        elif isinstance(value, str):
            observed = PathConstraint(
                values=frozenset({value}), source_files=frozenset({source_file})
            )
        else:
            continue
        constraints[normalized] = constraints.get(
            normalized, PathConstraint()
        ).merge(observed)


def _iter_scalar_paths(
    value: object, prefix: str = ""
) -> Iterable[tuple[str, JsonScalar]]:
    if isinstance(value, dict):
        for key, child in cast(dict[str, Any], value).items():
            child_path = f"{prefix}.{key}" if prefix else key
            yield from _iter_scalar_paths(child, child_path)
    elif isinstance(value, (str, int, float, bool)) or value is None:
        yield prefix, cast(JsonScalar, value)


def normalize_inventory_path(path: str) -> str:
    """Normalize concrete weapon slots to the inventory wildcard form."""
    return _WEAPON_KEY.sub("*", path)


def validate_mutation(
    mutation: Mutation, constraints: Mapping[str, PathConstraint]
) -> None:
    """Reject unknown paths and values outside the observed inventory range."""
    normalized = normalize_inventory_path(mutation.path)
    if normalized not in constraints:
        raise ValueError(f"字段路径不在数据清单白名单中：{mutation.path}")
    if mutation.operation == "delete":
        return
    value = mutation.value
    constraint = constraints[normalized]
    if isinstance(value, bool):
        token = str(value).lower()
        if constraint.values and token not in constraint.values:
            raise ValueError(f"{mutation.path}={value!r} 未在数据清单中观测到")
        return
    if isinstance(value, (int, float)):
        if constraint.minimum is None or constraint.maximum is None:
            raise ValueError(f"{mutation.path} 没有可验证的数值范围")
        if not constraint.minimum <= float(value) <= constraint.maximum:
            raise ValueError(
                f"{mutation.path}={value!r} 超出观测范围 "
                f"{constraint.minimum:g}–{constraint.maximum:g}"
            )
        return
    if isinstance(value, str) and constraint.values and value not in constraint.values:
        raise ValueError(f"{mutation.path}={value!r} 未在数据清单中观测到")


def _parse_template_source(source: str) -> tuple[str, int, int]:
    match = _TEMPLATE_SOURCE.fullmatch(source)
    if match is None:
        raise ValueError(f"无效模板来源：{source}")
    return match.group(1), int(match.group(2)), int(match.group(3))


def _navigate_parent(root: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
    parts = path.split(".")
    current = root
    for part in parts[:-1]:
        child = current.get(part)
        if child is None:
            child = {}
            current[part] = child
        if not isinstance(child, dict):
            raise ValueError(f"字段父级不是对象：{path}")
        current = cast(dict[str, Any], child)
    return current, parts[-1]


def _scrub_identity(
    value: object,
    self_steamids: frozenset[str],
    path: tuple[str, ...] = (),
) -> None:
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        section = path[-1] if path else None
        if section == "provider" and isinstance(mapping.get("steamid"), str):
            mapping["steamid"] = SYNTHETIC_SELF_STEAMID
        elif section == "player":
            raw_steamid = mapping.get("steamid")
            is_self = isinstance(raw_steamid, str) and raw_steamid in self_steamids
            if isinstance(raw_steamid, str):
                mapping["steamid"] = (
                    SYNTHETIC_SELF_STEAMID
                    if is_self
                    else SYNTHETIC_OTHER_STEAMID
                )
            if isinstance(mapping.get("name"), str):
                mapping["name"] = (
                    SYNTHETIC_SELF_NAME if is_self else SYNTHETIC_OTHER_NAME
                )
        for key, child in tuple(mapping.items()):
            child_path = (*path, key)
            _scrub_identity(child, self_steamids, child_path)
    elif isinstance(value, list):
        for child in value:
            _scrub_identity(child, self_steamids, path)


def synthesize_scenario(
    spec: ScenarioSpec,
    *,
    recordings_directory: Path = RECORDINGS_DIRECTORY,
    constraints: Mapping[str, PathConstraint] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Apply validated scalar mutations to a real JSONL slice and anonymize it."""
    active_constraints = constraints or load_inventory_constraints()
    filename, start_line, end_line = _parse_template_source(spec.template_source)
    source_path = recordings_directory / filename
    source_lines = source_path.read_text(encoding="utf-8").splitlines()
    rows = [
        cast(dict[str, Any], json.loads(source_lines[index - 1]))
        for index in range(start_line, end_line + 1)
    ]
    self_steamids = frozenset(
        steamid
        for row in rows
        for payload in (row.get("payload"),)
        if isinstance(payload, dict)
        for provider in (payload.get("provider"),)
        if isinstance(provider, dict)
        for steamid in (provider.get("steamid"),)
        if isinstance(steamid, str)
    )
    if not self_steamids:
        raise ValueError(f"{spec.scenario_id} 的模板没有 provider.steamid")
    by_line = {line: rows[line - start_line] for line in range(start_line, end_line + 1)}
    for mutation in spec.mutations:
        validate_mutation(mutation, active_constraints)
        if mutation.line_number not in by_line:
            raise ValueError(
                f"{spec.scenario_id} 的改造行 {mutation.line_number} 不在模板区间内"
            )
        payload = by_line[mutation.line_number].get("payload")
        if not isinstance(payload, dict):
            raise ValueError(f"模板第 {mutation.line_number} 行没有 payload 对象")
        parent, key = _navigate_parent(cast(dict[str, Any], payload), mutation.path)
        if mutation.operation == "delete":
            parent.pop(key, None)
        else:
            parent[key] = mutation.value
    for row in rows:
        _scrub_identity(row, self_steamids)
    return tuple(rows)


def write_scenario(
    spec: ScenarioSpec,
    rows: Sequence[Mapping[str, Any]],
    *,
    scenarios_directory: Path = SCENARIOS_DIRECTORY,
) -> Path:
    """Write one deterministic compact JSONL scenario."""
    scenarios_directory.mkdir(parents=True, exist_ok=True)
    output = scenarios_directory / f"{spec.scenario_id}.jsonl"
    output.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    return output


def _selected_card(path: Path, event_type: EventType) -> BenchEvent:
    result = run_bench(
        path,
        model=None,
        provider=None,
        personality_style="inference",
        client=None,
        max_events=40,
        cards_only=True,
    )
    matches = tuple(event for event in result.events if event.event.type == event_type)
    if not matches:
        observed = ", ".join(event.event.type for event in result.events) or "无"
        raise ValueError(f"{path.name} 未选中 {event_type}；实际为：{observed}")
    return matches[-1]


def _timeline_kind_counts(paths: Iterable[Path]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in paths:
        session = GameSessionTracker(GSI_SILENCE_SECONDS)
        lifecycle = MatchLifecycleTracker()
        tracker = SituationTracker()
        seen: set[tuple[int, int | None, float, str, str | None]] = set()
        segment = 0
        for snapshot in load_recording(path):
            game = session.observe(snapshot)
            if lifecycle.observe(game):
                tracker.reset()
                segment += 1
            situation = tracker.observe(snapshot, game)
            for entry in situation.timeline:
                fingerprint = (
                    segment,
                    situation.round_number,
                    entry.seconds,
                    entry.kind,
                    entry.detail,
                )
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    counts[entry.kind] += 1
    return counts


def _scenario_report(specs: Sequence[ScenarioSpec]) -> str:
    lines = ["# M3-T6 合成场景定义", ""]
    for spec in specs:
        lines.extend(
            (
                f"### {spec.scenario_id} —— {spec.description}",
                f"- 分类：{spec.category}类",
                f"- 模板来源：{spec.template_source}",
                "- 改造："
                + (
                    "；".join(
                        f"第{item.line_number}行 {item.operation} {item.path}"
                        + (f"={item.value!r}" if item.operation == "set" else "")
                        for item in spec.mutations
                    )
                    if spec.mutations
                    else "沿用模板中的已观测状态变化，不额外改值"
                ),
                "- 必答：" + "、".join(spec.required_facts),
                "- 禁项：" + "；".join(spec.forbidden_claims),
                "",
            )
        )
    lines.extend(("## 未生成的越界场景", ""))
    lines.extend(
        f"- `{scenario_id}`：{reason}"
        for scenario_id, reason in SKIPPED_SCENARIOS
    )
    if not SKIPPED_SCENARIOS:
        lines.append("- 无")
    lines.append("")
    return "\n".join(lines)


def _cards_report(
    specs: Sequence[ScenarioSpec], cards: Mapping[str, BenchEvent]
) -> str:
    lines = [
        "# M3-T6 合成场景事件卡（cards-only）",
        "",
        "- 模型调用次数：0（cards-only）",
        "- 本报告仅复用生产 EventDetector、SpeechPolicy 与事件卡渲染器。",
        "",
    ]
    for spec in specs:
        event = cards[spec.scenario_id]
        lines.extend(
            (
                f"## `{spec.scenario_id}`",
                "",
                f"场景：{spec.description}",
                f"预期事件：`{spec.expected_event_type}`",
                "必答清单：" + "、".join(spec.required_facts),
                "",
                "```text",
                event.event_card,
                "```",
                "",
            )
        )
    return "\n".join(lines)


def _answer_keys(specs: Sequence[ScenarioSpec]) -> str:
    data = [
        {
            "case_id": spec.scenario_id,
            "expected_summary": spec.description,
            "required_facts": list(spec.required_facts),
            "forbidden_claims": list(spec.forbidden_claims),
        }
        for spec in specs
    ]
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def generate_all(
    *,
    specs: Sequence[ScenarioSpec] = SCENARIO_SPECS,
    scenarios_directory: Path = SCENARIOS_DIRECTORY,
    reports_directory: Path = REPORTS_DIRECTORY,
) -> tuple[Path, ...]:
    """Generate all scenarios and zero-call review artifacts."""
    constraints = load_inventory_constraints()
    write_observed_constraints(constraints)
    scenarios_directory.mkdir(parents=True, exist_ok=True)
    expected_names = {f"{spec.scenario_id}.jsonl" for spec in specs}
    for stale_path in scenarios_directory.glob("*.jsonl"):
        if stale_path.name not in expected_names:
            stale_path.unlink()
    paths: list[Path] = []
    cards: dict[str, BenchEvent] = {}
    for spec in specs:
        rows = synthesize_scenario(spec, constraints=constraints)
        output = write_scenario(spec, rows, scenarios_directory=scenarios_directory)
        paths.append(output)
        cards[spec.scenario_id] = _selected_card(output, spec.expected_event_type)

    reports_directory.mkdir(parents=True, exist_ok=True)
    (reports_directory / "m3-t6-scenarios.md").write_text(
        _scenario_report(specs), encoding="utf-8"
    )
    (reports_directory / "m3-t6-cards-only.md").write_text(
        _cards_report(specs, cards), encoding="utf-8"
    )
    (reports_directory / "m3-t6-answer-keys.json").write_text(
        _answer_keys(specs), encoding="utf-8"
    )

    real_paths = tuple(
        path
        for path in RECORDINGS_DIRECTORY.glob("*.jsonl")
        if path.stat().st_size > 0
    )
    real_counts = _timeline_kind_counts(real_paths)
    synthetic_counts = _timeline_kind_counts(paths)
    coverage_kinds: tuple[TimelineKind, ...] = (
        "round_live",
        "bought",
        *REPORTED_TIMELINE_KINDS,
        "burn_start",
        "burn_end",
    )
    coverage_lines = [
        "# M3-T6 时间线覆盖统计",
        "",
        "当前代码共有 22 个 TimelineKind；以下同时列出规格中的18个动作/状态 kind、",
        "round_live 与 bought 两个锚点，以及已由完整真实录制证实的 burn_start/burn_end。",
        "",
        "| kind | 全部真实录制 | 合成集 |",
        "|---|---:|---:|",
    ]
    coverage_lines.extend(
        f"| `{kind}` | {real_counts[kind]} | {synthetic_counts[kind]} |"
        for kind in coverage_kinds
    )
    coverage_lines.extend(
        (
            "",
            f"- 场景数：{len(specs)}",
            f"- 合成录制总大小：{sum(path.stat().st_size for path in paths)} bytes",
            "- 模型调用次数：0",
            "",
        )
    )
    (reports_directory / "m3-t6-timeline-coverage.md").write_text(
        "\n".join(coverage_lines), encoding="utf-8"
    )
    return tuple(paths)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generate", action="store_true", help="生成全部合成录制与 cards-only 报告"
    )
    args = parser.parse_args(argv)
    if not args.generate:
        parser.error("请指定 --generate")
    paths = generate_all()
    print(f"generated {len(paths)} scenarios; model calls: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
