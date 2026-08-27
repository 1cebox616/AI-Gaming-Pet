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
from pet.core.input_telemetry import ActionInputEvent, ActionInputTimeline
from pet.games.generic.adapter import (
    FAST_PROMPT_PATH,
    GenericVisionAdapter,
    TitleRule,
    WindowTitleMap,
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
            decision=SimpleNamespace(
                should_save=True,
                region_grid=self.region,
                baseline_monotonic_seconds=_now - 1.26,
            )
        )


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
                text=self.texts[index] if index < len(self.texts) else f"观察 {index + 1}",
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
    assert [row["text"] for row in rows] == ["观察 1", "观察 2"]
    assert rows[0]["region"] == ["r2c3"]
    first_prompt = str(client.calls[0]["user_prompt"])
    second_prompt = str(client.calls[1]["user_prompt"])
    assert "最近观察" not in first_prompt
    assert "最近观察" not in second_prompt
    assert "观察 1" not in second_prompt
    assert "玩家输入：\nW 按住 0.5 秒" in first_prompt
    assert "B" not in first_prompt
    assert "与 1.3 秒前的变化基线相比" in first_prompt
    assert "r2c3" in first_prompt
    assert "必须恰好输出两行" in first_prompt
    assert "25个为硬上限" in first_prompt
    assert "40个为硬上限" in first_prompt
    assert "格子编号只是定位信息，输出中不得出现" in first_prompt
    assert "对象本身没有出现、消失或移动" in first_prompt
    assert "以“仅”开头且总长四到六个字" in first_prompt
    assert rows[0]["user_prompt"] == first_prompt
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


def test_latest_wins_keeps_one_pending_frame_and_marks_replacements(tmp_path: Path) -> None:
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
    assert sum(row["dropped"] == "superseded" for row in rows) == 4
    assert all(row["dropped"] != "error:inflight_limit" for row in rows)
    assert len(client.calls) == 3
    assert rows[-1]["frame_ts"] == 7.0
    assert rows[-1]["dropped"] is None


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
    adapter_config, llm_config = _configuration(
        tmp_path,
        max_inflight=1,
        input_context=True,
    )
    client = FakeClient([0.02, 0.0])
    input_timeline = ActionInputTimeline(retention_seconds=None)
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
        adapter.start_replay(output, input_context=input_timeline)
        await adapter.submit_replay_frame(
            _frame(1),
            "Grey Zone Warfare",
            ("r2c3",),
            0.0,
        )
        await adapter.submit_replay_frame(
            _frame(2),
            "Grey Zone Warfare",
            (),
            1.0,
        )
        await adapter.finish_replay()

    asyncio.run(scenario())
    rows = [
        json.loads(line)
        for line in (output / "observations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["frame_ts"] for row in rows] == [1.0, 2.0]
    assert "观察 1" not in str(client.calls[1]["user_prompt"])
    assert "最近观察" not in str(client.calls[1]["user_prompt"])
    assert "玩家输入：\n左键点击 1 次" in str(client.calls[1]["user_prompt"])
    assert "必须恰好输出一行" in str(
        client.calls[1]["user_prompt"]
    )
    assert "禁止输出【刚刚】" in str(client.calls[1]["user_prompt"])
    assert (output / "observations.md").is_file()
    assert (output / "session.json").is_file()
    session = json.loads((output / "session.json").read_text(encoding="utf-8"))
    assert session["truncated_count"] == 0
    assert session["average_visible_output_tokens"] == 20.0
    assert session["parameters"]["input_context"] is True


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
    payload = json.loads((session / "session.json").read_text(encoding="utf-8"))
    assert payload["parameters"]["input_context"] is False
    assert statuses[-1].summary["input_context"] == "no"  # type: ignore[union-attr]


def test_fast_prompt_contains_two_part_contract_without_concrete_examples() -> None:
    prompt = FAST_PROMPT_PATH.read_text(encoding="utf-8")
    assert "固定先输出【画面】" in prompt
    assert "不超过 25 个汉字" in prompt
    assert "仅当用户消息提供了变化区域信息时，再输出【刚刚】" in prompt
    assert "不超过 40 个汉字" in prompt
    assert "变化区域的报告优先级（高者优先占用字数）" in prompt
    assert "可读出的文字与数字" in prompt
    assert "对象的出现、消失、移动" in prompt
    assert "界面结构与状态图标变化" in prompt
    assert "四到六个字结束" in prompt
    assert "格子编号（形如 r3c5）是系统的定位信息" in prompt
    assert "判断依据是“发生变化的是什么”" in prompt
    assert "是否出现格子编号" in prompt
    assert "读不清必须说“读不清”" in prompt
    assert "悬浮信息框或状态面板是画面主体" in prompt
    assert "场景没有改变时" in prompt and "相似是正常的" in prompt
    assert "只描述所指区域现在是什么" in prompt
    assert "较上一帧" in prompt and "此前" in prompt and "原本" in prompt
    assert "没有变化区域信息时，必须省略【刚刚】" in prompt
    assert "玩家输入" in prompt
    assert "“玩家输入”不能触发这一段" in prompt
    assert "不得复述任何键名、按键次数或时间间隔" in prompt
    assert "自然方位" in prompt
    assert "表明玩家" in prompt
    assert "不得编造" in prompt
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
