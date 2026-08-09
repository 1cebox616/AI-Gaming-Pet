"""Template commentary and real GSI-to-WebSocket integration tests."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
from pathlib import Path
import random
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
import pytest

from pet.bridge import PetBridge
from pet.commentary import (
    CommentaryGenerator,
    GameCommentaryEngine,
    commentary_category,
    format_commentary_replay,
    replay_commentary,
)
from pet.commentary_templates import COMMENTARY_TEMPLATES, CommentaryCategory
from pet.config import EventsConfig, GsiConfig, IdleConfig, PolicyConfig, SpeechConfig
from pet.events import EventType, GameEvent
from pet.gsi import (
    GSI_SILENCE_SECONDS,
    GameSnapshot,
    GsiAck,
    GsiService,
    parse_snapshot,
)
from pet.lines import Emotion
from pet.session import GameSessionTracker
from pet.speech import SpeechService

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_event_samples.json"
VALID_EMOTIONS: set[Emotion] = {
    "neutral",
    "happy",
    "angry",
    "surprised",
    "speechless",
}


@pytest.fixture(scope="module")
def event_samples() -> dict[str, Any]:
    data: object = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _event(
    event_type: EventType,
    *,
    facts: dict[str, Any] | None = None,
) -> GameEvent:
    return GameEvent(
        id="event-test",
        type=event_type,
        ts=10.0,
        subject_steamid="76561198000000001",
        subject_is_self=True,
        round_number=2,
        facts=facts or {},
    )


def _event_for_category(category: CommentaryCategory) -> GameEvent:
    if category == "kill":
        return _event("kill", facts={"round_kill_index": 1})
    if category == "kill_headshot":
        return _event("kill_headshot", facts={"round_kill_index": 1})
    if category.startswith("multi_"):
        suffix = category.removeprefix("multi_")
        return _event(
            "multi_kill",
            facts={"count": int(suffix)} if suffix.isdecimal() else {},
        )
    if category == "death":
        return _event("death", facts={"survival_seconds": 22.5})
    if category == "death_thrown_away":
        return _event(
            "death_thrown_away",
            facts={"survival_seconds": 7.25, "equip_value": 4200},
        )
    result, method = category.rsplit("_", 1)
    event_type: EventType = "round_win" if result == "round_win" else "round_loss"
    return _event(
        event_type,
        facts={
            "method": None if method == "general" else f"ct_win_{method}",
            "score_ct": 4,
            "score_t": 2,
        },
    )


def _recorded_death_snapshots(
    event_samples: dict[str, Any],
) -> tuple[GameSnapshot, ...]:
    samples = event_samples["ordinary_death_with_trade_kill"]["samples"]
    return tuple(
        parse_snapshot(sample["payload"], received_at=sample["ts"])
        for sample in samples
    )


def test_every_template_category_has_five_real_lines_and_generates_valid_output() -> None:
    assert COMMENTARY_TEMPLATES
    for category, templates in COMMENTARY_TEMPLATES.items():
        event = _event_for_category(category)
        utterances = tuple(
            CommentaryGenerator(random.Random(seed)).generate(event)
            for seed in range(10)
        )

        assert len(templates) >= 5
        assert commentary_category(event) == category
        assert len({utterance.text for utterance in utterances}) == len(templates)
        assert all(utterance.text.strip() for utterance in utterances)
        assert all(utterance.emotion in VALID_EMOTIONS for utterance in utterances)
        assert all("None" not in utterance.text for utterance in utterances)
        assert all("_win_" not in utterance.text for utterance in utterances)


@pytest.mark.parametrize(
    "event_type",
    (
        "kill",
        "kill_headshot",
        "multi_kill",
        "death",
        "death_thrown_away",
        "round_win",
        "round_loss",
    ),
)
def test_missing_facts_still_produce_natural_non_none_commentary(
    event_type: EventType,
) -> None:
    utterance = CommentaryGenerator(random.Random(4)).generate(_event(event_type))

    assert utterance.text.strip()
    assert "None" not in utterance.text
    assert "_win_" not in utterance.text


def test_known_facts_are_safely_filled_when_the_selected_template_uses_them() -> None:
    kill = CommentaryGenerator(random.Random(2)).generate(
        _event("kill", facts={"round_kill_index": 3})
    )
    death = CommentaryGenerator(random.Random(2)).generate(
        _event("death", facts={"survival_seconds": 12.5})
    )
    thrown = CommentaryGenerator(random.Random(1)).generate(
        _event("death_thrown_away", facts={"equip_value": 3900})
    )
    result = CommentaryGenerator(random.Random(2)).generate(
        _event(
            "round_win",
            facts={"method": "t_win_bomb", "score_ct": 4, "score_t": 2},
        )
    )

    assert "第 3 杀" in kill.text
    assert "12.5 秒" in death.text
    assert "3900" in thrown.text
    assert "4:2" in result.text


def test_unknown_win_method_uses_general_lines_without_leaking_identifier() -> None:
    unknown_method = "ct_win_secret_method"
    event = _event("round_win", facts={"method": unknown_method})
    utterance = CommentaryGenerator(random.Random(0)).generate(event)

    assert commentary_category(event) == "round_win_general"
    assert unknown_method not in utterance.text
    assert "secret" not in utterance.text
    assert "None" not in utterance.text


def test_same_category_never_repeats_the_same_line_consecutively() -> None:
    generator = CommentaryGenerator(random.Random(23))
    event = _event("kill", facts={"round_kill_index": 1})
    lines = [generator.generate(event).text for _ in range(50)]

    assert all(current != previous for previous, current in zip(lines, lines[1:]))


def test_real_recording_generates_exactly_one_line_per_selected_event(
    event_samples: dict[str, Any],
) -> None:
    result = replay_commentary(
        _recorded_death_snapshots(event_samples),
        EventsConfig(),
        PolicyConfig(),
    )
    selected_count = sum(item.decision.selected for item in result.dispositions)
    utterance_count = sum(item.utterance is not None for item in result.dispositions)

    assert selected_count == 1
    assert utterance_count == selected_count
    assert all(
        item.utterance is None or "None" not in item.utterance.text
        for item in result.dispositions
    )


def test_fixed_seed_makes_real_recording_replay_identical(
    event_samples: dict[str, Any],
) -> None:
    snapshots = _recorded_death_snapshots(event_samples)

    first = format_commentary_replay(
        replay_commentary(snapshots, EventsConfig(), PolicyConfig())
    )
    second = format_commentary_replay(
        replay_commentary(snapshots, EventsConfig(), PolicyConfig())
    )

    assert first == second
    assert "实际生成话术：1" in first
    assert "话术[" in first


def _create_real_gsi_commentary_app() -> FastAPI:
    speech_service = SpeechService(SpeechConfig(enabled=False))
    bridge = PetBridge(speech_service, IdleConfig(enabled=True))
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    engine = GameCommentaryEngine(
        EventsConfig(),
        PolicyConfig(),
        CommentaryGenerator(random.Random(31)),
    )

    async def observe(snapshot: GameSnapshot) -> None:
        game = session.observe(snapshot)
        commentary = engine.observe(snapshot, game, muted=bridge.is_muted())
        await bridge.update_game(game)
        if commentary.utterance is not None:
            await bridge.broadcast_commentary(commentary.utterance)

    gsi_service = GsiService(GsiConfig(record=False), snapshot_listener=observe)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await gsi_service.start()
        try:
            yield
        finally:
            await gsi_service.shutdown()
            await bridge.shutdown()
            speech_service.shutdown()

    app = FastAPI(lifespan=lifespan)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await bridge.serve(websocket)

    @app.post("/gsi", response_model=GsiAck)
    async def receive_gsi(request: Request) -> GsiAck:
        return await gsi_service.receive(request)

    return app


def test_real_gsi_payloads_reach_existing_websocket_utterance_chain(
    event_samples: dict[str, Any],
) -> None:
    samples = event_samples["ordinary_death_with_trade_kill"]["samples"]

    with TestClient(_create_real_gsi_commentary_app()) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_json()
            websocket.receive_json()
            for sample in samples:
                response = client.post("/gsi", json=sample["payload"])
                assert response.status_code == 200

            state_message = websocket.receive_json()
            utterance_message = websocket.receive_json()

    assert state_message["type"] == "state"
    assert state_message["game"]["state"] == "playing"
    assert utterance_message["type"] == "utterance"
    assert utterance_message["id"].startswith("game-event-")
    assert utterance_message["text"].strip()
    assert "None" not in utterance_message["text"]
    assert utterance_message["emotion"] in VALID_EMOTIONS
