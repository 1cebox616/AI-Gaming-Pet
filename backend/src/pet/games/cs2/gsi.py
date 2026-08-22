"""Receive, parse, record, and install CS2 Game State Integration data."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from pet.core.adapter_api import BACKEND_HTTP_ORIGIN
from pet.core.config import GsiConfig

if sys.platform == "win32":
    import winreg

logger = logging.getLogger(__name__)
_WARNED_TYPE_PATHS: set[tuple[str, str]] = set()

GSI_CONFIG_FILENAME = "gamestate_integration_ai_gaming_pet.cfg"
GSI_APP_ID = "730"
GSI_ENDPOINT = f"{BACKEND_HTTP_ORIGIN}/gsi"
GSI_SILENCE_SECONDS = 60.0
GSI_SUMMARY_INTERVAL_SECONDS = 1.0
RECORDINGS_DIRECTORY = Path(__file__).resolve().parents[4] / "recordings"
GSI_CONFIG_CONTENT = f'''"AI Gaming Pet"
{{
    "uri" "{GSI_ENDPOINT}"
    "timeout" "5.0"
    "buffer" "0.1"
    "throttle" "0.1"
    "heartbeat" "30.0"
    "data"
    {{
        "provider" "1"
        "map" "1"
        "round" "1"
        "player_id" "1"
        "player_state" "1"
        "player_match_stats" "1"
        "player_weapons" "1"
        "map_round_wins" "1"
    }}
}}
'''

RawPayload = Any


class RoundWin(BaseModel):
    """One completed round from CS2's map.round_wins history."""

    model_config = ConfigDict(frozen=True)

    round: int
    team: Literal["CT", "T"]
    method: str


@dataclass(frozen=True, slots=True)
class WeaponSlot:
    """One weapon from CS2's player.weapons block."""

    name: str
    type: str | None
    ammo_clip: int | None
    ammo_clip_max: int | None
    ammo_reserve: int | None
    state: str | None


class GameSnapshot(BaseModel):
    """The normalized subset of one CS2 GSI payload used by later milestones."""

    ts: float
    player_steamid: str | None = None
    provider_steamid: str | None = None
    activity: str | None = None
    map_mode: str | None = None
    map_name: str | None = None
    map_phase: str | None = None
    round_number: int | None = None
    round_phase: str | None = None
    round_win_team: str | None = None
    bomb_state: str | None = None
    team: str | None = None
    health: int | None = None
    armor: int | None = None
    helmet: bool | None = None
    money: int | None = None
    equip_value: int | None = None
    has_defusekit: bool | None = None
    flashed: int | None = None
    smoked: int | None = None
    burning: int | None = None
    round_kills: int | None = None
    round_killhs: int | None = None
    match_kills: int | None = None
    match_assists: int | None = None
    match_deaths: int | None = None
    match_mvps: int | None = None
    match_score: int | None = None
    score_ct: int | None = None
    score_t: int | None = None
    ct_consecutive_round_losses: int | None = None
    t_consecutive_round_losses: int | None = None
    round_wins: tuple[RoundWin, ...] | None = None
    active_weapon: str | None = None
    weapons: tuple[WeaponSlot, ...] | None = None


class GsiAck(BaseModel):
    """Always-successful acknowledgement returned to CS2."""

    status: Literal["ok"] = "ok"


