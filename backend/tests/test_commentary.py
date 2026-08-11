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
    commentary_category,
    templates_for_map,
)
from pet.commentary_rules import CALLOUT_TERMS, find_forbidden_raw_curses
from pet.commentary_templates import (
    COMMENTARY_TEMPLATES,
    CommentaryCategory,
    CommentaryTemplate,
)
from pet.config import (
    EventsConfig,
    GsiConfig,
    IdleConfig,
    PersonalityStyle,
    PolicyConfig,
    SpeechConfig,
)
from pet.events import EventDetector, EventType, GameEvent
from pet.gsi import (
    GSI_SILENCE_SECONDS,
    GameSnapshot,
    GsiAck,
    GsiService,
    parse_snapshot,
)
from pet.lines import IDLE_UTTERANCES_BY_PERSONALITY, Utterance
from pet.main import GameSnapshotProcessor
from pet.policy import SpeechPolicy
from pet.replay import format_commentary_replay, replay_commentary
from pet.session import GameSessionTracker
from pet.situation import SituationTracker
from pet.speech import SpeechService

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_event_samples.json"
PERSONAL_ACTION_PHRASES = (
    "你拆",
    "你引爆",
    "你守住",
    "你埋",
    "你灭队",
)
MAP_NAMES = (
    "dust2",
    "mirage",
    "nuke",
    "anubis",
    "ancient",
    "inferno",
    "overpass",
    "vertigo",
    "cache",
)


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
    if category == "death_after_kill":
        return _event(
            "death_after_kill",
            facts={"seconds_since_last_kill": 3.5, "round_kills": 1},
        )
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


def test_every_template_category_generates_valid_output() -> None:
    assert set(COMMENTARY_TEMPLATES) == {"brother", "caster"}
    for personality_style, category_tables in COMMENTARY_TEMPLATES.items():
        for category in category_tables:
            event = _event_for_category(category)
            utterance = CommentaryGenerator(
                random.Random(1), personality_style=personality_style
            ).generate(event)

            assert commentary_category(event) == category
            assert utterance.text.strip()


def test_round_result_templates_do_not_claim_the_player_executed_the_result() -> None:
    for category_tables in COMMENTARY_TEMPLATES.values():
        for category, templates in category_tables.items():
            if category.startswith("round_"):
                assert all(
                    phrase not in template.text
                    for template in templates
                    for phrase in PERSONAL_ACTION_PHRASES
                )


def test_all_dialogue_avoids_unmasked_raw_curses() -> None:
    commentary_texts = (
        template.text
        for category_tables in COMMENTARY_TEMPLATES.values()
        for templates in category_tables.values()
        for template in templates
    )
    idle_texts = (
        utterance.text
        for utterances in IDLE_UTTERANCES_BY_PERSONALITY.values()
        for utterance in utterances
    )
    assert all(
        not find_forbidden_raw_curses(text)
        for text in (*commentary_texts, *idle_texts)
    )


def test_raw_curse_allowlist_keeps_benign_words_without_hiding_real_hits() -> None:
    assert find_forbidden_raw_curses("这操作太秀了") == ()
    assert find_forbidden_raw_curses("趴在草丛里") == ()
    assert find_forbidden_raw_curses("操") == ("操",)


def test_unscoped_dialogue_has_no_map_names_or_callouts() -> None:
    for category_tables in COMMENTARY_TEMPLATES.values():
        for templates in category_tables.values():
            for template in templates:
                lower_text = template.text.casefold()
                assert all(callout not in template.text for callout in CALLOUT_TERMS)
                if template.applicable_maps is None:
                    assert all(map_name not in lower_text for map_name in MAP_NAMES)
    for utterances in IDLE_UTTERANCES_BY_PERSONALITY.values():
        assert all(
            map_name not in utterance.text.casefold()
            and all(callout not in utterance.text for callout in CALLOUT_TERMS)
            for utterance in utterances
            for map_name in MAP_NAMES
        )


def test_map_scoped_templates_only_match_their_map_and_unknown_map_uses_generic() -> None:
    generic = CommentaryTemplate("通用句", "neutral")
    dust2_only = CommentaryTemplate("Dust2 句", "happy", ("Dust2",))
    mirage_only = CommentaryTemplate("Mirage 句", "surprised", ("mirage",))
    templates = (generic, dust2_only, mirage_only)

    assert templates_for_map(templates, "de_DUST2") == (generic, dust2_only)
    assert templates_for_map(templates, "DE_mIrAgE") == (generic, mirage_only)
    assert templates_for_map(templates, None) == (generic,)


