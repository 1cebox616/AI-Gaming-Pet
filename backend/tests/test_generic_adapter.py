from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import time

from PIL import Image

from pet.core.adapter_api import CoreServices
from pet.core.config import (
    AdapterConfig,
    GenericVisionConfig,
    LlmConfig,
    LlmProfileConfig,
)
from pet.core.llm import LlmResult, LlmUsage, image_upload_metadata
from pet.games.generic.adapter import GenericVisionAdapter, TitleRule, WindowTitleMap


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
            decision=SimpleNamespace(should_save=True, region_grid=self.region)
        )


class FakeClient:
    def __init__(self, delays: list[float] | None = None) -> None:
        self.delays = delays or []
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
                text=f"观察 {index + 1}",
                usage=LlmUsage(100, 20, None),
                latency_seconds=0.01,
                model="fixture-model",
                provider="fixture-provider",
            )
        finally:
            self.active -= 1

    def close(self) -> None:
        self.closed = True


def _configuration(
    log_dir: Path,
    *,
    enabled: bool = True,
    timeout: float = 0.2,
    max_inflight: int = 4,
    cost_warn: float = 1.0,
) -> tuple[AdapterConfig, LlmConfig]:
    adapter = AdapterConfig(
        generic=GenericVisionConfig(
            enabled=enabled,
            poll_interval_seconds=0.005,
            fast_timeout_seconds=timeout,
            max_inflight=max_inflight,
            observation_log_dir=str(log_dir),
            cost_warn_per_hour=cost_warn,
        )
    )
    llm = LlmConfig(
        profiles={
            "vision_fast": LlmProfileConfig(
                enabled=True,
                model="fixture-model",
                provider="fixture-provider",
                temperature=0.0,
                max_tokens=80,
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
    """Wait for flushed JSONL rows instead of assuming scheduler timing."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        sessions = tuple(path for path in log_root.iterdir() if path.is_dir())
        if len(sessions) == 1:
            session = sessions[0]
            path = session / "observations.jsonl"
            if path.is_file():
                rows = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                ]
                if len(rows) >= expected_count:
                    return session, rows
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"observations.jsonl did not reach {expected_count} rows within "
                f"{timeout_seconds:.1f} seconds"
            )
        await asyncio.sleep(0.005)


def test_disabled_adapter_never_initializes_capture(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(tmp_path, enabled=False)
    touched = False
    statuses: list[object] = []

    def capture_factory() -> FakeBackend:
        nonlocal touched
        touched = True
        return FakeBackend([])

    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=capture_factory,
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: (_ for _ in ()).throw(AssertionError("client created")),
        title_map=_title_map(),
    )

    async def scenario() -> None:
        await adapter.start(_core(statuses))
        await adapter.stop()

    asyncio.run(scenario())
    assert touched is False
    assert statuses[-1].state == "disabled"  # type: ignore[union-attr]


def test_frames_call_model_and_are_logged_in_frame_order(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(tmp_path)
    backend = FakeBackend([_frame(1), _frame(2)])
    client = FakeClient([0.05, 0.005])
    statuses: list[object] = []
    adapter = GenericVisionAdapter(
        adapter_config,
        llm_config,
        capture_backend_factory=lambda: backend,
        selector_factory=lambda _sparsity: AlwaysSelect(),
        client_factory=lambda *_args: client,
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
    assert [row["text"] for row in rows] == ["观察 1", "观察 2"]
    assert rows[0]["region"] == ["r2c3"]
    assert "最近观察" in client.calls[0]["user_prompt"]
    assert "r2c3" in client.calls[0]["user_prompt"]
    assert statuses[-1].summary["game"] == "Grey Zone Warfare"  # type: ignore[union-attr]
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


def test_inflight_limit_drops_without_exceeding_limit(tmp_path: Path) -> None:
    adapter_config, llm_config = _configuration(tmp_path, max_inflight=2)
    backend = FakeBackend([_frame(value) for value in range(1, 8)])
    client = FakeClient([0.06] * 7)
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
    assert any(row["dropped"] == "error:inflight_limit" for row in rows)


def test_title_lookup_falls_back_to_original_window_title() -> None:
    mapping = _title_map()
    assert mapping.identify("GZW ", "anything.exe") == "Grey Zone Warfare"
    assert mapping.identify("Unknown Window 123", "unknown.exe") == "Unknown Window 123"


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
    adapter_config, llm_config = _configuration(tmp_path, max_inflight=1)
    client = FakeClient([0.02, 0.0])
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
        adapter.start_replay(output, context_lines=1)
        await adapter.submit_replay_frame(_frame(1), "Grey Zone Warfare", ("r2c3",))
        await adapter.submit_replay_frame(_frame(2), "Grey Zone Warfare", ())
        await adapter.finish_replay()

    asyncio.run(scenario())
    rows = [
        json.loads(line)
        for line in (output / "observations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["frame_ts"] for row in rows] == [1.0, 2.0]
    assert "观察 1" in str(client.calls[1]["user_prompt"])
    assert "最多1条" in str(client.calls[1]["user_prompt"])
    assert (output / "observations.md").is_file()
    assert (output / "session.json").is_file()