def parse_snapshot(payload: object, *, received_at: float | None = None) -> GameSnapshot:
    """Normalize one payload while isolating type errors to individual fields."""
    ts = time.time() if received_at is None else received_at
    if not isinstance(payload, Mapping):
        logger.warning("ignoring structurally invalid CS2 GSI payload: expected an object")
        return GameSnapshot(ts=ts)

    return GameSnapshot(
        ts=ts,
        player_steamid=_read(payload, ("player", "steamid"), str),
        provider_steamid=_read(payload, ("provider", "steamid"), str),
        activity=_read(payload, ("player", "activity"), str),
        map_mode=_read(payload, ("map", "mode"), str),
        map_name=_read(payload, ("map", "name"), str),
        map_phase=_read(payload, ("map", "phase"), str),
        round_number=_read(payload, ("map", "round"), int),
        round_phase=_read(payload, ("round", "phase"), str),
        round_win_team=_read(payload, ("round", "win_team"), str),
        bomb_state=_read(payload, ("round", "bomb"), str),
        team=_read(payload, ("player", "team"), str),
        health=_read(payload, ("player", "state", "health"), int),
        armor=_read(payload, ("player", "state", "armor"), int),
        helmet=_read(payload, ("player", "state", "helmet"), bool),
        money=_read(payload, ("player", "state", "money"), int),
        equip_value=_read(payload, ("player", "state", "equip_value"), int),
        has_defusekit=_read_presence_bool(
            payload, ("player", "state", "defusekit")
        ),
        flashed=_read(payload, ("player", "state", "flashed"), int),
        smoked=_read(payload, ("player", "state", "smoked"), int),
        burning=_read(payload, ("player", "state", "burning"), int),
        round_kills=_read(payload, ("player", "state", "round_kills"), int),
        round_killhs=_read(payload, ("player", "state", "round_killhs"), int),
        match_kills=_read(payload, ("player", "match_stats", "kills"), int),
        match_assists=_read(payload, ("player", "match_stats", "assists"), int),
        match_deaths=_read(payload, ("player", "match_stats", "deaths"), int),
        match_mvps=_read(payload, ("player", "match_stats", "mvps"), int),
        match_score=_read(payload, ("player", "match_stats", "score"), int),
        score_ct=_read(payload, ("map", "team_ct", "score"), int),
        score_t=_read(payload, ("map", "team_t", "score"), int),
        ct_consecutive_round_losses=_read(
            payload, ("map", "team_ct", "consecutive_round_losses"), int
        ),
        t_consecutive_round_losses=_read(
            payload, ("map", "team_t", "consecutive_round_losses"), int
        ),
        round_wins=_read_round_wins(payload),
        active_weapon=_read_active_weapon(payload),
        weapons=_read_weapons(payload),
    )


def human_round_number(snapshot: GameSnapshot) -> int | None:
    """Return the one human-readable round described by a snapshot."""
    if snapshot.round_number is None:
        return None
    if snapshot.round_phase == "over" or snapshot.round_win_team is not None:
        return snapshot.round_number
    return snapshot.round_number + 1


def _read(
    payload: Mapping[str, Any], path: tuple[str, ...], expected_type: type[str | int | bool]
) -> str | int | bool | None:
    current: object = payload
    for index, part in enumerate(path):
        if not isinstance(current, Mapping):
            _warn_type_once(".".join(path[:index]), current, "object")
            return None
        if part not in current:
            return None
        current = current[part]

    valid = isinstance(current, expected_type)
    if expected_type is int and isinstance(current, bool):
        valid = False
    if valid:
        return current  # type: ignore[return-value]

    _warn_type_once(".".join(path), current, expected_type.__name__)
    return None


def _read_presence_bool(
    payload: Mapping[str, Any], path: tuple[str, ...]
) -> bool | None:
    """Read a boolean that GSI omits when false, preserving missing parents."""
    parent = _mapping_at(payload, path[:-1])
    if parent is None:
        return None
    field = path[-1]
    if field not in parent:
        return False
    value = parent[field]
    if isinstance(value, bool):
        return value
    _warn_type_once(".".join(path), value, "bool")
    return None


def _read_active_weapon(payload: Mapping[str, Any]) -> str | None:
    weapons = _mapping_at(payload, ("player", "weapons"))
    if weapons is None:
        return None
    for weapon_key, weapon in weapons.items():
        if not isinstance(weapon, Mapping):
            _warn_type_once(f"player.weapons.{weapon_key}", weapon, "object")
            continue
        if weapon.get("state") != "active":
            continue
        name = weapon.get("name")
        if isinstance(name, str):
            return name
        _warn_type_once("player.weapons.<active>.name", name, "str")
        return None
    return None


