"""M5-B-T2-4 stable-cluster deep naming contract tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from PIL import Image

from pet.core.belief import EvidenceStore, SceneVerifiedPayload
from pet.core.config import (
    AdapterConfig,
    GenericVisionConfig,
    LlmConfig,
    LlmProfileConfig,
    OcrConfig,
    SceneConfig,
    SceneNamingConfig,
)
from pet.core.llm import LlmResult, LlmUsage
from pet.games.generic.adapter import (
    GenericVisionAdapter,
    SceneNamingContext,
    WindowTitleMap,
)
from pet.games.generic.deep_read import DeepReadRequest, DeepVisionReader


@dataclass(frozen=True, slots=True)
class Metadata:
    window_title: str
    process_name: str
    captured_at: datetime
    monotonic_seconds: float


@dataclass(frozen=True, slots=True)
class Frame:
    bitmap: Image.Image
    metadata: Metadata


class FastClient:
    def complete_with_images_stream(self, **_kwargs: object) -> LlmResult:
        return LlmResult(
            text="【画面】固定测试画面",
            usage=LlmUsage(10, 5, 0.0001),
            latency_seconds=0.001,
            model="fast-actual",
            provider="fixture",
            finish_reason="stop",
        )

    def close(self) -> None:
        return None


class DeepClient:
    def __init__(self, text: str, *, fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def complete_with_images(self, **kwargs: object) -> LlmResult:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("fixture deep failure")
        return LlmResult(
            text=self.text,
            usage=LlmUsage(300, 40, 0.0123),
            latency_seconds=0.25,
            model="deep-actual",
            provider="fixture-deep",
            finish_reason="stop",
        )

    def close(self) -> None:
        return None


class BlockingDeepClient(DeepClient):
    def __init__(self) -> None:
        super().__init__("")
        self.release = threading.Event()

    def complete_with_images(self, **kwargs: object) -> LlmResult:
        self.calls.append(kwargs)
        self.release.wait(timeout=1.0)
        return LlmResult(
            text='{"matches_existing":null,"label":"主菜单",'
            '"annotation":"用于选择功能的主界面。","modality":"observed"}',
            usage=LlmUsage(10, 5, 0.001),
            latency_seconds=1.0,
            model="deep-actual",
            provider="fixture-deep",
            finish_reason="stop",
        )


def _frame(second: float) -> Frame:
    return Frame(
        Image.new("RGB", (320, 180), (40, 70, 110)),
        Metadata(
            "Fixture Game",
            "fixture.exe",
            datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
            second,
        ),
    )


def _configuration(memory: Path) -> tuple[AdapterConfig, LlmConfig]:
    adapter = AdapterConfig(
        generic=GenericVisionConfig(
            enabled=True,
            input_context=False,
            fast_timeout_seconds=1.0,
            ocr=OcrConfig(enabled=False),
            scene=SceneConfig(
                enabled=True,
                hash_kind="ahash",
                hash_bits=64,
                hamming_threshold=0,
                stable_min_seconds=1.0,
                card_min_dwell_seconds=1.0,
                card_flush_seconds=999.0,
                memory_dir=str(memory),
                naming=SceneNamingConfig(
                    enabled=True,
                    max_requests_per_session=8,
                    representative_frame_count=3,
                ),
            ),
        )
    )
    llm = LlmConfig(
        enabled=True,
        profiles={
            "vision_fast": LlmProfileConfig(
                enabled=True,
                model="fast",
                provider="fixture",
                temperature=0.0,
                max_tokens=200,
                input_price_per_million_usd=1.0,
                output_price_per_million_usd=1.0,
            ),
            "vision_deep": LlmProfileConfig(
                enabled=True,
                model="deep",
                provider="fixture-deep",
                temperature=0.0,
                timeout_seconds=30.0,
                max_tokens=1024,
            ),
        },
    )
    return adapter, llm


def _adapter(
    memory: Path,
    deep: DeepClient,
) -> GenericVisionAdapter:
    adapter_config, llm_config = _configuration(memory)
    fast = FastClient()
    return GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("replay must not capture")
        ),
        selector_factory=lambda _ratio: (_ for _ in ()).throw(
            AssertionError("replay must not select")
        ),
        client_factory=lambda *_args: fast,
        deep_client_factory=lambda *_args: deep,
        title_map=WindowTitleMap(()),
    )


async def _submit(adapter: GenericVisionAdapter, second: float) -> None:
    await adapter.submit_replay_frame(
        _frame(second),
        "Fixture Game",
        (),
        max(0.0, second - 1.0),
        confirmed_region=(),
        change_ratio=0.6,
        global_change=20.0,
        region_intensity=0.0,
        forced=False,
    )


def test_stable_cluster_is_named_once_and_uncertain_is_preserved(
    tmp_path: Path,
) -> None:
    deep = DeepClient(
        '{"matches_existing":null,"label":"主菜单",'
        '"annotation":"游戏启动后用于选择功能的主界面。",'
        '"modality":"uncertain"}'
    )
    adapter = _adapter(tmp_path / "memory", deep)
    output = tmp_path / "session"

    async def scenario() -> None:
        adapter.start_replay(output, input_context=None)
        for second in (0.0, 1.0, 2.0):
            await _submit(adapter, second)
        await adapter.finish_replay()

    asyncio.run(scenario())
    assert len(deep.calls) == 1
    call = deep.calls[0]
    images = call["images"]
    assert isinstance(images, tuple)
    assert all(image.target_width == 1920 for image in images)
    assert call["reasoning_effort"] == "none"
    assert "matches_existing 必须为 null" in str(call["user_prompt"])
    card = json.loads(
        (tmp_path / "memory" / "fixture-game" / "gamecard.json").read_text(
            encoding="utf-8"
        )
    )
    scene = card["scenes"][0]
    assert scene["label"] == "主菜单"
    assert scene["label_status"] == "uncertain"
    assert len(scene["deep_evidence_ids"]) == 1
    events = list(EvidenceStore.read(output / "evidence.jsonl"))
    verified = [event for event in events if event.kind == "scene_verified"]
    assert len(verified) == 1
    assert verified[0].evidence_id == scene["deep_evidence_ids"][0]
    assert verified[0].outcome == "ok"
    assert isinstance(verified[0].payload, SceneVerifiedPayload)
    assert verified[0].payload.root_capture_ids == ["f1"]
    assert verified[0].payload.cost_usd == 0.0123


def test_unstable_cluster_proposal_is_rejected_and_persisted_as_failed_evidence(
    tmp_path: Path,
) -> None:
    deep = DeepClient(
        '{"matches_existing":null,"label":"加载界面",'
        '"annotation":"等待进入下一段内容时显示。",'
        '"modality":"observed"}'
    )
    adapter = _adapter(tmp_path / "memory", deep)
    output = tmp_path / "unstable"

    async def scenario() -> None:
        adapter.start_replay(output, input_context=None)
        await _submit(adapter, 0.0)
        assert adapter._scene_clusterer is not None
        assert adapter._scene_session is not None
        cluster = adapter._scene_clusterer.cluster(1)
        frames = tuple(adapter._scene_frame_buffer)
        adapter._scene_frame_buffer = []
        adapter._scene_frame_buffer_cluster_id = None
        await adapter._run_scene_naming(
            SceneNamingContext(
                game_name="Fixture Game",
                cluster=cluster,
                session=adapter._scene_session,
                frames=frames,
                trigger_frame_ts=0.0,
            )
        )
        await adapter.finish_replay()

    asyncio.run(scenario())
    verified = [
        event
        for event in EvidenceStore.read(output / "evidence.jsonl")
        if event.kind == "scene_verified"
    ]
    assert len(verified) == 1
    assert verified[0].outcome == "failed"
    assert isinstance(verified[0].payload, SceneVerifiedPayload)
    assert "stable gate" in (verified[0].payload.validation_error or "")
    card = json.loads(
        (tmp_path / "memory" / "fixture-game" / "gamecard.json").read_text(
            encoding="utf-8"
        )
    )
    assert card["scenes"] == []


def test_deep_failure_does_not_retry_in_session_and_next_session_can_retry(
    tmp_path: Path,
) -> None:
    memory = tmp_path / "memory"
    failing = DeepClient("", fail=True)
    first = _adapter(memory, failing)

    async def first_session() -> None:
        first.start_replay(tmp_path / "failed", input_context=None)
        for second in (0.0, 1.0, 2.0):
            await _submit(first, second)
        await first.finish_replay()

    asyncio.run(first_session())
    assert len(failing.calls) == 1
    first_card = json.loads(
        (memory / "fixture-game" / "gamecard.json").read_text(encoding="utf-8")
    )
    assert first_card["scenes"][0]["label_status"] == "unnamed"

    succeeding = DeepClient(
        '{"matches_existing":null,"label":"普通游玩画面",'
        '"annotation":"玩家正常操控游戏世界时显示。",'
        '"modality":"observed"}'
    )
    second = _adapter(memory, succeeding)

    async def second_session() -> None:
        second.start_replay(tmp_path / "retried", input_context=None)
        for moment in (10.0, 11.0):
            await _submit(second, moment)
        await second.finish_replay()

    asyncio.run(second_session())
    assert len(succeeding.calls) == 1
    second_card = json.loads(
        (memory / "fixture-game" / "gamecard.json").read_text(encoding="utf-8")
    )
    assert second_card["scenes"][0]["label"] == "普通游玩画面"
    assert second_card["scenes"][0]["label_status"] == "named"

    already_named = DeepClient(
        '{"matches_existing":true,"label":"错误的新名字",'
        '"annotation":"确认匹配时这些返回内容不应覆盖当前命名。",'
        '"modality":"observed"}'
    )
    third = _adapter(memory, already_named)

    async def third_session() -> None:
        third.start_replay(tmp_path / "already-named", input_context=None)
        for moment in (20.0, 21.0):
            await _submit(third, moment)
        await third.finish_replay()

    asyncio.run(third_session())
    assert len(already_named.calls) == 1
    prompt = str(already_named.calls[0]["user_prompt"])
    assert "当前短名：普通游玩画面" in prompt
    third_card = json.loads(
        (memory / "fixture-game" / "gamecard.json").read_text(encoding="utf-8")
    )
    assert third_card["scenes"][0]["label"] == "普通游玩画面"
    third_evidence_path = tmp_path / "already-named" / "evidence.jsonl"
    third_verified = [
        event
        for event in EvidenceStore.read(third_evidence_path)
        if event.kind == "scene_verified"
    ]
    assert len(third_verified) == 1
    assert isinstance(third_verified[0].payload, SceneVerifiedPayload)
    assert third_verified[0].payload.label == "普通游玩画面"

    changed = DeepClient(
        '{"matches_existing":false,"label":"设置菜单",'
        '"annotation":"玩家调整游戏选项时显示的设置界面。",'
        '"modality":"observed"}'
    )
    fourth = _adapter(memory, changed)

    async def fourth_session() -> None:
        fourth.start_replay(tmp_path / "renamed", input_context=None)
        for moment in (30.0, 31.0):
            await _submit(fourth, moment)
        await fourth.finish_replay()

    asyncio.run(fourth_session())
    assert len(changed.calls) == 1
    fourth_card = json.loads(
        (memory / "fixture-game" / "gamecard.json").read_text(encoding="utf-8")
    )
    assert fourth_card["scenes"][0]["label"] == "设置菜单"
    assert fourth_card["scenes"][0]["annotation"] == "玩家调整游戏选项时显示的设置界面。"
    fourth_verified = [
        event
        for event in EvidenceStore.read(tmp_path / "renamed" / "evidence.jsonl")
        if event.kind == "scene_verified"
    ]
    assert len(fourth_verified) == 1
    assert isinstance(fourth_verified[0].payload, SceneVerifiedPayload)
    assert fourth_verified[0].payload.label == "设置菜单"


def test_deep_reader_enforces_wall_clock_timeout() -> None:
    client = BlockingDeepClient()
    reader = DeepVisionReader(
        client,
        LlmConfig(
            enabled=True,
            model="deep",
            provider="fixture-deep",
            timeout_seconds=0.01,
        ),
        input_price_per_million_usd=None,
        output_price_per_million_usd=None,
        reasoning_effort="none",
    )

    async def scenario() -> None:
        try:
            await reader.read(DeepReadRequest("system", "user", ()))
        except TimeoutError:
            client.release.set()
            return
        raise AssertionError("deep read exceeded its wall-clock deadline without timing out")

    try:
        asyncio.run(scenario())
    finally:
        client.release.set()
