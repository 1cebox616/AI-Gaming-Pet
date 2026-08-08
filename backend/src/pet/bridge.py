"""WebSocket bridge between the backend and connected desktop pets."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Mapping
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from pet.config import IdleConfig
from pet.lines import Utterance, next_idle_utterance
from pet.speech import SpeechService

logger = logging.getLogger(__name__)

UTTERANCE_MESSAGE_TYPE = "utterance"
REQUEST_IDLE_LINE_MESSAGE_TYPE = "request_idle_line"


class PetBridge:
    """Manage active pet WebSocket connections and their dialogue requests."""

    def __init__(
        self,
        speech_service: SpeechService,
        idle_configuration: IdleConfig | None = None,
    ) -> None:
        self._connections: set[WebSocket] = set()
        self._speech_service = speech_service
        self._idle_configuration = idle_configuration or IdleConfig()
        self._idle_task: asyncio.Task[None] | None = None
        self._idle_reset_event: asyncio.Event | None = None

    async def start_idle_broadcasts(self) -> None:
        """Start the optional randomized idle broadcast loop once per app lifetime."""
        if not self._idle_configuration.enabled:
            logger.info("idle broadcasts are disabled by backend configuration")
            return
        if self._idle_task is not None and not self._idle_task.done():
            return

        self._idle_reset_event = asyncio.Event()
        self._idle_task = asyncio.create_task(
            self._run_idle_broadcasts(),
            name="pet-idle-broadcasts",
        )

    async def shutdown(self) -> None:
        """Cancel the idle loop before FastAPI closes its event loop."""
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

        if message.get("type") != REQUEST_IDLE_LINE_MESSAGE_TYPE:
            logger.warning("ignoring unknown pet WebSocket message type")
            return

        self._reset_idle_timer()
        await self._send_utterance(websocket, next_idle_utterance())

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
                await self._broadcast_utterance(next_idle_utterance())
            else:
                logger.info("idle broadcast timer reset after a manual dialogue request")

    async def _broadcast_utterance(self, utterance: Utterance) -> None:
        """Deliver one utterance to every live client, removing failed connections."""
        delivered_connections = 0
        for websocket in tuple(self._connections):
            try:
                await self._send_utterance(websocket, utterance, speak=False)
            except Exception as error:
                logger.warning("removing pet WebSocket connection after broadcast failure: %s", error)
                self._connections.discard(websocket)
            else:
                delivered_connections += 1

        if delivered_connections:
            self._speech_service.speak(utterance.text)
            logger.info(
                "automatic idle utterance broadcast to %s connected pet(s)",
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