def _read_weapons(payload: Mapping[str, Any]) -> tuple[WeaponSlot, ...] | None:
    weapons = _mapping_at(payload, ("player", "weapons"))
    if weapons is None:
        return None

    indexed_weapons: list[tuple[int, str, Mapping[str, Any]]] = []
    for weapon_key, weapon in weapons.items():
        if not isinstance(weapon_key, str):
            _warn_type_once("player.weapons.<key>", weapon_key, "str")
            continue
        match = re.fullmatch(r"weapon_(\d+)", weapon_key)
        if match is None:
            _warn_type_once("player.weapons.<key>", weapon_key, "weapon_<number>")
            continue
        if not isinstance(weapon, Mapping):
            _warn_type_once(f"player.weapons.{weapon_key}", weapon, "object")
            continue
        indexed_weapons.append((int(match.group(1)), weapon_key, weapon))

    parsed: list[WeaponSlot] = []
    for _, weapon_key, weapon in sorted(
        indexed_weapons, key=lambda item: (item[0], item[1])
    ):
        name = _read_weapon_value(weapon, weapon_key, "name", str)
        if name is None:
            continue
        parsed.append(
            WeaponSlot(
                name=name,
                type=_read_weapon_value(weapon, weapon_key, "type", str),
                ammo_clip=_read_weapon_value(weapon, weapon_key, "ammo_clip", int),
                ammo_clip_max=_read_weapon_value(
                    weapon, weapon_key, "ammo_clip_max", int
                ),
                ammo_reserve=_read_weapon_value(
                    weapon, weapon_key, "ammo_reserve", int
                ),
                state=_read_weapon_value(weapon, weapon_key, "state", str),
            )
        )
    return tuple(parsed)


def _read_weapon_value(
    weapon: Mapping[str, Any],
    weapon_key: str,
    field: str,
    expected_type: type[str | int],
) -> str | int | None:
    if field not in weapon:
        return None
    value = weapon[field]
    valid = isinstance(value, expected_type)
    if expected_type is int and isinstance(value, bool):
        valid = False
    if valid:
        return value  # type: ignore[return-value]
    _warn_type_once(
        f"player.weapons.{weapon_key}.{field}", value, expected_type.__name__
    )
    return None


def _read_round_wins(payload: Mapping[str, Any]) -> tuple[RoundWin, ...] | None:
    round_wins = _mapping_at(payload, ("map", "round_wins"))
    if round_wins is None:
        return None

    parsed: list[RoundWin] = []
    for round_key, win_code in round_wins.items():
        if not isinstance(round_key, str) or not round_key.isdecimal():
            _warn_type_once("map.round_wins.<round>", round_key, "decimal str")
            continue
        if not isinstance(win_code, str):
            _warn_type_once(f"map.round_wins.{round_key}", win_code, "str")
            continue

        team_code, separator, method = win_code.partition("_win_")
        if separator == "" or method == "" or team_code not in {"ct", "t"}:
            logger.warning("ignoring unrecognized CS2 round win code %r", win_code)
            continue
        parsed.append(
            RoundWin(
                round=int(round_key),
                team="CT" if team_code == "ct" else "T",
                method=method,
            )
        )

    return tuple(sorted(parsed, key=lambda win: win.round))


def _mapping_at(payload: Mapping[str, Any], path: tuple[str, ...]) -> Mapping[str, Any] | None:
    current: object = payload
    for index, part in enumerate(path):
        if not isinstance(current, Mapping):
            _warn_type_once(".".join(path[:index]), current, "object")
            return None
        if part not in current:
            return None
        current = current[part]
    if isinstance(current, Mapping):
        return current
    _warn_type_once(".".join(path), current, "object")
    return None


def _warn_type_once(path: str, value: object, expected: str) -> None:
    warning_key = (path, type(value).__name__)
    if warning_key in _WARNED_TYPE_PATHS:
        return
    _WARNED_TYPE_PATHS.add(warning_key)
    logger.warning(
        "CS2 GSI field %s has type %s; expected %s and parsed it as None",
        path,
        type(value).__name__,
        expected,
    )


