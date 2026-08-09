"""WebSocket bridge between the backend and connected desktop pets."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Mapping
from typing import Any, Literal

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from pet.config import IdleConfig
from pet.lines import Utterance, next_idle_utterance
from pet.session import GameState
from pet.speech import SpeechService

logger = logging.getLogger(__name__)

UTTERANCE_MESSAGE_TYPE = "utterance"
REQUEST_IDLE_LINE_MESSAGE_TYPE = "request_idle_line"
STATE_MESSAGE_TYPE = "state"
SET_SPEECH_ENABLED_MESSAGE_TYPE = "set_speech_enabled"
SET_MUTED_MESSAGE_TYPE = "set_muted"
GAME_PROGRESS_BROADCAST_INTERVAL_SECONDS = 1.0
IDLE_GAME_STATES = {"offline", "menu"}


class BridgeStateMessage(BaseModel):
    """Authoritative runtime switches sent to every desktop client."""

    type: Literal["state"] = STATE_MESSAGE_TYPE
    speech_enabled: bool
    muted: bool
    game: GameState


class PetBridge:
    """Manage active pet WebSocket connections and their dialogue requests."""

    def __init__(
        self,
        speech_service: SpeechService,
        idle_configuration: IdleConfig | None = None,
        initial_game: GameState | None = None,
    ) -> None:
        self._connections: set[WebSocket] = set()
        self._speech_service = speech_service
        self._idle_configuration = idle_configuration or IdleConfig()
        self._idle_task: asyncio.Task[None] | None = None
        self._idle_reset_event: asyncio.Event | None = None
        self._speech_enabled = speech_service.is_enabled()
        self._muted = not self._idle_configuration.enabled
        self._game = initial_game or GameState.offline()
        self._last_game_broadcast_at = float("-inf")
        self._pending_game_broadcast: asyncio.Task[None] | None = None
        self._utterance_broadcast_lock = asyncio.Lock()

    async def start_idle_broadcasts(self) -> None:
        """Start the randomized idle loop, initially paused when configured off."""
        if self._idle_task is not None and not self._idle_task.done():
            return

        self._idle_reset_event = asyncio.Event()
        self._idle_task = asyncio.create_task(
            self._run_idle_broadcasts(),
            name="pet-idle-broadcasts",
        )

    async def shutdown(self) -> None:
        """Cancel the idle loop before FastAPI closes its event loop."""
        await self._cancel_pending_game_broadcast()
        idle_task = self._idle_task
        self._idle_task = None
        self._idle_reset_event = None
        if idle_task is None:
            return

        idle_task.cancel()
        try:
            await idle_task
        except asyncio.CancelledError:
            logger.info("idle broadcast task stopped")

    async def serve(self, websocket: WebSocket) -> None:
        """Accept one client, greet it, and process messages until it disconnects."""
        await websocket.accept()
        self._connections.add(websocket)

        try:
            await self._send_state(websocket)
            await self._send_utterance(websocket, next_idle_utterance())

            while True:
                raw_message = await websocket.receive_text()
                await self._handle_message(websocket, raw_message)
        except WebSocketDisconnect:
            logger.info("pet WebSocket client disconnected")
        finally:
            self._connections.discard(websocket)

    async def _handle_message(self, websocket: WebSocket, raw_message: str) -> None:
        try:
            message: Any = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("ignoring invalid JSON from pet WebSocket client")
            return

        if not isinstance(message, Mapping):
            logger.warning("ignoring non-object pet WebSocket message")
            return

        message_type = message.get("type")
        if message_type == REQUEST_IDLE_LINE_MESSAGE_TYPE:
            self._reset_idle_timer()
            await self._send_utterance(websocket, next_idle_utterance())
            return

        if message_type == SET_SPEECH_ENABLED_MESSAGE_TYPE:
            value = message.get("value")
            if type(value) is not bool:
                logger.warning("ignoring set_speech_enabled with a non-boolean value")
                return
            await self._set_speech_enabled(value)
            return

        if message_type == SET_MUTED_MESSAGE_TYPE:
            value = message.get("value")
            if type(value) is not bool:
                logger.warning("ignoring set_muted with a non-boolean value")
                return
            await self._set_muted(value)
            return

        logger.warning("ignoring unknown pet WebSocket message type")

    async def _run_idle_broadcasts(self) -> None:
        """Wait a randomized interval and broadcast only when a pet is connected."""
        configuration = self._idle_configuration
        logger.info(
            "idle broadcast task started with random intervals from %s to %s seconds",
            configuration.min_interval_seconds,
            configuration.max_interval_seconds,
        )

        while True:
            reset_event = self._idle_reset_event
            if reset_event is None:
                return

            reset_event.clear()
            if self._muted or self._game.state not in IDLE_GAME_STATES:
                reason = (
                    "the runtime switch"
                    if self._muted
                    else f"CS2 state {self._game.state}"
                )
                logger.info("automatic idle speech is paused by %s", reason)
                await reset_event.wait()
                continue

            interval_seconds = random.randint(
                configuration.min_interval_seconds,
                configuration.max_interval_seconds,
            )
            try:
                await asyncio.wait_for(reset_event.wait(), timeout=interval_seconds)
            except TimeoutError:
                if not self._connections:
                    logger.info("idle broadcast interval elapsed with no connected pets")
                    continue
                await self._broadcast_utterance(
                    next_idle_utterance(),
                    source="automatic idle",
                    skip_when_muted=True,
                )
            else:
                logger.info("idle broadcast timer reset after a runtime or manual change")

    async def _set_speech_enabled(self, enabled: bool) -> None:
        """Apply one speech switch request and publish the resulting state."""
        if self._speech_enabled == enabled:
            return

        self._speech_enabled = enabled
        self._speech_service.set_enabled(enabled)
        await self._cancel_pending_game_broadcast()
        self._last_game_broadcast_at = asyncio.get_running_loop().time()
        await self._broadcast_state()

    async def _set_muted(self, muted: bool) -> None:
        """Apply one automatic-speech switch request and reset its timer."""
        if self._muted == muted:
            return

        self._muted = muted
        self._reset_idle_timer()
        await self._cancel_pending_game_broadcast()
        self._last_game_broadcast_at = asyncio.get_running_loop().time()
        await self._broadcast_state()

    async def update_game(self, game: GameState) -> None:
        """Publish meaningful game changes and coalesce round/score progress."""
        previous = self._game
        if game == previous:
            return

        self._game = game
        if (previous.state in IDLE_GAME_STATES) != (game.state in IDLE_GAME_STATES):
            self._reset_idle_timer()
        if not self._connections:
            return

        if _only_round_or_score_changed(previous, game):
            now = asyncio.get_running_loop().time()
            remaining = GAME_PROGRESS_BROADCAST_INTERVAL_SECONDS - (
                now - self._last_game_broadcast_at
            )
            if remaining > 0:
                if self._pending_game_broadcast is None:
                    self._pending_game_broadcast = asyncio.create_task(
                        self._broadcast_game_after(remaining),
                        name="pet-game-state-broadcast",
                    )
                return

        await self._cancel_pending_game_broadcast()
        self._last_game_broadcast_at = asyncio.get_running_loop().time()
        await self._broadcast_state()

    async def _broadcast_game_after(self, delay_seconds: float) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(delay_seconds)
            self._last_game_broadcast_at = asyncio.get_running_loop().time()
            await self._broadcast_state()
        except asyncio.CancelledError:
            return
        finally:
            if self._pending_game_broadcast is current_task:
                self._pending_game_broadcast = None

    async def _cancel_pending_game_broadcast(self) -> None:
        task = self._pending_game_broadcast
        self._pending_game_broadcast = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _broadcast_state(self) -> None:
        """Publish runtime state to live clients and discard failed connections."""
        for websocket in tuple(self._connections):
            try:
                await self._send_state(websocket)
            except Exception as error:
                logger.warning(
                    "removing pet WebSocket connection after state broadcast failure: %s",
                    error,
                )
                self._connections.discard(websocket)

    async def broadcast_commentary(self, utterance: Utterance) -> None:
        """Send selected game commentary through the existing pet output chain."""
        await self._broadcast_utterance(
            utterance,
            source="game commentary",
            skip_when_muted=True,
        )

    def is_muted(self) -> bool:
        """Return the authoritative automatic-speech switch state."""
        return self._muted

    async def _broadcast_utterance(
        self,
        utterance: Utterance,
        *,
        source: str,
        skip_when_muted: bool,
    ) -> None:
        """Deliver one utterance to every live client, removing failed connections."""
        async with self._utterance_broadcast_lock:
            if skip_when_muted and self._muted:
                logger.info("%s utterance was skipped because automatic speech is muted", source)
                return
            delivered_connections = 0
            for websocket in tuple(self._connections):
                try:
                    await self._send_utterance(websocket, utterance, speak=False)
                except Exception as error:
                    logger.warning(
                        "removing pet WebSocket connection after broadcast failure: %s",
                        error,
                    )
                    self._connections.discard(websocket)
                else:
                    delivered_connections += 1

            if delivered_connections:
                self._speech_service.speak(utterance.text)
                logger.info(
                    "%s utterance broadcast to %s connected pet(s)",
                    source,
                    delivered_connections,
                )

    def _reset_idle_timer(self) -> None:
        """Restart the automatic interval after an on-demand idle line."""
        if self._idle_reset_event is not None:
            self._idle_reset_event.set()

    async def _send_utterance(
        self,
        websocket: WebSocket,
        utterance: Utterance,
        *,
        speak: bool = True,
    ) -> None:
        await websocket.send_json(
            {
                "type": UTTERANCE_MESSAGE_TYPE,
                "id": utterance.id,
                "text": utterance.text,
                "emotion": utterance.emotion,
            }
        )
        if speak:
            self._speech_service.speak(utterance.text)

    async def _send_state(self, websocket: WebSocket) -> None:
        """Send the current authoritative switches to one connected client."""
        message = BridgeStateMessage(
            speech_enabled=self._speech_enabled,
            muted=self._muted,
            game=self._game,
        )
        await websocket.send_json(message.model_dump())


def _only_round_or_score_changed(previous: GameState, current: GameState) -> bool:
    progress_fields = {"round", "score_ct", "score_t"}
    previous_without_progress = previous.model_dump(exclude=progress_fields)
    current_without_progress = current.model_dump(exclude=progress_fields)
    return previous_without_progress == current_without_progress
