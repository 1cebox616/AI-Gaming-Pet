"""Local HTTP entry point for the AI Gaming Pet backend."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging

from importlib.metadata import version

from fastapi import FastAPI, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from pet.bridge import PetBridge
from pet.config import load_config
from pet.gsi import GsiAck, GsiService, ensure_gsi_config
from pet.network import HOST, PORT
from pet.speech import SpeechService

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


configure_pet_logging()
configuration = load_config()
speech_service = SpeechService(configuration.speech)
pet_bridge = PetBridge(speech_service, configuration.idle)
gsi_service = GsiService(configuration.gsi)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Load the local voice before accepting desktop-pet connections."""
    await asyncio.to_thread(ensure_gsi_config)
    await asyncio.to_thread(speech_service.load)
    await pet_bridge.start_idle_broadcasts()
    await gsi_service.start()
    try:
        yield
    finally:
        await gsi_service.shutdown()
        await pet_bridge.shutdown()
        speech_service.shutdown()


app = FastAPI(title="AI Gaming Pet", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=[],
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Return the installed project's health and metadata version."""
    return HealthResponse(status="ok", version=version(PACKAGE_NAME))


@app.post("/gsi", response_model=GsiAck)
async def receive_gsi(request: Request) -> GsiAck:
    """Accept one CS2 Game State Integration update."""
    return await gsi_service.receive(request)


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
