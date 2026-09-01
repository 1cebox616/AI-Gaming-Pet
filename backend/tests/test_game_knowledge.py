"""M5-B-T3b shelf-one contract, persistence, and scheduling regressions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
from PIL import Image
import pytest

from pet.core.config import (
    AdapterConfig,
    GameKnowledgeConfig,
    GenericVisionConfig,
    LlmConfig,
    LlmProfileConfig,
    OcrConfig,
    SceneConfig,
)
from pet.core.gamecard import (
    GameCard,
    GameCardRepository,
    GameKnowledgeContent,
    render_game_knowledge_short_view,
    render_gamecard_markdown,
)
from pet.core.llm import (
    LlmCooldownError,
    LlmDispatchStats,
    LlmError,
    LlmResult,
    LlmUsage,
    OpenRouterClient,
)
from pet.core.belief import EvidenceStore
from pet.games.generic.adapter import GenericVisionAdapter, ObservationLog
from pet.games.generic.game_knowledge import (
    GAME_KNOWLEDGE_PROMPT_PATH,
    RESPONSE_FORMAT,
    GameKnowledgeCallResult,
    GameKnowledgeReader,
    parse_game_knowledge_response,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _content_value(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "genre": ["动作", "多人"],
        "perspective": "第一人称视角",
        "summary": "这是一份完整公开概述。",
        "core_gameplay": "完成公开目标，准备、行动、读取反馈并进入下一轮。",
        "game_structure": "按独立会话组织。",
        "setting_and_background": "只含不剧透的公开前提。",
        "release_and_service_status": "当前持续运营。",
    }
    value.update(overrides)
    return value


def _content(**kwargs: object) -> GameKnowledgeContent:
    return GameKnowledgeContent.model_validate(
        _content_value(**kwargs),
        strict=True,
    )


def _call_result(
    outcome: str,
    *,
    content: GameKnowledgeContent | None = None,
    failure_reason: str | None = None,
) -> GameKnowledgeCallResult:
    return GameKnowledgeCallResult(
        request_id=f"gk-{outcome}",
        outcome=outcome,  # type: ignore[arg-type]
        model="google/gemini-3.1-flash-lite",
        actual_model=("google/gemini-3.1-flash-lite" if outcome == "ok" else None),
        provider=("fixture-provider" if outcome == "ok" else None),
        latency_ms=2500.0,
        cost_usd=0.001 if outcome in {"ok", "schema_reject"} else 0.0,
        prompt_tokens=100 if outcome in {"ok", "schema_reject"} else None,
        completion_tokens=200 if outcome in {"ok", "schema_reject"} else None,
        content=content,
        failure_reason=failure_reason,
        normalization_actions=(),
    )


def test_production_prompt_and_schema_are_flat_v4_without_keybinds() -> None:
    fields = {
        "genre",
        "perspective",
        "summary",
        "core_gameplay",
        "game_structure",
        "setting_and_background",
        "release_and_service_status",
    }
    prompt = GAME_KNOWLEDGE_PROMPT_PATH.read_text(encoding="utf-8")
    schema = RESPONSE_FORMAT["json_schema"]["schema"]  # type: ignore[index]
    assert schema["additionalProperties"] is False  # type: ignore[index]
    assert set(schema["properties"]) == fields  # type: ignore[arg-type,index]
    assert schema["required"] == ["genre", "summary"]  # type: ignore[index]
    for field in fields:
        assert f'"{field}"' in prompt
    for obsolete in (
        "default_pc_keybinds",
        "game_overview",
        "player_goal",
        "core_loop",
        "major_systems",
        "modes_and_structure",
        "setting_and_premise",
    ):
        assert obsolete not in prompt
        assert obsolete not in json.dumps(schema, ensure_ascii=False)


def test_v4_parser_accepts_nullable_fields_and_ambiguity_free_cleanup() -> None:
    raw = json.dumps(_content_value(), ensure_ascii=False)
    parsed = parse_game_knowledge_response(f"```json\n{raw}\n```")
    assert parsed.content is not None
    assert parsed.normalization_actions == ("剥离 JSON 外文本／代码围栏",)

    nullable = _content_value(
        perspective=None,
        core_gameplay=None,
        game_structure=None,
        setting_and_background=None,
        release_and_service_status=None,
    )
    parsed_nullable = parse_game_knowledge_response(
        json.dumps(nullable, ensure_ascii=False)
    )
    assert parsed_nullable.content is not None
    assert parsed_nullable.content.perspective is None
    assert parsed_nullable.content.release_and_service_status is None

    partial = _content_value()
    partial.pop("summary")
    assert parse_game_knowledge_response(
        json.dumps(partial, ensure_ascii=False)
    ).content is None

    obsolete = _content_value()
    obsolete["default_pc_keybinds"] = {"移动": "W"}
    assert parse_game_knowledge_response(
        json.dumps(obsolete, ensure_ascii=False)
    ).content is None


def test_v4_omitted_optional_fields_are_persisted_as_explicit_nulls() -> None:
    parsed = parse_game_knowledge_response(
        json.dumps(
            {"genre": ["策略"], "summary": "公开且稳定的游戏说明。"},
            ensure_ascii=False,
        )
    )
    assert parsed.content is not None
    assert parsed.content.model_dump(mode="json") == {
        "genre": ["策略"],
        "perspective": None,
        "summary": "公开且稳定的游戏说明。",
        "core_gameplay": None,
        "game_structure": None,
        "setting_and_background": None,
        "release_and_service_status": None,
    }


def test_success_atomically_initializes_then_refreshes_without_field_merge(
    tmp_path: Path,
) -> None:
    repository = GameCardRepository(tmp_path)
    card = repository.load_or_create("fixture-game", "Fixture Game", NOW)
    first = _content(summary="第一份完整答案。")
    card, action = repository.record_knowledge_attempt(
        card,
        checked_at=NOW,
        model="google/gemini-3.1-flash-lite",
        mode="web",
        request_id="gk-first",
        outcome="ok",
        failure_reason=None,
        content=first,
    )
    assert action == "initialized"
    assert card.knowledge is not None
    assert card.knowledge.status == "initialized"
    assert card.knowledge.content == first

    second = _content(
        summary="第二份完整答案。",
        core_gameplay=None,
        game_structure=None,
    )
    card, action = repository.record_knowledge_attempt(
        card,
        checked_at=NOW + timedelta(minutes=1),
        model="google/gemini-3.1-flash-lite",
        mode="web",
        request_id="gk-second",
        outcome="ok",
        failure_reason=None,
        content=second,
    )
    assert action == "refreshed"
    assert card.knowledge is not None
    assert card.knowledge.status == "refreshed"
    assert card.knowledge.content == second
    assert card.knowledge.content.core_gameplay is None
    assert card.knowledge.content.game_structure is None
    assert len(card.knowledge.attempts) == 2
    assert (tmp_path / "fixture-game" / "gamecard.md").read_text(
        encoding="utf-8"
    ) == render_gamecard_markdown(card)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("cooldown_drop", "档位冷却中"),
        ("timeout", "超过总墙钟截止"),
        ("schema_reject", "未通过完整 V4 合同"),
        ("failed", "网关失败"),
    ],
)
def test_every_failure_keeps_content_and_records_card_attempt_and_evidence(
    tmp_path: Path,
    outcome: str,
    reason: str,
) -> None:
    repository = GameCardRepository(tmp_path / "memory")
    card = repository.load_or_create("fixture-game", "Fixture Game", NOW)
    previous = _content()
    card, _ = repository.record_knowledge_attempt(
        card,
        checked_at=NOW,
        model="google/gemini-3.1-flash-lite",
        mode="web",
        request_id="gk-seed",
        outcome="ok",
        failure_reason=None,
        content=previous,
    )
    output = tmp_path / "session"
    log = ObservationLog(output, {}, exact_directory=True)
    before_markdown = (output / "observations.md").read_bytes()
    result = _call_result(outcome, failure_reason=reason)

    card, action = repository.record_knowledge_attempt(
        card,
        checked_at=NOW + timedelta(minutes=1),
        model=result.model,
        mode="web",
        request_id=result.request_id,
        outcome=result.outcome,
        failure_reason=result.failure_reason,
        content=result.content,
    )
    evidence_id = log.append_game_knowledge(
        trigger_monotonic=10.0,
        learned_monotonic=12.5,
        result=result,
        write_action=action,
        game_id="fixture-game",
    )
    after_markdown = (output / "observations.md").read_bytes()
    log.close()

    assert action == "kept_previous"
    assert card.knowledge is not None
    assert card.knowledge.content == previous
    assert card.knowledge.status == "stale"
    # ROOT CAUSE: retained valid content keeps the provenance of the request
    # that produced it; a failed refresh is represented by attempts/evidence.
    assert card.knowledge.checked_at == NOW
    assert card.knowledge.model == "google/gemini-3.1-flash-lite"
    assert card.knowledge.mode == "web"
    assert card.knowledge.request_id == "gk-seed"
    assert card.knowledge.attempts[-1].result == outcome
    assert card.knowledge.attempts[-1].failure_reason == reason
    events = tuple(EvidenceStore.read(output / "evidence.jsonl"))
    assert len(events) == 1
    assert events[0].evidence_id == evidence_id
    assert events[0].kind == "game_knowledge"
    assert events[0].payload.outcome == outcome  # type: ignore[union-attr]
    assert events[0].payload.write_action == "kept_previous"  # type: ignore[union-attr]
    # ROOT CAUSE: non-frame knowledge evidence is not part of observations.md.
    assert before_markdown == after_markdown


def test_short_view_uses_utf8_bytes_as_a_conservative_token_hard_cap() -> None:
    for limit in (1, 16, 64, 256, 512):
        rendered = render_game_knowledge_short_view(_content(), limit)
        assert len(rendered.encode("utf-8")) <= limit


class _ReaderClient:
    def __init__(self, behavior: str) -> None:
        self.behavior = behavior
        self.calls = 0
        self.closed = False
        self.cancelled = False

    async def complete_with_web_search(self, **_kwargs: object) -> LlmResult:
        self.calls += 1
        if self.behavior == "cooldown_drop":
            raise LlmCooldownError(
                "cooling",
                profile_name="game_knowledge",
                cooldown_drop=True,
                cooldown_remaining_seconds=3.0,
            )
        if self.behavior == "timeout":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.behavior == "failed":
            raise LlmError("gateway failed", status_code=503)
        text = (
            "{}"
            if self.behavior == "schema_reject"
            else json.dumps(_content_value(), ensure_ascii=False)
        )
        return LlmResult(
            text=text,
            usage=LlmUsage(100, 200, 0.001),
            latency_seconds=0.01,
            model="google/gemini-3.1-flash-lite",
            provider="fixture-provider",
            finish_reason="stop",
        )

    def dispatch_stats(self) -> LlmDispatchStats:
        return LlmDispatchStats(
            "game_knowledge",
            0,
            0.0,
            int(self.behavior == "cooldown_drop"),
            self.behavior == "cooldown_drop",
            3.0 if self.behavior == "cooldown_drop" else 0.0,
        )

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "behavior",
    ["ok", "cooldown_drop", "timeout", "schema_reject", "failed"],
)
def test_reader_maps_each_attempt_once_without_retry(behavior: str) -> None:
    client = _ReaderClient(behavior)
    configuration = LlmConfig(
        enabled=True,
        model="google/gemini-3.1-flash-lite",
        provider="",
        temperature=0.0,
        timeout_seconds=1.0,
        max_tokens=8000,
    )
    reader = GameKnowledgeReader(
        client,
        configuration,
        wall_timeout_seconds=0.01 if behavior == "timeout" else 1.0,
        request_id_factory=lambda: "gk-fixture",
    )
    result = asyncio.run(reader.read("Fixture Game"))
    assert result.outcome == behavior
    assert client.calls == 1
    if behavior == "timeout":
        assert client.cancelled
        assert result.error_metadata == {
            "error_type": "wall_timeout",
            "cost_accounting": "unknown_after_cancellation",
        }


def test_openrouter_web_request_uses_only_bounded_default_endpoint_extensions() -> None:
    request_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "google/gemini-3.1-flash-lite",
                "provider": "fixture-provider",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            },
        )

    client = OpenRouterClient(
        "fixture-key",
        profile_name="game_knowledge",
        transport=httpx.MockTransport(handler),
    )
    try:
        asyncio.run(client.complete_with_web_search(
            model="google/gemini-3.1-flash-lite",
            provider=None,
            system_prompt="system",
            user_prompt="游戏名称：Fixture Game",
            max_tokens=8000,
            temperature=0.0,
            reasoning_effort="minimal",
            web_search_parameters={
                "engine": "exa",
                "max_results": 5,
                "max_total_results": 5,
            },
            provider_options={"sort": "throughput", "require_parameters": True},
            response_format={"type": "json_schema"},
            plugins=({"id": "response-healing"},),
        ))
    finally:
        client.close()
    assert request_body["tools"] == [
        {
            "type": "openrouter:web_search",
            "parameters": {
                "engine": "exa",
                "max_results": 5,
                "max_total_results": 5,
            },
        }
    ]
    assert request_body["provider"] == {
        "sort": "throughput",
        "require_parameters": True,
    }
    assert request_body["reasoning"] == {"effort": "minimal"}
    assert request_body["response_format"] == {"type": "json_schema"}
    assert request_body["plugins"] == [{"id": "response-healing"}]

    custom = OpenRouterClient(
        "fixture-key",
        profile_name="game_knowledge",
        base_url="https://custom.invalid/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        async def reject_custom_endpoint() -> None:
            with pytest.raises(LlmError, match="未发送服务商专有参数"):
                await custom.complete_with_web_search(
                    model="model",
                    provider=None,
                    system_prompt="system",
                    user_prompt="user",
                    max_tokens=8,
                    temperature=0.0,
                    reasoning_effort="minimal",
                    web_search_parameters={"max_results": 5},
                    provider_options={},
                    response_format={},
                    plugins=(),
                )

        asyncio.run(reject_custom_endpoint())
    finally:
        custom.close()


@dataclass(frozen=True)
class _Metadata:
    window_title: str
    process_name: str
    captured_at: datetime
    monotonic_seconds: float


@dataclass(frozen=True)
class _Frame:
    bitmap: Image.Image
    metadata: _Metadata


class _FastClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete_with_images_stream(self, **_kwargs: object) -> LlmResult:
        self.calls += 1
        return LlmResult(
            text="【画面】画面保持可见",
            usage=LlmUsage(10, 5, 0.0),
            latency_seconds=0.001,
            model="fixture-fast",
            provider="fixture",
        )

    def close(self) -> None:
        pass


class _BlockingKnowledgeClient(_ReaderClient):
    def __init__(self) -> None:
        super().__init__("ok")
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete_with_web_search(self, **kwargs: object) -> LlmResult:
        self.started.set()
        await asyncio.wait_for(self.release.wait(), timeout=2.0)
        return await super().complete_with_web_search(**kwargs)


def _adapter_configuration(tmp_path: Path) -> tuple[AdapterConfig, LlmConfig]:
    adapter = AdapterConfig(
        generic=GenericVisionConfig(
            enabled=True,
            poll_interval_seconds=0.01,
            fast_timeout_seconds=1.0,
            max_inflight=4,
            input_context=False,
            observation_log_dir=str(tmp_path / "logs"),
            ocr=OcrConfig(enabled=False),
            scene=SceneConfig(
                enabled=False,
                memory_dir=str(tmp_path / "memory"),
            ),
            knowledge=GameKnowledgeConfig(
                enabled=True,
                wall_timeout_seconds=1.0,
                short_view_token_limit=512,
            ),
        )
    )
    llm = LlmConfig(
        profiles={
            "vision_fast": LlmProfileConfig(
                enabled=True,
                model="fixture-fast",
                provider="fixture",
                temperature=0.0,
                timeout_seconds=1.0,
                max_tokens=200,
                input_price_per_million_usd=0.0,
                output_price_per_million_usd=0.0,
            ),
            "game_knowledge": LlmProfileConfig(
                enabled=True,
                model="google/gemini-3.1-flash-lite",
                provider="",
                temperature=0.0,
                timeout_seconds=1.0,
                max_tokens=8000,
            ),
        }
    )
    return adapter, llm


def _frame(sequence: int) -> _Frame:
    return _Frame(
        bitmap=Image.new("RGB", (64, 64), color=(sequence, 20, 30)),
        metadata=_Metadata(
            window_title="Fixture Game",
            process_name="fixture-game",
            captured_at=NOW + timedelta(seconds=sequence),
            monotonic_seconds=float(sequence),
        ),
    )


def test_knowledge_call_does_not_block_frame_uploads_and_runs_once_per_session(
    tmp_path: Path,
) -> None:
    adapter_config, llm_config = _adapter_configuration(tmp_path)
    fast = _FastClient()
    knowledge = _BlockingKnowledgeClient()
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("replay must not capture live frames")
        ),
        selector_factory=lambda _ratio: (_ for _ in ()).throw(
            AssertionError("replay schedules directly")
        ),
        client_factory=lambda *_args: fast,
        knowledge_client_factory=lambda *_args: knowledge,
    )
    output = tmp_path / "replay"

    async def scenario() -> None:
        adapter.start_replay(output, input_context=None)
        for sequence in range(1, 4):
            await adapter.submit_replay_frame(
                _frame(sequence),
                "Fixture Game",
                (),
                float(sequence - 1),
                confirmed_region=(),
                change_ratio=0.5,
                global_change=10.0,
                region_intensity=0.0,
                forced=False,
            )
        await asyncio.wait_for(knowledge.started.wait(), timeout=1.0)
        deadline = asyncio.get_running_loop().time() + 1.0
        while fast.calls < 3:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("frame uploads waited for game knowledge")
            await asyncio.sleep(0.005)
        knowledge.release.set()
        await adapter.finish_replay()

    asyncio.run(scenario())
    assert fast.calls == 3
    assert knowledge.calls == 1
    card = GameCard.model_validate_json(
        (tmp_path / "memory" / "fixture-game" / "gamecard.json").read_text(
            encoding="utf-8"
        )
    )
    assert card.knowledge is not None
    assert card.knowledge.status == "initialized"
    evidence = tuple(EvidenceStore.read(output / "evidence.jsonl"))
    assert sum(item.kind == "game_knowledge" for item in evidence) == 1


def test_knowledge_client_initialization_failure_is_line_local_and_recorded(
    tmp_path: Path,
) -> None:
    adapter_config, llm_config = _adapter_configuration(tmp_path)
    fast = _FastClient()

    def missing_knowledge_key(*_args: object) -> _ReaderClient:
        raise LlmError(
            "模型档位 game_knowledge 缺少环境变量 KNOWLEDGE_KEY；"
            "未回退到其他密钥或端点",
            profile_name="game_knowledge",
        )

    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("replay must not capture live frames")
        ),
        selector_factory=lambda _ratio: (_ for _ in ()).throw(
            AssertionError("replay schedules directly")
        ),
        client_factory=lambda *_args: fast,
        knowledge_client_factory=missing_knowledge_key,
    )
    output = tmp_path / "missing-key-replay"

    async def scenario() -> None:
        # ROOT CAUSE: optional off-path knowledge credentials are checked only
        # after the game is known; their absence cannot abort observer startup.
        adapter.start_replay(output, input_context=None)
        await adapter.submit_replay_frame(
            _frame(1),
            "Fixture Game",
            (),
            0.0,
            confirmed_region=(),
            change_ratio=0.5,
            global_change=10.0,
            region_intensity=0.0,
            forced=False,
        )
        await adapter.finish_replay()

    asyncio.run(scenario())
    assert fast.calls == 1
    card = GameCard.model_validate_json(
        (tmp_path / "memory" / "fixture-game" / "gamecard.json").read_text(
            encoding="utf-8"
        )
    )
    assert card.knowledge is not None
    assert card.knowledge.content is None
    assert card.knowledge.status == "stale"
    assert card.knowledge.request_id.startswith("gk-")
    assert card.knowledge.attempts[-1].result == "failed"
    evidence = tuple(EvidenceStore.read(output / "evidence.jsonl"))
    knowledge_events = [item for item in evidence if item.kind == "game_knowledge"]
    assert len(knowledge_events) == 1
    assert knowledge_events[0].payload.outcome == "failed"  # type: ignore[union-attr]
    assert knowledge_events[0].payload.write_action == "kept_previous"  # type: ignore[union-attr]


def test_two_replays_initialize_then_refresh_with_one_call_each(
    tmp_path: Path,
) -> None:
    adapter_config, llm_config = _adapter_configuration(tmp_path)
    knowledge_clients: list[_ReaderClient] = []

    async def run_session(number: int) -> None:
        fast = _FastClient()
        knowledge = _ReaderClient("ok")
        knowledge_clients.append(knowledge)
        adapter = GenericVisionAdapter(
            adapter_config,
            llm_config,
            capture_backend_factory=lambda: (_ for _ in ()).throw(
                AssertionError("replay must not capture live frames")
            ),
            selector_factory=lambda _ratio: (_ for _ in ()).throw(
                AssertionError("replay schedules directly")
            ),
            client_factory=lambda *_args: fast,
            knowledge_client_factory=lambda *_args: knowledge,
        )
        adapter.start_replay(tmp_path / f"replay-{number}", input_context=None)
        await adapter.submit_replay_frame(
            _frame(number),
            "Fixture Game",
            (),
            float(number - 1),
            confirmed_region=(),
            change_ratio=0.5,
            global_change=10.0,
            region_intensity=0.0,
            forced=False,
        )
        await adapter.finish_replay()
        assert fast.calls == 1

    asyncio.run(run_session(1))
    first = GameCard.model_validate_json(
        (tmp_path / "memory" / "fixture-game" / "gamecard.json").read_text(
            encoding="utf-8"
        )
    )
    assert first.knowledge is not None
    assert first.knowledge.status == "initialized"

    asyncio.run(run_session(2))
    second = GameCard.model_validate_json(
        (tmp_path / "memory" / "fixture-game" / "gamecard.json").read_text(
            encoding="utf-8"
        )
    )
    assert second.knowledge is not None
    assert second.knowledge.status == "refreshed"
    assert len(second.knowledge.attempts) == 2
    assert [client.calls for client in knowledge_clients] == [1, 1]
    for number in (1, 2):
        evidence = tuple(
            EvidenceStore.read(tmp_path / f"replay-{number}" / "evidence.jsonl")
        )
        assert sum(item.kind == "game_knowledge" for item in evidence) == 1


def test_four_repository_cards_use_v2_card_and_flat_v4_knowledge_contract() -> None:
    memory_root = Path(__file__).parents[1] / "memory"
    paths = sorted(memory_root.glob("*/gamecard.json"))
    assert len(paths) == 4
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "genre" not in raw
        assert "perspective" not in raw
        assert "keybinds" not in raw
        assert "view_constants" not in raw
        assert "hud_slots" not in raw
        assert "knowledge" in raw
        assert "hud_elements" in raw
        knowledge = raw["knowledge"]
        assert knowledge is not None
        content = knowledge["content"]
        assert content is not None
        assert set(content) == {
            "genre",
            "perspective",
            "summary",
            "core_gameplay",
            "game_structure",
            "setting_and_background",
            "release_and_service_status",
        }
        GameCard.model_validate_json(path.read_text(encoding="utf-8"), strict=True)
