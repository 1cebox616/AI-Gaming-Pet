"""Local HTTP entry point for the AI Gaming Pet backend."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import time

from importlib.metadata import version

from fastapi import FastAPI, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from pet.bridge import PetBridge
from pet.commentary import CommentaryGenerator
from pet.config import load_config
from pet.events import EventDetector
from pet.gsi import (
    GSI_SILENCE_SECONDS,
    GameSnapshot,
    GsiAck,
    GsiService,
    ensure_gsi_config,
)
from pet.network import HOST, PORT
from pet.online_commentary import OnlineCommentaryRuntime
from pet.policy import SpeechPolicy
from pet.session import GameSessionTracker, MatchLifecycleTracker
from pet.situation import SituationTracker
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
pet_bridge = PetBridge(
    speech_service,
    configuration.idle,
    personality_style=configuration.personality.style,
)
game_session_tracker = GameSessionTracker(offline_timeout_seconds=GSI_SILENCE_SECONDS)


class GameSnapshotProcessor:
    """Orchestrate the production fact-to-delivery chain for live snapshots."""

    def __init__(
        self,
        bridge: PetBridge,
        session: GameSessionTracker,
        detector: EventDetector,
        situation: SituationTracker,
        policy: SpeechPolicy,
        generator: CommentaryGenerator,
        online_runtime: OnlineCommentaryRuntime | None = None,
    ) -> None:
        self._bridge = bridge
        self._session = session
        self._lifecycle = MatchLifecycleTracker()
        self._detector = detector
        self._situation = situation
        self._policy = policy
        self._generator = generator
        self._online_runtime = online_runtime

    async def observe(self, snapshot: GameSnapshot) -> None:
        """Advance facts always, then spend policy quota only for live consumers."""
        game = self._session.observe(snapshot)
        if self._lifecycle.observe(game):
            self._detector.reset()
            self._situation.reset()
            self._policy.reset()
            if self._online_runtime is not None:
                await self._online_runtime.reset_match()

        round_situation = self._situation.observe(snapshot, game)
        self._policy.observe_snapshot(snapshot)
        events = self._detector.observe(snapshot, game)
        await self._bridge.update_game(game)
        if not self._bridge.has_consumers():
            return

        policy_batch = self._policy.decide(
            events,
            game,
            now=snapshot.ts,
            muted=self._bridge.is_muted(),
        )
        if policy_batch.selected_event is None:
            return
        if self._online_runtime is not None:
            await self._online_runtime.submit(
                snapshot,
                game,
                round_situation,
                policy_batch.selected_event,
            )
            return
        utterance = self._generator.generate(policy_batch.selected_event, map_name=snapshot.map_name)
        await self._bridge.broadcast_commentary(utterance)

    async def mark_offline(self, *, now: float) -> None:
        """Reset per-match state when GSI silence transitions the session offline."""
        game = self._session.current(now=now)
        if self._lifecycle.observe(game):
            self._detector.reset()
            self._situation.reset()
            self._policy.reset()
            if self._online_runtime is not None:
                await self._online_runtime.reset_match()
        await self._bridge.update_game(game)


commentary_generator = CommentaryGenerator(personality_style=configuration.personality.style)
online_commentary_runtime = OnlineCommentaryRuntime(
    configuration.llm,
    pet_bridge,
    commentary_generator,
)
pet_bridge.set_llm_state_provider(online_commentary_runtime.state)

game_snapshot_processor = GameSnapshotProcessor(
    pet_bridge,
    game_session_tracker,
    EventDetector(configuration.events),
    SituationTracker(),
    SpeechPolicy(configuration.policy),
    commentary_generator,
    online_commentary_runtime,
)


async def observe_gsi_snapshot(snapshot: GameSnapshot) -> None:
    """Interpret one GSI snapshot and synchronize connected desktop clients."""
    await game_snapshot_processor.observe(snapshot)


async def mark_gsi_offline() -> None:
    """Publish offline after the GSI heartbeat silence window expires."""
    await game_snapshot_processor.mark_offline(now=time.time())


gsi_service = GsiService(
    configuration.gsi,
    snapshot_listener=observe_gsi_snapshot,
    offline_listener=mark_gsi_offline,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Load the local voice before accepting desktop-pet connections."""
    await asyncio.to_thread(ensure_gsi_config)
    await asyncio.to_thread(speech_service.load)
    await pet_bridge.start_idle_broadcasts()
    await online_commentary_runtime.start()
    await gsi_service.start()
    try:
        yield
    finally:
        await gsi_service.shutdown()
        await online_commentary_runtime.shutdown()
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
