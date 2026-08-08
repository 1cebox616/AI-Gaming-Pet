"""Local HTTP entry point for the AI Gaming Pet backend."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from importlib.metadata import version

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from pet.bridge import PetBridge
from pet.speech import SpeechService

HOST = "127.0.0.1"
PORT = 8737
PACKAGE_NAME = "pet"


class HealthResponse(BaseModel):
    """The backend health payload returned to local clients."""

    status: str
    version: str


speech_service = SpeechService()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Load the local voice before accepting desktop-pet connections."""
    await asyncio.to_thread(speech_service.load)
    try:
        yield
    finally:
        speech_service.shutdown()


app = FastAPI(title="AI Gaming Pet", lifespan=lifespan)
pet_bridge = PetBridge(speech_service)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "tauri://localhost",
    ],
    allow_methods=["GET"],
    allow_headers=[],
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Return the installed project's health and metadata version."""
    return HealthResponse(status="ok", version=version(PACKAGE_NAME))


@app.websocket("/ws")
async def pet_websocket(websocket: WebSocket) -> None:
    """Serve the persistent dialogue bridge for one desktop pet client."""
    await pet_bridge.serve(websocket)


def run() -> None:
    """Run the local-only development server."""
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    run()
