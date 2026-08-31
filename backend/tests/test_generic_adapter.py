from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
from types import SimpleNamespace
import time

from PIL import Image
import pytest

from pet.core.adapter_api import CoreServices
from pet.core.belief import (
    EvidenceStore,
    FastObservationPayload,
    FrameMetricsPayload,
    KeyWindowPayload,
    SceneFingerprintPayload,
    render_observations_markdown,
)
from pet.core.config import (
    AdapterConfig,
    GenericVisionConfig,
    LlmConfig,
    LlmProfileConfig,
    OcrConfig,
    SceneConfig,
)
from pet.core.llm import LlmResult, LlmUsage, image_upload_metadata
from pet.core.input_telemetry import ActionInputEvent, ActionInputTimeline
import pet.games.generic.adapter as generic_adapter_module
from pet.games.generic.adapter import (
    FAST_PROMPT_PATH,
    GameIdentity,
    GenericVisionAdapter,
    ObservationLog,
    ObservationRecord,
    TitleRule,
    WindowTitleMap,
    _change_reason,
    _coarse_location,
    _focus_geometry,
    _focus_scope,
    _fast_outcome,
    _user_prompt,
)


@dataclass(frozen=True)
class FakeMetadata:
    window_title: str
    process_name: str
    captured_at: datetime
    monotonic_seconds: float


@dataclass(frozen=True)
class FakeFrame:
    bitmap: Image.Image
    metadata: FakeMetadata


class FakeBackend:
    def __init__(self, frames: list[FakeFrame]) -> None:
        self.frames = frames
        self.closed = False

    def capture_frame(self) -> FakeFrame | None:
        return self.frames.pop(0) if self.frames else None

    def close(self) -> None:
        self.closed = True


class AlwaysSelect:
    def __init__(self, region: tuple[str, ...] = ("r2c3",)) -> None:
        self.region = region

    def observe(self, _frame: Image.Image, _now: float) -> object:
        return SimpleNamespace(
            comparisons=SimpleNamespace(
                vs_baseline=SimpleNamespace(mean_amplitude=0.062)
            ),
            decision=SimpleNamespace(
                should_save=True,
                forced=False,
                region_grid=self.region,
                confirmed_region_grid=self.region,
                changed_block_ratio=len(self.region) / 144.0,
                baseline_monotonic_seconds=_now - 1.26,
                confirmed_region_intensity=0.60,
            )
        )


class SelectPattern(AlwaysSelect):
    def __init__(self, decisions: list[bool]) -> None:
        super().__init__()
        self.decisions = decisions

    def observe(self, frame: Image.Image, now: float) -> object:
        observation = super().observe(frame, now)
        observation.decision.should_save = self.decisions.pop(0)
        return observation


class FakeClient:
    def __init__(
        self,
        delays: list[float] | None = None,
        texts: list[str] | None = None,
    ) -> None:
        self.delays = delays or []
        self.texts = texts or []
        self.calls: list[dict[str, object]] = []
        self.active = 0
        self.max_active = 0
        self.closed = False

    def complete_with_images_stream(self, **kwargs: object) -> LlmResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        index = len(self.calls)
        self.calls.append(kwargs)
        try:
            assert kwargs["images"][0].encoding == "jpeg"  # type: ignore[index]
            metadata = image_upload_metadata(  # type: ignore[index]
                kwargs["images"][0], max_image_edge=None
            )
            assert metadata.width == 896
            if index < len(self.delays):
                time.sleep(self.delays[index])
            return LlmResult(
                text=(
                    self.texts[index]
                    if index < len(self.texts)
                    else f"【画面】观察 {index + 1}\n【局部】局部对象正在移动"
                ),
                usage=LlmUsage(100, 20, None),
                latency_seconds=0.01,
                model="fixture-model",
                provider="fixture-provider",
                finish_reason="stop",
            )
        finally:
            self.active -= 1

    def close(self) -> None:
        self.closed = True


class DelayedBitmapClient(FakeClient):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.bitmap_bytes: bytes | None = None
        self.bitmap_read = threading.Event()

    def complete_with_images_stream(self, **kwargs: object) -> LlmResult:
        self.calls.append(kwargs)
        time.sleep(self.delay)
        image = kwargs["images"][0]  # type: ignore[index]
        self.bitmap_bytes = image.path.tobytes()
        self.bitmap_read.set()
        return LlmResult(
            text="【画面】延迟观察",
            usage=LlmUsage(100, 20, None),
            latency_seconds=self.delay,
            model="fixture-model",
            provider="fixture-provider",
            finish_reason="stop",
        )


class DelayedFailureClient(FakeClient):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.finished = threading.Event()

    def complete_with_images_stream(self, **kwargs: object) -> LlmResult:
        self.calls.append(kwargs)
        time.sleep(self.delay)
        self.finished.set()
        raise RuntimeError("late worker failure")


def _configuration(
    log_dir: Path,
    *,
    enabled: bool = True,
    timeout: float = 0.2,
    max_inflight: int = 4,
    cost_warn: float = 1.0,
    input_context: bool = False,
) -> tuple[AdapterConfig, LlmConfig]:
    adapter = AdapterConfig(
        generic=GenericVisionConfig(
            enabled=enabled,
            poll_interval_seconds=0.005,
            fast_timeout_seconds=timeout,
            max_inflight=max_inflight,
            input_context=input_context,
            observation_log_dir=str(log_dir),
            cost_warn_per_hour=cost_warn,
            ocr=OcrConfig(enabled=False),
            scene=SceneConfig(enabled=False),
        )
    )
    llm = LlmConfig(
        profiles={
            "vision_fast": LlmProfileConfig(
                enabled=True,
                model="fixture-model",
                provider="fixture-provider",
                temperature=0.0,
                max_tokens=200,
                input_price_per_million_usd=1.0,
                output_price_per_million_usd=2.0,
            )
        }
    )
    return adapter, llm


