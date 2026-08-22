"""Real WebSocket coverage for game-state publication and throttling."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import time

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from pet.core.bridge import PetBridge
from pet.core.config import IdleConfig, SpeechConfig
from pet.games.cs2.session import GameState
from pet.core.speech import SpeechService


def _create_app() -> FastAPI:
    speech_service = SpeechService(SpeechConfig(enabled=False))
    bridge = PetBridge(speech_service, IdleConfig(enabled=False))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await bridge.start_idle_broadcasts()
        try:
            yield
        finally:
            await bridge.shutdown()

    app = FastAPI(lifespan=lifespan)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await bridge.serve(websocket)

    @app.post("/game")
    async def update_game(game: GameState) -> None:
        await bridge.update_game(game)

    return app


def _game(*, state: str = "playing", round_number: int = 6) -> dict[str, object]:
    return {
        "state": state,
        "mode": "casual",
        "map": "de_anubis",
        "round": round_number,
        "score_ct": 2,
        "score_t": 3,
        "subject_steamid": "76561198000000001",
        "subject_is_self": state != "spectating",
    }


def _status(*, state: str = "playing", round_number: int = 6) -> dict[str, object]:
    game = _game(state=state, round_number=round_number)
    return {
        "game_id": "",
        "state": state,
        "summary": {
            key: game[key]
            for key in (
                "mode",
                "map",
                "round",
                "score_ct",
                "score_t",
                "subject_steamid",
            )
        },
    }


def test_state_changes_are_immediate_and_progress_updates_are_coalesced() -> None:
    with TestClient(_create_app()) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_json()
            websocket.receive_json()

            assert client.post("/game", json=_game()).status_code == 200
            assert websocket.receive_json()["game"] == _status()

            started_at = time.perf_counter()
            assert client.post("/game", json=_game(round_number=7)).status_code == 200
            assert client.post("/game", json=_game(round_number=8)).status_code == 200

            coalesced = websocket.receive_json()
            elapsed = time.perf_counter() - started_at

    assert coalesced["game"] == _status(round_number=8)
    assert elapsed >= 0.8
