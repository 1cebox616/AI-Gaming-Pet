"""Interpret CS2 GSI snapshots as one current game session state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from pet.gsi import GameSnapshot

GameSessionState = Literal[
    "offline",
    "menu",
    "warmup",
    "playing",
    "spectating",
    "round_over",
    "match_over",
]


class GameState(BaseModel):
    """The game information exposed to desktop clients."""

    model_config = ConfigDict(frozen=True)

    state: GameSessionState
    mode: str | None = None
    map: str | None = None
    round: int | None = None
    score_ct: int | None = None
    score_t: int | None = None
    subject_steamid: str | None = None
    subject_is_self: bool | None = None

    @classmethod
    def offline(cls) -> GameState:
        return cls(state="offline")


class GameSessionTracker:
    """Reduce ordered snapshots into a stable, timeout-aware game state."""

    def __init__(self, offline_timeout_seconds: float) -> None:
        if offline_timeout_seconds <= 0:
            raise ValueError("offline timeout must be positive")
        self._offline_timeout_seconds = offline_timeout_seconds
        self._last_snapshot_at: float | None = None
        self._game = GameState.offline()

    def observe(self, snapshot: GameSnapshot) -> GameState:
        """Interpret one received snapshot and remember it as current."""
        self._last_snapshot_at = snapshot.ts
        interpreted = _interpret_snapshot(snapshot)
        if interpreted is not None:
            self._game = interpreted
        return self._game

    def current(self, *, now: float) -> GameState:
        """Return offline after the configured silence window expires."""
        if (
            self._last_snapshot_at is None
            or now - self._last_snapshot_at > self._offline_timeout_seconds
        ):
            self._game = GameState.offline()
        return self._game


def _interpret_snapshot(snapshot: GameSnapshot) -> GameState | None:
    has_map = any(
        value is not None
        for value in (
            snapshot.map_mode,
            snapshot.map_name,
            snapshot.map_phase,
            snapshot.round_number,
            snapshot.score_ct,
            snapshot.score_t,
        )
    )
    has_session_signal = any(
        value is not None
        for value in (
            snapshot.provider_steamid,
            snapshot.player_steamid,
            snapshot.activity,
        )
    )

    if not has_map and not has_session_signal:
        return None

    subject_steamid, subject_is_self = _identify_subject(snapshot)
    if not has_map:
        state: GameSessionState = "menu"
    elif snapshot.map_phase == "gameover":
        state = "match_over"
    elif snapshot.map_phase == "warmup":
        state = "warmup"
    elif snapshot.round_phase == "over" or snapshot.round_win_team is not None:
        state = "round_over"
    elif subject_is_self is False:
        state = "spectating"
    else:
        state = "playing"

    round_number = (
        snapshot.round_number + 1
        if snapshot.round_number is not None and state != "warmup"
        else None
    )
    return GameState(
        state=state,
        mode=snapshot.map_mode,
        map=snapshot.map_name,
        round=round_number,
        score_ct=snapshot.score_ct,
        score_t=snapshot.score_t,
        subject_steamid=subject_steamid,
        subject_is_self=subject_is_self,
    )


def _identify_subject(snapshot: GameSnapshot) -> tuple[str | None, bool | None]:
    provider_steamid = snapshot.provider_steamid
    player_steamid = snapshot.player_steamid
    if provider_steamid is None:
        return player_steamid, None
    if player_steamid is not None and player_steamid != provider_steamid:
        return player_steamid, False
    return provider_steamid, True
