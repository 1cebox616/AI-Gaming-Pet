"""Local HTTP entry point and game-independent application assembly."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
import logging

from fastapi import FastAPI, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from pet.core.adapter_api import CoreServices, GameAdapter, GameStatus, PORT_VERSION
from pet.core.bridge import PetBridge
from pet.core.config import load_config
from pet.core.network import HOST, PORT
from pet.core.speaker import OnlineCommentaryRuntime
from pet.core.speech import SpeechService
from pet.games import built_in_adapters

PACKAGE_NAME = "pet"
PET_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:1420",
    "http://localhost:1420",
    "tauri://localhost",
)

logger = logging.getLogger(__name__)


def configure_pet_logging() -> None:
    """Send every pet-module INFO log to the backend console."""
    pet_logger = logging.getLogger("pet")
    pet_logger.setLevel(logging.INFO)
    pet_logger.propagate = False
    if any(handler.name == "pet-console" for handler in pet_logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = "pet-console"
    handler.setFormatter(logging.Formatter(PET_LOG_FORMAT))
    pet_logger.addHandler(handler)


class HealthResponse(BaseModel):
    """The backend health payload returned to local clients."""

    status: str
    version: str


def _load_adapter() -> GameAdapter:
    game_id = configuration.active.game
    factory = built_in_adapters(configuration.llm).get(game_id)
    game_configuration = configuration.games.get(game_id)
    if factory is None or game_configuration is None:
        logger.error("active game adapter %r is not installed", game_id)
        raise RuntimeError(f"active game adapter is not installed: {game_id}")
    loaded = factory(game_configuration)
    if loaded.port_version != PORT_VERSION:
        logger.error(
            "adapter %s uses port v%s; core requires port v%s",
            loaded.adapter_id,
            loaded.port_version,
            PORT_VERSION,
        )
        raise RuntimeError(f"adapter port version mismatch: {loaded.adapter_id}")
    return loaded


configure_pet_logging()
configuration = load_config()
adapter = _load_adapter()
speech_service = SpeechService(configuration.speech)
pet_bridge = PetBridge(
    speech_service,
    configuration.idle,
    initial_game=GameStatus(
        game_id=adapter.adapter_id,
        state="offline",
        summary={},
    ),
    personality_style=configuration.personality.style,
)
speaker = OnlineCommentaryRuntime(configuration.llm, pet_bridge)
pet_bridge.set_llm_state_provider(speaker.state)
core_services = CoreServices(
    submit_speech=speaker.submit,
    publish_status=pet_bridge.update_game,
    can_submit_speech=pet_bridge.has_consumers,
    speech_is_muted=pet_bridge.is_muted,
    reset_speech_session=speaker.reset_match,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Start core services and the configured adapter in dependency order."""
    await asyncio.to_thread(speech_service.load)
    await pet_bridge.start_idle_broadcasts()
    await speaker.start()
    await adapter.start(core_services)
    try:
        yield
    finally:
        await adapter.stop()
        await speaker.shutdown()
        await pet_bridge.shutdown()
        speech_service.shutdown()


app = FastAPI(title="AI Gaming Pet", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=[],
)
if adapter.http_router is not None:
    app.include_router(adapter.http_router)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Return the installed project's health and metadata version."""
    return HealthResponse(status="ok", version=version(PACKAGE_NAME))


@app.websocket("/ws")
async def pet_websocket(websocket: WebSocket) -> None:
    """Serve the persistent dialogue bridge for one desktop pet client."""
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        logger.warning("rejecting pet WebSocket connection from origin %r", origin)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await pet_bridge.serve(websocket)


def run() -> None:
    """Run the local-only development server."""
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    run()
