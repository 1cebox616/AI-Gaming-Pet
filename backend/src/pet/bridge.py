"""WebSocket bridge between the backend and connected desktop pets."""

import json
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from pet.lines import Utterance, next_idle_utterance
from pet.speech import SpeechService

logger = logging.getLogger(__name__)

UTTERANCE_MESSAGE_TYPE = "utterance"
REQUEST_IDLE_LINE_MESSAGE_TYPE = "request_idle_line"


class PetBridge:
    """Manage active pet WebSocket connections and their dialogue requests."""

    def __init__(self, speech_service: SpeechService) -> None:
        self._connections: set[WebSocket] = set()
        self._speech_service = speech_service

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

        await self._send_utterance(websocket, next_idle_utterance())

    async def _send_utterance(self, websocket: WebSocket, utterance: Utterance) -> None:
        await websocket.send_json(
            {
                "type": UTTERANCE_MESSAGE_TYPE,
                "id": utterance.id,
                "text": utterance.text,
                "emotion": utterance.emotion,
            }
        )
        self._speech_service.speak(utterance.text)
