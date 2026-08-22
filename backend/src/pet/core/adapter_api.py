"""Versioned boundary between the game-independent core and game adapters.

``interrupt`` and ``supersedes_request_id`` are retained by the core in port v1,
but do not yet alter delivery.  Current delivery therefore remains equivalent to
``interrupt=True`` for every accepted request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

PORT_VERSION = 1
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8737
BACKEND_HTTP_ORIGIN = f"http://{BACKEND_HOST}:{BACKEND_PORT}"


class SpeechRequest(BaseModel):
    """One immutable, game-owned request for the core speech pipeline."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    game_id: str
    fact_text: str
    urgency: int = Field(ge=0, le=100)
    interrupt: bool
    supersedes_request_id: str | None
    vocabulary_id: str | None
    llm_profile: str | None
    fallback_text: str | None
    fallback_emotion: str | None
    ts: float


class GameStatus(BaseModel):
    """Game-defined status whose summary is opaque to the core."""

    model_config = ConfigDict(frozen=True)

    game_id: str
    state: str
    summary: dict[str, str | int | None]


class CoreServices(BaseModel):
    """Core callbacks exposed to an adapter.

    The three lifecycle/read callbacks preserve pre-port mute, consumer-quota,
    and per-match speaker-reset behavior; they carry no game-specific data.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    submit_speech: Callable[[SpeechRequest], Awaitable[None]]
    publish_status: Callable[[GameStatus], Awaitable[None]]
    can_submit_speech: Callable[[], bool]
    speech_is_muted: Callable[[], bool]
    reset_speech_session: Callable[[], Awaitable[None]]


class GameAdapter(Protocol):
    """Port-v1 lifecycle and transport surface implemented by every game."""

    adapter_id: str
    display_name: str
    port_version: int
    http_router: APIRouter | None

    async def start(self, core: CoreServices) -> None: ...

    async def stop(self) -> None: ...