@pytest.mark.parametrize(
    "event_type",
    (
        "kill",
        "kill_headshot",
        "multi_kill",
        "death",
        "death_after_kill",
        "death_thrown_away",
        "round_win",
        "round_loss",
    ),
)
@pytest.mark.parametrize("personality_style", ("brother", "caster"))
def test_missing_facts_still_produce_natural_non_none_commentary(
    event_type: EventType,
    personality_style: PersonalityStyle,
) -> None:
    utterance = CommentaryGenerator(
        random.Random(4),
        personality_style=personality_style,
    ).generate(_event(event_type))

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


def test_personality_switch_produces_clearly_different_commentary() -> None:
    event = _event("kill_headshot", facts={"round_kill_index": 2})

    brother = CommentaryGenerator(
        random.Random(7), personality_style="brother"
    ).generate(event)
    caster = CommentaryGenerator(
        random.Random(7), personality_style="caster"
    ).generate(event)

    assert brother.text != caster.text
    assert brother.model_dump() != caster.model_dump()


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

    outputs: dict[PersonalityStyle, str] = {}
    for personality_style in ("brother", "caster"):
        first = format_commentary_replay(
            replay_commentary(
                snapshots,
                EventsConfig(),
                PolicyConfig(),
                personality_style=personality_style,
            )
        )
        second = format_commentary_replay(
            replay_commentary(
                snapshots,
                EventsConfig(),
                PolicyConfig(),
                personality_style=personality_style,
            )
        )

        assert first == second
        assert "实际生成话术：1" in first
        assert "话术[" in first
        outputs[personality_style] = first

    assert outputs["brother"] != outputs["caster"]


class _CountingCommentaryGenerator(CommentaryGenerator):
    """Exercise the real generator while exposing how often generation ran."""

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        self.call_count = 0

    def generate(
        self, event: GameEvent, *, map_name: str | None = None
    ) -> Utterance:
        self.call_count += 1
        return super().generate(event, map_name=map_name)


def _create_real_gsi_commentary_app(
    *, policy_config: PolicyConfig | None = None
) -> tuple[FastAPI, _CountingCommentaryGenerator]:
    speech_service = SpeechService(SpeechConfig(enabled=False))
    bridge = PetBridge(speech_service, IdleConfig(enabled=True))
    session = GameSessionTracker(GSI_SILENCE_SECONDS)
    generator = _CountingCommentaryGenerator(random.Random(31))
    processor = GameSnapshotProcessor(
        bridge,
        session,
        EventDetector(EventsConfig()),
        SituationTracker(),
        SpeechPolicy(policy_config or PolicyConfig()),
        generator,
    )

    async def observe(snapshot: GameSnapshot) -> None:
        await processor.observe(snapshot)

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

    return app, generator


def test_real_gsi_payloads_reach_existing_websocket_utterance_chain(
    event_samples: dict[str, Any],
) -> None:
    samples = event_samples["ordinary_death_with_trade_kill"]["samples"]
    app, _ = _create_real_gsi_commentary_app()

    with TestClient(app) as client:
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
    assert utterance_message["emotion"] in {
        "neutral",
        "happy",
        "angry",
        "surprised",
        "speechless",
    }


def test_no_consumer_advances_real_facts_without_generation_or_policy_quota(
    event_samples: dict[str, Any],
) -> None:
    no_client_samples = event_samples["multi_kill_after_dropped_push"]["samples"]
    connected_sample = event_samples["cold_start_existing_stats"]["samples"][0]
    app, generator = _create_real_gsi_commentary_app(
        policy_config=PolicyConfig(
            cooldown_seconds=0,
            max_lines_per_round=1,
            alive_priority_threshold=0,
        )
    )

    with TestClient(app) as client:
        for sample in no_client_samples:
            response = client.post("/gsi", json=sample["payload"])
            assert response.status_code == 200
        assert generator.call_count == 0

        with client.websocket_connect("/ws") as websocket:
            websocket.receive_json()
            websocket.receive_json()
            response = client.post("/gsi", json=connected_sample["payload"])
            assert response.status_code == 200
            utterance = websocket.receive_json()

    assert generator.call_count == 1
    assert utterance["type"] == "utterance"
    assert utterance["id"].startswith("game-event-")
