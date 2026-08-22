"""Port-v1 adapter wiring the existing CS2 production pipeline."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request

from pet.core.adapter_api import CoreServices, GameStatus, PORT_VERSION, SpeechRequest
from pet.core.config import AdapterConfig
from pet.games.cs2.events import EventDetector, GameEvent
from pet.games.cs2.fact_sentences import render_fact_sentence
from pet.games.cs2.gsi import (
    GSI_SILENCE_SECONDS,
    GameSnapshot,
    GsiAck,
    GsiService,
    ensure_gsi_config,
)
from pet.games.cs2.policy import PolicyBatchDecision, SpeechPolicy
from pet.games.cs2.session import GameSessionTracker, GameState, MatchLifecycleTracker
from pet.games.cs2.situation import SituationTracker
from pet.games.cs2.template_speech import CommentaryGenerator


class GameSnapshotProcessor:
    """Advance CS2 facts and submit only policy-selected speech requests."""

    def __init__(
        self,
        core: CoreServices,
        configuration: AdapterConfig,
        *,
        session: GameSessionTracker | None = None,
        detector: EventDetector | None = None,
        situation: SituationTracker | None = None,
        policy: SpeechPolicy | None = None,
        generator: CommentaryGenerator | None = None,
    ) -> None:
        self._core = core
        self._session = session or GameSessionTracker(GSI_SILENCE_SECONDS)
        self._lifecycle = MatchLifecycleTracker()
        self._detector = detector or EventDetector(configuration.events)
        self._situation = situation or SituationTracker()
        self._policy = policy or SpeechPolicy(configuration.policy)
        self._generator = generator or CommentaryGenerator(
            personality_style=configuration.personality.style
        )

    async def observe(self, snapshot: GameSnapshot) -> None:
        game = self._session.observe(snapshot)
        if self._lifecycle.observe(game):
            self._detector.reset()
            self._situation.reset()
            self._policy.reset()
            await self._core.reset_speech_session()

        round_situation = self._situation.observe(snapshot, game)
        self._policy.observe_snapshot(snapshot)
        events = self._detector.observe(snapshot, game)
        await self._core.publish_status(_status(game))
        if not self._core.can_submit_speech():
            return

        policy_batch = self._policy.decide(
            events,
            game,
            now=snapshot.ts,
            muted=self._core.speech_is_muted(),
        )
        selected = policy_batch.selected_event
        if selected is None:
            return
        template = self._generator.generate(selected, map_name=snapshot.map_name)
        await self._core.submit_speech(
            SpeechRequest(
                request_id=selected.id,
                game_id="cs2",
                fact_text=render_fact_sentence(
                    snapshot, game, round_situation, selected
                ),
                urgency=_selected_priority(policy_batch, selected),
                interrupt=True,
                supersedes_request_id=None,
                vocabulary_id="cs2",
                llm_profile=None,
                fallback_text=template.text,
                fallback_emotion=template.emotion,
                ts=snapshot.ts,
            )
        )

    async def mark_offline(self, *, now: float) -> None:
        game = self._session.current(now=now)
        if self._lifecycle.observe(game):
            self._detector.reset()
            self._situation.reset()
            self._policy.reset()
            await self._core.reset_speech_session()
        await self._core.publish_status(_status(game))


class Cs2Adapter:
    """Own the CS2 HTTP endpoint and production snapshot pipeline."""

    adapter_id = "cs2"
    display_name = "CS2"
    port_version = PORT_VERSION

    def __init__(self, configuration: AdapterConfig) -> None:
        self.http_router = APIRouter()
        self._configuration = configuration
        self._core: CoreServices | None = None
        self._processor: GameSnapshotProcessor | None = None
        self._gsi_service: GsiService | None = None

        @self.http_router.post("/gsi", response_model=GsiAck)
        async def receive_gsi(request: Request) -> GsiAck:
            service = self._gsi_service
            if service is None:
                return GsiAck()
            return await service.receive(request)

    async def start(self, core: CoreServices) -> None:
        self._core = core
        self._processor = GameSnapshotProcessor(core, self._configuration)

        async def observe(snapshot: GameSnapshot) -> None:
            assert self._processor is not None
            await self._processor.observe(snapshot)

        async def mark_offline() -> None:
            assert self._processor is not None
            await self._processor.mark_offline(now=time.time())

        self._gsi_service = GsiService(
            self._configuration.gsi,
            snapshot_listener=observe,
            offline_listener=mark_offline,
        )
        await asyncio.to_thread(ensure_gsi_config)
        await self._gsi_service.start()

    async def stop(self) -> None:
        if self._gsi_service is not None:
            await self._gsi_service.shutdown()
        self._gsi_service = None
        self._processor = None
        self._core = None


def create_adapter(configuration: AdapterConfig) -> Cs2Adapter:
    """Create the built-in adapter without exposing its implementation to main."""
    return Cs2Adapter(configuration)


def _status(game: GameState) -> GameStatus:
    summary: dict[str, str | int | None] = {
        "mode": game.mode,
        "map": game.map,
        "round": game.round,
        "score_ct": game.score_ct,
        "score_t": game.score_t,
    }
    return GameStatus(game_id="cs2", state=game.state, summary=summary)


def _selected_priority(
    policy_batch: PolicyBatchDecision, selected: GameEvent
) -> int:
    return next(
        decision.priority
        for decision in policy_batch.decisions
        if decision.event.id == selected.id and decision.selected
    )