def _frame(sequence: int, *, title: str = "GZW ") -> FakeFrame:
    return FakeFrame(
        Image.new("RGB", (1600, 900), color=(sequence, 30, 40)),
        FakeMetadata(
            title,
            "fixture.exe",
            datetime(2026, 8, 26, 12, 0, sequence, tzinfo=timezone.utc),
            float(sequence),
        ),
    )


def _core(statuses: list[object]) -> CoreServices:
    async def no_speech(_request: object) -> None:
        raise AssertionError("generic vision must not submit SpeechRequest")

    async def publish(status: object) -> None:
        statuses.append(status)

    async def reset() -> None:
        return None

    return CoreServices(
        submit_speech=no_speech,
        publish_status=publish,
        can_submit_speech=lambda: True,
        speech_is_muted=lambda: False,
        reset_speech_session=reset,
    )


def _title_map() -> WindowTitleMap:
    return WindowTitleMap((TitleRule("Grey Zone Warfare", ("gzw",), ()),))


async def _wait_for_observation_rows(
    log_root: Path,
    expected_count: int,
    *,
    timeout_seconds: float = 10.0,
) -> tuple[Path, list[dict[str, object]]]:
    """Wait for complete fast evidence groups instead of assuming scheduler timing."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        sessions = tuple(path for path in log_root.iterdir() if path.is_dir())
        if len(sessions) == 1:
            session = sessions[0]
            path = session / "evidence.jsonl"
            if path.is_file():
                rows = _legacy_observation_rows(session)
                if len(rows) >= expected_count:
                    return session, rows
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"evidence.jsonl did not reach {expected_count} fast rows within "
                f"{timeout_seconds:.1f} seconds"
            )
        await asyncio.sleep(0.005)


def _legacy_observation_rows(directory: Path) -> list[dict[str, object]]:
    """Project evidence into the former row shape for unchanged report assertions."""
    session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    if session["origin_monotonic"] is None:
        return []
    origin = float(session["origin_monotonic"])
    grouped: dict[str, dict[str, object]] = {}
    for event in EvidenceStore.read(directory / "evidence.jsonl"):
        assert event.root_capture_id is not None
        grouped.setdefault(event.root_capture_id, {})[event.kind] = event
    rows: list[dict[str, object]] = []
    for root_capture_id in sorted(grouped, key=lambda value: int(value[1:])):
        frame = grouped[root_capture_id]
        if "fast_observation" not in frame:
            continue
        fast_event = frame["fast_observation"]
        metrics_event = frame["frame_metrics"]
        key_event = frame["key_window"]
        fast = fast_event.payload  # type: ignore[union-attr]
        metrics = metrics_event.payload  # type: ignore[union-attr]
        key = key_event.payload  # type: ignore[union-attr]
        assert isinstance(fast, FastObservationPayload)
        assert isinstance(metrics, FrameMetricsPayload)
        assert isinstance(key, KeyWindowPayload)
        rows.append(
            {
                "seq": int(root_capture_id[1:]),
                "frame_ts": origin + fast_event.observed_at,  # type: ignore[union-attr]
                "wall": metrics.wall,
                "game": fast.game,
                "text": fast.text,
                "region": fast_event.scope.cells if fast_event.scope else None,  # type: ignore[union-attr]
                "reason": metrics.reason,
                "change_ratio": round(metrics.change_ratio, 2),
                "global_change": round(metrics.global_change, 1),
                "region_area_ratio": (
                    round(metrics.region_area_ratio)
                    if metrics.region_area_ratio is not None
                    else None
                ),
                "region_intensity": (
                    round(metrics.region_intensity)
                    if metrics.region_intensity is not None
                    else None
                ),
                "input": key.summary,
                "latency_ms": round(fast.latency_ms, 3),
                "ttft_ms": round(fast.ttft_ms, 3) if fast.ttft_ms is not None else None,
                "dropped": fast.drop_reason,
                "user_prompt": fast.user_prompt,
                "speculation": fast.speculation,
                "input_tokens": fast.input_tokens,
                "output_tokens": fast.output_tokens,
                "actual_model": fast.actual_model,
                "actual_provider": fast.actual_provider,
            }
        )
    return rows


def test_disabled_adapter_never_initializes_capture(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(
        tmp_path,
        enabled=False,
        input_context=True,
    )
    touched = False
    input_touched = False
    statuses: list[object] = []

    def capture_factory() -> FakeBackend:
        nonlocal touched
        touched = True
        return FakeBackend([])

    def input_factory(_backend: object) -> ActionInputTimeline:
        nonlocal input_touched
        input_touched = True
        return ActionInputTimeline(retention_seconds=None)

    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=capture_factory,
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: (_ for _ in ()).throw(AssertionError("client created")),
        input_listener_factory=input_factory,
        title_map=_title_map(),
    )

    async def scenario() -> None:
        await adapter.start(_core(statuses))
        await adapter.stop()

    asyncio.run(scenario())
    assert touched is False
    assert input_touched is False
    assert statuses[-1].state == "disabled"  # type: ignore[union-attr]


def test_frames_call_model_and_are_logged_in_frame_order(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(tmp_path, input_context=True)
    backend = FakeBackend([_frame(1), _frame(2)])
    client = FakeClient([0.05, 0.005])
    input_timeline = ActionInputTimeline(retention_seconds=None)
    input_timeline.append(ActionInputEvent(0.2, "按下", "B"))
    input_timeline.append(ActionInputEvent(0.25, "按下", "W"))
    input_timeline.append(ActionInputEvent(0.75, "抬起", "W"))
    statuses: list[object] = []
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: backend,
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: client,
        input_listener_factory=lambda _backend: input_timeline,
        title_map=_title_map(),
    )

    async def scenario() -> None:
        await adapter.start(_core(statuses))
        try:
            return await _wait_for_observation_rows(tmp_path, 2)
        finally:
            await adapter.stop()

    session, rows = asyncio.run(scenario())
    assert [row["frame_ts"] for row in rows] == [1.0, 2.0]
    assert all("ttft_ms" in row for row in rows)
    assert [row["text"] for row in rows] == [
        "【画面】观察 1\n【局部】局部对象正在移动",
        "【画面】观察 2\n【局部】局部对象正在移动",
    ]
    assert rows[0]["region"] == ["r2c3"]
    assert rows[0]["reason"] == "sparse"
    assert rows[0]["change_ratio"] == 0.01
    assert rows[0]["global_change"] == 6.2
    assert rows[0]["region_area_ratio"] == 1
    assert rows[0]["region_intensity"] == 60
    assert rows[0]["input"] == "W 按住 0.5 秒"
    first_prompt = str(client.calls[0]["user_prompt"])
    second_prompt = str(client.calls[1]["user_prompt"])
    assert client.calls[0]["max_tokens"] == 200
    assert "最近观察" not in first_prompt
    assert "最近观察" not in second_prompt
    assert "观察 1" not in second_prompt
    assert "玩家输入：\nW 按住 0.5 秒" in first_prompt
    assert "B" not in first_prompt
    assert "画面左上、约占屏幕1%的区域正在变化" in first_prompt
    assert "区域内像素变化强度约60%，全局约6.2%" in first_prompt
    assert "r2c3" not in first_prompt
    assert "每段一到两句" in first_prompt
    assert "硬上限" not in first_prompt
    assert "【刚刚】" not in first_prompt
    assert "对象本身没有出现、消失或移动" in first_prompt
    assert "以“仅”开头且总长四到六个字" in first_prompt
    assert rows[0]["user_prompt"] == first_prompt
    markdown = (session / "observations.md").read_text(encoding="utf-8")
    assert "本会话始于 " in markdown
    assert "T+0：" in markdown
    assert "【全局画面】（全局像素变化6.2%）观察 1" in markdown
    assert "【局部｜区域占比1%】（区域像素变化60%）局部对象正在移动" in markdown
    assert "【玩家输入】W 按住 0.5 秒" in markdown
    summary = json.loads((session / "session.json").read_text(encoding="utf-8"))
    assert summary["reason_counts"] == {
        "sparse": 2,
        "coarse": 0,
        "large": 0,
        "forced": 0,
    }
    assert statuses[-1].summary["game"] == "Grey Zone Warfare"  # type: ignore[union-attr]
    assert statuses[-1].summary["input_context"] == "yes"  # type: ignore[union-attr]
    assert backend.closed and client.closed
    assert not tuple(session.rglob("*.png")) and not tuple(session.rglob("*.jpg"))


def test_timeout_is_recorded_and_loop_continues(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(tmp_path, timeout=0.01)
    backend = FakeBackend([_frame(1)])
    client = FakeClient([0.08])
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: backend,
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: client,
        title_map=_title_map(),
    )

    async def scenario() -> None:
        await adapter.start(_core([]))
        try:
            return await _wait_for_observation_rows(tmp_path, 1)
        finally:
            await adapter.stop()

    _, rows = asyncio.run(scenario())
    row = rows[0]
    assert row["dropped"] == "timeout"


# 锁住选择器单轮异常会终止观察循环并泄漏当轮位图的故障。
def test_poll_exception_isolated_and_failed_frame_bitmap_closed(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(tmp_path)
    first_frame = _frame(1)
    second_frame = _frame(2)
    backend = FakeBackend([first_frame, second_frame])
    client = FakeClient()
    statuses: list[object] = []

    class FailOnceSelector(AlwaysSelect):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def observe(self, frame: Image.Image, now: float) -> object:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("selector failed once")
            return super().observe(frame, now)

    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: backend,
        selector_factory=lambda _sparsity: FailOnceSelector(),
        client_factory=lambda *_args: client,
        title_map=_title_map(),
    )

    async def scenario() -> list[dict[str, object]]:
        await adapter.start(_core(statuses))
        try:
            _, rows = await _wait_for_observation_rows(tmp_path, 1)
            return rows
        finally:
            await adapter.stop()

    rows = asyncio.run(scenario())
    assert rows[0]["frame_ts"] == 2.0
    assert any(status.state == "error" for status in statuses)  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="Operation on closed image"):
        first_frame.bitmap.tobytes()


# 锁住等待方超时后在线程仍编码时过早关闭位图的故障。
def test_timeout_keeps_bitmap_alive_until_worker_finishes(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(tmp_path, timeout=0.01)
    frame = _frame(1)
    backend = FakeBackend([frame])
    client = DelayedBitmapClient(0.08)
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: backend,
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: client,
        title_map=_title_map(),
    )

    async def scenario() -> list[dict[str, object]]:
        await adapter.start(_core([]))
        try:
            _, rows = await _wait_for_observation_rows(tmp_path, 1)
            read_succeeded = await asyncio.to_thread(client.bitmap_read.wait, 2.0)
            assert read_succeeded is True
            deadline = asyncio.get_running_loop().time() + 2.0
            while True:
                try:
                    frame.bitmap.tobytes()
                except ValueError:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("worker completed but bitmap remained open")
                await asyncio.sleep(0.005)
            return rows
        finally:
            await adapter.stop()

    rows = asyncio.run(scenario())
    assert rows[0]["dropped"] == "timeout"
    assert client.bitmap_bytes is not None
    with pytest.raises(ValueError, match="Operation on closed image"):
        frame.bitmap.tobytes()


# 锁住超时后的工作线程迟到失败未取回而产生 asyncio ERROR 噪声的故障。
def test_timeout_retrieves_late_worker_exception_without_asyncio_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter_config, llm_config = _configuration(tmp_path, timeout=0.01)
    client = DelayedFailureClient(0.08)
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: FakeBackend([_frame(1)]),
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: client,
        title_map=_title_map(),
    )
    caplog.set_level(logging.ERROR, logger="asyncio")

    async def scenario() -> list[dict[str, object]]:
        await adapter.start(_core([]))
        try:
            _, rows = await _wait_for_observation_rows(tmp_path, 1)
            worker_finished = await asyncio.to_thread(client.finished.wait, 2.0)
            assert worker_finished is True
            await asyncio.sleep(0.05)
            return rows
        finally:
            await adapter.stop()

    rows = asyncio.run(scenario())
    assert rows[0]["dropped"] == "timeout"
    assert "Future exception was never retrieved" not in caplog.text


def test_latest_wins_keeps_one_pending_frame_and_marks_replacements(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(tmp_path, timeout=1.0, max_inflight=2)
    backend = FakeBackend([_frame(value) for value in range(1, 8)])
    client = FakeClient([0.20] * 7)
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: backend,
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: client,
        title_map=_title_map(),
    )

    async def scenario() -> None:
        await adapter.start(_core([]))
        try:
            return await _wait_for_observation_rows(tmp_path, 7)
        finally:
            await adapter.stop()

    _, rows = asyncio.run(scenario())
    assert client.max_active <= 2
    assert sum(row["dropped"] == "superseded" for row in rows) == 4
    assert all(row["dropped"] != "error:inflight_limit" for row in rows)
    assert len(client.calls) == 3
    assert rows[-1]["frame_ts"] == 7.0
    assert rows[-1]["dropped"] is None


def test_title_lookup_falls_back_to_original_window_title() -> None:
    mapping = _title_map()
    assert mapping.identify("GZW ", "anything.exe") == "Grey Zone Warfare"
    assert mapping.identify("Unknown Window 123", "unknown.exe") == "Unknown Window 123"
    matched = mapping.identify_identity("GZW Season", "anything.exe")
    unmatched = mapping.identify_identity("Unknown Window 123", "unknown.exe")
    assert matched.game_id == "grey-zone-warfare"
    assert matched.display_name == "GZW Season"
    assert unmatched.game_id == "unknown-exe"
    assert unmatched.display_name == "Unknown Window 123"


def test_selected_frames_each_emit_one_scene_event_without_changing_markdown(
    tmp_path: Path,
) -> None:
    adapter_config, llm_config = _configuration(tmp_path)
    adapter_config.generic.scene = SceneConfig(
        enabled=True,
        hash_kind="ahash",
        hash_bits=64,
        hamming_threshold=1,
        stable_min_seconds=1.0,
        card_flush_seconds=999.0,
        memory_dir=str(tmp_path / "memory"),
    )
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("replay must not initialize capture")
        ),
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: FakeClient(),
        title_map=_title_map(),
    )
    output = tmp_path / "scene-replay"

    async def scenario() -> None:
        adapter.start_replay(output, input_context=None)
        for sequence in (1, 2):
            await adapter.submit_replay_frame(
                _frame(sequence),
                "Grey Zone Warfare",
                (),
                float(sequence - 1),
                confirmed_region=(),
                change_ratio=0.6,
                global_change=20.0,
                region_intensity=0.0,
                forced=False,
            )
        await adapter.finish_replay()

    asyncio.run(scenario())
    events = list(EvidenceStore.read(output / "evidence.jsonl"))
    scene_events = [event for event in events if event.kind == "scene_fingerprint"]
    assert len(scene_events) == 2
    assert all(isinstance(event.payload, SceneFingerprintPayload) for event in scene_events)
    assert [event.root_capture_id for event in scene_events] == ["f1", "f2"]
    assert (output / "observations.md").read_text(encoding="utf-8") == (
        render_observations_markdown(events, adapter._log.started_at)
        if adapter._log is not None
        else render_observations_markdown(
            events,
            datetime.fromisoformat(
                json.loads((output / "session.json").read_text(encoding="utf-8"))[
                    "started_at"
                ]
            ),
        )
    )
    card = json.loads(
        (tmp_path / "memory" / "grey-zone-warfare" / "gamecard.json").read_text(
            encoding="utf-8"
        )
    )
    assert card["scenes"] == []


def test_no_change_polling_frames_advance_scene_stability_without_evidence(
    tmp_path: Path,
) -> None:
    adapter_config, llm_config = _configuration(tmp_path)
    adapter_config.generic.scene = SceneConfig(
        enabled=True,
        hash_kind="ahash",
        hash_bits=64,
        hamming_threshold=1,
        stable_min_seconds=5.0,
        card_flush_seconds=999.0,
        memory_dir=str(tmp_path / "memory"),
    )
    frames = [_frame(sequence) for sequence in range(1, 8)]
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: FakeBackend(frames),
        selector_factory=lambda _sparsity: SelectPattern(
            [True, False, False, False, False, False, True]
        ),
        client_factory=lambda *_args: FakeClient(),
        title_map=_title_map(),
    )

    async def scenario() -> Path:
        await adapter.start(_core([]))
        try:
            session, _rows = await _wait_for_observation_rows(tmp_path, 2)
            return session
        finally:
            await adapter.stop()

    session = asyncio.run(scenario())
    events = list(EvidenceStore.read(session / "evidence.jsonl"))
    scene_events = [event for event in events if event.kind == "scene_fingerprint"]

    assert [event.root_capture_id for event in scene_events] == ["f1", "f2"]
    assert len(scene_events) == 2
    assert scene_events[0].payload.stable is False  # type: ignore[union-attr]
    assert scene_events[1].payload.stable is True  # type: ignore[union-attr]


def test_selected_scene_switch_reports_last_selected_cluster_across_intermediate_clusters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_config, llm_config = _configuration(tmp_path)
    adapter_config.generic.scene = SceneConfig(
        enabled=True,
        hash_kind="ahash",
        hash_bits=64,
        hamming_threshold=0,
        stable_min_seconds=1.0,
        card_flush_seconds=999.0,
        memory_dir=str(tmp_path / "memory"),
    )
    hashes = iter(
        (
            "0000000000000000",
            "ffffffffffffffff",
            "0f0f0f0f0f0f0f0f",
            "0000000000000000",
        )
    )
    monkeypatch.setattr(
        generic_adapter_module,
        "perceptual_hash",
        lambda _image, _kind, _bits: next(hashes),
    )
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("replay must not initialize capture")
        ),
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: FakeClient(),
        title_map=_title_map(),
    )
    output = tmp_path / "selected-switch"
    identity = GameIdentity(
        "grey-zone-warfare",
        "Grey Zone Warfare",
        "Grey Zone Warfare",
    )

    async def scenario() -> None:
        adapter.start_replay(output, input_context=None)
        await adapter.submit_replay_frame(
            _frame(1),
            "Grey Zone Warfare",
            (),
            0.0,
            confirmed_region=(),
            change_ratio=0.6,
            global_change=20.0,
            region_intensity=0.0,
            forced=False,
        )
        for sequence in (2, 3):
            frame = _frame(sequence)
            adapter._observe_scene_frame(frame, identity)
            frame.bitmap.close()
        await adapter.submit_replay_frame(
            _frame(4),
            "Grey Zone Warfare",
            (),
            0.0,
            confirmed_region=(),
            change_ratio=0.6,
            global_change=20.0,
            region_intensity=0.0,
            forced=False,
        )
        await adapter.finish_replay()

    asyncio.run(scenario())
    scene_events = [
        event
        for event in EvidenceStore.read(output / "evidence.jsonl")
        if event.kind == "scene_fingerprint"
    ]
    assert [event.payload.cluster_id for event in scene_events] == [1, 4]  # type: ignore[union-attr]
    assert scene_events[0].payload.switched_from is None  # type: ignore[union-attr]
    assert scene_events[1].payload.switched_from == 1  # type: ignore[union-attr]


def test_game_switch_flushes_old_card_before_loading_new_card(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(tmp_path)
    adapter_config.generic.scene = SceneConfig(
        enabled=True,
        hash_kind="ahash",
        hash_bits=64,
        hamming_threshold=1,
        stable_min_seconds=1.0,
        card_flush_seconds=999.0,
        memory_dir=str(tmp_path / "memory"),
    )
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("replay must not initialize capture")
        ),
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: FakeClient(),
        title_map=_title_map(),
    )
    output = tmp_path / "switch-replay"

    async def scenario() -> None:
        adapter.start_replay(output, input_context=None)
        for sequence in (1, 2):
            frame = _frame(sequence)
            identity = GameIdentity("first-game", "First Game", "First Game")
            scene_state = adapter._observe_scene_frame(frame, identity)
            await adapter._schedule(
                frame,
                "First Game",
                (),
                0.0,
                confirmed_region=(),
                change_ratio=0.6,
                global_change=20.0,
                region_intensity=0.0,
                forced=False,
                wait_for_capacity=True,
                scene_state=scene_state,
            )
        frame = _frame(3)
        identity = GameIdentity("second-game", "Second Game", "Second Game")
        scene_state = adapter._observe_scene_frame(frame, identity)
        await adapter._schedule(
            frame,
            "Second Game",
            (),
            0.0,
            confirmed_region=(),
            change_ratio=0.6,
            global_change=20.0,
            region_intensity=0.0,
            forced=False,
            wait_for_capacity=True,
            scene_state=scene_state,
        )
        await adapter.finish_replay()

    asyncio.run(scenario())
    first = json.loads(
        (tmp_path / "memory" / "first-game" / "gamecard.json").read_text(
            encoding="utf-8"
        )
    )
    second = json.loads(
        (tmp_path / "memory" / "second-game" / "gamecard.json").read_text(
            encoding="utf-8"
        )
    )
    assert first["scenes"] == []
    assert second["scenes"] == []


def test_cost_uses_profile_prices_and_sets_warning(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(tmp_path, cost_warn=0.000001)
    backend = FakeBackend([_frame(1)])
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: backend,
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: FakeClient(),
        title_map=_title_map(),
    )

    async def scenario() -> None:
        await adapter.start(_core([]))
        try:
            return await _wait_for_observation_rows(tmp_path, 1)
        finally:
            await adapter.stop()

    session, _ = asyncio.run(scenario())
    summary = json.loads((session / "session.json").read_text(encoding="utf-8"))
    assert summary["total_cost_usd"] == 0.00014
    assert adapter._cost_warning is True


def test_offline_replay_uses_shared_ordered_pipeline_with_backpressure(
    tmp_path: Path,
) -> None:
    adapter_config, llm_config = _configuration(
        tmp_path,
        max_inflight=1,
        input_context=True,
    )
    client = FakeClient([0.02, 0.0])
    input_timeline = ActionInputTimeline(retention_seconds=None)
    input_timeline.append(ActionInputEvent(0.5, "按下", "KeyW"))
    input_timeline.append(ActionInputEvent(0.6, "抬起", "KeyW"))
    input_timeline.append(ActionInputEvent(1.5, "按下", "MouseLeft"))
    input_timeline.append(ActionInputEvent(1.6, "抬起", "MouseLeft"))
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("replay must not initialize capture")
        ),
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: client,
        title_map=_title_map(),
    )
    output = tmp_path / "exact-replay"

    async def scenario() -> None:
        adapter.start_replay(
            output,
            input_context=input_timeline,
            input_window_start_monotonic=1.0,
        )
        await adapter.submit_replay_frame(
            _frame(1),
            "Grey Zone Warfare",
            ("r2c3",),
            0.0,
            confirmed_region=("r2c3",),
            change_ratio=1 / 144,
            global_change=6.2,
            region_intensity=60.0,
            forced=False,
        )
        await adapter.submit_replay_frame(
            _frame(2),
            "Grey Zone Warfare",
            (),
            1.0,
            confirmed_region=(),
            change_ratio=0.0,
            global_change=0.0,
            region_intensity=0.0,
            forced=True,
        )
        await adapter.finish_replay()

    asyncio.run(scenario())
    rows = _legacy_observation_rows(output)
    assert [row["frame_ts"] for row in rows] == [1.0, 2.0]
    assert "此窗口内无玩家输入" in str(client.calls[0]["user_prompt"])
    assert "玩家输入：\nW" not in str(client.calls[0]["user_prompt"])
    assert "观察 1" not in str(client.calls[1]["user_prompt"])
    assert "最近观察" not in str(client.calls[1]["user_prompt"])
    assert "玩家输入：\n左键点击 1 次" in str(client.calls[1]["user_prompt"])
    assert "本帧为定时快照，此前约 1.0 秒未检测到显著变化" in str(
        client.calls[1]["user_prompt"]
    )
    assert "不要输出【局部】" in str(client.calls[1]["user_prompt"])
    assert (output / "observations.md").is_file()
    assert (output / "session.json").is_file()
    session = json.loads((output / "session.json").read_text(encoding="utf-8"))
    assert session["truncated_count"] == 0
    assert session["average_visible_output_tokens"] == 20.0
    assert session["parameters"]["input_context"] is True
    assert session["reason_counts"] == {
        "sparse": 1,
        "coarse": 0,
        "large": 0,
        "forced": 1,
    }
    markdown = (output / "observations.md").read_text(encoding="utf-8")
    assert "T+1（心跳）：" in markdown
    assert "【玩家输入】左键点击 1 次" in markdown


def test_input_context_false_has_no_listener_message_segment_or_status_flag(
    tmp_path: Path,
) -> None:
    adapter_config, llm_config = _configuration(tmp_path, input_context=False)
    backend = FakeBackend([_frame(1)])
    client = FakeClient()
    input_listener_created = False
    statuses: list[object] = []

    def forbidden_input_factory(_backend: object) -> ActionInputTimeline:
        nonlocal input_listener_created
        input_listener_created = True
        raise AssertionError("disabled input context must not initialize a listener")

    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: backend,
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: client,
        input_listener_factory=forbidden_input_factory,
        title_map=_title_map(),
    )

    async def scenario() -> tuple[Path, list[dict[str, object]]]:
        await adapter.start(_core(statuses))
        try:
            return await _wait_for_observation_rows(tmp_path, 1)
        finally:
            await adapter.stop()

    session, rows = asyncio.run(scenario())
    assert input_listener_created is False
    assert "玩家输入" not in str(client.calls[0]["user_prompt"])
    assert "玩家输入" not in str(rows[0]["user_prompt"])
    assert rows[0]["input"] == "无输入"
    payload = json.loads((session / "session.json").read_text(encoding="utf-8"))
    assert payload["parameters"]["input_context"] is False
    assert statuses[-1].summary["input_context"] == "no"  # type: ignore[union-attr]


def test_fast_prompt_contains_two_part_contract_without_concrete_examples() -> None:
    prompt = FAST_PROMPT_PATH.read_text(encoding="utf-8")
    assert "固定输出【画面】" in prompt
    assert "一到两句" in prompt
    assert "仅当用户消息明确提供“聚焦区域”" in prompt
    assert "区域及紧邻环境正在发生什么" in prompt
    assert "全局像素变化大而没有聚焦区域" in prompt
    assert "区域占比小而区域强度高" in prompt
    assert "聚焦区域的报告优先级（高者优先）" in prompt
    assert "可读出的画面文字与数字" in prompt
    assert "对象正在进入、离开或移动" in prompt
    assert "界面结构与状态图标正在变化" in prompt
    assert "四到六个字结束" in prompt
    assert "判断依据是“正在变化的是什么”" in prompt
    assert "读不清必须说“读不清”" in prompt
    assert "悬浮信息框或状态面板是画面主体" in prompt
    assert "场景没有改变时" in prompt and "相似是正常的" in prompt
    assert "出现了" in prompt and "较上一帧" in prompt and "原本" in prompt
    assert "玩家输入" in prompt
    assert "“玩家输入”不能触发这一段" in prompt
    assert "不得复述任何键名、按键次数或时间间隔" in prompt
    assert "鼠标大幅移动时，当前帧可能是视角转动后的新朝向" in prompt
    assert "窗口内无输入而画面有变化时，该变化并非玩家操作所致" in prompt
    assert "允许把输入与画面连起来陈述当下可直接确认的事实" in prompt
    assert "目的性推断只能放在可选的【推测】段" in prompt
    assert "必须有键鼠输入或画面证据支撑" in prompt
    assert "无把握时省略【推测】" in prompt
    assert "禁止在【画面】或【局部】中写目的性推断" in prompt
    assert "表明玩家" in prompt
    assert "不得编造" in prompt
    assert "硬上限" not in prompt
    assert "格子" not in prompt
    assert "【刚刚】" not in prompt
    assert "无明显变化" not in prompt
    assert "优先讲新变化" not in prompt
    forbidden_concrete_examples = (
        "闪电",
        "手电筒",
        "路灯",
        "直升机",
        "卡牌",
        "枪械",
        "Slay the Spire",
        "Grey Zone Warfare",
        "r8c1",
    )
    assert not [value for value in forbidden_concrete_examples if value in prompt]


def test_coarse_location_maps_nine_grid_and_bbox_boundaries() -> None:
    assert _coarse_location(("r1c1",)) == "左上"
    assert _coarse_location(("r5c3",)) == "左上"
    assert _coarse_location(("r5c4",)) == "上方"
    assert _coarse_location(("r6c3",)) == "左侧"
    assert _coarse_location(("r6c4",)) == "中央"
    assert _coarse_location(("r11c7",)) == "右下"
    assert _coarse_location(("r16c9",)) == "右下"
    assert _coarse_location(("r1c1", "r16c9")) == "中央"
    assert _focus_geometry(("r1c1",)) == ("左上", 100 / 144)
    assert _focus_geometry(("r1c1", "r2c2")) == ("左上", 400 / 144)


def test_focus_wide_and_heartbeat_templates_never_send_grid_coordinates() -> None:
    assert _change_reason(False, 0.25) == "sparse"
    assert _change_reason(False, 0.250001) == "coarse"
    assert _change_reason(False, 0.50) == "coarse"
    assert _change_reason(False, 0.500001) == "large"
    assert _change_reason(True, 0.0) == "forced"
    assert _change_reason(True, 0.80) == "large"
    focused = _user_prompt(
        "Fixture",
        "此窗口内无玩家输入",
        reason="sparse",
        global_change=6.2,
        region_area_ratio=30.0,
        region_intensity=60.0,
        focus_location="中央偏左",
        baseline_seconds_ago=1.26,
    )
    assert "画面中央偏左、约占屏幕30%的区域正在变化" in focused
    assert "区域内像素变化强度约60%，全局约6.2%" in focused
    assert "30%、60%、6.2%是系统定位数值" in focused
    assert "【画面】和【局部】" in focused
    assert "r2c3" not in focused

    wide = _user_prompt(
        "Fixture",
        None,
        reason="large",
        global_change=18.4,
        region_area_ratio=None,
        region_intensity=None,
        focus_location=None,
        baseline_seconds_ago=2.0,
    )
    assert "本帧变化范围较广（全局像素变化约18.4%），未提供聚焦区域" in wide
    assert "18.4%是系统定位数值" in wide
    assert "不要输出【局部】" in wide

    forced = _user_prompt(
        "Fixture",
        None,
        reason="forced",
        global_change=0.0,
        region_area_ratio=None,
        region_intensity=None,
        focus_location=None,
        baseline_seconds_ago=60.04,
    )
    assert "本帧为定时快照，此前约 60.0 秒未检测到显著变化" in forced
    assert "不要输出【局部】" in forced
    assert not re.search(r"(?i)r\d+c\d+", focused + wide + forced)


def test_observation_log_writes_mechanical_fields_markers_and_reason_counts(
    tmp_path: Path,
) -> None:
    log = ObservationLog(tmp_path / "mechanical-log", {}, exact_directory=True)
    fixtures = (
        ("sparse", 0.12, None, "W 按住 0.5 秒"),
        ("coarse", 0.38, "左上", "无输入"),
        ("large", 0.62, None, "无输入"),
        ("forced", 0.0, None, "无输入"),
    )
    for sequence, (reason, ratio, location, input_text) in enumerate(fixtures, start=1):
        scope = _focus_scope(("r2c3",)) if reason == "sparse" else None
        log.append_frame_metrics(
            sequence=sequence,
            frame_ts=float(sequence),
            wall=f"2026-08-26T12:00:0{sequence}+00:00",
            reason=reason,  # type: ignore[arg-type]
            change_ratio=ratio,
            global_change=6.2,
            region_area_ratio=30.0 if reason in {"sparse", "coarse"} else None,
            region_intensity=60.0 if reason in {"sparse", "coarse"} else None,
            scope=scope,
        )
        log.append_key_window(
            sequence=sequence,
            frame_ts=float(sequence),
            summary=input_text,
            window_start=float(sequence - 1),
        )
        log.append(
            ObservationRecord(
                seq=sequence,
                frame_ts=float(sequence),
                wall=f"2026-08-26T12:00:0{sequence}+00:00",
                game="Fixture",
                text=(
                    "【画面】当前场景\n【局部】对象正在移动\n【推测】似乎正在查看某界面"
                    if reason in {"sparse", "coarse"}
                    else "【画面】当前场景"
                ),
                region=("r2c3",) if reason == "sparse" else None,
                reason=reason,  # type: ignore[arg-type]
                change_ratio=ratio,
                global_change=6.2,
                region_area_ratio=30.0 if reason in {"sparse", "coarse"} else None,
                region_intensity=60.0 if reason in {"sparse", "coarse"} else None,
                input=input_text,
                focus_location=location,
                scope=scope,
                latency_ms=10.0,
                ttft_ms=5.0,
                dropped=None,
                cost_usd=0.0,
                model_called=True,
                visible_output_tokens=4,
                truncated=False,
                speculation=(
                    "似乎正在查看某界面" if reason in {"sparse", "coarse"} else None
                ),
                input_tokens=100,
                output_tokens=20,
                actual_model="fixture-model",
                actual_provider="fixture-provider",
                learned_at=float(sequence) + 0.01,
            )
        )
    log.close()

    directory = tmp_path / "mechanical-log"
    rows = _legacy_observation_rows(directory)
    assert [(row["reason"], row["change_ratio"], row["input"]) for row in rows] == [
        ("sparse", 0.12, "W 按住 0.5 秒"),
        ("coarse", 0.38, "无输入"),
        ("large", 0.62, "无输入"),
        ("forced", 0.0, "无输入"),
    ]
    assert all(row["global_change"] == 6.2 for row in rows)
    assert [row["region_area_ratio"] for row in rows] == [30, 30, None, None]
    assert [row["region_intensity"] for row in rows] == [60, 60, None, None]
    assert [row["speculation"] for row in rows] == [
        "似乎正在查看某界面",
        "似乎正在查看某界面",
        None,
        None,
    ]
    assert rows[0]["input_tokens"] == 100 and rows[0]["output_tokens"] == 20
    markdown = (directory / "observations.md").read_text(encoding="utf-8")
    assert "本会话始于 " in markdown
    assert "T+0：" in markdown and "T+3（心跳）：" in markdown
    assert "【全局画面】（全局像素变化6.2%）当前场景" in markdown
    assert markdown.count("【局部｜区域占比30%】（区域像素变化60%）对象正在移动") == 2
    assert markdown.count("【推测】似乎正在查看某界面") == 2
    assert "【玩家输入】W 按住 0.5 秒" in markdown
    assert markdown.count("【玩家输入】无输入") == 3
    session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    assert session["reason_counts"] == {
        "sparse": 1,
        "coarse": 1,
        "large": 1,
        "forced": 1,
    }


def test_b_t1_old_fake_client_scenario_covers_markdown_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_started_at = datetime(2026, 8, 29, 12, 34, 56, tzinfo=timezone.utc)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            if tz is None:
                return fixed_started_at.replace(tzinfo=None)
            return fixed_started_at

    monkeypatch.setattr(generic_adapter_module, "datetime", FixedDatetime)
    adapter_config, llm_config = _configuration(
        tmp_path,
        timeout=0.15,
        max_inflight=1,
        input_context=True,
    )
    client = FakeClient(
        delays=[0.01, 0.30, 0.0],
        texts=[
            "【画面】主区域保持清晰\n【局部】左上对象正在移动\n【推测】似乎正在查看界面",
            "【画面】这条迟到结果不得进入日志",
            "【画面】定时快照保持稳定",
        ],
    )
    input_timeline = ActionInputTimeline(retention_seconds=None)
    input_timeline.append(ActionInputEvent(0.2, "按下", "W"))
    input_timeline.append(ActionInputEvent(0.8, "抬起", "W"))
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("baseline replay must not initialize capture")
        ),
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: client,
        title_map=_title_map(),
    )
    output = tmp_path / "b-t1-baseline"

    async def scenario() -> None:
        adapter.start_replay(
            output,
            input_context=input_timeline,
            input_window_start_monotonic=0.0,
        )
        await adapter._schedule(
            _frame(1),
            "Grey Zone Warfare",
            ("r2c3", "r3c3"),
            0.0,
            confirmed_region=("r2c3", "r3c3"),
            change_ratio=2 / 144,
            global_change=6.25,
            region_intensity=60.4,
            forced=False,
        )
        await adapter._schedule(
            _frame(2),
            "Grey Zone Warfare",
            (),
            1.0,
            confirmed_region=(),
            change_ratio=0.38,
            global_change=18.75,
            region_intensity=0.0,
            forced=False,
        )
        await adapter._schedule(
            _frame(3),
            "Grey Zone Warfare",
            (),
            2.0,
            confirmed_region=(),
            change_ratio=0.62,
            global_change=31.25,
            region_intensity=0.0,
            forced=False,
        )
        deadline = asyncio.get_running_loop().time() + 2.0
        while adapter._inflight or adapter._queued_frame is not None or client.active:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("baseline timeout and late worker did not drain")
            await asyncio.sleep(0.005)
        await adapter._schedule(
            _frame(4),
            "Grey Zone Warfare",
            (),
            -56.0,
            confirmed_region=(),
            change_ratio=0.0,
            global_change=0.0,
            region_intensity=0.0,
            forced=True,
        )
        await adapter.finish_replay()

    asyncio.run(scenario())
    markdown_path = output / "observations.md"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "本会话始于 2026-08-29T12:34:56+00:00" in markdown
    assert "[丢弃：superseded]" in markdown
    assert "[丢弃：timeout]" in markdown
    assert "T+3（心跳）：" in markdown
    assert "【局部｜区域占比1%】（区域像素变化60%）左上对象正在移动" in markdown
    assert "【推测】似乎正在查看界面" in markdown
    assert "【全局画面】（全局像素变化0.0%）定时快照保持稳定" in markdown
    assert "【玩家输入】W 按住 0.6 秒" in markdown
    assert len(client.calls) == 3
    request_signatures = []
    for call in client.calls:
        images = call["images"]
        signature_payload = {
            key: call[key]
            for key in (
                "model",
                "provider",
                "system_prompt",
                "user_prompt",
                "max_image_edge",
                "max_tokens",
                "temperature",
                "reasoning_enabled",
            )
        }
        signature_payload["images"] = [
            {
                "label": image.label,
                "max_edge": image.max_edge,
                "target_width": image.target_width,
                "encoding": image.encoding,
                "jpeg_quality": image.jpeg_quality,
            }
            for image in images  # type: ignore[union-attr]
        ]
        serialized = json.dumps(
            signature_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        request_signatures.append(hashlib.sha256(serialized).hexdigest())
    assert request_signatures == [
        "2ed4930f41dc7a37cbbe152da190d9798f74377fb980d15b795474790988d0de",
        "b62370ecf5a63ebbaf0d63e130f93d22ab7d07b44c017c6167358e1e17e65d54",
        "d3d6a695e273aa1dbbbfdf67bff8ef19b51b6d1bc58f6d665d255e08ea352c07",
    ]
    baseline_path = (
        Path(__file__).parent
        / "fixtures"
        / "generic"
        / "observations-baseline-b-t1.md"
    )
    assert markdown_path.read_bytes() == baseline_path.read_bytes()
    events = list(EvidenceStore.read(output / "evidence.jsonl"))
    grouped: dict[str, dict[str, object]] = {}
    for event in events:
        assert event.root_capture_id is not None
        grouped.setdefault(event.root_capture_id, {})[event.kind] = event
    assert set(grouped) == {"f1", "f2", "f3", "f4"}
    assert all(
        set(frame) == {"frame_metrics", "key_window", "fast_observation"}
        for frame in grouped.values()
    )
    # ROOT CAUSE: concurrent replacement once discarded every mechanical fact for
    # that frame; belief must still advance when a model result never exists.
    for root_capture_id in ("f2", "f3"):
        assert "frame_metrics" in grouped[root_capture_id]
        assert "key_window" in grouped[root_capture_id]
    fast_outcomes = {
        root: frame["fast_observation"].outcome  # type: ignore[union-attr]
        for root, frame in grouped.items()
    }
    assert fast_outcomes == {
        "f1": "ok",
        "f2": "superseded",
        "f3": "dropped",
        "f4": "ok",
    }
    for event in events:
        if event.kind in {"frame_metrics", "key_window"}:
            assert event.learned_at == event.observed_at
        if event.kind == "fast_observation":
            payload = event.payload
            assert isinstance(payload, FastObservationPayload)
            # Both values derive from the same monotonic elapsed duration; 1 ns
            # only absorbs binary floating-point multiply/divide roundoff.
            assert event.learned_at - event.observed_at == pytest.approx(
                payload.latency_ms / 1000.0,
                abs=1e-9,
            )
    session = json.loads((output / "session.json").read_text(encoding="utf-8"))
    assert session["origin_monotonic"] == 1.0
    regenerated = render_observations_markdown(events, fixed_started_at)
    assert regenerated.encode("utf-8") == markdown_path.read_bytes()
    assert not (output / "observations.jsonl").exists()


@pytest.mark.parametrize(
    ("drop_reason", "outcome"),
    [
        (None, "ok"),
        ("timeout", "dropped"),
        ("error:HTTP 429 rate limited", "dropped"),
        ("error:stopped", "dropped"),
        ("superseded", "superseded"),
        ("error:模型返回空观察", "failed"),
        ("error:provider disconnected", "failed"),
    ],
)
def test_fast_outcome_mapping(drop_reason: str | None, outcome: str) -> None:
    assert _fast_outcome(drop_reason) == outcome
