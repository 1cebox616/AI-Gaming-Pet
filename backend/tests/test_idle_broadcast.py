"""End-to-end idle broadcast tests with real FastAPI WebSocket clients."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from pet.core.bridge import PetBridge
from pet.core.config import IdleConfig, SpeechConfig
from pet.games.cs2.session import GameState
from pet.core.speech import SpeechService

TEST_IDLE_INTERVAL_SECONDS = 1


def _fast_idle_configuration() -> IdleConfig:
    """Bypass production's 10-second floor only for real WebSocket timing tests."""
    return IdleConfig.model_construct(
        enabled=True,
        min_interval_seconds=TEST_IDLE_INTERVAL_SECONDS,
        max_interval_seconds=TEST_IDLE_INTERVAL_SECONDS,
    )


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
        await asyncio.sleep(1.2)

    @app.post("/game")
    async def update_game(game: GameState) -> None:
        await bridge.update_game(game)

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
    assert state == {
        "type": "state",
        "speech_enabled": False,
        "muted": False,
        "game": {
            "game_id": "",
            "state": "offline",
            "summary": {},
        },
    }
    _assert_utterance(websocket.receive_json())


def test_idle_broadcast_reaches_two_clients_with_one_shared_utterance() -> None:
    """The background loop broadcasts exactly one generated line to all live pets."""
    app, _ = _create_idle_test_app(_fast_idle_configuration())

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
    app, bridge = _create_idle_test_app(_fast_idle_configuration())

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as live_websocket:
            _receive_initial_messages(live_websocket)
            with client.websocket_connect("/closed"):
                live_broadcast = live_websocket.receive_json()

            _assert_utterance(live_broadcast)
            assert len(bridge._connections) == 1


def test_manual_idle_request_restarts_the_automatic_broadcast_interval() -> None:
    """An on-demand line prevents a nearly due automatic line from following it."""
    app, _ = _create_idle_test_app(_fast_idle_configuration())

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            _receive_initial_messages(websocket)
            time.sleep(0.6)
            websocket.send_json({"type": "request_idle_line"})
            manual_reply = websocket.receive_json()
            manual_completed_at = time.perf_counter()

            automatic_reply = websocket.receive_json()
            automatic_elapsed = time.perf_counter() - manual_completed_at

    _assert_utterance(manual_reply)
    _assert_utterance(automatic_reply)
    assert automatic_reply["id"] != manual_reply["id"]
    assert 0.8 <= automatic_elapsed <= 1.5


def test_gameplay_pauses_idle_broadcast_and_menu_restarts_full_interval() -> None:
    """No idle line is queued in-game; returning to menu starts a fresh wait."""
    app, _ = _create_idle_test_app(_fast_idle_configuration())
    playing = GameState(
        state="playing",
        mode="casual",
        map="de_anubis",
        round=3,
        score_ct=1,
        score_t=1,
        subject_steamid="76561198000000001",
        subject_is_self=True,
    )
    menu = GameState(
        state="menu",
        subject_steamid="76561198000000001",
        subject_is_self=True,
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            _receive_initial_messages(websocket)
            assert client.post("/game", json=playing.model_dump()).status_code == 200
            assert websocket.receive_json()["game"]["state"] == "playing"

            time.sleep(1.05)
            assert client.post("/game", json=menu.model_dump()).status_code == 200
            menu_message = websocket.receive_json()
            resumed_at = time.perf_counter()
            automatic_reply = websocket.receive_json()
            resumed_elapsed = time.perf_counter() - resumed_at

    assert menu_message["game"]["state"] == "menu"
    _assert_utterance(automatic_reply)
    assert 0.8 <= resumed_elapsed <= 1.5