class RawGsiRecorder:
    """Append raw payloads through one background writer task."""

    def __init__(self, enabled: bool, recordings_directory: Path = RECORDINGS_DIRECTORY) -> None:
        self._enabled = enabled
        self._directory = recordings_directory
        self._queue: asyncio.Queue[str | None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self.path: Path | None = None

    async def start(self) -> None:
        if not self._enabled:
            logger.info("CS2 GSI raw recording is disabled")
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.path = self._directory / f"gsi-{timestamp}.jsonl"
        await asyncio.to_thread(self.path.touch)
        self._queue = asyncio.Queue()
        self._writer_task = asyncio.create_task(self._write_lines(), name="gsi-recorder")
        logger.info("recording raw CS2 GSI payloads to %s", self.path)

    def record(self, received_at: float, payload: RawPayload) -> None:
        if (
            self._writer_task is None
            or self._writer_task.done()
            or self._queue is None
        ):
            return
        line = json.dumps(
            {"ts": received_at, "payload": payload}, ensure_ascii=False, separators=(",", ":")
        )
        self._queue.put_nowait(line)

    async def shutdown(self) -> None:
        if self._writer_task is None or self._queue is None:
            return
        self._queue.put_nowait(None)
        await self._writer_task
        self._writer_task = None
        self._queue = None

    async def _write_lines(self) -> None:
        assert self.path is not None
        assert self._queue is not None
        while True:
            line = await self._queue.get()
            if line is None:
                return
            try:
                await asyncio.to_thread(_append_line, self.path, line)
            except OSError as error:
                logger.error("stopped CS2 GSI recording after a write failure: %s", error)
                return


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as recording:
        recording.write(line)
        recording.write("\n")


class GsiService:
    """Own the low-latency receive path and its background resources."""

    def __init__(
        self,
        configuration: GsiConfig,
        recordings_directory: Path = RECORDINGS_DIRECTORY,
        snapshot_listener: Callable[[GameSnapshot], Awaitable[None]] | None = None,
        offline_listener: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._recorder = RawGsiRecorder(configuration.record, recordings_directory)
        self._snapshot_listener = snapshot_listener
        self._offline_listener = offline_listener
        self._snapshot_queue: asyncio.Queue[GameSnapshot | None] | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._last_summary_at = 0.0
        self._last_received_at: float | None = None
        self._silence_reported = False
        self._monitor_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._recorder.start()
        if self._snapshot_listener is not None:
            self._snapshot_queue = asyncio.Queue()
            self._listener_task = asyncio.create_task(
                self._run_snapshot_listener(),
                name="gsi-snapshot-listener",
            )
        self._monitor_task = asyncio.create_task(self._monitor_silence(), name="gsi-monitor")

    async def shutdown(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        if self._snapshot_queue is not None and self._listener_task is not None:
            self._snapshot_queue.put_nowait(None)
            await self._listener_task
            self._snapshot_queue = None
            self._listener_task = None
        await self._recorder.shutdown()

    async def receive(self, request: Request) -> GsiAck:
        received_at = time.time()
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            logger.warning("received invalid CS2 GSI JSON and ignored it: %s", error)
            return GsiAck()

        snapshot = parse_snapshot(payload, received_at=received_at)
        self._recorder.record(received_at, payload)
        self._observe(snapshot)
        if self._snapshot_queue is not None:
            self._snapshot_queue.put_nowait(snapshot)
        return GsiAck()

    async def _run_snapshot_listener(self) -> None:
        assert self._snapshot_queue is not None
        assert self._snapshot_listener is not None
        while True:
            snapshot = await self._snapshot_queue.get()
            if snapshot is None:
                return
            try:
                await self._snapshot_listener(snapshot)
            except Exception as error:
                logger.error("failed to publish a CS2 game snapshot: %s", error)

    def _observe(self, snapshot: GameSnapshot) -> None:
        now = time.monotonic()
        if self._silence_reported:
            logger.info("CS2 GSI updates resumed after more than 60 seconds")
            self._silence_reported = False
        self._last_received_at = now
        if now - self._last_summary_at < GSI_SUMMARY_INTERVAL_SECONDS:
            return
        self._last_summary_at = now
        same_identity = (
            None
            if snapshot.player_steamid is None or snapshot.provider_steamid is None
            else snapshot.player_steamid == snapshot.provider_steamid
        )
        logger.info(
            "CS2 GSI activity=%s mode=%s round=%s round_phase=%s health=%s "
            "round_kills=%s player_is_provider=%s",
            snapshot.activity,
            snapshot.map_mode,
            snapshot.round_number,
            snapshot.round_phase,
            snapshot.health,
            snapshot.round_kills,
            same_identity,
        )

    async def _monitor_silence(self) -> None:
        while True:
            await asyncio.sleep(1)
            if (
                self._last_received_at is not None
                and not self._silence_reported
                and time.monotonic() - self._last_received_at > GSI_SILENCE_SECONDS
            ):
                logger.info("no CS2 GSI update received for more than 60 seconds")
                self._silence_reported = True
                if self._offline_listener is not None:
                    await self._offline_listener()


def ensure_gsi_config() -> Path | None:
    """Install or repair the CS2 integration file, returning its path when found."""
    target = find_cs2_cfg_directory()
    if target is None:
        logger.error(
            "could not find CS2. Place %s in the game's game\\csgo\\cfg directory with "
            "this complete content:\n%s",
            GSI_CONFIG_FILENAME,
            GSI_CONFIG_CONTENT,
        )
        return None

    config_path = target / GSI_CONFIG_FILENAME
    try:
        existing = config_path.read_text(encoding="utf-8") if config_path.is_file() else None
        if existing != GSI_CONFIG_CONTENT:
            config_path.write_text(GSI_CONFIG_CONTENT, encoding="utf-8", newline="\n")
            logger.info("installed CS2 GSI configuration at %s", config_path)
        else:
            logger.info("CS2 GSI configuration is current at %s", config_path)
    except OSError as error:
        logger.error(
            "could not write CS2 GSI configuration %s: %s. Required content:\n%s",
            config_path,
            error,
            GSI_CONFIG_CONTENT,
        )
        return None
    return config_path


def find_cs2_cfg_directory() -> Path | None:
    """Resolve CS2 from Steam registry metadata and library manifests."""
    if sys.platform != "win32":
        logger.error("automatic CS2 GSI installation is supported only on Windows")
        return None
    for steam_root in _steam_roots_from_registry():
        libraries_file = steam_root / "steamapps" / "libraryfolders.vdf"
        try:
            libraries = _parse_keyvalues(libraries_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.warning("could not parse Steam library list %s: %s", libraries_file, error)
            continue
        library_table = libraries.get("libraryfolders")
        if not isinstance(library_table, Mapping):
            continue
        for library in library_table.values():
            if not isinstance(library, Mapping):
                continue
            apps = library.get("apps")
            library_path = library.get("path")
            if not isinstance(apps, Mapping) or GSI_APP_ID not in apps or not isinstance(
                library_path, str
            ):
                continue
            cfg_directory = _cfg_directory_from_library(Path(library_path))
            if cfg_directory is not None and cfg_directory.is_dir():
                return cfg_directory
            if cfg_directory is not None:
                logger.error(
                    "CS2 manifest found, but expected GSI config path is %s",
                    cfg_directory / GSI_CONFIG_FILENAME,
                )
    return None


def _cfg_directory_from_library(library: Path) -> Path | None:
    manifest = library / "steamapps" / f"appmanifest_{GSI_APP_ID}.acf"
    try:
        manifest_data = _parse_keyvalues(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("could not parse CS2 Steam manifest %s: %s", manifest, error)
        return None
    app_state = manifest_data.get("AppState")
    install_directory = app_state.get("installdir") if isinstance(app_state, Mapping) else None
    if not isinstance(install_directory, str):
        return None
    return (
        library
        / "steamapps"
        / "common"
        / install_directory
        / "game"
        / "csgo"
        / "cfg"
    )


def _steam_roots_from_registry() -> Sequence[Path]:
    roots: list[Path] = []
    registry_locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for hive, key_name, value_name in registry_locations:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if isinstance(value, str):
            path = Path(value)
            if path not in roots:
                roots.append(path)
    return roots


_TOKEN_PATTERN = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])')


def _parse_keyvalues(text: str) -> dict[str, Any]:
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(text):
        quoted, brace = match.groups()
        tokens.append(_unescape_keyvalues(quoted) if quoted is not None else brace)
    position = 0

    def parse_table(expect_closing: bool) -> dict[str, Any]:
        nonlocal position
        table: dict[str, Any] = {}
        while position < len(tokens):
            token = tokens[position]
            position += 1
            if token == "}":
                if expect_closing:
                    return table
                raise ValueError("unexpected closing brace")
            if token == "{":
                raise ValueError("unexpected opening brace")
            key = token
            if position >= len(tokens):
                raise ValueError(f"missing value for {key}")
            value = tokens[position]
            position += 1
            if value == "{":
                table[key] = parse_table(True)
            elif value == "}":
                raise ValueError(f"missing value for {key}")
            else:
                table[key] = value
        if expect_closing:
            raise ValueError("missing closing brace")
        return table

    return parse_table(False)


def _unescape_keyvalues(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the CS2 GSI integration")
    parser.add_argument("--install", action="store_true", help="install or repair the CS2 config")
    arguments = parser.parse_args()
    if not arguments.install:
        parser.error("the only supported action is --install")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    if ensure_gsi_config() is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
