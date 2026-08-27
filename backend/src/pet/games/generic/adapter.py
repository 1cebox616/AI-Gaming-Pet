"""Disabled-by-default generic visual observer implementing port v1."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
import tomllib
from typing import Protocol

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
REGION_TEMPLATE = (
    "画面被划分为 16 行 9 列的网格。与 {seconds:.1f} 秒前的变化基线相比，"
    "以下格子发生了变化：{cells}。"
)


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
    region_grid: tuple[str, ...]
    baseline_monotonic_seconds: float


class SelectionObservationLike(Protocol):
    decision: SelectionDecisionLike


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
    latency_ms: float
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
            "latency_ms": round(self.latency_ms, 3),
            "dropped": self.dropped,
            "user_prompt": self.user_prompt,
        }


@dataclass(slots=True)
class PendingFrame:
    seq: int
    frame: CapturedFrameLike
    game: str
    region: tuple[str, ...]
    baseline_monotonic_seconds: float


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
        self._jsonl = (self.directory / "observations.jsonl").open("w", encoding="utf-8", newline="\n")
        self._markdown = (self.directory / "observations.md").open("w", encoding="utf-8", newline="\n")
        self._markdown.write("# 通用视觉观察日志\n\n")
        self._markdown.flush()
        self.started_at = datetime.now(timezone.utc)
        self.parameters = parameters
        self.records = 0
        self.calls = 0
        self.dropped = 0
        self.failures = 0
        self.total_cost_usd = 0.0
        self.visible_output_token_total = 0
        self.visible_output_token_count = 0
        self.truncated = 0
        self._write_session(None)

    def append(self, record: ObservationRecord) -> None:
        self._jsonl.write(json.dumps(record.json_value(), ensure_ascii=False) + "\n")
        self._jsonl.flush()
        if record.dropped is None:
            self._markdown.write(f"- {record.wall} · {record.game}：{record.text}\n")
        else:
            self._markdown.write(f"- {record.wall} · {record.game}：[丢弃：{record.dropped}]\n")
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
            "region_sparsity_max": self._settings.region_sparsity_max,
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
    ) -> None:
        """Submit one retained frame with bounded backpressure for offline replay."""
        if self._log is None:
            raise RuntimeError("离线观察器尚未启动")
        await self._schedule(
            frame,
            game,
            region,
            baseline_monotonic_seconds,
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
                self._selector = self._selector_factory(self._settings.region_sparsity_max)
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
        wait_for_capacity: bool = False,
    ) -> None:
        sequence = self._next_sequence
        self._next_sequence += 1
        pending = PendingFrame(
            sequence,
            frame,
            game,
            region,
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
        baseline_seconds_ago: float | None = None
        if pending.region:
            baseline_seconds_ago = frame_ts - pending.baseline_monotonic_seconds
            if baseline_seconds_ago < -1e-6:
                raise ValueError("变化基线时间晚于当前帧")
            baseline_seconds_ago = max(0.0, baseline_seconds_ago)
        user_prompt = _user_prompt(
            pending.game,
            input_summary,
            pending.region,
            baseline_seconds_ago=baseline_seconds_ago,
        )
        task = asyncio.create_task(
            self._observe_frame(
                pending.seq,
                pending.frame,
                pending.game,
                pending.region,
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
            0.0,
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
        user_prompt: str,
    ) -> None:
        started = self._clock()
        dropped: str | None = None
        text = ""
        cost = 0.0
        visible_output_tokens: int | None = None
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
                (self._clock() - started) * 1000.0,
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
    region: Sequence[str],
    *,
    baseline_seconds_ago: float | None,
) -> str:
    lines = [
        "请观察当前这一张完整游戏画面，并遵守系统提示。",
        f"已知游戏：{game}",
    ]
    if input_summary is not None:
        lines.extend(("玩家输入：", input_summary))
    if region:
        if baseline_seconds_ago is None:
            raise ValueError("区域提示缺少变化基线秒差")
        lines.append(
            REGION_TEMPLATE.format(
                seconds=baseline_seconds_ago,
                cells="、".join(region),
            )
        )
        lines.append(
            "本条提供了变化区域信息；必须恰好输出两行：第一行以【画面】开头，"
            "标签后以15个汉字为目标、25个为硬上限，只保留主体和关键状态；"
            "第二行以【刚刚】开头，标签后以30个汉字为目标、40个为硬上限。"
            "若只有纯光照或特效，标签后必须以“仅”开头且总长四到六个字。"
            "两行都不得复述游戏名。"
        )
    else:
        lines.append(
            "本条没有提供变化区域信息；必须恰好输出一行，以【画面】开头，"
            "标签后以15个汉字为目标、25个为硬上限，只保留主体和关键状态；"
            "禁止输出【刚刚】，不得复述游戏名。"
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
