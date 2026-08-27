"""Disabled-by-default generic visual observer implementing port v1."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import time
import tomllib
from typing import Literal, Protocol

from fastapi import APIRouter
from PIL import Image

from pet.core.adapter_api import CoreServices, GameStatus, PORT_VERSION
from pet.core.config import AdapterConfig, LlmConfig, resolve_llm_profile
from pet.core.llm import (
    LlmImage,
    LlmResult,
    LlmVisionClientProtocol,
    OpenRouterClient,
)
from pet.core.prompt import PROMPTS_DIRECTORY

logger = logging.getLogger(__name__)

BACKEND_DIRECTORY = Path(__file__).resolve().parents[4]
DEFAULT_TITLE_MAP_PATH = BACKEND_DIRECTORY / "data" / "generic" / "window-title-map.toml"
FAST_PROMPT_PATH = PROMPTS_DIRECTORY / "generic" / "observation-fast.md"
HEARTBEAT_SECONDS = 60.0
FOCUS_REGION_TEMPLATE = (
    "画面{location}、约占屏幕{area:.0f}%的区域正在变化"
    "（区域内像素变化强度约{intensity:.0f}%，全局约{global_change:.1f}%）。"
    "请重点观察该区域及其紧邻环境。"
)
WIDE_CHANGE_TEMPLATE = "本帧变化范围较广（全局像素变化约{global_change:.1f}%），未提供聚焦区域。"
FORCED_TEMPLATE = "本帧为定时快照，此前约 {seconds:.1f} 秒未检测到显著变化。"
MODEL_NO_INPUT_SUMMARY = "此窗口内无玩家输入"
LOG_NO_INPUT_SUMMARY = "无输入"
GRID_COORDINATE_PATTERN = re.compile(r"(?i)r\d+c\d+")
ChangeReason = Literal["sparse", "coarse", "large", "forced"]
CHANGE_REASONS: tuple[ChangeReason, ...] = ("sparse", "coarse", "large", "forced")


class FrameMetadataLike(Protocol):
    window_title: str
    process_name: str
    captured_at: datetime
    monotonic_seconds: float


class CapturedFrameLike(Protocol):
    bitmap: Image.Image
    metadata: FrameMetadataLike


class CaptureBackendLike(Protocol):
    def capture_frame(self) -> CapturedFrameLike | None: ...

    def close(self) -> None: ...


class SelectionDecisionLike(Protocol):
    should_save: bool
    forced: bool
    region_grid: tuple[str, ...]
    confirmed_region_grid: tuple[str, ...]
    changed_block_ratio: float
    baseline_monotonic_seconds: float
    confirmed_region_intensity: float


class FrameMetricsLike(Protocol):
    mean_amplitude: float


class FrameComparisonsLike(Protocol):
    vs_baseline: FrameMetricsLike


class SelectionObservationLike(Protocol):
    decision: SelectionDecisionLike
    comparisons: FrameComparisonsLike


class FrameSelectorLike(Protocol):
    def observe(self, frame: Image.Image, now: float) -> SelectionObservationLike: ...


class InputContextLike(Protocol):
    def summarize_window(
        self,
        start_exclusive: float | None,
        end_inclusive: float,
    ) -> str: ...

    def close(self) -> None: ...


CaptureBackendFactory = Callable[[], CaptureBackendLike]
SelectorFactory = Callable[[float], FrameSelectorLike]
ClientFactory = Callable[[str, str | None, str, float], LlmVisionClientProtocol]
InputListenerFactory = Callable[[CaptureBackendLike], InputContextLike]


@dataclass(frozen=True, slots=True)
class TitleRule:
    game: str
    title_contains: tuple[str, ...]
    process_names: tuple[str, ...]


class WindowTitleMap:
    """Case-insensitive deterministic mapping; no model inference is involved."""

    def __init__(self, rules: Sequence[TitleRule]) -> None:
        self._rules = tuple(rules)

    @classmethod
    def load(cls, path: Path = DEFAULT_TITLE_MAP_PATH) -> WindowTitleMap:
        with path.open("rb") as stream:
            payload = tomllib.load(stream)
        rules = []
        for item in payload.get("games", []):
            rules.append(
                TitleRule(
                    game=str(item["name"]),
                    title_contains=tuple(str(value).casefold() for value in item.get("title_contains", [])),
                    process_names=tuple(str(value).casefold() for value in item.get("process_names", [])),
                )
            )
        return cls(rules)

    def identify(self, title: str, process_name: str) -> str:
        folded_title = title.casefold()
        folded_process = process_name.casefold()
        for rule in self._rules:
            if any(value in folded_title for value in rule.title_contains):
                return rule.game
            if folded_process in rule.process_names:
                return rule.game
        return title


@dataclass(slots=True)
class ObservationRecord:
    seq: int
    frame_ts: float
    wall: str
    game: str
    text: str
    region: tuple[str, ...] | None
    reason: ChangeReason
    change_ratio: float
    global_change: float
    region_area_ratio: float | None
    region_intensity: float | None
    input: str
    focus_location: str | None
    latency_ms: float
    ttft_ms: float | None
    dropped: str | None
    cost_usd: float
    model_called: bool
    visible_output_tokens: int | None
    truncated: bool
    user_prompt: str | None = None

    def json_value(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "frame_ts": self.frame_ts,
            "wall": self.wall,
            "game": self.game,
            "text": self.text,
            "region": list(self.region) if self.region else None,
            "reason": self.reason,
            "change_ratio": round(self.change_ratio, 2),
            "global_change": round(self.global_change, 1),
            "region_area_ratio": (
                round(self.region_area_ratio)
                if self.region_area_ratio is not None
                else None
            ),
            "region_intensity": (
                round(self.region_intensity)
                if self.region_intensity is not None
                else None
            ),
            "input": self.input,
            "latency_ms": round(self.latency_ms, 3),
            "ttft_ms": round(self.ttft_ms, 3) if self.ttft_ms is not None else None,
            "dropped": self.dropped,
            "user_prompt": self.user_prompt,
        }


@dataclass(slots=True)
class PendingFrame:
    seq: int
    frame: CapturedFrameLike
    game: str
    region: tuple[str, ...]
    confirmed_region: tuple[str, ...]
    reason: ChangeReason
    change_ratio: float
    global_change: float
    region_area_ratio: float | None
    region_intensity: float | None
    focus_location: str | None
    baseline_monotonic_seconds: float


def _change_reason(forced: bool, change_ratio: float) -> ChangeReason:
    # A max-silence upload may coincide with a freshly confirmed change.  It is
    # only a heartbeat when the detector found no change; otherwise expose the
    # actual change scale without altering the selector's upload decision.
    if forced and change_ratio == 0.0:
        return "forced"
    if change_ratio <= 0.25:
        return "sparse"
    if change_ratio <= 0.50:
        return "coarse"
    return "large"


def _coarse_location(region: Sequence[str]) -> str:
    return _focus_geometry(region)[0]


def _focus_geometry(region: Sequence[str]) -> tuple[str, float]:
    """Return the neutral nine-grid location and bbox screen percentage."""
    if not region:
        raise ValueError("聚焦区域缺少 confirmed 格子")
    rows: list[int] = []
    columns: list[int] = []
    for cell in region:
        try:
            row_text, column_text = cell.removeprefix("r").split("c", maxsplit=1)
            row = int(row_text)
            column = int(column_text)
        except (ValueError, AttributeError) as error:
            raise ValueError(f"无法解析变化格子：{cell}") from error
        if not 1 <= row <= 16 or not 1 <= column <= 9:
            raise ValueError(f"变化格子越界：{cell}")
        rows.append(row)
        columns.append(column)
    center_row = (min(rows) + max(rows)) / 2.0
    center_column = (min(columns) + max(columns)) / 2.0
    vertical = (
        0
        if center_row <= 16.0 / 3.0
        else (1 if center_row <= 32.0 / 3.0 else 2)
    )
    horizontal = (
        0
        if center_column <= 3.0
        else (1 if center_column <= 6.0 else 2)
    )
    location = (
        ("左上", "上方", "右上"),
        ("左侧", "中央", "右侧"),
        ("左下", "下方", "右下"),
    )[vertical][horizontal]
    bbox_cells = (max(rows) - min(rows) + 1) * (max(columns) - min(columns) + 1)
    return location, bbox_cells / (16 * 9) * 100.0


def _logged_input(input_summary: str | None) -> str:
    if input_summary is None or input_summary.strip() == MODEL_NO_INPUT_SUMMARY:
        return LOG_NO_INPUT_SUMMARY
    return input_summary.strip()


def _model_segment(text: str, label: str) -> str | None:
    match = re.search(
        rf"{re.escape(label)}\s*(.*?)(?=【(?:画面|局部)】|\Z)",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    return " ".join(match.group(1).split())


class ObservationLog:
    """Frame-ordered machine and human logs; screenshots never enter this class."""

    def __init__(
        self,
        root: Path,
        parameters: dict[str, object],
        *,
        exact_directory: bool = False,
    ) -> None:
        if exact_directory:
            self.directory = root
            self.directory.mkdir(parents=True, exist_ok=False)
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            self.directory = root / stamp
            suffix = 1
            while self.directory.exists():
                self.directory = root / f"{stamp}-{suffix}"
                suffix += 1
            self.directory.mkdir(parents=True)
        self.started_at = datetime.now(timezone.utc)
        self._first_frame_ts: float | None = None
        self._jsonl = (self.directory / "observations.jsonl").open("w", encoding="utf-8", newline="\n")
        self._markdown = (self.directory / "observations.md").open("w", encoding="utf-8", newline="\n")
        self._markdown.write(
            "# 通用视觉观察日志\n\n"
            f"本会话始于 {self.started_at.isoformat()}\n\n"
        )
        self._markdown.flush()
        self.parameters = parameters
        self.records = 0
        self.calls = 0
        self.dropped = 0
        self.failures = 0
        self.total_cost_usd = 0.0
        self.visible_output_token_total = 0
        self.visible_output_token_count = 0
        self.truncated = 0
        self.reason_counts: Counter[str] = Counter()
        self._write_session(None)

    def append(self, record: ObservationRecord) -> None:
        self._jsonl.write(json.dumps(record.json_value(), ensure_ascii=False) + "\n")
        self._jsonl.flush()
        if self._first_frame_ts is None:
            self._first_frame_ts = record.frame_ts
        relative_seconds = max(0, round(record.frame_ts - self._first_frame_ts))
        heartbeat = "（心跳）" if record.reason == "forced" else ""
        self._markdown.write(f"T+{relative_seconds}{heartbeat}：\n")
        if record.dropped is not None:
            self._markdown.write(f"[丢弃：{record.dropped}]\n")
        else:
            scene = _model_segment(record.text, "【画面】")
            if scene is None:
                scene = " ".join(record.text.split())
            self._markdown.write(
                f"【全局画面】（全局像素变化{record.global_change:.1f}%）{scene}\n"
            )
            if record.region_area_ratio is not None:
                local = _model_segment(record.text, "【局部】")
                if local is not None:
                    assert record.region_intensity is not None
                    self._markdown.write(
                        f"【局部｜区域占比{record.region_area_ratio:.0f}%】"
                        f"（区域像素变化{record.region_intensity:.0f}%）{local}\n"
                    )
        self._markdown.write(f"【玩家输入】{record.input}\n\n")
        self._markdown.flush()
        self.records += 1
        if record.model_called:
            self.calls += 1
        self.total_cost_usd += record.cost_usd
        if record.dropped is not None:
            self.dropped += 1
        if record.model_called and record.dropped is not None:
            self.failures += 1
        if record.visible_output_tokens is not None:
            self.visible_output_token_total += record.visible_output_tokens
            self.visible_output_token_count += 1
        if record.truncated:
            self.truncated += 1
        self.reason_counts[record.reason] += 1
        self._write_session(None)

    def close(self) -> None:
        self._write_session(datetime.now(timezone.utc))
        self._jsonl.close()
        self._markdown.close()

    def _write_session(self, ended_at: datetime | None) -> None:
        value = {
            "started_at": self.started_at.isoformat(),
            "ended_at": ended_at.isoformat() if ended_at else None,
            "parameters": self.parameters,
            "observation_attempt_count": self.records,
            "call_count": self.calls,
            "dropped_count": self.dropped,
            "failure_count": self.failures,
            "failure_rate": round(self.failures / self.calls, 6) if self.calls else 0.0,
            "total_cost_usd": round(self.total_cost_usd, 9),
            "truncated_count": self.truncated,
            "reason_counts": {
                reason: self.reason_counts[reason] for reason in CHANGE_REASONS
            },
            "average_visible_output_tokens": (
                round(
                    self.visible_output_token_total / self.visible_output_token_count,
                    6,
                )
                if self.visible_output_token_count
                else None
            ),
        }
        (self.directory / "session.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _default_client_factory(
    profile_name: str,
    base_url: str | None,
    api_key_env: str,
    timeout_seconds: float,
) -> LlmVisionClientProtocol:
    return OpenRouterClient.from_profile(
        profile_name=profile_name,
        base_url=base_url,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
    )


class GenericVisionAdapter:
    adapter_id = "generic"
    display_name = "通用视觉"
    port_version = PORT_VERSION
    http_router: APIRouter | None = None

    def __init__(
        self,
        configuration: AdapterConfig,
        llm_configuration: LlmConfig,
        *,
        capture_backend_factory: CaptureBackendFactory,
        selector_factory: SelectorFactory,
        client_factory: ClientFactory = _default_client_factory,
        input_listener_factory: InputListenerFactory | None = None,
        title_map: WindowTitleMap | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._settings = configuration.generic
        self._llm_configuration = llm_configuration
        self._capture_backend_factory = capture_backend_factory
        self._selector_factory = selector_factory
        self._client_factory = client_factory
        self._input_listener_factory = input_listener_factory
        self._title_map = title_map or WindowTitleMap.load()
        self._clock = clock
        self._core: CoreServices | None = None
        self._task: asyncio.Task[None] | None = None
        self._backend: CaptureBackendLike | None = None
        self._selector: FrameSelectorLike | None = None
        self._client: LlmVisionClientProtocol | None = None
        self._log: ObservationLog | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        self._queued_frame: PendingFrame | None = None
        self._pending: dict[int, ObservationRecord] = {}
        self._next_sequence = 1
        self._next_write_sequence = 1
        self._input_context: InputContextLike | None = None
        self._last_dispatched_frame_ts: float | None = None
        self._last_status: tuple[str, str, bool, bool] | None = None
        self._last_status_at = float("-inf")
        self._consecutive_failures = 0
        self._degraded = False
        self._cost_warning = False
        self._current_game = ""
        self._stopping = False

    async def start(self, core: CoreServices) -> None:
        if self._task is not None:
            return
        self._core = core
        if not self._settings.enabled:
            await self._publish_status("disabled", "")
            return

        self._initialize_observer(log_root=None, exact_directory=False)
        self._task = asyncio.create_task(self._run(), name="generic-vision-observer")

    def _initialize_observer(
        self,
        *,
        log_root: Path | None,
        exact_directory: bool,
        extra_parameters: dict[str, object] | None = None,
    ) -> None:
        """Initialize the model and shared frame-to-log path.

        Production and offline replay intentionally enter through this same
        initialization and scheduling implementation. Replay only substitutes
        the read-only source of frames and an exact output directory.
        """
        profile_id = self._settings.llm_profile
        profile = self._llm_configuration.profiles.get(profile_id)
        if profile is None:
            raise RuntimeError(f"通用视觉模型档位不存在：{profile_id}")
        if profile.input_price_per_million_usd is None or profile.output_price_per_million_usd is None:
            raise RuntimeError(f"通用视觉模型档位 {profile_id} 缺少输入或输出单价")
        effective = resolve_llm_profile(self._llm_configuration, profile_id)
        if not effective.enabled or not effective.model.strip():
            raise RuntimeError(f"通用视觉模型档位 {profile_id} 未启用或未配置型号")
        self._effective = effective
        self._input_price = profile.input_price_per_million_usd
        self._output_price = profile.output_price_per_million_usd
        self._client = self._client_factory(
            profile_id,
            effective.base_url,
            effective.api_key_env,
            self._settings.fast_timeout_seconds,
        )
        if log_root is None:
            log_root = Path(self._settings.observation_log_dir)
            if not log_root.is_absolute():
                log_root = BACKEND_DIRECTORY / log_root
        parameters: dict[str, object] = {
            "poll_interval_seconds": self._settings.poll_interval_seconds,
            "send_width": self._settings.send_width,
            "fast_timeout_seconds": self._settings.fast_timeout_seconds,
            "max_inflight": self._settings.max_inflight,
            "input_context": self._settings.input_context,
            "region_focus_max": self._settings.region_focus_max,
            "llm_profile": profile_id,
            "model": effective.model,
            "provider": effective.provider or None,
            "max_tokens": effective.max_tokens,
            "input_price_per_million_usd": self._input_price,
            "output_price_per_million_usd": self._output_price,
        }
        if extra_parameters:
            parameters.update(extra_parameters)
        self._log = ObservationLog(
            log_root,
            parameters,
            exact_directory=exact_directory,
        )

    def start_replay(
        self,
        output_directory: Path,
        *,
        input_context: InputContextLike | None,
        extra_parameters: dict[str, object] | None = None,
    ) -> None:
        """Start the production observation path without a live capture loop."""
        if self._task is not None or self._log is not None:
            raise RuntimeError("通用视觉观察器已经启动")
        if self._settings.input_context and input_context is None:
            raise ValueError("input_context is required when replay input context is enabled")
        self._input_context = input_context if self._settings.input_context else None
        self._initialize_observer(
            log_root=output_directory,
            exact_directory=True,
            extra_parameters=extra_parameters,
        )

    async def submit_replay_frame(
        self,
        frame: CapturedFrameLike,
        game: str,
        region: tuple[str, ...],
        baseline_monotonic_seconds: float,
        *,
        confirmed_region: tuple[str, ...],
        change_ratio: float,
        global_change: float,
        region_intensity: float,
        forced: bool,
    ) -> None:
        """Submit one retained frame with bounded backpressure for offline replay."""
        if self._log is None:
            raise RuntimeError("离线观察器尚未启动")
        await self._schedule(
            frame,
            game,
            region,
            baseline_monotonic_seconds,
            confirmed_region=confirmed_region,
            change_ratio=change_ratio,
            global_change=global_change,
            region_intensity=region_intensity,
            forced=forced,
            wait_for_capacity=True,
        )

    async def finish_replay(self) -> None:
        """Drain all paid calls and close the production-format observation log."""
        while self._inflight or self._queued_frame is not None:
            self._launch_queued_frame()
            if not self._inflight:
                break
            await asyncio.gather(*tuple(self._inflight), return_exceptions=True)
        self._close_input_context()
        close_client = getattr(self._client, "close", None)
        if callable(close_client):
            close_client()
        self._client = None
        if self._log is not None:
            self._log.close()
            self._log = None

    @property
    def total_cost_usd(self) -> float:
        """Return the flushed configured-price total for status and cost guards."""
        return self._log.total_cost_usd if self._log is not None else 0.0

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._close_backend()
        if self._queued_frame is not None:
            queued = self._queued_frame
            self._queued_frame = None
            queued.frame.bitmap.close()
            self._complete_record(
                self._dropped_record(queued, "error:stopped")
            )
        for task in tuple(self._inflight):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        close_client = getattr(self._client, "close", None)
        if callable(close_client):
            close_client()
        self._client = None
        if self._log is not None:
            self._log.close()
            self._log = None

    async def _run(self) -> None:
        try:
            while True:
                started = self._clock()
                await self._poll_once()
                delay = max(0.0, self._settings.poll_interval_seconds - (self._clock() - started))
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    async def _poll_once(self) -> None:
        if self._backend is None:
            try:
                self._backend = self._capture_backend_factory()
                self._selector = self._selector_factory(self._settings.region_focus_max)
                if self._settings.input_context:
                    if self._input_listener_factory is None:
                        raise RuntimeError("通用视觉输入上下文缺少生产监听器工厂")
                    self._input_context = self._input_listener_factory(self._backend)
            except Exception as error:
                logger.warning("通用视觉未找到可捕获的前台窗口：%s", error)
                self._close_backend()
                await self._publish_status("no_window", "")
                return
        assert self._selector is not None
        try:
            frame = await asyncio.to_thread(self._backend.capture_frame)
        except Exception as error:
            logger.warning("通用视觉捕获失败，将重新选择前台窗口：%s", error)
            self._close_backend()
            await self._publish_status("no_window", "")
            return
        if frame is None:
            await self._publish_status("watching", self._current_game)
            return

        game = self._title_map.identify(frame.metadata.window_title, frame.metadata.process_name)
        self._current_game = game
        observation = self._selector.observe(frame.bitmap, frame.metadata.monotonic_seconds)
        if observation.decision.should_save:
            await self._schedule(
                frame,
                game,
                observation.decision.region_grid,
                observation.decision.baseline_monotonic_seconds,
                confirmed_region=observation.decision.confirmed_region_grid,
                change_ratio=observation.decision.changed_block_ratio,
                global_change=observation.comparisons.vs_baseline.mean_amplitude * 100.0,
                region_intensity=observation.decision.confirmed_region_intensity * 100.0,
                forced=observation.decision.forced,
            )
        else:
            frame.bitmap.close()
        await self._publish_status("watching", game)

    async def _schedule(
        self,
        frame: CapturedFrameLike,
        game: str,
        region: tuple[str, ...],
        baseline_monotonic_seconds: float,
        *,
        confirmed_region: tuple[str, ...],
        change_ratio: float,
        global_change: float,
        region_intensity: float,
        forced: bool,
        wait_for_capacity: bool = False,
    ) -> None:
        sequence = self._next_sequence
        self._next_sequence += 1
        if not 0.0 <= change_ratio <= 1.0:
            raise ValueError("change_ratio 必须在 0–1")
        if not 0.0 <= global_change <= 100.0:
            raise ValueError("global_change 必须在 0–100")
        if not 0.0 <= region_intensity <= 100.0:
            raise ValueError("region_intensity 必须在 0–100")
        reason = _change_reason(forced, change_ratio)
        has_focus = (
            reason != "forced"
            and bool(confirmed_region)
            and change_ratio <= self._settings.region_focus_max
        )
        focus_location: str | None = None
        region_area_ratio: float | None = None
        focused_intensity: float | None = None
        if has_focus:
            focus_location, region_area_ratio = _focus_geometry(confirmed_region)
            focused_intensity = region_intensity
        pending = PendingFrame(
            sequence,
            frame,
            game,
            region,
            confirmed_region,
            reason,
            change_ratio,
            global_change,
            region_area_ratio,
            focused_intensity,
            focus_location,
            baseline_monotonic_seconds,
        )
        while wait_for_capacity and len(self._inflight) >= self._settings.max_inflight:
            done, _ = await asyncio.wait(
                tuple(self._inflight),
                return_when=asyncio.FIRST_COMPLETED,
            )
            self._inflight.difference_update(done)
        if len(self._inflight) >= self._settings.max_inflight:
            replaced = self._queued_frame
            self._queued_frame = pending
            if replaced is not None:
                replaced.frame.bitmap.close()
                self._complete_record(self._dropped_record(replaced, "superseded"))
            return
        self._launch_frame(pending)

    def _launch_frame(self, pending: PendingFrame) -> None:
        frame_ts = pending.frame.metadata.monotonic_seconds
        input_summary: str | None = None
        if self._settings.input_context:
            if self._input_context is None:
                raise RuntimeError("输入上下文已启用但监听器未初始化")
            input_summary = self._input_context.summarize_window(
                self._last_dispatched_frame_ts,
                frame_ts,
            )
            self._last_dispatched_frame_ts = frame_ts
        logged_input = _logged_input(input_summary)
        baseline_seconds_ago = frame_ts - pending.baseline_monotonic_seconds
        if baseline_seconds_ago < -1e-6:
            raise ValueError("变化基线时间晚于当前帧")
        baseline_seconds_ago = max(0.0, baseline_seconds_ago)
        user_prompt = _user_prompt(
            pending.game,
            input_summary,
            reason=pending.reason,
            global_change=pending.global_change,
            region_area_ratio=pending.region_area_ratio,
            region_intensity=pending.region_intensity,
            focus_location=pending.focus_location,
            baseline_seconds_ago=baseline_seconds_ago,
        )
        if GRID_COORDINATE_PATTERN.search(user_prompt):
            raise AssertionError("模型消息不得包含格子坐标")
        task = asyncio.create_task(
            self._observe_frame(
                pending.seq,
                pending.frame,
                pending.game,
                pending.region,
                pending.reason,
                pending.change_ratio,
                pending.global_change,
                pending.region_area_ratio,
                pending.region_intensity,
                logged_input,
                pending.focus_location,
                user_prompt,
            ),
            name=f"generic-vision-call-{pending.seq}",
        )
        self._inflight.add(task)
        task.add_done_callback(self._observation_done)

    def _observation_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        self._launch_queued_frame()

    def _launch_queued_frame(self) -> None:
        if (
            self._stopping
            or self._queued_frame is None
            or len(self._inflight) >= self._settings.max_inflight
        ):
            return
        pending = self._queued_frame
        self._queued_frame = None
        self._launch_frame(pending)

    @staticmethod
    def _dropped_record(pending: PendingFrame, reason: str) -> ObservationRecord:
        return ObservationRecord(
            pending.seq,
            pending.frame.metadata.monotonic_seconds,
            pending.frame.metadata.captured_at.astimezone(timezone.utc).isoformat(),
            pending.game,
            "",
            pending.region or None,
            pending.reason,
            pending.change_ratio,
            pending.global_change,
            pending.region_area_ratio,
            pending.region_intensity,
            LOG_NO_INPUT_SUMMARY,
            pending.focus_location,
            0.0,
            None,
            reason,
            0.0,
            False,
            None,
            False,
        )

    async def _observe_frame(
        self,
        sequence: int,
        frame: CapturedFrameLike,
        game: str,
        region: tuple[str, ...],
        reason: ChangeReason,
        change_ratio: float,
        global_change: float,
        region_area_ratio: float | None,
        region_intensity: float | None,
        logged_input: str,
        focus_location: str | None,
        user_prompt: str,
    ) -> None:
        started = self._clock()
        dropped: str | None = None
        text = ""
        cost = 0.0
        visible_output_tokens: int | None = None
        ttft_ms: float | None = None
        truncated = False
        try:
            assert self._client is not None
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.complete_with_images_stream,
                    model=self._effective.model,
                    provider=self._effective.provider or None,
                    system_prompt=FAST_PROMPT_PATH.read_text(encoding="utf-8").strip(),
                    user_prompt=user_prompt,
                    images=(
                        LlmImage(
                            frame.bitmap,
                            "当前画面",
                            target_width=self._settings.send_width,
                            encoding="jpeg",
                        ),
                    ),
                    max_image_edge=None,
                    max_tokens=self._effective.max_tokens,
                    temperature=self._effective.temperature,
                    reasoning_enabled=False,
                ),
                timeout=self._settings.fast_timeout_seconds,
            )
            text = result.text.strip()
            if not text:
                raise RuntimeError("模型返回空观察")
            cost = self._price(result)
            visible_output_tokens = _visible_output_tokens(result)
            ttft_ms = (
                result.ttft_seconds * 1000.0
                if result.ttft_seconds is not None
                else None
            )
            truncated = result.finish_reason in {"length", "max_tokens"} or (
                visible_output_tokens is not None
                and visible_output_tokens >= self._effective.max_tokens
            )
            self._consecutive_failures = 0
        except asyncio.TimeoutError:
            dropped = "timeout"
            self._register_failure("timeout")
        except asyncio.CancelledError:
            dropped = "error:stopped"
            raise
        except Exception as error:
            dropped = f"error:{_one_line(error)}"
            self._register_failure(str(error))
        finally:
            frame.bitmap.close()

        self._complete_record(
            ObservationRecord(
                sequence,
                frame.metadata.monotonic_seconds,
                frame.metadata.captured_at.astimezone(timezone.utc).isoformat(),
                game,
                text,
                region or None,
                reason,
                change_ratio,
                global_change,
                region_area_ratio,
                region_intensity,
                logged_input,
                focus_location,
                (self._clock() - started) * 1000.0,
                ttft_ms,
                dropped,
                cost,
                True,
                visible_output_tokens,
                truncated,
                user_prompt,
            )
        )

    def _complete_record(self, record: ObservationRecord) -> None:
        self._pending[record.seq] = record
        while self._next_write_sequence in self._pending:
            current = self._pending.pop(self._next_write_sequence)
            assert self._log is not None
            self._log.append(current)
            self._next_write_sequence += 1
        self._refresh_cost_warning()

    def _price(self, result: LlmResult) -> float:
        prompt = result.usage.prompt_tokens or 0
        completion = result.usage.completion_tokens or 0
        return (prompt * self._input_price + completion * self._output_price) / 1_000_000.0

    def _refresh_cost_warning(self) -> None:
        if self._log is None:
            return
        elapsed = max(0.001, (datetime.now(timezone.utc) - self._log.started_at).total_seconds())
        hourly = self._log.total_cost_usd * 3600.0 / elapsed
        warning = hourly > self._settings.cost_warn_per_hour
        if warning and not self._cost_warning:
            logger.warning(
                "通用视觉按当前会话折算花费 %.3f 美元/小时，超过配置警戒线 %.3f；仅提示，不熔断",
                hourly,
                self._settings.cost_warn_per_hour,
            )
        self._cost_warning = warning

    def _register_failure(self, detail: str) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= 10 and not self._degraded:
            self._degraded = True
            logger.error("通用视觉连续 10 次调用失败，继续观看但已降级：%s", detail)

    async def _publish_status(self, state: str, game: str) -> None:
        now = self._clock()
        key = (state, game, self._degraded, self._cost_warning)
        if key == self._last_status and now - self._last_status_at < HEARTBEAT_SECONDS:
            return
        self._last_status = key
        self._last_status_at = now
        cost = self._log.total_cost_usd if self._log is not None else 0.0
        assert self._core is not None
        await self._core.publish_status(
            GameStatus(
                game_id=self.adapter_id,
                state=state,
                summary={
                    "game": game or None,
                    "input_context": "yes" if self._settings.input_context else "no",
                    "session_cost_usd": f"{cost:.6f}",
                    "cost_warning": "yes" if self._cost_warning else "no",
                    "degraded": "yes" if self._degraded else "no",
                },
            )
        )

    def _close_backend(self) -> None:
        try:
            self._close_input_context()
        finally:
            if self._backend is not None:
                self._backend.close()
                self._backend = None
                self._selector = None

    def _close_input_context(self) -> None:
        if self._input_context is not None:
            context = self._input_context
            self._input_context = None
            context.close()


def _user_prompt(
    game: str,
    input_summary: str | None,
    *,
    reason: ChangeReason,
    global_change: float,
    region_area_ratio: float | None,
    region_intensity: float | None,
    focus_location: str | None,
    baseline_seconds_ago: float,
) -> str:
    lines = [
        "请观察当前这一张完整游戏画面，并遵守系统提示。",
        f"已知游戏：{game}",
    ]
    if input_summary is not None:
        lines.extend(("玩家输入：", input_summary))
    if region_area_ratio is not None:
        if focus_location is None or region_intensity is None:
            raise ValueError("聚焦区域提示缺少机械方位或强度")
        lines.append(
            FOCUS_REGION_TEMPLATE.format(
                location=focus_location,
                area=region_area_ratio,
                intensity=region_intensity,
                global_change=global_change,
            )
        )
        lines.append(
            "本条提供了聚焦区域；输出【画面】和【局部】两段，每段一到两句。"
            "【局部】要说明该区域及紧邻环境正在发生什么，并把局部所见放进环境里说清它是什么。"
            "若发生变化的只是亮度、颜色、光效或粒子，而对象本身没有出现、消失或移动，"
            "【局部】标签后必须以“仅”开头且总长四到六个字。"
            f"{region_area_ratio:.0f}%、{region_intensity:.0f}%、{global_change:.1f}%"
            "是系统定位数值，严禁在输出中写出或改写这三个数；日志会机械添加。"
            "不得复述游戏名。"
        )
    elif reason == "forced":
        lines.append(FORCED_TEMPLATE.format(seconds=baseline_seconds_ago))
        lines.append(
            "本条是定时心跳快照；只输出【画面】，用一到两句描述当前场景与正在发生的事；"
            "不要输出【局部】，不得复述游戏名。"
        )
    else:
        lines.append(WIDE_CHANGE_TEMPLATE.format(global_change=global_change))
        lines.append(
            "本条没有聚焦区域；只输出【画面】，用一到两句按当前新场景完整定场；"
            f"不要输出【局部】；{global_change:.1f}%是系统定位数值，严禁在输出中写出或改写，"
            "日志会机械添加；不得复述游戏名。"
        )
    return "\n".join(lines)


def _one_line(error: object) -> str:
    return " ".join(str(error).split())[:240]


def _visible_output_tokens(result: LlmResult) -> int | None:
    completion = result.usage.completion_tokens
    if completion is None:
        return None
    return max(completion - (result.usage.reasoning_tokens or 0), 0)


def create_adapter(
    configuration: AdapterConfig,
    llm_configuration: LlmConfig,
    *,
    capture_backend_factory: CaptureBackendFactory,
    selector_factory: SelectorFactory,
    input_listener_factory: InputListenerFactory,
) -> GenericVisionAdapter:
    return GenericVisionAdapter(
        configuration,
        llm_configuration,
        capture_backend_factory=capture_backend_factory,
        selector_factory=selector_factory,
        input_listener_factory=input_listener_factory,
    )
