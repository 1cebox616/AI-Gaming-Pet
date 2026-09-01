"""Disabled-by-default generic visual observer implementing port v1."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import functools
import json
import logging
from pathlib import Path
import re
import time
import tomllib
import uuid
from typing import Literal, Protocol

import numpy as np
from fastapi import APIRouter
from PIL import Image

from pet.core.adapter_api import CoreServices, GameStatus, PORT_VERSION
from pet.core.belief import (
    ChangeReason,
    EvidenceEvent,
    EvidenceOutcome,
    EvidenceStore,
    FastObservationPayload,
    FrameMetricsPayload,
    GameKnowledgePayload,
    KeyWindowPayload,
    MouseMotionPayload,
    OcrFramePayload,
    ObservationsMarkdownWriter,
    SceneFingerprintPayload,
    SceneVerifiedPayload,
    Scope,
    TextObservedPayload,
)
from pet.core.input_telemetry import ActionInputWindow, MouseMotionAggregate
from pet.core.config import AdapterConfig, LlmConfig, resolve_llm_profile
from pet.core.gamecard import (
    GameCard,
    GameCardRepository,
    GameCardSession,
    GameKnowledgeWriteAction,
    SceneCardVerification,
    slugify_game_id,
)
from pet.core.llm import (
    LlmCooldownError,
    LlmDispatchStats,
    LlmError,
    LlmImage,
    LlmResult,
    LlmVisionClientProtocol,
    OpenRouterClient,
)
from pet.core.prompt import PROMPTS_DIRECTORY
from pet.core.ocr_probe import OcrFrameResult, OcrLine
from pet.core.ocr_rapid import ENGINE_NAME, RapidOcrEngine, set_current_thread_below_normal
from pet.core.ocr_selective import OcrEngine, TextLineCache, normalize_text
from pet.core.scene_fingerprint import (
    SceneCluster,
    SceneClusterer,
    SceneFingerprintMatch,
    perceptual_hash,
)
from pet.games.generic.deep_read import DeepReadResult, DeepVisionReader
from pet.games.generic.game_knowledge import (
    GAME_KNOWLEDGE_MODE,
    GameKnowledgeCallResult,
    GameKnowledgeClientProtocol,
    GameKnowledgeReader,
)
from pet.games.generic.scene_naming import (
    ExistingSceneNaming,
    SceneNamingFrame,
    build_scene_naming_request,
    parse_scene_naming_proposal,
    validate_scene_naming_proposal,
)

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
    def summarize_window_result(
        self,
        start_exclusive: float | None,
        end_inclusive: float,
    ) -> ActionInputWindow: ...

    def close(self) -> None: ...


CaptureBackendFactory = Callable[[], CaptureBackendLike]
SelectorFactory = Callable[[float], FrameSelectorLike]
ClientFactory = Callable[[str, str | None, str, float], LlmVisionClientProtocol]
KnowledgeClientFactory = Callable[
    [str, str | None, str, float],
    GameKnowledgeClientProtocol,
]
InputListenerFactory = Callable[[CaptureBackendLike], InputContextLike]


@dataclass(frozen=True, slots=True)
class TitleRule:
    game: str
    title_contains: tuple[str, ...]
    process_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GameIdentity:
    game_id: str
    display_name: str
    context_name: str


@dataclass(frozen=True, slots=True)
class SceneFrameState:
    fingerprint: str
    match: SceneFingerprintMatch
    elapsed_ms: float


@dataclass(slots=True)
class BufferedSceneFrame:
    root_capture_id: str
    image: Image.Image
    frame_ts: float
    fingerprint_evidence_id: str


@dataclass(frozen=True, slots=True)
class SceneNamingContext:
    game_name: str
    cluster: SceneCluster
    session: GameCardSession
    frames: tuple[BufferedSceneFrame, ...]
    trigger_frame_ts: float
    existing_scene: ExistingSceneNaming | None = None


@dataclass(frozen=True, slots=True)
class GameKnowledgeContext:
    game_id: str
    game_name: str
    card: GameCard
    trigger_monotonic: float


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
        return self.identify_identity(title, process_name).context_name

    def identify_identity(self, title: str, process_name: str) -> GameIdentity:
        folded_title = title.casefold()
        folded_process = process_name.casefold()
        for rule in self._rules:
            if any(value in folded_title for value in rule.title_contains):
                return GameIdentity(
                    game_id=slugify_game_id(rule.game, title),
                    display_name=title,
                    context_name=rule.game,
                )
            if folded_process in rule.process_names:
                return GameIdentity(
                    game_id=slugify_game_id(rule.game, title),
                    display_name=title,
                    context_name=rule.game,
                )
        return GameIdentity(
            game_id=slugify_game_id(process_name, title),
            display_name=title,
            context_name=title,
        )


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
    scope: Scope | None
    latency_ms: float
    ttft_ms: float | None
    dropped: str | None
    cost_usd: float
    model_called: bool
    visible_output_tokens: int | None
    truncated: bool
    user_prompt: str | None = None
    speculation: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    actual_model: str | None = None
    actual_provider: str | None = None
    error_metadata: dict[str, object] | None = None
    learned_at: float = 0.0


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
    scope: Scope | None
    scheduled_at_clock: float
    key_window_recorded: bool = False


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


def _focus_scope(region: Sequence[str]) -> Scope:
    """Convert the detector's 16-row by 9-column cells to normalized x/y bounds."""
    location, area_ratio = _focus_geometry(region)
    rows: list[int] = []
    columns: list[int] = []
    for cell in region:
        row_text, column_text = cell.removeprefix("r").split("c", maxsplit=1)
        rows.append(int(row_text))
        columns.append(int(column_text))
    return Scope(
        cells=list(region),
        grid=(16, 9),
        bbox=(
            (min(columns) - 1) / 9.0,
            (min(rows) - 1) / 16.0,
            max(columns) / 9.0,
            max(rows) / 16.0,
        ),
        location=location,
        area_ratio=area_ratio,
    )


def _logged_input(input_summary: str | None) -> str:
    if input_summary is None or input_summary.strip() == MODEL_NO_INPUT_SUMMARY:
        return LOG_NO_INPUT_SUMMARY
    return input_summary.strip()


