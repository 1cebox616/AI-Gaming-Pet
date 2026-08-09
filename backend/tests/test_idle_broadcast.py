"""End-to-end idle broadcast tests with real FastAPI WebSocket clients."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from pet.bridge import PetBridge
from pet.config import IdleConfig, SpeechConfig
from pet.speech import SpeechService


def _create_idle_test_app(idle_configuration: IdleConfig) -> tuple[FastAPI, PetBridge]:
    """Build a real app with speech disabled so tests never need an audio device."""
    speech_service = SpeechService(SpeechConfig(enabled=False))
    bridge = PetBridge(speech_service, idle_configuration)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(speech_service.load)
        await bridge.start_idle_broadcasts()
        try:
            yield
        finally:
            await bridge.shutdown()
            speech_service.shutdown()

    app = FastAPI(lifespan=lifespan)

    @app.websocket("/ws")
    async def pet_websocket(websocket: WebSocket) -> None:
        await bridge.serve(websocket)

    @app.websocket("/closed")
    async def closed_websocket(websocket: WebSocket) -> None:
        """Place a truly closed socket in the bridge for failed-send cleanup coverage."""
        await websocket.accept()
        bridge._connections.add(websocket)
        await websocket.close()
        await asyncio.sleep(12)

    return app, bridge


def _assert_utterance(message: dict[str, Any]) -> None:
    """Check a delivered message is a complete protocol utterance."""
    assert message["type"] == "utterance"
    assert isinstance(message["id"], str)
    assert message["id"]
    assert isinstance(message["text"], str)
    assert message["text"]
    assert isinstance(message["emotion"], str)
    assert message["emotion"]


def _receive_initial_messages(websocket: Any) -> None:
    """Consume the required state-first handshake followed by its greeting."""
    state = websocket.receive_json()
    assert state == {"type": "state", "speech_enabled": False, "muted": False}
    _assert_utterance(websocket.receive_json())


def test_idle_broadcast_reaches_two_clients_with_one_shared_utterance() -> None:
    """The background loop broadcasts exactly one generated line to all live pets."""
    app, _ = _create_idle_test_app(
        IdleConfig(enabled=True, min_interval_seconds=10, max_interval_seconds=10)
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as first_websocket:
            _receive_initial_messages(first_websocket)
            with client.websocket_connect("/ws") as second_websocket:
                _receive_initial_messages(second_websocket)

                first_broadcast = first_websocket.receive_json()
                second_broadcast = second_websocket.receive_json()

    _assert_utterance(first_broadcast)
    _assert_utterance(second_broadcast)
    assert first_broadcast["id"] == second_broadcast["id"]


def test_failed_broadcast_connection_is_removed_without_blocking_a_live_pet() -> None:
    """A closed client is pruned while a live client still receives the idle line."""
    app, bridge = _create_idle_test_app(
        IdleConfig(enabled=True, min_interval_seconds=10, max_interval_seconds=10)
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as live_websocket:
            _receive_initial_messages(live_websocket)
            with client.websocket_connect("/closed"):
                live_broadcast = live_websocket.receive_json()

            _assert_utterance(live_broadcast)
            assert len(bridge._connections) == 1


def test_manual_idle_request_restarts_the_automatic_broadcast_interval() -> None:
    """An on-demand line prevents a nearly due automatic line from following it."""
    app, _ = _create_idle_test_app(
        IdleConfig(enabled=True, min_interval_seconds=10, max_interval_seconds=10)
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            _receive_initial_messages(websocket)
            time.sleep(6)
            websocket.send_json({"type": "request_idle_line"})
            manual_reply = websocket.receive_json()
            manual_completed_at = time.perf_counter()

            automatic_reply = websocket.receive_json()
            automatic_elapsed = time.perf_counter() - manual_completed_at

    _assert_utterance(manual_reply)
    _assert_utterance(automatic_reply)
    assert automatic_reply["id"] != manual_reply["id"]
    assert 9 <= automatic_elapsed <= 12