def _model_segment(text: str, label: str) -> str | None:
    match = re.search(
        rf"{re.escape(label)}\s*(.*?)(?=【(?:画面|局部|推测)】|\Z)",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    return " ".join(match.group(1).split())


def _fast_outcome(drop_reason: str | None) -> EvidenceOutcome:
    if drop_reason is None:
        return "ok"
    if drop_reason == "superseded":
        return "superseded"
    if (
        drop_reason in {"timeout", "error:stopped"}
        or "429" in drop_reason
        or drop_reason.startswith("cooldown:")
    ):
        return "dropped"
    return "failed"


def _representative_scene_frames(
    frames: Sequence[BufferedSceneFrame],
    limit: int,
) -> tuple[BufferedSceneFrame, ...]:
    if not frames or limit < 1:
        raise ValueError("representative scene selection needs frames and a positive limit")
    if len(frames) <= limit:
        return tuple(frames)
    if limit == 1:
        return (frames[len(frames) // 2],)
    if limit == 2:
        return (frames[0], frames[-1])
    return (frames[0], frames[len(frames) // 2], frames[-1])


class ObservationLog:
    """Session summary around append-only evidence and its regenerable view."""

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
        self._evidence = EvidenceStore.open(self.directory)
        self._markdown = ObservationsMarkdownWriter(
            self.directory / "observations.md",
            self.started_at,
        )
        self.parameters = parameters
        self.records = 0
        self.calls = 0
        self.dropped = 0
        self.failures = 0
        self.total_cost_usd = 0.0
        self.deep_calls = 0
        self.deep_failures = 0
        self.deep_total_cost_usd = 0.0
        self.game_knowledge_attempts = 0
        self.game_knowledge_total_cost_usd = 0.0
        self.game_knowledge_outcomes: Counter[str] = Counter()
        self.llm_errors: list[dict[str, object]] = []
        self.dispatch_profiles: dict[str, dict[str, object]] = {}
        self.visible_output_token_total = 0
        self.visible_output_token_count = 0
        self.truncated = 0
        self.reason_counts: Counter[str] = Counter()
        self._write_session(None)

    def append_frame_metrics(
        self,
        *,
        sequence: int,
        frame_ts: float,
        wall: str,
        reason: ChangeReason,
        change_ratio: float,
        global_change: float,
        region_area_ratio: float | None,
        region_intensity: float | None,
        scope: Scope | None,
    ) -> None:
        observed_at = self._relative(frame_ts)
        root_capture_id = f"f{sequence}"
        event = EvidenceEvent(
            evidence_id=self._evidence.new_evidence_id(root_capture_id, "detector"),
            source="detector",
            kind="frame_metrics",
            root_capture_id=root_capture_id,
            observed_at=observed_at,
            learned_at=observed_at,
            scope=scope,
            payload=FrameMetricsPayload(
                reason=reason,
                change_ratio=change_ratio,
                global_change=global_change,
                region_area_ratio=region_area_ratio,
                region_intensity=region_intensity,
                heartbeat=reason == "forced",
                wall=wall,
            ),
            derived_from=[],
            context_version=None,
            outcome="ok",
        )
        self._append_event(event)
        self.reason_counts[reason] += 1
        self._write_session(None)

    def append_key_window(
        self,
        *,
        sequence: int,
        frame_ts: float,
        summary: str,
        window_start: float | None,
    ) -> None:
        observed_at = self._relative(frame_ts)
        relative_start = (
            max(0.0, window_start - self._origin())
            if window_start is not None
            else None
        )
        root_capture_id = f"f{sequence}"
        event = EvidenceEvent(
            evidence_id=self._evidence.new_evidence_id(root_capture_id, "input"),
            source="input",
            kind="key_window",
            root_capture_id=root_capture_id,
            observed_at=observed_at,
            learned_at=observed_at,
            scope=None,
            payload=KeyWindowPayload(
                summary=summary,
                window_start=relative_start,
                window_end=observed_at,
            ),
            derived_from=[],
            context_version=None,
            outcome="ok",
        )
        self._append_event(event)

    def append_mouse_motion(
        self,
        motion: MouseMotionAggregate,
        *,
        learned_ts: float,
    ) -> str:
        observed_at = self._relative(motion.window_end)
        learned_at = max(observed_at, self._relative(learned_ts))
        relative_start = (
            max(0.0, motion.window_start - self._origin())
            if motion.window_start is not None
            else None
        )
        evidence_id = self._evidence.new_evidence_id(None, "mouse")
        self._append_event(
            EvidenceEvent(
                evidence_id=evidence_id,
                source="mouse",
                kind="mouse_motion",
                root_capture_id=None,
                observed_at=observed_at,
                learned_at=learned_at,
                scope=None,
                payload=MouseMotionPayload(
                    window_start=relative_start,
                    window_end=observed_at,
                    direction=motion.direction,
                    magnitude=motion.magnitude,
                    raw_count_total=motion.raw_count_total,
                    estimated_degrees=motion.estimated_degrees,
                ),
                derived_from=[],
                context_version=None,
                outcome="ok",
            )
        )
        return evidence_id

    def append(self, record: ObservationRecord) -> None:
        observed_at = self._relative(record.frame_ts)
        learned_at = max(observed_at, record.learned_at - self._origin())
        root_capture_id = f"f{record.seq}"
        outcome = _fast_outcome(record.dropped)
        text = record.text if outcome == "ok" else ""
        event = EvidenceEvent(
            evidence_id=self._evidence.new_evidence_id(root_capture_id, "fast"),
            source="fast",
            kind="fast_observation",
            root_capture_id=root_capture_id,
            observed_at=observed_at,
            learned_at=learned_at,
            scope=record.scope,
            payload=FastObservationPayload(
                text=text,
                scene=_model_segment(text, "【画面】"),
                local=_model_segment(text, "【局部】"),
                speculation=(
                    record.speculation or _model_segment(text, "【推测】")
                    if outcome == "ok"
                    else None
                ),
                game=record.game,
                latency_ms=record.latency_ms,
                ttft_ms=record.ttft_ms,
                visible_output_tokens=record.visible_output_tokens,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                truncated=record.truncated,
                cost_usd=record.cost_usd,
                actual_model=record.actual_model,
                actual_provider=record.actual_provider,
                user_prompt=record.user_prompt,
                drop_reason=record.dropped,
            ),
            derived_from=[],
            context_version=None,
            outcome=outcome,
        )
        self._append_event(event)
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
        if record.error_metadata is not None:
            self.record_llm_error(record.error_metadata, phase="fast", write=False)
        self._write_session(None)

    def append_ocr_frame(
        self,
        *,
        sequence: int,
        frame_ts: float,
        learned_ts: float,
        engine: str,
        num_threads: int,
        det_limit_side_len: int,
        recognized_line_count: int,
        elapsed_ms: float,
        det_ms: float | None,
        rec_ms: float | None,
        cpu_core_seconds: float | None,
        trigger: str,
        outcome_detail: str,
    ) -> None:
        observed_at = self._relative(frame_ts)
        learned_at = max(observed_at, self._relative(learned_ts))
        root_capture_id = f"f{sequence}"
        self._append_event(
            EvidenceEvent(
                evidence_id=self._evidence.new_evidence_id(root_capture_id, "ocr"),
                source="ocr",
                kind="ocr_frame",
                root_capture_id=root_capture_id,
                observed_at=observed_at,
                learned_at=learned_at,
                scope=None,
                payload=OcrFramePayload(
                    engine=engine,
                    num_threads=num_threads,
                    det_limit_side_len=det_limit_side_len,
                    recognized_line_count=recognized_line_count,
                    elapsed_ms=elapsed_ms,
                    det_ms=det_ms,
                    rec_ms=rec_ms,
                    cpu_core_seconds=cpu_core_seconds,
                    trigger=trigger,
                    outcome_detail=outcome_detail,
                ),
                derived_from=[],
                context_version=None,
                outcome="ok",
            )
        )

    def append_text_observed(
        self,
        *,
        sequence: int,
        frame_ts: float,
        learned_ts: float,
        line: OcrLine,
        change: str,
        previous_text: str | None,
        streak: int,
        engine: str,
    ) -> None:
        text = normalize_text(line.text)
        if not text or line.width <= 0.0 or line.height <= 0.0:
            return
        observed_at = self._relative(frame_ts)
        learned_at = max(observed_at, self._relative(learned_ts))
        root_capture_id = f"f{sequence}"
        self._append_event(
            EvidenceEvent(
                evidence_id=self._evidence.new_evidence_id(root_capture_id, "ocr"),
                source="ocr",
                kind="text_observed",
                root_capture_id=root_capture_id,
                observed_at=observed_at,
                learned_at=learned_at,
                scope=None,
                payload=TextObservedPayload(
                    text=text,
                    bbox=(line.x, line.y, line.x + line.width, line.y + line.height),
                    quad=line.quad,
                    change=change,
                    previous_text=(
                        normalize_text(previous_text)
                        if previous_text is not None
                        else None
                    ),
                    streak=streak,
                    engine=engine,
                    engine_confidence=line.confidence,
                ),
                derived_from=[],
                context_version=None,
                outcome="ok",
            )
        )

    def append_scene_fingerprint(
        self,
        *,
        sequence: int,
        frame_ts: float,
        fingerprint: str,
        cluster_id: int,
        distance: int,
        is_new_cluster: bool,
        switched_from: int | None,
        stable: bool,
        card_candidate_scene_id: str | None,
        card_candidate_distance: int | None,
        elapsed_ms: float,
    ) -> str:
        observed_at = self._relative(frame_ts)
        root_capture_id = f"f{sequence}"
        evidence_id = self._evidence.new_evidence_id(root_capture_id, "scene")
        self._append_event(
            EvidenceEvent(
                evidence_id=evidence_id,
                source="scene",
                kind="scene_fingerprint",
                root_capture_id=root_capture_id,
                observed_at=observed_at,
                learned_at=observed_at,
                scope=None,
                payload=SceneFingerprintPayload(
                    hash=fingerprint,
                    cluster_id=cluster_id,
                    distance=distance,
                    is_new_cluster=is_new_cluster,
                    switched_from=switched_from,
                    stable=stable,
                    card_candidate_scene_id=card_candidate_scene_id,
                    card_candidate_distance=card_candidate_distance,
                    elapsed_ms=elapsed_ms,
                ),
                derived_from=[],
                context_version=None,
                outcome="ok",
            )
        )
        return evidence_id

    def append_scene_verified(
        self,
        *,
        trigger_frame_ts: float,
        learned_ts: float,
        session_cluster_id: int,
        label: str,
        annotation: str,
        modality: str,
        matches_existing: bool | None,
        candidate_scene_id: str | None,
        candidate_label: str | None,
        action: Literal["new", "reused", "replaced", "rejected"],
        root_capture_ids: Sequence[str],
        fingerprint_evidence_ids: Sequence[str],
        result: DeepReadResult,
        validation_error: str | None,
    ) -> str:
        if not root_capture_ids:
            raise ValueError("scene verification requires viewed frame roots")
        observed_at = self._relative(trigger_frame_ts)
        learned_at = max(observed_at, self._relative(learned_ts))
        root_capture_id = root_capture_ids[-1]
        evidence_id = self._evidence.new_evidence_id(root_capture_id, "deep")
        self._append_event(
            EvidenceEvent(
                evidence_id=evidence_id,
                source="deep",
                kind="scene_verified",
                root_capture_id=root_capture_id,
                observed_at=observed_at,
                learned_at=learned_at,
                scope=None,
                payload=SceneVerifiedPayload(
                    session_cluster_id=session_cluster_id,
                    label=label,
                    annotation=annotation,
                    modality=modality,
                    matches_existing=matches_existing,
                    candidate_scene_id=candidate_scene_id,
                    candidate_label=candidate_label,
                    action=action,
                    root_capture_ids=list(root_capture_ids),
                    model=result.result.model,
                    provider=result.result.provider,
                    prompt_tokens=result.result.usage.prompt_tokens,
                    completion_tokens=result.result.usage.completion_tokens,
                    cost_usd=result.cost_usd,
                    latency_ms=result.result.latency_seconds * 1000.0,
                    validation_error=validation_error,
                ),
                derived_from=list(fingerprint_evidence_ids),
                context_version=None,
                outcome="failed" if action == "rejected" else "ok",
            )
        )
        self.record_deep_call(result.cost_usd, failed=validation_error is not None)
        return evidence_id

    def append_game_knowledge(
        self,
        *,
        trigger_monotonic: float,
        learned_monotonic: float,
        result: GameKnowledgeCallResult,
        write_action: GameKnowledgeWriteAction,
        game_id: str,
    ) -> str:
        observed_at = self._relative(trigger_monotonic)
        learned_at = max(observed_at, self._relative(learned_monotonic))
        evidence_id = self._evidence.new_evidence_id(None, "init")
        event_outcome: EvidenceOutcome = {
            "ok": "ok",
            "cooldown_drop": "dropped",
            "timeout": "dropped",
            "failed": "failed",
            "schema_reject": "failed",
        }[result.outcome]
        self._append_event(
            EvidenceEvent(
                evidence_id=evidence_id,
                source="init",
                kind="game_knowledge",
                root_capture_id=None,
                observed_at=observed_at,
                learned_at=learned_at,
                scope=None,
                payload=GameKnowledgePayload(
                    game_id=game_id,
                    mode=GAME_KNOWLEDGE_MODE,
                    model=result.model,
                    request_id=result.request_id,
                    outcome=result.outcome,
                    latency_ms=result.latency_ms,
                    cost_usd=result.cost_usd,
                    write_action=write_action,
                    actual_model=result.actual_model,
                    provider=result.provider,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    failure_reason=result.failure_reason,
                    normalization_actions=list(result.normalization_actions),
                ),
                derived_from=[],
                context_version=None,
                outcome=event_outcome,
            )
        )
        self.game_knowledge_attempts += 1
        self.game_knowledge_outcomes[result.outcome] += 1
        self.game_knowledge_total_cost_usd += result.cost_usd
        self.total_cost_usd += result.cost_usd
        if result.error_metadata is not None:
            self.record_llm_error(
                result.error_metadata,
                phase="game_knowledge",
                write=False,
            )
        self._write_session(None)
        return evidence_id

    def record_deep_call(self, cost_usd: float, *, failed: bool) -> None:
        if cost_usd < 0:
            raise ValueError("deep call cost must be nonnegative")
        self.deep_calls += 1
        self.deep_failures += int(failed)
        self.deep_total_cost_usd += cost_usd
        self.total_cost_usd += cost_usd
        self._write_session(None)

    def record_llm_error(
        self,
        metadata: dict[str, object],
        *,
        phase: str,
        write: bool = True,
    ) -> None:
        self.llm_errors.append(
            {
                "phase": phase,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                **metadata,
            }
        )
        if write:
            self._write_session(None)

    def update_dispatch_statistics(
        self,
        snapshots: Sequence[LlmDispatchStats],
    ) -> None:
        grouped: dict[str, dict[str, object]] = {}
        for item in snapshots:
            current = grouped.setdefault(
                item.profile_name,
                {
                    "rate_limit_count": 0,
                    "cooldown_seconds": 0.0,
                    "cooldown_drop_count": 0,
                    "cooling_down": False,
                    "cooldown_remaining_seconds": 0.0,
                },
            )
            current["rate_limit_count"] = int(current["rate_limit_count"]) + item.rate_limit_count
            current["cooldown_seconds"] = float(current["cooldown_seconds"]) + item.cooldown_seconds
            current["cooldown_drop_count"] = int(current["cooldown_drop_count"]) + item.cooldown_drop_count
            current["cooling_down"] = bool(current["cooling_down"]) or item.cooling_down
            current["cooldown_remaining_seconds"] = max(
                float(current["cooldown_remaining_seconds"]),
                item.cooldown_remaining_seconds,
            )
        merged = dict(self.dispatch_profiles)
        for profile_name, current in grouped.items():
            previous = merged.get(profile_name)
            if previous is not None:
                current["rate_limit_count"] = max(
                    int(previous["rate_limit_count"]),
                    int(current["rate_limit_count"]),
                )
                current["cooldown_seconds"] = max(
                    float(previous["cooldown_seconds"]),
                    float(current["cooldown_seconds"]),
                )
                current["cooldown_drop_count"] = max(
                    int(previous["cooldown_drop_count"]),
                    int(current["cooldown_drop_count"]),
                )
            merged[profile_name] = current
        self.dispatch_profiles = merged

    def close(self) -> None:
        self._write_session(datetime.now(timezone.utc))
        self._markdown.close()
        self._evidence.close()

    def _relative(self, frame_ts: float) -> float:
        if self._first_frame_ts is None:
            self._first_frame_ts = frame_ts
            self._write_session(None)
        return max(0.0, frame_ts - self._origin())

    def _origin(self) -> float:
        if self._first_frame_ts is None:
            raise RuntimeError("evidence origin is unavailable before the first frame")
        return self._first_frame_ts

    def _append_event(self, event: EvidenceEvent) -> None:
        self._evidence.append(event)
        self._markdown.append(event)

    def _write_session(self, ended_at: datetime | None) -> None:
        rate_limit_count = sum(
            int(item["rate_limit_count"])
            for item in self.dispatch_profiles.values()
        )
        cooldown_seconds = sum(
            float(item["cooldown_seconds"])
            for item in self.dispatch_profiles.values()
        )
        cooldown_drop_count = sum(
            int(item["cooldown_drop_count"])
            for item in self.dispatch_profiles.values()
        )
        value = {
            "started_at": self.started_at.isoformat(),
            "ended_at": ended_at.isoformat() if ended_at else None,
            "origin_monotonic": self._first_frame_ts,
            "parameters": self.parameters,
            "observation_attempt_count": self.records,
            "call_count": self.calls,
            "dropped_count": self.dropped,
            "failure_count": self.failures,
            "failure_rate": round(self.failures / self.calls, 6) if self.calls else 0.0,
            "total_cost_usd": round(self.total_cost_usd, 9),
            "deep_call_count": self.deep_calls,
            "deep_failure_count": self.deep_failures,
            "deep_total_cost_usd": round(self.deep_total_cost_usd, 9),
            "game_knowledge_attempt_count": self.game_knowledge_attempts,
            "game_knowledge_total_cost_usd": round(
                self.game_knowledge_total_cost_usd,
                9,
            ),
            "game_knowledge_outcomes": dict(self.game_knowledge_outcomes),
            "rate_limit_count": rate_limit_count,
            "cooldown_seconds": round(cooldown_seconds, 6),
            "cooldown_drop_count": cooldown_drop_count,
            "llm_dispatch_profiles": self.dispatch_profiles,
            "llm_errors": self.llm_errors,
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


def _default_knowledge_client_factory(
    profile_name: str,
    base_url: str | None,
    api_key_env: str,
    timeout_seconds: float,
) -> GameKnowledgeClientProtocol:
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
        deep_client_factory: ClientFactory | None = None,
        knowledge_client_factory: KnowledgeClientFactory | None = None,
        input_listener_factory: InputListenerFactory | None = None,
        title_map: WindowTitleMap | None = None,
        clock: Callable[[], float] = time.perf_counter,
        ocr_engine: OcrEngine | None = None,
    ) -> None:
        self._settings = configuration.generic
        self._llm_configuration = llm_configuration
        self._capture_backend_factory = capture_backend_factory
        self._selector_factory = selector_factory
        self._client_factory = client_factory
        self._deep_client_factory = deep_client_factory or client_factory
        self._knowledge_client_factory = (
            knowledge_client_factory or _default_knowledge_client_factory
        )
        self._input_listener_factory = input_listener_factory
        self._title_map = title_map or WindowTitleMap.load()
        self._clock = clock
        self._ocr_engine_override = ocr_engine
        self._core: CoreServices | None = None
        self._task: asyncio.Task[None] | None = None
        self._backend: CaptureBackendLike | None = None
        self._selector: FrameSelectorLike | None = None
        self._client: LlmVisionClientProtocol | None = None
        self._log: ObservationLog | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        self._task_frames: dict[asyncio.Task[None], PendingFrame] = {}
        self._completed_sequences: set[int] = set()
        self._queued_frame: PendingFrame | None = None
        self._next_sequence = 1
        self._input_context: InputContextLike | None = None
        self._last_dispatched_frame_ts: float | None = None
        self._last_status: tuple[str, str, bool, bool] | None = None
        self._last_status_at = float("-inf")
        self._consecutive_failures = 0
        self._degraded = False
        self._cost_warning = False
        self._current_game = ""
        self._stopping = False
        self._ocr_engine: OcrEngine | None = None
        self._ocr_executor: ThreadPoolExecutor | None = None
        self._ocr_busy: asyncio.Future[OcrFrameResult] | None = None
        self._ocr_waiters: set[asyncio.Task[None]] = set()
        self._ocr_cache = TextLineCache()
        self._ocr_priority_attempted = False
        self._scene_repository: GameCardRepository | None = None
        self._scene_session: GameCardSession | None = None
        self._scene_clusterer: SceneClusterer | None = None
        self._scene_game_id: str | None = None
        self._scene_origin_monotonic: float | None = None
        self._scene_last_flush_at = 0.0
        self._scene_last_selected_cluster_id: int | None = None
        self._scene_changed_since_selected = False
        self._deep_reader: DeepVisionReader | None = None
        self._deep_client: LlmVisionClientProtocol | None = None
        self._knowledge_reader: GameKnowledgeReader | None = None
        self._knowledge_client: GameKnowledgeClientProtocol | None = None
        self._knowledge_configuration: LlmConfig | None = None
        self._game_knowledge_tasks: set[asyncio.Task[None]] = set()
        self._scene_naming_tasks: set[asyncio.Task[None]] = set()
        self._scene_naming_attempted: set[int] = set()
        self._scene_naming_request_count = 0
        self._scene_frame_buffer_cluster_id: int | None = None
        self._scene_frame_buffer: list[BufferedSceneFrame] = []

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
            "ocr": self._settings.ocr.model_dump(),
            "scene": self._settings.scene.model_dump(),
            "knowledge": self._settings.knowledge.model_dump(),
        }
        if extra_parameters:
            parameters.update(extra_parameters)
        self._log = ObservationLog(
            log_root,
            parameters,
            exact_directory=exact_directory,
        )
        self._initialize_ocr()
        self._initialize_scene()
        self._sync_dispatch_statistics()

    def start_replay(
        self,
        output_directory: Path,
        *,
        input_context: InputContextLike | None,
        input_window_start_monotonic: float | None = None,
        extra_parameters: dict[str, object] | None = None,
    ) -> None:
        """Start the production observation path without a live capture loop."""
        if self._task is not None or self._log is not None:
            raise RuntimeError("通用视觉观察器已经启动")
        if self._settings.input_context and input_context is None:
            raise ValueError("input_context is required when replay input context is enabled")
        self._input_context = input_context if self._settings.input_context else None
        self._last_dispatched_frame_ts = input_window_start_monotonic
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
        identity = GameIdentity(
            slugify_game_id(game, game),
            game,
            game,
        )
        scene_state = self._observe_scene_frame(frame, identity)
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
            scene_state=scene_state,
        )

    async def finish_replay(self) -> None:
        """Drain all paid calls and close the production-format observation log."""
        while self._inflight or self._queued_frame is not None:
            self._launch_queued_frame()
            if not self._inflight:
                break
            await asyncio.gather(*tuple(self._inflight), return_exceptions=True)
        self._close_input_context()
        await self._finish_ocr()
        await self._finish_scene_naming()
        await self._finish_game_knowledge()
        close_client = getattr(self._client, "close", None)
        self._sync_dispatch_statistics()
        if callable(close_client):
            close_client()
        self._client = None
        self._close_deep_reader()
        self._close_knowledge_reader()
        if self._log is not None:
            self._close_scene_session()
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
            self._record_key_window(
                queued,
                LOG_NO_INPUT_SUMMARY,
                self._last_dispatched_frame_ts,
            )
            self._complete_record(
                self._dropped_record(queued, "error:stopped")
            )
        for task in tuple(self._inflight):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        await self._finish_ocr()
        await self._finish_scene_naming()
        await self._finish_game_knowledge()
        close_client = getattr(self._client, "close", None)
        self._sync_dispatch_statistics()
        if callable(close_client):
            close_client()
        self._client = None
        self._close_deep_reader()
        self._close_knowledge_reader()
        if self._log is not None:
            self._close_scene_session()
            self._log.close()
            self._log = None

    async def _run(self) -> None:
        while True:
            started = self._clock()
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("通用视觉单轮观察失败，将继续下一轮")
                try:
                    await self._publish_status("error", self._current_game)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("通用视觉单轮异常后的状态发布失败")
            delay = max(0.0, self._settings.poll_interval_seconds - (self._clock() - started))
            await asyncio.sleep(delay)

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

        ownership_transferred = False
        try:
            identity = self._title_map.identify_identity(
                frame.metadata.window_title,
                frame.metadata.process_name,
            )
            game = identity.context_name
            self._current_game = game
            scene_state = self._observe_scene_frame(frame, identity)
            observation = self._selector.observe(
                frame.bitmap,
                frame.metadata.monotonic_seconds,
            )
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
                    scene_state=scene_state,
                )
                ownership_transferred = True
        finally:
            if not ownership_transferred:
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
        scene_state: SceneFrameState | None = None,
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
        scope: Scope | None = None
        if has_focus:
            scope = _focus_scope(confirmed_region)
            focus_location = scope.location
            region_area_ratio = scope.area_ratio
            focused_intensity = region_intensity
        pending = PendingFrame(
            seq=sequence,
            frame=frame,
            game=game,
            region=region,
            confirmed_region=confirmed_region,
            reason=reason,
            change_ratio=change_ratio,
            global_change=global_change,
            region_area_ratio=region_area_ratio,
            region_intensity=focused_intensity,
            focus_location=focus_location,
            baseline_monotonic_seconds=baseline_monotonic_seconds,
            scope=scope,
            scheduled_at_clock=self._clock(),
        )
        assert self._log is not None
        self._log.append_frame_metrics(
            sequence=sequence,
            frame_ts=frame.metadata.monotonic_seconds,
            wall=frame.metadata.captured_at.astimezone(timezone.utc).isoformat(),
            reason=reason,
            change_ratio=change_ratio,
            global_change=global_change,
            region_area_ratio=region_area_ratio,
            region_intensity=focused_intensity,
            scope=scope,
        )
        self._schedule_ocr(pending)
        self._record_scene_fingerprint(pending, scene_state)
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
                self._record_key_window(
                    replaced,
                    LOG_NO_INPUT_SUMMARY,
                    self._last_dispatched_frame_ts,
                )
                self._complete_record(self._dropped_record(replaced, "superseded"))
            return
        self._launch_frame(pending)

    def _initialize_scene(self) -> None:
        self._close_scene_frame_buffer()
        self._scene_session = None
        self._scene_clusterer = None
        self._scene_game_id = None
        self._scene_origin_monotonic = None
        self._scene_last_flush_at = 0.0
        self._scene_last_selected_cluster_id = None
        self._scene_changed_since_selected = False
        self._scene_naming_attempted.clear()
        self._scene_naming_request_count = 0
        self._deep_reader = None
        self._deep_client = None
        self._knowledge_reader = None
        self._knowledge_client = None
        self._knowledge_configuration = None
        settings = self._settings.scene
        knowledge = self._settings.knowledge
        if not settings.enabled and not knowledge.enabled:
            self._scene_repository = None
            return
        memory_root = Path(settings.memory_dir)
        if not memory_root.is_absolute():
            memory_root = BACKEND_DIRECTORY / memory_root
        self._scene_repository = GameCardRepository(memory_root)
        if knowledge.enabled:
            profile = self._llm_configuration.profiles.get(knowledge.llm_profile)
            if profile is None:
                raise RuntimeError(
                    f"游戏知识模型档位不存在：{knowledge.llm_profile}"
                )
            effective_knowledge = resolve_llm_profile(
                self._llm_configuration,
                knowledge.llm_profile,
            )
            if not effective_knowledge.enabled or not effective_knowledge.model.strip():
                raise RuntimeError(
                    f"游戏知识模型档位 {knowledge.llm_profile} 未启用或未配置型号"
                )
            # Client creation is deliberately lazy and line-local.  A missing
            # knowledge-only credential must produce one failed attempt after the
            # game is known, not prevent capture/OCR/fingerprint/fast vision from
            # starting at all.
            self._knowledge_configuration = effective_knowledge
        if not settings.enabled:
            return
        naming = settings.naming
        if not naming.enabled:
            return
        profile = self._llm_configuration.profiles.get(naming.llm_profile)
        if profile is None:
            raise RuntimeError(f"场景指纹核查模型档位不存在：{naming.llm_profile}")
        effective = resolve_llm_profile(self._llm_configuration, naming.llm_profile)
        if not effective.enabled or not effective.model.strip():
            raise RuntimeError(
                f"场景指纹核查模型档位 {naming.llm_profile} 未启用或未配置型号"
            )
        client = self._deep_client_factory(
            naming.llm_profile,
            effective.base_url,
            effective.api_key_env,
            effective.timeout_seconds,
        )
        self._deep_client = client
        self._deep_reader = DeepVisionReader(
            client,
            effective,
            input_price_per_million_usd=profile.input_price_per_million_usd,
            output_price_per_million_usd=profile.output_price_per_million_usd,
            reasoning_effort="none",
        )

    def _observe_scene_frame(
        self,
        frame: CapturedFrameLike,
        identity: GameIdentity,
    ) -> SceneFrameState | None:
        settings = self._settings.scene
        if not settings.enabled and not self._settings.knowledge.enabled:
            return None
        assert self._scene_repository is not None
        if self._scene_game_id != identity.game_id:
            self._close_scene_session()
            self._scene_naming_attempted.clear()
            self._scene_naming_request_count = 0
            self._close_scene_frame_buffer()
            card = self._scene_repository.load_or_create(
                identity.game_id,
                identity.display_name,
                frame.metadata.captured_at,
            )
            if settings.enabled:
                self._scene_session = GameCardSession(
                    self._scene_repository,
                    card,
                    frame.metadata.captured_at,
                )
                self._scene_clusterer = SceneClusterer(
                    settings.hamming_threshold,
                    settings.stable_min_seconds,
                    self._scene_repository.card_references(card),
                )
            self._scene_game_id = identity.game_id
            self._scene_origin_monotonic = frame.metadata.monotonic_seconds
            self._scene_last_flush_at = 0.0
            self._schedule_game_knowledge(
                GameKnowledgeContext(
                    game_id=identity.game_id,
                    game_name=identity.context_name,
                    card=card,
                    trigger_monotonic=frame.metadata.monotonic_seconds,
                )
            )
        if not settings.enabled:
            return None
        assert self._scene_session is not None
        assert self._scene_clusterer is not None
        assert self._scene_origin_monotonic is not None
        relative_seconds = max(
            0.0,
            frame.metadata.monotonic_seconds - self._scene_origin_monotonic,
        )
        started = self._clock()
        fingerprint = perceptual_hash(
            frame.bitmap,
            settings.hash_kind,
            settings.hash_bits,
        )
        match = self._scene_clusterer.observe(fingerprint, relative_seconds)
        elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
        if match.switched_from is not None:
            self._scene_changed_since_selected = True
        if relative_seconds - self._scene_last_flush_at >= settings.card_flush_seconds:
            self._flush_scene_session()
            self._scene_last_flush_at = relative_seconds
        if match.stable:
            self._schedule_scene_naming(
                identity.context_name,
                match.cluster_id,
            )
        return SceneFrameState(
            fingerprint=fingerprint,
            match=match,
            elapsed_ms=elapsed_ms,
        )

    def _schedule_game_knowledge(self, context: GameKnowledgeContext) -> None:
        if (
            not self._settings.knowledge.enabled
            or self._knowledge_configuration is None
        ):
            return
        task = asyncio.create_task(
            self._run_game_knowledge(context),
            name=f"generic-game-knowledge-{context.game_id}",
        )
        self._game_knowledge_tasks.add(task)
        task.add_done_callback(self._game_knowledge_tasks.discard)

    async def _run_game_knowledge(self, context: GameKnowledgeContext) -> None:
        assert self._scene_repository is not None
        assert self._knowledge_configuration is not None
        started = self._clock()
        try:
            reader = self._ensure_knowledge_reader()
            result = await reader.read(context.game_name)
        except (LlmError, OSError, ValueError) as error:
            metadata = error.metadata() if isinstance(error, LlmError) else {
                "error_type": "local_initialization"
            }
            result = self._knowledge_initialization_failure(
                error,
                started=started,
                error_metadata=metadata,
            )
        except Exception as error:
            logger.exception("游戏知识线初始化出现未预期异常")
            result = self._knowledge_initialization_failure(
                error,
                started=started,
                error_metadata={"error_type": "unexpected_local"},
            )
        checked_at = datetime.now(timezone.utc)
        try:
            card, write_action = self._scene_repository.record_knowledge_attempt(
                context.card,
                checked_at=checked_at,
                model=result.model,
                mode=GAME_KNOWLEDGE_MODE,
                request_id=result.request_id,
                outcome=result.outcome,
                failure_reason=result.failure_reason,
                content=result.content,
            )
        except (OSError, ValueError) as error:
            logger.exception("游戏知识结果写卡失败：%s", context.game_id)
            result = replace(
                result,
                outcome="failed",
                content=None,
                failure_reason=f"游戏卡写入失败：{_one_line(error)}",
            )
            write_action = "kept_previous"
            card = context.card
        if (
            self._scene_session is not None
            and self._scene_session.card.game_id == context.game_id
        ):
            self._scene_session.card = card
        if self._log is not None:
            self._log.append_game_knowledge(
                trigger_monotonic=context.trigger_monotonic,
                learned_monotonic=self._clock(),
                result=result,
                write_action=write_action,
                game_id=context.game_id,
            )
        self._sync_dispatch_statistics()
        self._refresh_cost_warning()
        if result.outcome == "ok":
            logger.info(
                "游戏知识线完成 %s：%s（%.3fs，$%.6f）",
                context.game_id,
                write_action,
                result.latency_ms / 1000.0,
                result.cost_usd,
            )
        else:
            logger.warning(
                "游戏知识线未更新 %s：%s；保留上一份内容",
                context.game_id,
                result.failure_reason,
            )

    def _ensure_knowledge_reader(self) -> GameKnowledgeReader:
        if self._knowledge_reader is not None:
            return self._knowledge_reader
        assert self._knowledge_configuration is not None
        settings = self._settings.knowledge
        configuration = self._knowledge_configuration
        client = self._knowledge_client_factory(
            settings.llm_profile,
            configuration.base_url,
            configuration.api_key_env,
            configuration.timeout_seconds,
        )
        reader = GameKnowledgeReader(
            client,
            configuration,
            wall_timeout_seconds=settings.wall_timeout_seconds,
            clock=self._clock,
        )
        self._knowledge_client = client
        self._knowledge_reader = reader
        return reader

    def _knowledge_initialization_failure(
        self,
        error: Exception,
        *,
        started: float,
        error_metadata: dict[str, object],
    ) -> GameKnowledgeCallResult:
        assert self._knowledge_configuration is not None
        return GameKnowledgeCallResult(
            request_id=f"gk-{uuid.uuid4()}",
            outcome="failed",
            model=self._knowledge_configuration.model,
            actual_model=None,
            provider=self._knowledge_configuration.provider or None,
            latency_ms=max(0.0, (self._clock() - started) * 1000.0),
            cost_usd=0.0,
            prompt_tokens=None,
            completion_tokens=None,
            content=None,
            failure_reason=f"游戏知识线初始化失败：{_one_line(error)}",
            normalization_actions=(),
            error_metadata=error_metadata,
        )

    def _record_scene_fingerprint(
        self,
        pending: PendingFrame,
        scene_state: SceneFrameState | None,
    ) -> None:
        settings = self._settings.scene
        if not settings.enabled:
            return
        if scene_state is None:
            raise RuntimeError("selected frame lacks its polling-time scene fingerprint")
        assert self._scene_clusterer is not None
        match = scene_state.match
        candidate = match.card_candidate
        switched_from = (
            self._scene_last_selected_cluster_id
            if self._scene_changed_since_selected
            and self._scene_last_selected_cluster_id is not None
            else None
        )
        assert self._log is not None
        evidence_id = self._log.append_scene_fingerprint(
            sequence=pending.seq,
            frame_ts=pending.frame.metadata.monotonic_seconds,
            fingerprint=scene_state.fingerprint,
            cluster_id=match.cluster_id,
            distance=match.distance,
            is_new_cluster=match.is_new_cluster,
            switched_from=switched_from,
            stable=match.stable,
            card_candidate_scene_id=(candidate.scene_id if candidate else None),
            card_candidate_distance=(candidate.distance if candidate else None),
            elapsed_ms=scene_state.elapsed_ms,
        )
        self._scene_clusterer.record_evidence(match.cluster_id, evidence_id)
        self._buffer_scene_frame(pending, match.cluster_id, evidence_id)
        self._scene_last_selected_cluster_id = match.cluster_id
        self._scene_changed_since_selected = False
        if match.stable:
            self._schedule_scene_naming(
                pending.game,
                match.cluster_id,
            )

    def _buffer_scene_frame(
        self,
        pending: PendingFrame,
        cluster_id: int,
        fingerprint_evidence_id: str,
    ) -> None:
        if not self._settings.scene.naming.enabled:
            return
        if cluster_id in self._scene_naming_attempted:
            return
        if self._scene_frame_buffer_cluster_id != cluster_id:
            self._close_scene_frame_buffer()
            self._scene_frame_buffer_cluster_id = cluster_id
        self._scene_frame_buffer.append(
            BufferedSceneFrame(
                root_capture_id=f"f{pending.seq}",
                image=pending.frame.bitmap.copy(),
                frame_ts=pending.frame.metadata.monotonic_seconds,
                fingerprint_evidence_id=fingerprint_evidence_id,
            )
        )
        maximum = self._settings.scene.naming.representative_frame_count * 3
        while len(self._scene_frame_buffer) > maximum:
            removed = self._scene_frame_buffer.pop(1)
            removed.image.close()

    def _schedule_scene_naming(
        self,
        game_name: str,
        cluster_id: int,
    ) -> None:
        settings = self._settings.scene.naming
        reader = self._deep_reader
        session = self._scene_session
        clusterer = self._scene_clusterer
        if not settings.enabled or reader is None or session is None or clusterer is None:
            return
        if cluster_id in self._scene_naming_attempted:
            return
        cluster = clusterer.cluster(cluster_id)
        if not session.needs_verification(cluster):
            self._scene_naming_attempted.add(cluster_id)
            return
        card_scene = session.named_candidate(cluster)
        existing_scene = (
            ExistingSceneNaming(
                scene_id=card_scene.scene_id,
                label=card_scene.label,
                annotation=card_scene.annotation,
                label_status=card_scene.label_status,
            )
            if card_scene is not None
            else None
        )
        if self._scene_naming_request_count >= settings.max_requests_per_session:
            self._scene_naming_attempted.add(cluster_id)
            logger.warning(
                "场景命名达到每会话 %d 次上限；session:c%d 保持未命名",
                settings.max_requests_per_session,
                cluster_id,
            )
            return
        if self._scene_frame_buffer_cluster_id != cluster_id or not self._scene_frame_buffer:
            return
        chosen = _representative_scene_frames(
            self._scene_frame_buffer,
            settings.representative_frame_count,
        )
        chosen_ids = {id(frame) for frame in chosen}
        for frame in self._scene_frame_buffer:
            if id(frame) not in chosen_ids:
                frame.image.close()
        self._scene_frame_buffer = []
        self._scene_frame_buffer_cluster_id = None
        self._scene_naming_attempted.add(cluster_id)
        self._scene_naming_request_count += 1
        context = SceneNamingContext(
            game_name=game_name,
            cluster=cluster,
            session=session,
            frames=chosen,
            trigger_frame_ts=chosen[-1].frame_ts,
            existing_scene=existing_scene,
        )
        task = asyncio.create_task(
            self._run_scene_naming(context),
            name=f"generic-scene-naming-{cluster_id}",
        )
        self._scene_naming_tasks.add(task)
        task.add_done_callback(self._scene_naming_tasks.discard)

    async def _run_scene_naming(self, context: SceneNamingContext) -> None:
        cost_recorded = False
        deep_result: DeepReadResult | None = None
        try:
            assert self._deep_reader is not None
            stable_ocr_lines: tuple[str, ...] = ()
            request = build_scene_naming_request(
                game_name=context.game_name,
                session_cluster_id=context.cluster.cluster_id,
                frames=tuple(
                    SceneNamingFrame(frame.root_capture_id, frame.image)
                    for frame in context.frames
                ),
                stable_ocr_lines=stable_ocr_lines,
                send_width=self._settings.scene.naming.upload_width,
                existing_scene=context.existing_scene,
            )
            deep_result = await self._deep_reader.read(request)
            proposal = parse_scene_naming_proposal(deep_result.result.text)
            decision = validate_scene_naming_proposal(
                context.cluster,
                proposal,
                context.existing_scene,
            )
            assert self._log is not None
            evidence_id = self._log.append_scene_verified(
                trigger_frame_ts=context.trigger_frame_ts,
                learned_ts=self._clock(),
                session_cluster_id=context.cluster.cluster_id,
                label=decision.label,
                annotation=decision.annotation,
                modality=decision.modality,
                matches_existing=decision.matches_existing,
                candidate_scene_id=decision.candidate_scene_id,
                candidate_label=decision.candidate_label,
                action=decision.action,
                root_capture_ids=tuple(frame.root_capture_id for frame in context.frames),
                fingerprint_evidence_ids=tuple(
                    frame.fingerprint_evidence_id for frame in context.frames
                ),
                result=deep_result,
                validation_error=decision.validation_error,
            )
            cost_recorded = True
            if not decision.accepted:
                if decision.needs_review and context.existing_scene is not None:
                    context.session.mark_candidate_for_review(
                        context.cluster,
                        evidence_id,
                    )
                logger.warning(
                    "拒绝 session:c%d 场景命名判决：%s",
                    context.cluster.cluster_id,
                    decision.validation_error,
                )
                return
            assert decision.applied_label is not None
            assert decision.applied_annotation is not None
            assert decision.applied_label_status is not None
            context.session.record_verification(
                context.cluster,
                SceneCardVerification(
                    label=decision.applied_label,
                    annotation=decision.applied_annotation,
                    modality=decision.modality,
                    label_status=decision.applied_label_status,
                    needs_review=decision.needs_review,
                    verified_at=datetime.now(timezone.utc),
                    evidence_id=evidence_id,
                ),
            )
        except asyncio.CancelledError:
            raise
        except LlmCooldownError as error:
            logger.info(
                "session:c%d 场景指纹核查被档位冷却直接丢弃，本会话不重试：%s",
                context.cluster.cluster_id,
                error.diagnostic(),
            )
            if self._log is not None:
                self._log.record_llm_error(error.metadata(), phase="deep")
        except LlmError as error:
            logger.error(
                "session:c%d 场景指纹核查失败，本会话不重试：%s",
                context.cluster.cluster_id,
                error.diagnostic(),
            )
            if self._log is not None:
                self._log.record_llm_error(error.metadata(), phase="deep")
                self._log.record_deep_call(
                    deep_result.cost_usd if deep_result is not None else 0.0,
                    failed=True,
                )
        except Exception as error:
            logger.error(
                "session:c%d 场景指纹核查失败，本会话不重试：%s",
                context.cluster.cluster_id,
                _one_line(error),
            )
            if not cost_recorded and self._log is not None:
                self._log.record_deep_call(
                    deep_result.cost_usd if deep_result is not None else 0.0,
                    failed=True,
                )
        finally:
            self._sync_dispatch_statistics()
            for frame in context.frames:
                frame.image.close()

    async def _finish_scene_naming(self) -> None:
        if self._scene_naming_tasks:
            await asyncio.gather(
                *tuple(self._scene_naming_tasks),
                return_exceptions=True,
            )

    async def _finish_game_knowledge(self) -> None:
        if self._game_knowledge_tasks:
            await asyncio.gather(
                *tuple(self._game_knowledge_tasks),
                return_exceptions=True,
            )

    def _close_scene_frame_buffer(self) -> None:
        for frame in self._scene_frame_buffer:
            frame.image.close()
        self._scene_frame_buffer = []
        self._scene_frame_buffer_cluster_id = None

    def _flush_scene_session(self) -> None:
        if self._scene_session is None or self._scene_clusterer is None:
            return
        try:
            self._scene_session.flush(self._scene_clusterer.clusters)
        except (OSError, ValueError):
            logger.exception("游戏卡写入失败；保留内存状态并继续观察")

    def _close_scene_session(self) -> None:
        self._flush_scene_session()
        self._close_scene_frame_buffer()
        self._scene_session = None
        self._scene_clusterer = None
        self._scene_game_id = None
        self._scene_origin_monotonic = None
        self._scene_last_flush_at = 0.0
        self._scene_last_selected_cluster_id = None
        self._scene_changed_since_selected = False

    def _close_deep_reader(self) -> None:
        self._sync_dispatch_statistics()
        reader, self._deep_reader = self._deep_reader, None
        if reader is not None:
            reader.close()
        self._deep_client = None

    def _close_knowledge_reader(self) -> None:
        self._sync_dispatch_statistics()
        reader, self._knowledge_reader = self._knowledge_reader, None
        if reader is not None:
            reader.close()
        self._knowledge_client = None

    def _initialize_ocr(self) -> None:
        settings = self._settings.ocr
        self._ocr_cache.clear()
        if not settings.enabled:
            return
        if settings.engine != ENGINE_NAME:
            raise RuntimeError(f"未知 OCR 引擎：{settings.engine}")
        model_dir = Path(settings.model_dir)
        if not model_dir.is_absolute():
            model_dir = BACKEND_DIRECTORY / model_dir
        engine = self._ocr_engine_override or RapidOcrEngine(
            model_dir=model_dir,
            num_threads=settings.num_threads,
            det_limit_side_len=settings.det_limit_side_len,
        )
        try:
            engine.start()
        except Exception as error:
            try:
                engine.close()
            except Exception as close_error:
                logger.debug("OCR 启动失败后的关闭也失败：%s", _one_line(close_error))
            logger.error("OCR 引擎不可用，本会话关闭 OCR 并继续运行：%s", _one_line(error))
            self._ocr_engine = None
            return
        self._ocr_engine = engine
        self._ocr_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="generic-ocr",
        )

    def _schedule_ocr(self, pending: PendingFrame) -> None:
        engine = self._ocr_engine
        executor = self._ocr_executor
        if engine is None or executor is None:
            return
        trigger = "heartbeat" if pending.reason == "forced" else "detector"
        busy = self._ocr_busy
        if busy is not None and not busy.done():
            self._append_ocr_frame_result(
                pending,
                trigger=trigger,
                outcome_detail="late",
                result=None,
                elapsed_ms=0.0,
            )
            return

        pixels = np.asarray(pending.frame.bitmap, dtype=np.uint8)
        if pixels.ndim == 2:
            pixels = np.repeat(pixels[:, :, None], 3, axis=2)
        if pixels.shape[2] < 3:
            raise ValueError("OCR frame must have at least three color channels")
        # PIL/WGC presents RGB(A); the measured RapidOCR memory path expects BGR.
        image = pixels[:, :, :3][:, :, ::-1].copy()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(executor, self._recognize_ocr, engine, image)
        self._ocr_busy = future

        def release(completed: asyncio.Future[OcrFrameResult]) -> None:
            if self._ocr_busy is completed:
                self._ocr_busy = None
            if completed.cancelled():
                return
            late_error = completed.exception()
            if late_error is not None:
                logger.debug("OCR 工作线程迟到失败：%s", _one_line(late_error))

        future.add_done_callback(release)
        waiter = asyncio.create_task(
            self._record_ocr_completion(pending, trigger, future),
            name=f"generic-ocr-{pending.seq}",
        )
        self._ocr_waiters.add(waiter)
        waiter.add_done_callback(self._ocr_waiters.discard)

    def _recognize_ocr(
        self,
        engine: OcrEngine,
        image: np.ndarray,
    ) -> OcrFrameResult:
        if not self._ocr_priority_attempted:
            self._ocr_priority_attempted = True
            try:
                set_current_thread_below_normal()
            except OSError as error:
                logger.warning("无法降低 OCR worker 线程优先级：%s", _one_line(error))
        return engine.recognize(image)

    async def _record_ocr_completion(
        self,
        pending: PendingFrame,
        trigger: str,
        future: asyncio.Future[OcrFrameResult],
    ) -> None:
        try:
            result = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self._settings.poll_interval_seconds,
            )
        except asyncio.TimeoutError:
            self._append_ocr_frame_result(
                pending,
                trigger=trigger,
                outcome_detail="late",
                result=None,
                elapsed_ms=self._settings.poll_interval_seconds * 1000.0,
            )
            return
        except Exception as error:
            logger.error("OCR frame f%d 识别失败：%s", pending.seq, _one_line(error))
            self._append_ocr_frame_result(
                pending,
                trigger=trigger,
                outcome_detail="failed",
                result=None,
                elapsed_ms=max(0.0, self._clock() - pending.scheduled_at_clock) * 1000.0,
            )
            return

        learned_ts = self._clock()
        self._append_ocr_frame_result(
            pending,
            trigger=trigger,
            outcome_detail="ok",
            result=result,
            elapsed_ms=result.duration_ms,
            learned_ts=learned_ts,
        )
        diff = self._ocr_cache.update(result.lines)
        assert self._log is not None
        for item in diff.lines:
            change = {
                "added": "new",
                "changed": "changed",
                "unchanged": "stable",
            }[item.kind]
            self._log.append_text_observed(
                sequence=pending.seq,
                frame_ts=pending.frame.metadata.monotonic_seconds,
                learned_ts=learned_ts,
                line=item.line,
                change=change,
                previous_text=item.previous_text,
                streak=item.streak,
                engine=ENGINE_NAME,
            )
        for cached in diff.gone:
            self._log.append_text_observed(
                sequence=pending.seq,
                frame_ts=pending.frame.metadata.monotonic_seconds,
                learned_ts=learned_ts,
                line=cached.line,
                change="gone",
                previous_text=None,
                streak=cached.streak,
                engine=ENGINE_NAME,
            )

    def _append_ocr_frame_result(
        self,
        pending: PendingFrame,
        *,
        trigger: str,
        outcome_detail: str,
        result: OcrFrameResult | None,
        elapsed_ms: float,
        learned_ts: float | None = None,
    ) -> None:
        assert self._log is not None
        settings = self._settings.ocr
        self._log.append_ocr_frame(
            sequence=pending.seq,
            frame_ts=pending.frame.metadata.monotonic_seconds,
            learned_ts=self._clock() if learned_ts is None else learned_ts,
            engine=ENGINE_NAME,
            num_threads=settings.num_threads,
            det_limit_side_len=settings.det_limit_side_len,
            recognized_line_count=len(result.lines) if result is not None else 0,
            elapsed_ms=elapsed_ms,
            det_ms=result.det_ms if result is not None else None,
            rec_ms=result.rec_ms if result is not None else None,
            cpu_core_seconds=result.cpu_core_seconds if result is not None else None,
            trigger=trigger,
            outcome_detail=outcome_detail,
        )

    async def _finish_ocr(self) -> None:
        if self._ocr_waiters:
            await asyncio.gather(*tuple(self._ocr_waiters), return_exceptions=True)
        busy = self._ocr_busy
        if busy is not None and not busy.done():
            await asyncio.gather(asyncio.shield(busy), return_exceptions=True)
        self._ocr_busy = None
        engine, self._ocr_engine = self._ocr_engine, None
        if engine is not None:
            engine.close()
        executor, self._ocr_executor = self._ocr_executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    def _launch_frame(self, pending: PendingFrame) -> None:
        frame_ts = pending.frame.metadata.monotonic_seconds
        input_summary: str | None = None
        input_window: ActionInputWindow | None = None
        window_start = self._last_dispatched_frame_ts
        if self._settings.input_context:
            if self._input_context is None:
                raise RuntimeError("输入上下文已启用但监听器未初始化")
            input_window = self._input_context.summarize_window_result(
                self._last_dispatched_frame_ts,
                frame_ts,
            )
            input_summary = input_window.summary
            self._last_dispatched_frame_ts = frame_ts
        logged_input = _logged_input(input_summary)
        if input_window is not None and input_window.mouse_motion is not None:
            assert self._log is not None
            aggregation_elapsed = max(
                0.0,
                self._clock() - pending.scheduled_at_clock,
            )
            self._log.append_mouse_motion(
                input_window.mouse_motion,
                learned_ts=frame_ts + aggregation_elapsed,
            )
        self._record_key_window(pending, logged_input, window_start)
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
            self._observe_frame(pending, logged_input, user_prompt),
            name=f"generic-vision-call-{pending.seq}",
        )
        self._inflight.add(task)
        self._task_frames[task] = pending
        task.add_done_callback(self._observation_done)

    def _record_key_window(
        self,
        pending: PendingFrame,
        summary: str,
        window_start: float | None,
    ) -> None:
        if pending.key_window_recorded:
            raise RuntimeError(f"key window already recorded for frame f{pending.seq}")
        assert self._log is not None
        self._log.append_key_window(
            sequence=pending.seq,
            frame_ts=pending.frame.metadata.monotonic_seconds,
            summary=summary,
            window_start=window_start,
        )
        pending.key_window_recorded = True

    def _observation_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        pending = self._task_frames.pop(task)
        if task.cancelled() and pending.seq not in self._completed_sequences:
            self._complete_record(self._dropped_record(pending, "error:stopped"))
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

    def _dropped_record(self, pending: PendingFrame, reason: str) -> ObservationRecord:
        elapsed = max(0.0, self._clock() - pending.scheduled_at_clock)
        return ObservationRecord(
            seq=pending.seq,
            frame_ts=pending.frame.metadata.monotonic_seconds,
            wall=pending.frame.metadata.captured_at.astimezone(timezone.utc).isoformat(),
            game=pending.game,
            text="",
            region=pending.region or None,
            reason=pending.reason,
            change_ratio=pending.change_ratio,
            global_change=pending.global_change,
            region_area_ratio=pending.region_area_ratio,
            region_intensity=pending.region_intensity,
            input=LOG_NO_INPUT_SUMMARY,
            focus_location=pending.focus_location,
            scope=pending.scope,
            latency_ms=elapsed * 1000.0,
            ttft_ms=None,
            dropped=reason,
            cost_usd=0.0,
            model_called=False,
            visible_output_tokens=None,
            truncated=False,
            learned_at=pending.frame.metadata.monotonic_seconds + elapsed,
        )

    async def _observe_frame(
        self,
        pending: PendingFrame,
        logged_input: str,
        user_prompt: str,
    ) -> None:
        dropped: str | None = None
        text = ""
        cost = 0.0
        visible_output_tokens: int | None = None
        ttft_ms: float | None = None
        truncated = False
        speculation: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        actual_model: str | None = None
        actual_provider: str | None = None
        error_metadata: dict[str, object] | None = None
        model_called = True
        cancelled = False
        try:
            assert self._client is not None
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                None,
                functools.partial(
                    self._client.complete_with_images_stream,
                    model=self._effective.model,
                    provider=self._effective.provider or None,
                    system_prompt=FAST_PROMPT_PATH.read_text(encoding="utf-8").strip(),
                    user_prompt=user_prompt,
                    images=(
                        LlmImage(
                            pending.frame.bitmap,
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
            )

            # dropped 已承载产品层事实；每秒一帧下 warning 会在上游抖动时刷屏，
            # 所以迟到异常在取回后刻意只记 debug，而不是静默吞掉或重复告警。
            def release_bitmap(completed: asyncio.Future[LlmResult]) -> None:
                pending.frame.bitmap.close()
                if completed.cancelled():
                    return
                late_error = completed.exception()
                if late_error is not None:
                    logger.debug("通用视觉工作线程迟到失败：%s", _one_line(late_error))

            future.add_done_callback(release_bitmap)
            result = await asyncio.wait_for(
                asyncio.shield(future),
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
            speculation = _model_segment(text, "【推测】")
            input_tokens = result.usage.prompt_tokens
            output_tokens = result.usage.completion_tokens
            actual_model = result.model
            actual_provider = result.provider
            self._consecutive_failures = 0
        except asyncio.TimeoutError:
            dropped = "timeout"
            self._register_failure("timeout")
        except asyncio.CancelledError:
            dropped = "error:stopped"
            cancelled = True
        except LlmCooldownError as error:
            dropped = f"cooldown:{error.diagnostic()}"
            error_metadata = error.metadata()
            model_called = False
        except LlmError as error:
            dropped = f"error:{error.diagnostic()}"
            error_metadata = error.metadata()
            self._register_failure(error.diagnostic())
        except Exception as error:
            dropped = f"error:{_one_line(error)}"
            self._register_failure(str(error))
        elapsed = max(0.0, self._clock() - pending.scheduled_at_clock)
        self._complete_record(
            ObservationRecord(
                seq=pending.seq,
                frame_ts=pending.frame.metadata.monotonic_seconds,
                wall=pending.frame.metadata.captured_at.astimezone(timezone.utc).isoformat(),
                game=pending.game,
                text=text,
                region=pending.region or None,
                reason=pending.reason,
                change_ratio=pending.change_ratio,
                global_change=pending.global_change,
                region_area_ratio=pending.region_area_ratio,
                region_intensity=pending.region_intensity,
                input=logged_input,
                focus_location=pending.focus_location,
                scope=pending.scope,
                latency_ms=elapsed * 1000.0,
                ttft_ms=ttft_ms,
                dropped=dropped,
                cost_usd=cost,
                model_called=model_called,
                visible_output_tokens=visible_output_tokens,
                truncated=truncated,
                user_prompt=user_prompt,
                speculation=speculation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                actual_model=actual_model,
                actual_provider=actual_provider,
                error_metadata=error_metadata,
                learned_at=pending.frame.metadata.monotonic_seconds + elapsed,
            )
        )
        if cancelled:
            raise asyncio.CancelledError

    def _complete_record(self, record: ObservationRecord) -> None:
        if record.seq in self._completed_sequences:
            raise RuntimeError(f"fast observation already recorded for frame f{record.seq}")
        self._completed_sequences.add(record.seq)
        assert self._log is not None
        self._sync_dispatch_statistics()
        self._log.append(record)
        self._refresh_cost_warning()

    def _dispatch_statistics(self) -> tuple[LlmDispatchStats, ...]:
        snapshots: list[LlmDispatchStats] = []
        seen: set[int] = set()
        for client in (self._client, self._deep_client, self._knowledge_client):
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            snapshot = getattr(client, "dispatch_stats", None)
            if callable(snapshot):
                snapshots.append(snapshot())
        return tuple(snapshots)

    def _sync_dispatch_statistics(self) -> None:
        if self._log is not None:
            self._log.update_dispatch_statistics(self._dispatch_statistics())

    def _price(self, result: LlmResult) -> float:
        if result.usage.cost_usd is not None:
            return result.usage.cost_usd
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
            "本条是定时心跳快照；输出【画面】，用一到两句描述当前场景与正在发生的事；"
            "不要输出【局部】，不得复述游戏名。"
        )
    else:
        lines.append(WIDE_CHANGE_TEMPLATE.format(global_change=global_change))
        lines.append(
            "本条没有聚焦区域；输出【画面】，用一到两句按当前新场景完整定场；"
            f"不要输出【局部】；{global_change:.1f}%是系统定位数值，严禁在输出中写出或改写，"
            "日志会机械添加；不得复述游戏名。"
        )
    lines.append(
        "可以把键鼠输入与画面连起来陈述当前可直接确认的事实。若有证据支持目的性推断，"
        "只能另加【推测】一段并写一句；无把握时省略【推测】。"
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
