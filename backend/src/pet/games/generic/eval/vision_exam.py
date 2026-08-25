"""Run an explicitly confirmed, offline-authored visual-model exam."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import difflib
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import tomllib
from typing import Literal, cast

import httpx

from pet.core.config import LlmConfig, load_config
from pet.core.llm import (
    LlmError,
    LlmImage,
    LlmResult,
    LlmStreamingUnsupported,
    LlmVisionClientProtocol,
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_BASE_URL,
    OpenRouterClient,
    image_upload_metadata,
)

BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
FAST_PROMPT_PATH = BACKEND_DIRECTORY / "prompts" / "generic" / "observation-fast.md"
DEEP_PROMPT_PATH = BACKEND_DIRECTORY / "prompts" / "generic" / "observation-deep.md"
DEFAULT_OUTPUT_ROOT = BACKEND_DIRECTORY / "eval-reports"
DEFAULT_CONFIG_PATH = BACKEND_DIRECTORY / "config.toml"
DEFAULT_LOCAL_CONFIG_PATH = BACKEND_DIRECTORY / "config.local.toml"
DEFAULT_ANSWER_KEY_PATH = (
    BACKEND_DIRECTORY / "data" / "generic" / "vision-exam" / "answer-key.md"
)
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"
OPENROUTER_MODEL_ENDPOINTS_URL = f"{OPENROUTER_BASE_URL}/models/{{slug}}/endpoints"
SPEED_ROUND_PROVIDER_NAME = "Alibaba"
QUESTION_TYPES = {"single", "sequence"}
QUESTION_FIELDS = {
    "id",
    "type",
    "game_context",
    "frames",
    "seconds",
    "region_grid",
}
REGION_CELL_PATTERN = re.compile(r"r(?:[1-9]|1[0-6])c[1-9]")
REGION_ROWS = 16
REGION_COLUMNS = 9
REGION_CELL_COUNT = REGION_ROWS * REGION_COLUMNS
RegionMode = Literal["off", "sparse", "always"]
REGION_MODES: tuple[RegionMode, ...] = ("off", "sparse", "always")
OutputMode = Literal["fast", "deep", "fast-relaxed"]
CLI_OUTPUT_MODES = ("fast", "deep")
FAST_MAX_TOKENS = 60  # 初始值，待考卷实测修订。
DEEP_MAX_TOKENS = 1600  # 初始值，待考卷实测修订。
FAST_RELAXED_MAX_TOKENS = 200  # 仅供 60-token 截断率超过 30% 的规定重跑。
OUTPUT_PROMPT_PATHS: Mapping[OutputMode, Path] = {
    "fast": FAST_PROMPT_PATH,
    "deep": DEEP_PROMPT_PATH,
    "fast-relaxed": FAST_PROMPT_PATH,
}
OUTPUT_MAX_TOKENS: Mapping[OutputMode, int] = {
    "fast": FAST_MAX_TOKENS,
    "deep": DEEP_MAX_TOKENS,
    "fast-relaxed": FAST_RELAXED_MAX_TOKENS,
}
DEFAULT_REGION_SPARSITY_MAX = 0.25  # 此数待实测确定。
DEFAULT_COST_CAP_USD = 5.0
DEFAULT_ESTIMATED_INPUT_TOKENS = 4_000
FAST_RELAXED_TRIGGER_FRACTION = 0.30


class VisionExamError(Exception):
    """A manifest, target, prompt, or output error safe to show to a user."""


@dataclass(frozen=True, slots=True)
class ExamQuestion:
    """One authored visual question with fully resolved local files."""

    question_id: str
    question_type: Literal["single", "sequence"]
    game_context: str | None
    frames: tuple[Path, ...]
    relative_seconds: tuple[float, ...]
    region_grid: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class ExamManifest:
    """A validated collection of visual questions."""

    path: Path
    questions: tuple[ExamQuestion, ...]


@dataclass(frozen=True, slots=True)
class ExamVariant:
    """One output-mode, upload-width, and region-mode variant."""

    send_width: int
    region_mode: RegionMode
    region_sparsity_max: float
    output_mode: OutputMode
    max_tokens_override: int | None = None

    @property
    def max_tokens(self) -> int:
        return (
            self.max_tokens_override
            if self.max_tokens_override is not None
            else OUTPUT_MAX_TOKENS[self.output_mode]
        )

    @property
    def prompt_path(self) -> Path:
        return OUTPUT_PROMPT_PATHS[self.output_mode]

    @property
    def name(self) -> str:
        width = "native" if self.send_width == 0 else str(self.send_width)
        return f"output-{self.output_mode}__region-{self.region_mode}__width-{width}"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Configured USD prices per one million input and output tokens."""

    input_per_million_usd: float
    output_per_million_usd: float


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """One exact live OpenRouter catalog match used by a formal run."""

    requested_name: str
    name: str
    slug: str
    canonical_slug: str | None
    input_modalities: tuple[str, ...]
    price: ModelPrice
    supported_parameters: tuple[str, ...]
    reasoning_mandatory: bool
    reasoning_disabled: bool
    selected_provider_name: str | None
    selected_provider_slug: str | None
    selected_provider_endpoint: str | None
    provider_locked: bool


@dataclass(frozen=True, slots=True)
class ModelResolution:
    """Timestamped results from one live fetch of the official model catalog."""

    endpoint: str
    fetched_at: str
    models: tuple[ResolvedModel, ...]


@dataclass(frozen=True, slots=True)
class ModelTarget:
    """One requested model or resolved config profile."""

    label: str
    model: str
    provider: str | None
    temperature: float
    timeout_seconds: float
    price: ModelPrice
    reasoning_disabled: bool = False
    provider_lock_status: str = "未请求"
    provider_endpoint: str | None = None
    provider_display_name: str | None = None
    provider_region: str | None = None
    endpoint_host: str | None = None
    reasoning_parameter_mode: str | None = None


@dataclass(frozen=True, slots=True)
class ExamRecord:
    """One model attempt, including invalid output or transport failure."""

    question_id: str
    question_type: str
    variant: str
    output_mode: str
    max_tokens: int
    upload_width: int
    region_mode: str
    region_grid_fraction: float | None
    region_injected: bool
    image_dimensions: tuple[str, ...]
    image_byte_sizes: tuple[int, ...]
    target_label: str
    requested_model: str
    actual_model: str | None
    provider: str | None
    provider_region: str | None
    endpoint_host: str | None
    response_text: str
    error: str | None
    skipped: bool
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    configured_cost_usd: float | None
    upstream_cost_usd: float | None
    reasoning_tokens: int | None
    visible_output_tokens: int | None
    visible_output_empty: bool
    truncated: bool
    fast_relaxed: bool
    finish_reason: str | None
    ttft_ms: float | None
    streamed: bool
    repetition: int

    @property
    def succeeded(self) -> bool:
        return self.error is None


ClientFactory = Callable[[ModelTarget], LlmVisionClientProtocol]


@dataclass(frozen=True, slots=True)
class ExamRunOutcome:
    """Records plus objective guard and automatic-rerun state."""

    records: tuple[ExamRecord, ...]
    actual_cost_usd: float
    cost_guard_stopped: bool
    relaxed_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Transparent token-volume estimate used only by the preflight guard."""

    base_calls: int
    maximum_calls_with_relaxed: int
    estimated_input_tokens: int
    maximum_output_tokens: int
    estimated_cost_usd: float


def load_manifest(path: Path) -> ExamManifest:
    """Parse and strictly validate one TOML exam manifest."""
    try:
        with path.open("rb") as manifest_file:
            payload = tomllib.load(manifest_file)
    except OSError as error:
        raise VisionExamError(f"无法读取考卷清单 {path}：{error}") from error
    except tomllib.TOMLDecodeError as error:
        raise VisionExamError(f"考卷清单 TOML 无法解析：{error}") from error

    version = payload.get("version")
    if version != 1:
        raise VisionExamError("考卷清单 version 必须为 1")
    unexpected_top_level = set(payload) - {"version", "questions"}
    if unexpected_top_level:
        joined = "、".join(sorted(unexpected_top_level))
        raise VisionExamError(f"考卷清单含不允许的顶层字段：{joined}")
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise VisionExamError("考卷清单至少需要一道 [[questions]]")

    base_directory = path.resolve().parent
    questions: list[ExamQuestion] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_questions, start=1):
        if not isinstance(raw, Mapping):
            raise VisionExamError(f"第 {index} 道题不是 TOML 表")
        question = _parse_question(raw, index=index, base_directory=base_directory)
        if question.question_id in seen_ids:
            raise VisionExamError(f"题号重复：{question.question_id}")
        seen_ids.add(question.question_id)
        questions.append(question)
    return ExamManifest(path=path.resolve(), questions=tuple(questions))


def _parse_question(
    raw: Mapping[str, object],
    *,
    index: int,
    base_directory: Path,
) -> ExamQuestion:
    question_id = _required_text(raw, "id", index=index)
    unexpected_fields = set(raw) - QUESTION_FIELDS
    if unexpected_fields:
        joined = "、".join(sorted(unexpected_fields))
        raise VisionExamError(f"题 {question_id} 含不允许字段：{joined}")
    raw_type = _required_text(raw, "type", index=index)
    if raw_type not in QUESTION_TYPES:
        raise VisionExamError(f"题 {question_id} 的 type 必须为 single 或 sequence")
    question_type: Literal["single", "sequence"] = (
        "single" if raw_type == "single" else "sequence"
    )

    frames = _path_list(raw.get("frames"), "frames", question_id, base_directory)
    seconds = _number_list(raw.get("seconds"), "seconds", question_id)
    if len(frames) != len(seconds):
        raise VisionExamError(f"题 {question_id} 的 frames 与 seconds 数量不一致")
    if question_type == "single" and len(frames) != 1:
        raise VisionExamError(f"single 题 {question_id} 必须且只能有一帧")
    if question_type == "sequence" and len(frames) < 2:
        raise VisionExamError(f"sequence 题 {question_id} 至少需要两帧")
    if any(second < 0 for second in seconds):
        raise VisionExamError(f"题 {question_id} 的 seconds 不得为负数")
    if any(current <= previous for previous, current in zip(seconds, seconds[1:])):
        raise VisionExamError(f"题 {question_id} 的 seconds 必须严格递增")

    region_grid = (
        _region_grid(raw["region_grid"], question_id)
        if "region_grid" in raw
        else None
    )
    game_context = _optional_text(
        raw.get("game_context"),
        "game_context",
        question_id,
    )
    return ExamQuestion(
        question_id=question_id,
        question_type=question_type,
        game_context=game_context,
        frames=frames,
        relative_seconds=seconds,
        region_grid=region_grid,
    )


def _required_text(raw: Mapping[str, object], key: str, *, index: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise VisionExamError(f"第 {index} 道题的 {key} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, key: str, question_id: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise VisionExamError(f"题 {question_id} 的 {key} 必须是非空字符串")
    return value.strip()


def _region_grid(value: object, question_id: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise VisionExamError(f"题 {question_id} 的 region_grid 必须是格子坐标列表")
    cells: list[str] = []
    for item in value:
        if not isinstance(item, str) or REGION_CELL_PATTERN.fullmatch(item) is None:
            raise VisionExamError(
                f"题 {question_id} 的 region_grid 含无效格子坐标：{item!r}"
            )
        if item in cells:
            raise VisionExamError(f"题 {question_id} 的 region_grid 含重复坐标：{item}")
        cells.append(item)
    return tuple(cells)


def _path_list(
    value: object,
    key: str,
    question_id: str,
    base_directory: Path,
    *,
    allow_empty: bool = False,
) -> tuple[Path, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise VisionExamError(f"题 {question_id} 的 {key} 必须是非空路径列表")
    paths: list[Path] = []
    for raw_path in value:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise VisionExamError(f"题 {question_id} 的 {key} 含无效路径")
        candidate = (base_directory / raw_path).resolve()
        if not candidate.is_file():
            raise VisionExamError(f"题 {question_id} 缺少文件：{candidate}")
        paths.append(candidate)
    return tuple(paths)


def _number_list(value: object, key: str, question_id: str) -> tuple[float, ...]:
    if not isinstance(value, list) or not value:
        raise VisionExamError(f"题 {question_id} 的 {key} 必须是非空数字列表")
    numbers: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            raise VisionExamError(f"题 {question_id} 的 {key} 含非数字值")
        number = float(item)
        if not math.isfinite(number):
            raise VisionExamError(f"题 {question_id} 的 {key} 含非有限数")
        numbers.append(number)
    return tuple(numbers)


def build_timeline(question: ExamQuestion) -> str:
    """Describe observed frames and explicit unsampled gaps without inventing motion."""
    if question.question_type != "sequence":
        return ""
    parts: list[str] = []
    for index, second in enumerate(question.relative_seconds):
        if index:
            gap_start = question.relative_seconds[index - 1] + 0.1
            if gap_start < second:
                parts.append(f"第{gap_start:.1f}至{second:.1f}秒未采样")
        parts.append(f"第{second:.1f}秒：帧{index + 1}")
    return (
        "这是稀疏采样截图，不是连续视频。时间轴："
        + "；".join(parts)
        + "。不得把未采样间隔中的动作当作已看见的事实。"
    )


def build_user_prompt(question: ExamQuestion, variant: ExamVariant) -> str:
    """Build the text portion associated with one question variant."""
    parts: list[str] = []
    if question.game_context is not None:
        parts.append(
            "已知上下文（由窗口标题与进程名确定）："
            f"游戏名为 {question.game_context}。"
        )
    timeline = build_timeline(question)
    if timeline:
        parts.append(timeline)
    if should_inject_region(question, variant):
        parts.append(
            f"画面被划分为 {REGION_ROWS} 行 {REGION_COLUMNS} 列的网格。"
            "与上一采样帧相比，以下格子发生了变化："
            + "、".join(question.region_grid)
            + "。"
        )
    return "\n".join(parts)


def region_grid_fraction(question: ExamQuestion) -> float | None:
    """Return the objective share of the fixed grid marked as changed."""
    if question.region_grid is None:
        return None
    return len(question.region_grid) / REGION_CELL_COUNT


def should_inject_region(question: ExamQuestion, variant: ExamVariant) -> bool:
    """Apply the selected production sparsity policy to available grid data."""
    if not question.region_grid or variant.region_mode == "off":
        return False
    if variant.region_mode == "always":
        return True
    fraction = region_grid_fraction(question)
    return fraction is not None and fraction <= variant.region_sparsity_max


def build_images(question: ExamQuestion, variant: ExamVariant) -> tuple[LlmImage, ...]:
    """Select only ordered full frames for one request."""
    target_width = None if variant.send_width == 0 else variant.send_width
    return tuple(
        LlmImage(
            path=path,
            label=f"全图帧{index}（相对第{second:.1f}秒）",
            target_width=target_width,
        )
        for index, (path, second) in enumerate(
            zip(question.frames, question.relative_seconds),
            start=1,
        )
    )


def build_variants(
    *,
    send_widths: Sequence[int],
    region_modes: Sequence[str],
    output_modes: Sequence[str],
    region_sparsity_max: float,
) -> tuple[ExamVariant, ...]:
    """Build a stable Cartesian product while removing repeated switches."""
    widths = tuple(dict.fromkeys(send_widths or (1280,)))
    if any(width < 0 for width in widths):
        raise VisionExamError("--send-width 不得为负数；0 表示原生分辨率")
    if not 0.0 <= region_sparsity_max <= 1.0:
        raise VisionExamError("--region-sparsity-max 必须在 0–1")
    raw_modes = tuple(dict.fromkeys(region_modes or ("off",)))
    if any(mode not in REGION_MODES for mode in raw_modes):
        raise VisionExamError("--region-mode 必须是 off、sparse 或 always")
    modes = tuple(cast(RegionMode, mode) for mode in raw_modes)
    raw_output_modes = tuple(dict.fromkeys(output_modes or ("fast",)))
    if any(mode not in CLI_OUTPUT_MODES for mode in raw_output_modes):
        raise VisionExamError("--output-mode 必须是 fast 或 deep")
    selected_output_modes = tuple(
        cast(OutputMode, mode) for mode in raw_output_modes
    )
    return tuple(
        ExamVariant(width, mode, region_sparsity_max, output_mode)
        for output_mode in selected_output_modes
        for width in widths
        for mode in modes
    )


def build_formal_variants() -> tuple[ExamVariant, ...]:
    """Return the four deliberately pruned M5-T2.9 production-shape variants."""
    return (
        ExamVariant(1280, "off", DEFAULT_REGION_SPARSITY_MAX, "fast"),
        ExamVariant(1280, "sparse", DEFAULT_REGION_SPARSITY_MAX, "fast"),
        ExamVariant(1280, "off", DEFAULT_REGION_SPARSITY_MAX, "deep"),
        ExamVariant(0, "off", DEFAULT_REGION_SPARSITY_MAX, "deep"),
    )


def build_speed_round_variants() -> tuple[ExamVariant, ...]:
    """Return the three fixed M5-T2.10 fast/sparse upload widths."""
    return tuple(
        ExamVariant(width, "sparse", DEFAULT_REGION_SPARSITY_MAX, "fast")
        for width in (1280, 896, 640)
    )


def estimate_formal_cost(
    *,
    question_count: int,
    variants: Sequence[ExamVariant],
    targets: Sequence[ModelTarget],
    estimated_input_tokens_per_attempt: int,
    repetitions: int = 1,
    include_relaxed: bool = True,
) -> CostEstimate:
    """Estimate worst-case cost, including every model's possible relaxed rerun."""
    if question_count <= 0:
        raise VisionExamError("预计花费需要至少一道题")
    if estimated_input_tokens_per_attempt <= 0:
        raise VisionExamError("--estimated-input-tokens 必须大于 0")
    if repetitions <= 0:
        raise VisionExamError("重复遍数必须大于 0")
    base_calls_per_model = question_count * len(variants) * repetitions
    fast_variants = [variant for variant in variants if variant.output_mode == "fast"]
    relaxed_calls_per_model = (
        question_count * len(fast_variants) if include_relaxed else 0
    )
    estimated_input_tokens = 0
    maximum_output_tokens = 0
    estimated_cost_usd = 0.0
    for target in targets:
        target_input_tokens = estimated_input_tokens_per_attempt * (
            base_calls_per_model + relaxed_calls_per_model
        )
        target_output_tokens = question_count * repetitions * sum(
            variant.max_tokens for variant in variants
        ) + relaxed_calls_per_model * FAST_RELAXED_MAX_TOKENS
        estimated_input_tokens += target_input_tokens
        maximum_output_tokens += target_output_tokens
        estimated_cost_usd += (
            target_input_tokens * target.price.input_per_million_usd
            + target_output_tokens * target.price.output_per_million_usd
        ) / 1_000_000
    return CostEstimate(
        base_calls=base_calls_per_model * len(targets),
        maximum_calls_with_relaxed=(
            base_calls_per_model + relaxed_calls_per_model
        )
        * len(targets),
        estimated_input_tokens=estimated_input_tokens,
        maximum_output_tokens=maximum_output_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )


def run_exam(
    *,
    manifest: ExamManifest,
    variants: Sequence[ExamVariant],
    targets: Sequence[ModelTarget],
    client_factory: ClientFactory,
) -> tuple[ExamRecord, ...]:
    """Run ordinary variants without the formal-run cost guard or relaxed reruns."""
    return run_formal_exam(
        manifest=manifest,
        variants=variants,
        targets=targets,
        client_factory=client_factory,
        cost_cap_usd=None,
        enable_relaxed=False,
    ).records


def run_formal_exam(
    *,
    manifest: ExamManifest,
    variants: Sequence[ExamVariant],
    targets: Sequence[ModelTarget],
    client_factory: ClientFactory,
    cost_cap_usd: float | None,
    enable_relaxed: bool = True,
    repetitions: int = 1,
    streaming: bool = False,
) -> ExamRunOutcome:
    """Run all targets, preserve failures, guard cost, and rerun truncated fast groups."""
    if repetitions <= 0:
        raise VisionExamError("重复遍数必须大于 0")
    system_prompts = {
        output_mode: _read_prompt(prompt_path)
        for output_mode, prompt_path in OUTPUT_PROMPT_PATHS.items()
    }
    records: list[ExamRecord] = []
    actual_cost_usd = 0.0
    stopped = False
    relaxed_models: list[str] = []

    def append_record(record: ExamRecord) -> None:
        nonlocal actual_cost_usd, stopped
        records.append(record)
        actual_cost_usd += _record_actual_cost(record)
        if (
            cost_cap_usd is not None
            and actual_cost_usd > cost_cap_usd * 1.5
        ):
            stopped = True

    for target in targets:
        target_start = len(records)
        stream_for_target = streaming
        client: LlmVisionClientProtocol | None = None
        initialization_error: str | None = None
        try:
            client = client_factory(target)
        except Exception as error:
            initialization_error = f"客户端初始化失败：{error}"
        try:
            for repetition in range(1, repetitions + 1):
                for question in manifest.questions:
                    for variant in variants:
                        if initialization_error is not None or client is None:
                            append_record(
                                _failed_record(
                                    question,
                                    variant,
                                    target,
                                    initialization_error or "客户端不可用",
                                    repetition=repetition,
                                )
                            )
                            continue
                        record = _run_attempt(
                            client=client,
                            question=question,
                            variant=variant,
                            target=target,
                            system_prompt=system_prompts[variant.output_mode],
                            streaming=stream_for_target,
                            repetition=repetition,
                        )
                        append_record(record)
                        if stream_for_target and record.succeeded and not record.streamed:
                            stream_for_target = False
                        if stopped:
                            break
                    if stopped:
                        break
                if stopped:
                    break
            target_records = records[target_start:]
            base_fast = [
                record
                for record in target_records
                if record.output_mode == "fast"
            ]
            truncated_fraction = (
                sum(record.truncated for record in base_fast)
                / len(base_fast)
                if base_fast
                else 0.0
            )
            if (
                not stopped
                and enable_relaxed
                and truncated_fraction > FAST_RELAXED_TRIGGER_FRACTION
            ):
                relaxed_models.append(target.label)
                relaxed_variants = tuple(
                    ExamVariant(
                        variant.send_width,
                        variant.region_mode,
                        variant.region_sparsity_max,
                        "fast-relaxed",
                    )
                    for variant in variants
                    if variant.output_mode == "fast"
                )
                for question in manifest.questions:
                    for variant in relaxed_variants:
                        if client is None:
                            break
                        append_record(
                            _run_attempt(
                                client=client,
                                question=question,
                                variant=variant,
                                target=target,
                                system_prompt=system_prompts["fast-relaxed"],
                                streaming=False,
                                repetition=1,
                            )
                        )
                        if stopped:
                            break
                    if stopped:
                        break
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        if stopped:
            break
    return ExamRunOutcome(
        records=tuple(records),
        actual_cost_usd=actual_cost_usd,
        cost_guard_stopped=stopped,
        relaxed_models=tuple(relaxed_models),
    )


def _record_actual_cost(record: ExamRecord) -> float:
    if record.upstream_cost_usd is not None:
        return record.upstream_cost_usd
    if record.configured_cost_usd is not None:
        return record.configured_cost_usd
    return 0.0


def _run_attempt(
    *,
    client: LlmVisionClientProtocol,
    question: ExamQuestion,
    variant: ExamVariant,
    target: ModelTarget,
    system_prompt: str,
    streaming: bool = False,
    repetition: int = 1,
) -> ExamRecord:
    images = build_images(question, variant)
    region_fraction = region_grid_fraction(question)
    region_injected = should_inject_region(question, variant)
    upload_metadata = ()
    try:
        upload_metadata = tuple(
            image_upload_metadata(image, max_image_edge=None) for image in images
        )
        call_arguments = {
            "model": target.model,
            "provider": target.provider,
            "system_prompt": system_prompt,
            "user_prompt": build_user_prompt(question, variant),
            "images": images,
            "max_image_edge": None,
            "max_tokens": variant.max_tokens,
            "temperature": target.temperature,
            "reasoning_effort": (
                "none" if target.reasoning_parameter_mode == "effort_none" else None
            ),
            "reasoning_enabled": (
                False
                if target.reasoning_disabled
                or target.reasoning_parameter_mode == "enabled_false"
                else None
            ),
        }
        if streaming:
            try:
                result = client.complete_with_images_stream(**call_arguments)
            except LlmStreamingUnsupported:
                # An explicit lack of SSE support is a transport capability result,
                # not a model failure. The required fallback remains one normal call.
                result = client.complete_with_images(**call_arguments)
        else:
            result = client.complete_with_images(**call_arguments)
    except Exception as error:
        latency = (
            error.latency_seconds * 1000
            if isinstance(error, LlmError) and error.latency_seconds is not None
            else None
        )
        return _failed_record(
            question,
            variant,
            target,
            str(error),
            latency_ms=latency,
            region_injected=region_injected,
            image_dimensions=tuple(
                f"{metadata.width}x{metadata.height}" for metadata in upload_metadata
            ),
            image_byte_sizes=tuple(
                metadata.byte_size for metadata in upload_metadata
            ),
            provider=(error.provider if isinstance(error, LlmError) else None),
            repetition=repetition,
        )

    # JSON 外壳实测约耗 25–30 个输出 token（约 0.7 秒），快线 60-token
    # 初始预算付不起；游戏身份也已经由窗口标题查表确定，因此两线都保留纯文本。
    return _record_from_result(
        question,
        variant,
        target,
        result,
        error=None,
        region_fraction=region_fraction,
        region_injected=region_injected,
        image_dimensions=tuple(
            f"{metadata.width}x{metadata.height}" for metadata in upload_metadata
        ),
        image_byte_sizes=tuple(metadata.byte_size for metadata in upload_metadata),
        repetition=repetition,
    )


def _record_from_result(
    question: ExamQuestion,
    variant: ExamVariant,
    target: ModelTarget,
    result: LlmResult,
    *,
    error: str | None,
    region_fraction: float | None,
    region_injected: bool,
    image_dimensions: tuple[str, ...],
    image_byte_sizes: tuple[int, ...],
    repetition: int,
) -> ExamRecord:
    visible_tokens = _visible_output_tokens(result)
    visible_empty = not result.text.strip()
    truncated = variant.output_mode.startswith("fast") and (
        visible_empty or result.finish_reason in {"length", "max_tokens"}
    )
    return ExamRecord(
        question_id=question.question_id,
        question_type=question.question_type,
        variant=variant.name,
        output_mode=variant.output_mode,
        max_tokens=variant.max_tokens,
        upload_width=variant.send_width,
        region_mode=variant.region_mode,
        region_grid_fraction=region_fraction,
        region_injected=region_injected,
        image_dimensions=image_dimensions,
        image_byte_sizes=image_byte_sizes,
        target_label=target.label,
        requested_model=target.model,
        actual_model=result.model,
        provider=result.provider or target.provider_display_name or target.provider,
        provider_region=target.provider_region,
        endpoint_host=target.endpoint_host,
        response_text=result.text,
        error=error,
        skipped=False,
        latency_ms=result.latency_seconds * 1000,
        input_tokens=result.usage.prompt_tokens,
        output_tokens=result.usage.completion_tokens,
        configured_cost_usd=_configured_cost(result, target.price),
        upstream_cost_usd=result.usage.cost_usd,
        reasoning_tokens=result.usage.reasoning_tokens,
        visible_output_tokens=visible_tokens,
        visible_output_empty=visible_empty,
        truncated=truncated,
        fast_relaxed=variant.output_mode == "fast-relaxed",
        finish_reason=result.finish_reason,
        ttft_ms=(
            result.ttft_seconds * 1000
            if result.ttft_seconds is not None
            else None
        ),
        streamed=result.streamed,
        repetition=repetition,
    )


def _failed_record(
    question: ExamQuestion,
    variant: ExamVariant,
    target: ModelTarget,
    error: str,
    *,
    latency_ms: float | None = None,
    region_injected: bool | None = None,
    image_dimensions: tuple[str, ...] = (),
    image_byte_sizes: tuple[int, ...] = (),
    provider: str | None = None,
    repetition: int = 1,
) -> ExamRecord:
    return ExamRecord(
        question_id=question.question_id,
        question_type=question.question_type,
        variant=variant.name,
        output_mode=variant.output_mode,
        max_tokens=variant.max_tokens,
        upload_width=variant.send_width,
        region_mode=variant.region_mode,
        region_grid_fraction=region_grid_fraction(question),
        region_injected=(
            should_inject_region(question, variant)
            if region_injected is None
            else region_injected
        ),
        image_dimensions=image_dimensions,
        image_byte_sizes=image_byte_sizes,
        target_label=target.label,
        requested_model=target.model,
        actual_model=None,
        provider=(
            provider
            or target.provider_display_name
            or target.provider
        ),
        provider_region=target.provider_region,
        endpoint_host=target.endpoint_host,
        response_text="",
        error=error,
        skipped=False,
        latency_ms=latency_ms,
        input_tokens=None,
        output_tokens=None,
        configured_cost_usd=None,
        upstream_cost_usd=None,
        reasoning_tokens=None,
        visible_output_tokens=None,
        visible_output_empty=True,
        truncated=False,
        fast_relaxed=variant.output_mode == "fast-relaxed",
        finish_reason=None,
        ttft_ms=None,
        streamed=False,
        repetition=repetition,
    )


def _visible_output_tokens(result: LlmResult) -> int | None:
    completion = result.usage.completion_tokens
    if completion is None:
        return None
    reasoning = result.usage.reasoning_tokens or 0
    return max(completion - reasoning, 0)


def _configured_cost(result: LlmResult, price: ModelPrice | None) -> float | None:
    if price is None:
        return None
    prompt_tokens = result.usage.prompt_tokens
    completion_tokens = result.usage.completion_tokens
    if prompt_tokens is None or completion_tokens is None:
        return None
    return (
        prompt_tokens * price.input_per_million_usd
        + completion_tokens * price.output_per_million_usd
    ) / 1_000_000


CSV_COLUMNS = (
    "题号",
    "题型",
    "变体",
    "输出模式",
    "max_tokens",
    "上传宽度",
    "区域提示模式",
    "本题变化格子占比",
    "本次是否实际注入了提示",
    "本次实际上传的图像像素尺寸",
    "本次实际上传的图像字节数",
    "目标档位",
    "请求模型",
    "实际模型",
    "服务商",
    "实际上游",
    "实际上游地区",
    "端点主机名",
    "TTFT毫秒",
    "是否流式",
    "第几遍",
    "回答原文",
    "错误原文",
    "是否跳过",
    "往返毫秒",
    "输入token",
    "实际输出token",
    "推理token",
    "可见输出token",
    "可见输出是否为空",
    "是否截断",
    "是否fast-relaxed重跑",
    "结束原因",
    "配置折算花费美元",
    "上游报告花费美元",
)


def write_outputs(
    *,
    output_directory: Path,
    records: Sequence[ExamRecord],
    run_payload: Mapping[str, object],
    answer_key_path: Path,
    speed_round: bool = False,
) -> None:
    """Write machine records, split human grading batches, and self-contained run data."""
    output_directory.mkdir(parents=True, exist_ok=False)
    _write_csv(output_directory / "results.csv", records)
    summary = summarize(records)
    answers = load_answer_key(answer_key_path)
    if speed_round:
        (output_directory / "grading-fast.md").write_text(
            render_speed_grading_sheet(records, answers),
            encoding="utf-8",
        )
        summary_text = render_speed_summary(summary)
    else:
        (output_directory / "grading-fast.md").write_text(
            render_grading_sheet(records, answers, batch="fast"),
            encoding="utf-8",
        )
        (output_directory / "grading-deep.md").write_text(
            render_grading_sheet(records, answers, batch="deep"),
            encoding="utf-8",
        )
        summary_text = render_machine_summary(summary)
    (output_directory / "summary.md").write_text(summary_text, encoding="utf-8")
    complete_payload = dict(run_payload)
    complete_payload["summary"] = summary
    (output_directory / "run.json").write_text(
        json.dumps(complete_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True, slots=True)
class AnswerKeyEntry:
    """Only the authoritative bullets needed beside human grading rows."""

    question_id: str
    core: tuple[str, ...]
    details: tuple[str, ...]
    doubtful: tuple[str, ...]
    forbidden: tuple[str, ...]


def load_answer_key(path: Path) -> Mapping[str, AnswerKeyEntry]:
    """Extract authoritative scoring bullets without changing the answer-key source."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise VisionExamError(f"无法读取答案键 {path}：{error}") from error
    sections = re.split(r"(?=^## \d+\. `)", text, flags=re.MULTILINE)[1:]
    entries: dict[str, AnswerKeyEntry] = {}
    for section in sections:
        question_id = section.split("`", 2)[1]
        owner = _markdown_subsection(section, "产品负责人判定", "离线复核")
        forbidden = _markdown_subsection(section, "不得出现的内容", "不确定项")
        entry = AnswerKeyEntry(
            question_id=question_id,
            core=_tagged_bullets(owner, "核心"),
            details=_tagged_bullets(owner, "细节"),
            doubtful=_tagged_bullets(owner, "存疑"),
            forbidden=tuple(
                line.removeprefix("- ").strip()
                for line in forbidden.splitlines()
                if line.startswith("- ")
            ),
        )
        if not entry.core or not entry.forbidden:
            raise VisionExamError(f"答案键题 {question_id} 缺少核心或不得出现条目")
        entries[question_id] = entry
    if not entries:
        raise VisionExamError("答案键没有可解析题目")
    return entries


def _markdown_subsection(section: str, heading: str, next_heading: str) -> str:
    marker = f"### {heading}"
    next_marker = f"### {next_heading}"
    if marker not in section or next_marker not in section:
        raise VisionExamError(f"答案键小节不完整：{heading}")
    return section.split(marker, 1)[1].split(next_marker, 1)[0]


def _tagged_bullets(text: str, tag: str) -> tuple[str, ...]:
    prefix = f"- 【{tag}】"
    return tuple(
        line.removeprefix("- ").strip()
        for line in text.splitlines()
        if line.startswith(prefix)
    )


def render_grading_sheet(
    records: Sequence[ExamRecord],
    answers: Mapping[str, AnswerKeyEntry],
    *,
    batch: Literal["fast", "deep"],
) -> str:
    """Render one unscored batch with authoritative bullets beside every question."""
    selected_modes = {"fast", "fast-relaxed"} if batch == "fast" else {"deep"}
    selected = [record for record in records if record.output_mode in selected_modes]
    lines = [
        f"# M5-T2.9 {'快线' if batch == 'fast' else '深线'}人工判卷表",
        "",
        "准确性判定、漏了什么、编造了什么由产品负责人填写；以下人工列均为空。",
    ]
    ordered_question_ids = tuple(dict.fromkeys(record.question_id for record in selected))
    for question_id in ordered_question_ids:
        answer = answers.get(question_id)
        if answer is None:
            raise VisionExamError(f"答案键缺少题目 {question_id}")
        question_records = [
            record for record in selected if record.question_id == question_id
        ]
        lines.extend(
            (
                "",
                f"## `{question_id}`",
                "",
                "| 模型 | 变体 | max_tokens | 推理token | 可见输出token | 是否截断 | 回答原文 | 错误原文 | 准确性判定 | 漏了什么 | 编造了什么 |",
                "|---|---|---:|---:|---:|---|---|---|---|---|---|",
            )
        )
        for record in question_records:
            lines.append(
                "| "
                + " | ".join(
                    (
                        _markdown_cell(record.target_label),
                        _markdown_cell(record.variant),
                        str(record.max_tokens),
                        _display_number(record.reasoning_tokens, 0),
                        _display_number(record.visible_output_tokens, 0),
                        str(record.truncated).lower(),
                        _markdown_cell(record.response_text),
                        _markdown_cell(record.error or ""),
                        "",
                        "",
                        "",
                    )
                )
                + " |"
            )
        scoring_points = answer.core + (answer.details if batch == "deep" else ())
        lines.extend(("", "### 计分要点", ""))
        lines.extend(f"- {point}" for point in scoring_points)
        if batch == "deep":
            lines.extend(("", "### 【存疑】（不计分）", ""))
            lines.extend(f"- {point}" for point in answer.doubtful)
            if not answer.doubtful:
                lines.append("- 无")
        lines.extend(("", "### 不得出现的内容", ""))
        lines.extend(f"- {point}" for point in answer.forbidden)
    return "\n".join(lines) + "\n"


def render_speed_grading_sheet(
    records: Sequence[ExamRecord],
    answers: Mapping[str, AnswerKeyEntry],
) -> str:
    """Render only repetition one, grouped by model and width for T2.10."""
    selected = [
        record
        for record in records
        if record.output_mode == "fast" and record.repetition == 1
    ]
    lines = [
        "# M5-T2.10 快线人工判卷表（第一遍）",
        "",
        "准确性判定、漏了什么、编造了什么由产品负责人填写；以下人工列均为空。",
    ]
    configurations = tuple(
        dict.fromkeys((record.target_label, record.upload_width) for record in selected)
    )
    for model, width in configurations:
        width_label = "原生" if width == 0 else str(width)
        lines.extend(("", f"## `{model}` × 宽度 {width_label}", ""))
        configuration_records = [
            record
            for record in selected
            if record.target_label == model and record.upload_width == width
        ]
        for record in configuration_records:
            answer = answers.get(record.question_id)
            if answer is None:
                raise VisionExamError(f"答案键缺少题目 {record.question_id}")
            lines.extend(
                (
                    f"### `{record.question_id}`",
                    "",
                    "| 实际上游 | TTFT(ms) | 总时延(ms) | 是否截断 | 回答原文 | "
                    "错误原文 | 准确性判定 | 漏了什么 | 编造了什么 |",
                    "|---|---:|---:|---|---|---|---|---|---|",
                    "| "
                    + " | ".join(
                        (
                            _markdown_cell(record.provider or ""),
                            _display_number(record.ttft_ms, 3),
                            _display_number(record.latency_ms, 3),
                            str(record.truncated).lower(),
                            _markdown_cell(record.response_text),
                            _markdown_cell(record.error or ""),
                            "",
                            "",
                            "",
                        )
                    )
                    + " |",
                    "",
                    "#### 【核心】要点",
                    "",
                )
            )
            lines.extend(f"- {point}" for point in answer.core)
            lines.extend(("", "#### 不得出现的内容", ""))
            lines.extend(f"- {point}" for point in answer.forbidden)
            lines.append("")
    return "\n".join(lines) + "\n"


def render_machine_summary(summary: Mapping[str, object]) -> str:
    """Render objective statistics only; no ranking or quality language is generated."""
    lines = [
        "# M5-T2.9 机器统计",
        "",
        "## 按模型",
        "",
        "| 模型 | 调用数 | 失败数 | 截断数 | 延迟中位(ms) | 延迟P90(ms) | 延迟最大(ms) | 平均输入token | 推理token合计 | 平均可见输出token | 总花费(USD) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    _append_machine_rows(lines, summary.get("models"))
    lines.extend(
        (
            "",
            "## 按输出模式",
            "",
        "| 输出模式 | 调用数 | 失败数 | 截断数 | 延迟中位(ms) | 延迟P90(ms) | "
        "延迟最大(ms) | 平均输入token | 推理token合计 | 平均可见输出token | 总花费(USD) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        )
    )
    _append_machine_rows(lines, summary.get("output_modes"))
    lines.extend(
        (
            "",
            "## 按上传宽度",
            "",
            "| 上传宽度 | 调用数 | 平均输入token | 延迟中位(ms) | 延迟P90(ms) | 延迟最大(ms) |",
            "|---|---:|---:|---:|---:|---:|",
        )
    )
    widths = summary.get("upload_widths")
    if isinstance(widths, Mapping):
        for name, raw in widths.items():
            if isinstance(raw, Mapping):
                lines.append(
                    f"| {_markdown_cell(str(name))} | {raw.get('attempts', 0)} | "
                    f"{_display_number(raw.get('average_input_tokens_per_attempt'), 2)} | "
                    f"{_display_number(raw.get('latency_median_ms'), 3)} | "
                    f"{_display_number(raw.get('latency_p90_ms'), 3)} | "
                    f"{_display_number(raw.get('latency_max_ms'), 3)} |"
                )
    sparse = summary.get("sparse")
    lines.extend(("", "## sparse 区域提示", ""))
    if isinstance(sparse, Mapping):
        lines.append(f"- 调用数：{sparse.get('attempts', 0)}")
        lines.append(f"- 实际注入数：{sparse.get('injected', 0)}")
        lines.append(
            f"- 实际注入率：{_display_number(sparse.get('injection_rate'), 6)}"
        )
    lines.extend(
        (
            "",
            "## 全部调用",
            "",
            f"- 失败数：{summary.get('failures', 0)}",
            f"- 截断数：{summary.get('truncated', 0)}",
            f"- 实际总花费（USD）：{_display_number(summary.get('total_actual_cost_usd'), 9)}",
        )
    )
    relaxed_models = summary.get("relaxed_models")
    if isinstance(relaxed_models, list) and relaxed_models:
        lines.extend(("", "## fast-relaxed 重跑", ""))
        for model in relaxed_models:
            lines.append(
                f"- `{model}`：该模型的快线受推理预算影响，60 token 档不可比。"
            )
    return "\n".join(lines) + "\n"


def render_speed_summary(summary: Mapping[str, object]) -> str:
    """Render only objective M5-T2.10 latency, stability, and cost statistics."""
    lines = [
        "# M5-T2.10 快线提速轮机器统计",
        "",
        "## 模型 × 上传宽度",
        "",
        "| 模型 × 宽度 | 调用数 | TTFT非空率 | TTFT中位/P90/最大(ms) | "
        "总时延中位/P90/最大(ms) | 失败率 | 截断率 |",
        "|---|---:|---:|---|---|---:|---:|",
    ]
    model_widths = summary.get("model_widths")
    if isinstance(model_widths, Mapping):
        for name, raw in model_widths.items():
            if not isinstance(raw, Mapping):
                continue
            lines.append(
                f"| {_markdown_cell(str(name))} | {raw.get('attempts', 0)} | "
                f"{_display_number(raw.get('ttft_nonempty_rate'), 6)} | "
                f"{_display_number(raw.get('ttft_median_ms'), 3)} / "
                f"{_display_number(raw.get('ttft_p90_ms'), 3)} / "
                f"{_display_number(raw.get('ttft_max_ms'), 3)} | "
                f"{_display_number(raw.get('latency_median_ms'), 3)} / "
                f"{_display_number(raw.get('latency_p90_ms'), 3)} / "
                f"{_display_number(raw.get('latency_max_ms'), 3)} | "
                f"{_display_number(raw.get('failure_rate'), 6)} | "
                f"{_display_number(raw.get('truncated_rate'), 6)} |"
            )
    lines.extend(
        (
            "",
            "## 两遍稳定性",
            "",
            "| 模型 × 宽度 | 第一遍中位(ms) | 第二遍中位(ms) | "
            "中位差：第二遍-第一遍(ms) | 绝对差(ms) |",
            "|---|---:|---:|---:|---:|",
        )
    )
    stability = summary.get("repetition_stability")
    if isinstance(stability, Mapping):
        for name, raw in stability.items():
            if not isinstance(raw, Mapping):
                continue
            medians = raw.get("repetition_medians_ms")
            median_map = medians if isinstance(medians, Mapping) else {}
            lines.append(
                f"| {_markdown_cell(str(name))} | "
                f"{_display_number(median_map.get('1'), 3)} | "
                f"{_display_number(median_map.get('2'), 3)} | "
                f"{_display_number(raw.get('median_difference_ms'), 3)} | "
                f"{_display_number(raw.get('absolute_median_difference_ms'), 3)} |"
            )
    lines.extend(
        (
            "",
            "## 按上传宽度",
            "",
            "| 宽度 | 调用数 | 平均输入token | 平均单帧花费(USD) |",
            "|---|---:|---:|---:|",
        )
    )
    widths = summary.get("upload_widths")
    if isinstance(widths, Mapping):
        for name, raw in widths.items():
            if isinstance(raw, Mapping):
                lines.append(
                    f"| {_markdown_cell(str(name))} | {raw.get('attempts', 0)} | "
                    f"{_display_number(raw.get('average_input_tokens_per_attempt'), 2)} | "
                    f"{_display_number(raw.get('average_cost_per_frame_usd'), 9)} |"
                )
    lines.extend(("", "## 流式与实际上游", ""))
    total_attempts = sum(
        int(raw.get("attempts", 0))
        for raw in model_widths.values()
        if isinstance(raw, Mapping)
    ) if isinstance(model_widths, Mapping) else 0
    lines.append(f"- TTFT 非空：{summary.get('ttft_nonempty', 0)}/{total_attempts}")
    lines.append(f"- 流式完成：{summary.get('streamed', 0)}/{total_attempts}")
    providers = summary.get("providers")
    if isinstance(providers, Mapping):
        for name, raw in providers.items():
            if isinstance(raw, Mapping):
                lines.append(
                    f"- `{name}`：调用 {raw.get('attempts', 0)}，"
                    f"TTFT 非空率 {_display_number(raw.get('ttft_nonempty_rate'), 6)}"
                )
    high_failure: list[tuple[str, float]] = []
    if isinstance(model_widths, Mapping):
        for name, raw in model_widths.items():
            if isinstance(raw, Mapping):
                rate = raw.get("failure_rate")
                if isinstance(rate, (int, float)) and rate > 0.30:
                    high_failure.append((str(name), float(rate)))
    lines.extend(("", "## 失败率超过 30% 的配置", ""))
    if high_failure:
        lines.extend(
            f"- `{name}`：{rate:.6f}" for name, rate in high_failure
        )
    else:
        lines.append("- 无")
    lines.extend(
        (
            "",
            "## 全部调用",
            "",
            f"- 失败数：{summary.get('failures', 0)}",
            f"- 截断数：{summary.get('truncated', 0)}",
            f"- 实际总花费（USD）："
            f"{_display_number(summary.get('total_actual_cost_usd'), 9)}",
        )
    )
    return "\n".join(lines) + "\n"


def _append_machine_rows(lines: list[str], raw_groups: object) -> None:
    if not isinstance(raw_groups, Mapping):
        return
    for name, raw in raw_groups.items():
        if not isinstance(raw, Mapping):
            continue
        lines.append(
            f"| {_markdown_cell(str(name))} | {raw.get('attempts', 0)} | "
            f"{raw.get('failures', 0)} | {raw.get('truncated', 0)} | "
            f"{_display_number(raw.get('latency_median_ms'), 3)} | "
            f"{_display_number(raw.get('latency_p90_ms'), 3)} | "
            f"{_display_number(raw.get('latency_max_ms'), 3)} | "
            f"{_display_number(raw.get('average_input_tokens_per_attempt'), 2)} | "
            f"{raw.get('reasoning_tokens', 0)} | "
            f"{_display_number(raw.get('average_visible_output_tokens'), 2)} | "
            f"{_display_number(raw.get('total_actual_cost_usd'), 9)} |"
        )


def _write_csv(path: Path, records: Sequence[ExamRecord]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "题号": record.question_id,
                    "题型": record.question_type,
                    "变体": record.variant,
                    "输出模式": record.output_mode,
                    "max_tokens": record.max_tokens,
                    "上传宽度": record.upload_width,
                    "区域提示模式": record.region_mode,
                    "本题变化格子占比": (
                        ""
                        if record.region_grid_fraction is None
                        else f"{record.region_grid_fraction:.9f}"
                    ),
                    "本次是否实际注入了提示": str(record.region_injected).lower(),
                    "本次实际上传的图像像素尺寸": ";".join(record.image_dimensions),
                    "本次实际上传的图像字节数": ";".join(
                        str(value) for value in record.image_byte_sizes
                    ),
                    "目标档位": record.target_label,
                    "请求模型": record.requested_model,
                    "实际模型": record.actual_model or "",
                    "服务商": record.provider or "",
                    "实际上游": record.provider or "",
                    "实际上游地区": record.provider_region or "地区未知",
                    "端点主机名": record.endpoint_host or "",
                    "TTFT毫秒": _optional_number(record.ttft_ms, 3),
                    "是否流式": str(record.streamed).lower(),
                    "第几遍": record.repetition,
                    "回答原文": record.response_text,
                    "错误原文": record.error or "",
                    "是否跳过": str(record.skipped).lower(),
                    "往返毫秒": _optional_number(record.latency_ms, 3),
                    "输入token": record.input_tokens if record.input_tokens is not None else "",
                    "实际输出token": (
                        record.output_tokens
                        if record.output_tokens is not None
                        else ""
                    ),
                    "推理token": (
                        record.reasoning_tokens
                        if record.reasoning_tokens is not None
                        else ""
                    ),
                    "可见输出token": (
                        record.visible_output_tokens
                        if record.visible_output_tokens is not None
                        else ""
                    ),
                    "可见输出是否为空": str(record.visible_output_empty).lower(),
                    "是否截断": str(record.truncated).lower(),
                    "是否fast-relaxed重跑": str(record.fast_relaxed).lower(),
                    "结束原因": record.finish_reason or "",
                    "配置折算花费美元": _optional_number(record.configured_cost_usd, 9),
                    "上游报告花费美元": _optional_number(record.upstream_cost_usd, 9),
                }
            )


def summarize(records: Sequence[ExamRecord]) -> dict[str, object]:
    """Compute model, question, variant, width, and output-mode groups."""
    sparse_records = [record for record in records if record.region_mode == "sparse"]
    return {
        "models": _group_summary(records, lambda item: item.target_label),
        "questions": _group_summary(records, lambda item: item.question_id),
        "variants": _group_summary(records, lambda item: item.variant),
        "upload_widths": _group_summary(
            records,
            lambda item: "native" if item.upload_width == 0 else str(item.upload_width),
        ),
        "output_modes": _group_summary(records, lambda item: item.output_mode),
        "model_widths": _group_summary(
            records,
            lambda item: (
                f"{item.target_label} | "
                f"{'native' if item.upload_width == 0 else item.upload_width}"
            ),
        ),
        "repetition_stability": _repetition_stability(records),
        "providers": _group_summary(
            records, lambda item: item.provider or "未取得"
        ),
        "sparse": {
            "attempts": len(sparse_records),
            "injected": sum(record.region_injected for record in sparse_records),
            "injection_rate": (
                sum(record.region_injected for record in sparse_records)
                / len(sparse_records)
                if sparse_records
                else None
            ),
        },
        "total_actual_cost_usd": sum(_record_actual_cost(record) for record in records),
        "failures": sum(not record.succeeded and not record.skipped for record in records),
        "skipped": sum(record.skipped for record in records),
        "truncated": sum(record.truncated for record in records),
        "ttft_nonempty": sum(record.ttft_ms is not None for record in records),
        "streamed": sum(record.streamed for record in records),
        "relaxed_models": sorted(
            {record.target_label for record in records if record.fast_relaxed}
        ),
    }


def _group_summary(
    records: Sequence[ExamRecord],
    key: Callable[[ExamRecord], str],
) -> dict[str, dict[str, object]]:
    groups: dict[str, list[ExamRecord]] = {}
    for record in records:
        groups.setdefault(key(record), []).append(record)
    return {name: _summary_row(items) for name, items in sorted(groups.items())}


def _summary_row(records: Sequence[ExamRecord]) -> dict[str, object]:
    attempted = [item for item in records if not item.skipped]
    successful = [item for item in attempted if item.succeeded]
    latencies = [item.latency_ms for item in attempted if item.latency_ms is not None]
    token_totals = [
        item.input_tokens + item.output_tokens
        for item in attempted
        if item.input_tokens is not None and item.output_tokens is not None
    ]
    input_tokens = [
        item.input_tokens for item in attempted if item.input_tokens is not None
    ]
    costs = [
        item.configured_cost_usd
        for item in attempted
        if item.configured_cost_usd is not None
    ]
    actual_costs = [_record_actual_cost(item) for item in attempted]
    visible_tokens = [
        item.visible_output_tokens
        for item in attempted
        if item.visible_output_tokens is not None
    ]
    reasoning_tokens = [
        item.reasoning_tokens
        for item in attempted
        if item.reasoning_tokens is not None
    ]
    ttft_values = [item.ttft_ms for item in attempted if item.ttft_ms is not None]
    total_frames = sum(len(item.image_dimensions) for item in attempted)
    failure_count = len(attempted) - len(successful)
    truncated_count = sum(item.truncated for item in attempted)
    return {
        "attempts": len(records),
        "executed_attempts": len(attempted),
        "skipped": len(records) - len(attempted),
        "successes": len(successful),
        "failures": failure_count,
        "failure_rate": failure_count / len(attempted) if attempted else None,
        "latency_median_ms": statistics.median(latencies) if latencies else None,
        "latency_p90_ms": _percentile(latencies, 0.9) if latencies else None,
        "latency_max_ms": max(latencies) if latencies else None,
        "ttft_median_ms": statistics.median(ttft_values) if ttft_values else None,
        "ttft_p90_ms": _percentile(ttft_values, 0.9) if ttft_values else None,
        "ttft_max_ms": max(ttft_values) if ttft_values else None,
        "ttft_nonempty_rate": len(ttft_values) / len(attempted) if attempted else None,
        "average_tokens_per_attempt": statistics.fmean(token_totals) if token_totals else None,
        "average_input_tokens_per_attempt": (
            statistics.fmean(input_tokens) if input_tokens else None
        ),
        "average_configured_cost_usd_per_attempt": statistics.fmean(costs) if costs else None,
        "total_actual_cost_usd": sum(actual_costs),
        "average_cost_per_frame_usd": (
            sum(actual_costs) / total_frames if total_frames else None
        ),
        "reasoning_tokens": sum(reasoning_tokens),
        "average_visible_output_tokens": (
            statistics.fmean(visible_tokens) if visible_tokens else None
        ),
        "empty_visible_outputs": sum(item.visible_output_empty for item in records),
        "truncated": truncated_count,
        "truncated_rate": truncated_count / len(records) if records else None,
        "fast_relaxed_attempts": sum(item.fast_relaxed for item in records),
    }


def _repetition_stability(
    records: Sequence[ExamRecord],
) -> dict[str, dict[str, object]]:
    groups: dict[str, list[ExamRecord]] = {}
    for record in records:
        width = "native" if record.upload_width == 0 else str(record.upload_width)
        groups.setdefault(f"{record.target_label} | {width}", []).append(record)
    output: dict[str, dict[str, object]] = {}
    for name, group in sorted(groups.items()):
        by_repetition: dict[int, list[float]] = {}
        for record in group:
            if record.latency_ms is not None:
                by_repetition.setdefault(record.repetition, []).append(record.latency_ms)
        medians = {
            repetition: statistics.median(values)
            for repetition, values in sorted(by_repetition.items())
            if values
        }
        first = medians.get(1)
        second = medians.get(2)
        output[name] = {
            "repetition_medians_ms": {str(key): value for key, value in medians.items()},
            "median_difference_ms": (
                second - first if first is not None and second is not None else None
            ),
            "absolute_median_difference_ms": (
                abs(second - first) if first is not None and second is not None else None
            ),
        }
    return output


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _display_number(value: object, digits: int) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "未取得"
    return f"{float(value):.{digits}f}"


def _optional_number(value: float | None, digits: int) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def resolve_models_from_openrouter(
    requested_names: Sequence[str],
    *,
    client: httpx.Client | None = None,
    fetched_at: datetime | None = None,
) -> ModelResolution:
    """Resolve live models, prices, and Alibaba endpoint availability."""
    if not requested_names:
        raise VisionExamError("--resolve-models 至少需要一个 --model 名称")
    owns_client = client is None
    catalog_client = client or httpx.Client(timeout=30.0)
    try:
        payload = _get_openrouter_json(
            catalog_client, OPENROUTER_MODELS_URL, "型号目录"
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            raise VisionExamError("OpenRouter 型号目录缺少 data 数组")
        raw_models = [item for item in payload["data"] if isinstance(item, Mapping)]
        if not raw_models:
            raise VisionExamError("OpenRouter 型号目录没有可解析型号")

        resolved: list[ResolvedModel] = []
        errors: list[str] = []
        for requested_name in requested_names:
            match = _match_catalog_model(requested_name, raw_models)
            if match is None:
                candidates = _closest_catalog_slugs(requested_name, raw_models)
                errors.append(
                    f"{requested_name}：不存在；最接近："
                    + ("、".join(candidates) if candidates else "无")
                )
                continue
            try:
                model = _parse_resolved_model(requested_name, match)
            except VisionExamError as error:
                errors.append(str(error))
                continue
            if "image" not in model.input_modalities:
                candidates = _closest_catalog_slugs(requested_name, raw_models)
                errors.append(
                    f"{requested_name} -> {model.slug}：不支持图像输入；最接近："
                    + ("、".join(candidates) if candidates else "无")
                )
                continue
            endpoints_url = OPENROUTER_MODEL_ENDPOINTS_URL.format(slug=model.slug)
            endpoint_payload = _get_openrouter_json(
                catalog_client, endpoints_url, f"{model.slug} 端点目录"
            )
            model = _attach_selected_provider(model, endpoint_payload)
            resolved.append(model)
        if errors:
            raise VisionExamError("候选型号解析失败：\n- " + "\n- ".join(errors))
        slugs = [model.slug for model in resolved]
        if len(set(slugs)) != len(slugs):
            raise VisionExamError("多个候选名称解析到了同一个 slug")
        timestamp = fetched_at or datetime.now().astimezone()
        return ModelResolution(
            endpoint=OPENROUTER_MODELS_URL,
            fetched_at=timestamp.isoformat(),
            models=tuple(resolved),
        )
    finally:
        if owns_client:
            catalog_client.close()


def _get_openrouter_json(
    client: httpx.Client,
    url: str,
    label: str,
) -> object:
    try:
        response = client.get(url)
    except httpx.HTTPError as error:
        raise VisionExamError(f"OpenRouter {label}请求失败：{error}") from error
    if not response.is_success:
        raise VisionExamError(
            f"OpenRouter {label}请求失败（HTTP {response.status_code}）"
        )
    try:
        return response.json()
    except ValueError as error:
        raise VisionExamError(f"OpenRouter {label}不是合法 JSON：{error}") from error


def _attach_selected_provider(
    model: ResolvedModel,
    payload: object,
) -> ResolvedModel:
    if not isinstance(payload, Mapping):
        raise VisionExamError(f"{model.slug}：端点目录不是 JSON 对象")
    data_value = payload.get("data")
    data = data_value if isinstance(data_value, Mapping) else {}
    endpoints_value = data.get("endpoints")
    if not isinstance(endpoints_value, list):
        raise VisionExamError(f"{model.slug}：端点目录缺少 endpoints 数组")
    matches = [
        endpoint
        for endpoint in endpoints_value
        if isinstance(endpoint, Mapping)
        and isinstance(endpoint.get("provider_name"), str)
        and endpoint["provider_name"].casefold() == SPEED_ROUND_PROVIDER_NAME.casefold()
    ]
    if not matches:
        return model
    endpoint = matches[0]
    provider_slug = endpoint.get("tag")
    endpoint_name = endpoint.get("name")
    if not isinstance(provider_slug, str) or not provider_slug.strip():
        raise VisionExamError(f"{model.slug}：Alibaba 端点缺少路由 tag")
    endpoint_price = _parse_price_mapping(
        endpoint.get("pricing"),
        label=f"{model.slug} Alibaba 端点",
    )
    return replace(
        model,
        price=endpoint_price,
        selected_provider_name=SPEED_ROUND_PROVIDER_NAME,
        selected_provider_slug=provider_slug,
        selected_provider_endpoint=(
            endpoint_name if isinstance(endpoint_name, str) else None
        ),
        provider_locked=True,
    )


def _match_catalog_model(
    requested_name: str,
    raw_models: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    requested = requested_name.strip()
    if not requested:
        return None
    exact_slug = [item for item in raw_models if item.get("id") == requested]
    if len(exact_slug) == 1:
        return exact_slug[0]
    normalized = _normalize_model_name(requested)
    matches = [
        item
        for item in raw_models
        if normalized in _catalog_match_keys(item)
    ]
    return matches[0] if len(matches) == 1 else None


def _catalog_match_keys(raw: Mapping[str, object]) -> set[str]:
    keys: set[str] = set()
    slug = raw.get("id")
    if isinstance(slug, str):
        keys.add(_normalize_model_name(slug))
    name = raw.get("name")
    if isinstance(name, str):
        keys.add(_normalize_model_name(name))
        if ":" in name:
            keys.add(_normalize_model_name(name.split(":", 1)[1]))
    return keys


def _normalize_model_name(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _closest_catalog_slugs(
    requested_name: str,
    raw_models: Sequence[Mapping[str, object]],
    *,
    limit: int = 5,
) -> tuple[str, ...]:
    requested = _normalize_model_name(requested_name)
    scored: list[tuple[float, str]] = []
    for raw in raw_models:
        slug = raw.get("id")
        if not isinstance(slug, str):
            continue
        score = max(
            (
                difflib.SequenceMatcher(None, requested, key).ratio()
                for key in _catalog_match_keys(raw)
            ),
            default=0.0,
        )
        scored.append((score, slug))
    return tuple(slug for _, slug in sorted(scored, reverse=True)[:limit])


def _parse_resolved_model(
    requested_name: str,
    raw: Mapping[str, object],
) -> ResolvedModel:
    slug = raw.get("id")
    name = raw.get("name")
    if not isinstance(slug, str) or not slug.strip():
        raise VisionExamError(f"{requested_name}：目录条目缺少 slug")
    if not isinstance(name, str) or not name.strip():
        raise VisionExamError(f"{requested_name} -> {slug}：目录条目缺少名称")
    architecture_value = raw.get("architecture")
    architecture = (
        architecture_value if isinstance(architecture_value, Mapping) else {}
    )
    modalities_value = architecture.get("input_modalities")
    modalities = (
        tuple(item for item in modalities_value if isinstance(item, str))
        if isinstance(modalities_value, list)
        else ()
    )
    price = _parse_price_mapping(
        raw.get("pricing"), label=f"{requested_name} -> {slug}"
    )
    parameters_value = raw.get("supported_parameters")
    parameters = (
        tuple(item for item in parameters_value if isinstance(item, str))
        if isinstance(parameters_value, list)
        else ()
    )
    reasoning_value = raw.get("reasoning")
    reasoning = reasoning_value if isinstance(reasoning_value, Mapping) else {}
    mandatory = reasoning.get("mandatory") is True
    canonical_value = raw.get("canonical_slug")
    return ResolvedModel(
        requested_name=requested_name,
        name=name,
        slug=slug,
        canonical_slug=(canonical_value if isinstance(canonical_value, str) else None),
        input_modalities=modalities,
        price=price,
        supported_parameters=parameters,
        reasoning_mandatory=mandatory,
        reasoning_disabled=("reasoning" in parameters and not mandatory),
        selected_provider_name=None,
        selected_provider_slug=None,
        selected_provider_endpoint=None,
        provider_locked=False,
    )


def _parse_price_mapping(value: object, *, label: str) -> ModelPrice:
    pricing = value if isinstance(value, Mapping) else {}
    try:
        prompt_price = float(pricing["prompt"])
        completion_price = float(pricing["completion"])
    except (KeyError, TypeError, ValueError) as error:
        raise VisionExamError(f"{label}：无法解析当前输入/输出单价") from error
    if (
        not math.isfinite(prompt_price)
        or not math.isfinite(completion_price)
        or prompt_price < 0
        or completion_price < 0
    ):
        raise VisionExamError(f"{label}：当前单价无效")
    return ModelPrice(prompt_price * 1_000_000, completion_price * 1_000_000)


def targets_from_resolution(
    resolution: ModelResolution,
    *,
    temperature: float,
    timeout_seconds: float,
    lock_selected_provider: bool = False,
) -> tuple[ModelTarget, ...]:
    """Convert exact live catalog records into runnable targets."""
    return tuple(
        ModelTarget(
            label=model.slug,
            model=model.slug,
            provider=(
                model.selected_provider_slug
                if lock_selected_provider and model.provider_locked
                else None
            ),
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            price=model.price,
            reasoning_disabled=model.reasoning_disabled,
            provider_lock_status=(
                "已锁定"
                if lock_selected_provider and model.provider_locked
                else "未锁定" if lock_selected_provider else "未请求"
            ),
            provider_endpoint=(
                model.selected_provider_endpoint if lock_selected_provider else None
            ),
            provider_display_name=(
                model.selected_provider_name if lock_selected_provider else None
            ),
        )
        for model in resolution.models
    )


def resolve_targets(
    model_arguments: Sequence[str],
    *,
    llm_config: LlmConfig,
    provider: str | None,
    temperature: float,
    timeout_seconds: float,
    prices: Mapping[str, ModelPrice],
) -> tuple[ModelTarget, ...]:
    """Resolve direct model IDs or profile:<name> references without hard-coded IDs."""
    if not model_arguments:
        raise VisionExamError("至少传入一个 --model")
    targets: list[ModelTarget] = []
    for argument in model_arguments:
        price = prices.get(argument)
        if price is None:
            raise VisionExamError(
                f"目标 {argument} 缺少 --price；不得猜测模型单价"
            )
        if argument.startswith("profile:"):
            profile_name = argument.removeprefix("profile:")
            profile = llm_config.profiles.get(profile_name)
            if profile is None:
                raise VisionExamError(f"config.toml 中不存在档位 {profile_name}")
            selected_model = profile.model or llm_config.model
            if not selected_model.strip():
                raise VisionExamError(f"档位 {profile_name} 没有可用 model")
            targets.append(
                ModelTarget(
                    label=argument,
                    model=selected_model,
                    provider=_blank_to_none(
                        profile.provider or llm_config.provider
                    ),
                    temperature=(
                        profile.temperature
                        if profile.temperature is not None
                        else llm_config.temperature
                    ),
                    timeout_seconds=(
                        profile.timeout_seconds
                        if profile.timeout_seconds is not None
                        else llm_config.timeout_seconds
                    ),
                    price=price,
                )
            )
            continue
        if not argument.strip():
            raise VisionExamError("--model 不得为空")
        targets.append(
            ModelTarget(
                label=argument,
                model=argument,
                provider=_blank_to_none(provider),
                temperature=temperature,
                timeout_seconds=timeout_seconds,
                price=price,
            )
        )
    return tuple(targets)


def parse_prices(arguments: Sequence[str]) -> dict[str, ModelPrice]:
    """Parse repeatable TARGET=INPUT,OUTPUT prices without guessing missing values."""
    prices: dict[str, ModelPrice] = {}
    for argument in arguments:
        label, separator, values = argument.partition("=")
        price_parts = values.split(",") if separator else []
        if not label.strip() or len(price_parts) != 2:
            raise VisionExamError("--price 格式必须为 TARGET=输入单价,输出单价")
        try:
            input_price, output_price = (float(item) for item in price_parts)
        except ValueError as error:
            raise VisionExamError(f"--price 含非数字单价：{argument}") from error
        if not math.isfinite(input_price) or not math.isfinite(output_price):
            raise VisionExamError("--price 单价必须是有限数")
        if input_price < 0 or output_price < 0:
            raise VisionExamError("--price 单价不得为负数")
        prices[label.strip()] = ModelPrice(input_price, output_price)
    return prices


def _blank_to_none(value: str | None) -> str | None:
    return value if value is not None and value.strip() else None


def upload_files(manifest: ExamManifest, variants: Sequence[ExamVariant]) -> tuple[Path, ...]:
    """Return the exact de-duplicated file set selected by all variants."""
    paths: list[Path] = []
    for question in manifest.questions:
        paths.extend(question.frames)
    return tuple(dict.fromkeys(paths))


def print_upload_plan(
    files: Sequence[Path],
    targets: Sequence[ModelTarget],
    variants: Sequence[ExamVariant],
    estimate: CostEstimate,
    *,
    cost_cap_usd: float,
    repetitions: int = 1,
) -> None:
    print("=" * 68)
    print("警告：M5-T2 将把下面列出的本地图像发送到网络模型服务。")
    print("只有这次前台命令会上传；实时宠物与截屏探针不会上传。")
    print("目标模型/档位：")
    for target in targets:
        print(
            f"  - {target.label} -> {target.model}；"
            f"输入/输出百万 token 单价 ${target.price.input_per_million_usd:g}/"
            f"${target.price.output_per_million_usd:g}；"
            f"上游{target.provider_lock_status}"
            + (f" {target.provider} ({target.provider_endpoint})" if target.provider else "")
        )
    print("变体：")
    for variant in variants:
        print(f"  - {variant.name}")
    print("预计量（包含本轮配置允许的全部调用）：")
    print(f"  - 每个配置完整遍数：{repetitions}")
    print(f"  - 基础调用：{estimate.base_calls} 次")
    print(f"  - 含重跑最多：{estimate.maximum_calls_with_relaxed} 次")
    print(f"  - 估计输入 token：{estimate.estimated_input_tokens}")
    print(f"  - 输出 token 预算上界：{estimate.maximum_output_tokens}")
    print(f"  - 预计花费：${estimate.estimated_cost_usd:.6f}")
    print(f"  - 花费上限：${cost_cap_usd:.6f}；运行中止线：${cost_cap_usd * 1.5:.6f}")
    print("待上传文件：")
    for path in files:
        print(f"  - {path}")
    print("=" * 68)


def confirm_upload(
    *,
    assume_yes: bool,
    input_function: Callable[[str], str] | None = None,
) -> bool:
    """Require an explicit affirmative answer unless --yes was supplied."""
    if assume_yes:
        return True
    reader = input_function or input
    answer = reader("确认上传以上文件？请输入 YES 继续：")
    return answer.strip() == "YES"


def render_copy_command(
    *,
    manifest_path: Path,
    resolution: ModelResolution,
    cost_cap_usd: float,
    timeout_seconds: float,
    speed_round: bool = False,
) -> str:
    """Render a PowerShell-safe command with the exact live slugs and prices."""
    arguments = [
        "python",
        "-m",
        "pet.games.generic.eval.vision_exam",
        str(manifest_path),
        "--resolve-models",
    ]
    if speed_round:
        arguments.append("--speed-round")
    for model in resolution.models:
        arguments.extend(("--model", model.slug))
        arguments.extend(
            (
                "--price",
                f"{model.slug}={model.price.input_per_million_usd:g},"
                f"{model.price.output_per_million_usd:g}",
            )
        )
    arguments.extend(("--cost-cap", f"{cost_cap_usd:g}", "--timeout", f"{timeout_seconds:g}"))
    return " ".join(_powershell_quote(argument) for argument in arguments)


def _powershell_quote(value: str) -> str:
    if value and not any(character.isspace() or character in "'\"`$" for character in value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _read_prompt(path: Path) -> str:
    try:
        prompt = path.read_text(encoding="utf-8")
    except OSError as error:
        raise VisionExamError(f"无法读取观察提示词 {path}：{error}") from error
    if not prompt.strip():
        raise VisionExamError(f"观察提示词为空：{path}")
    return prompt


def _default_client_factory(target: ModelTarget) -> OpenRouterClient:
    return OpenRouterClient.from_env(timeout_seconds=target.timeout_seconds)


def _run_directory(root: Path, started_at: datetime) -> Path:
    timestamp = started_at.strftime("%Y%m%d-%H%M%S-%f")
    return root / f"vision-exam-{timestamp}"


def _provider_lock_verification(
    records: Sequence[ExamRecord],
    targets: Sequence[ModelTarget],
) -> dict[str, object]:
    verification: dict[str, object] = {}
    for target in targets:
        target_records = [record for record in records if record.target_label == target.label]
        actual = sorted(
            {record.provider for record in target_records if record.provider is not None}
        )
        expected = target.provider
        verification[target.label] = {
            "lock_status": target.provider_lock_status,
            "expected_provider_slug": expected,
            "endpoint": target.provider_endpoint,
            "actual_providers": actual,
            "consistent": (
                bool(actual)
                and all(
                    name.casefold() == SPEED_ROUND_PROVIDER_NAME.casefold()
                    for name in actual
                )
                if target.provider_lock_status == "已锁定"
                else None
            ),
        }
    return verification


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M5-T2 离线视觉模型考卷")
    parser.add_argument("manifest", type=Path, help="TOML 考卷清单")
    parser.add_argument("--model", action="append", default=[], help="模型 ID 或 profile:<档位名>；可重复")
    parser.add_argument(
        "--resolve-models",
        action="store_true",
        help="实时读取 OpenRouter 官方型号目录；同时使用 M5-T2.9 的四个剪枝变体",
    )
    parser.add_argument(
        "--speed-round",
        action="store_true",
        help="M5-T2.10：只跑 fast/sparse、1280/896/640，各两遍并记录 TTFT",
    )
    parser.add_argument("--provider", help="直接模型的可选服务商锁定")
    parser.add_argument("--price", action="append", default=[], help="TARGET=输入百万token美元,输出百万token美元；可重复")
    parser.add_argument(
        "--send-width",
        type=int,
        action="append",
        dest="send_widths",
        help="可重复；0=原生分辨率，未传时默认 1280",
    )
    parser.add_argument(
        "--region-mode",
        action="append",
        choices=REGION_MODES,
        dest="region_modes",
        help="可重复：off、sparse、always；未传时默认 off",
    )
    parser.add_argument(
        "--output-mode",
        action="append",
        choices=CLI_OUTPUT_MODES,
        dest="output_modes",
        help="可重复：fast 或 deep；未传时默认 fast",
    )
    parser.add_argument(
        "--region-sparsity-max",
        type=float,
        default=DEFAULT_REGION_SPARSITY_MAX,
        help="sparse 模式的最大变化格子占比，默认 0.25（待实测）",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--cost-cap", type=float, default=DEFAULT_COST_CAP_USD)
    parser.add_argument(
        "--estimated-input-tokens",
        type=int,
        default=DEFAULT_ESTIMATED_INPUT_TOKENS,
        help="预估每次调用的输入 token，仅用于运行前花费护栏",
    )
    parser.add_argument("--answer-key", type=Path, default=DEFAULT_ANSWER_KEY_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--local-config", type=Path, default=DEFAULT_LOCAL_CONFIG_PATH)
    parser.add_argument("--yes", action="store_true", help="打印清单后跳过交互输入")
    return parser


def _configure_console_encoding() -> None:
    """Keep Chinese safety prompts readable in redirected Windows terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console_encoding()
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if not 0 <= arguments.temperature <= 2:
            raise VisionExamError("--temperature 必须在 0–2")
        if arguments.timeout <= 0:
            raise VisionExamError("--timeout 必须大于 0")
        if arguments.cost_cap <= 0:
            raise VisionExamError("--cost-cap 必须大于 0")
        if arguments.speed_round and not arguments.resolve_models:
            raise VisionExamError("--speed-round 必须与 --resolve-models 同时使用")
        manifest = load_manifest(arguments.manifest)
        resolution: ModelResolution | None = None
        repetitions = 1
        streaming = False
        enable_relaxed = True
        if arguments.resolve_models:
            if arguments.send_widths or arguments.region_modes or arguments.output_modes:
                raise VisionExamError(
                    "--resolve-models 正式跑卷固定使用四个剪枝变体，不得同时传变体轴"
                )
            if arguments.speed_round:
                variants = build_speed_round_variants()
                repetitions = 2
                streaming = True
                enable_relaxed = False
            else:
                variants = build_formal_variants()
            resolution = resolve_models_from_openrouter(arguments.model)
            targets = targets_from_resolution(
                resolution,
                temperature=arguments.temperature,
                timeout_seconds=arguments.timeout,
                lock_selected_provider=arguments.speed_round,
            )
            supplied_prices = parse_prices(arguments.price)
            for model in resolution.models:
                supplied = supplied_prices.get(model.slug)
                if supplied is not None and supplied != model.price:
                    raise VisionExamError(
                        f"命令行单价与实时目录不一致：{model.slug}"
                    )
        else:
            variants = build_variants(
                send_widths=arguments.send_widths or (),
                region_modes=arguments.region_modes or (),
                output_modes=arguments.output_modes or (),
                region_sparsity_max=arguments.region_sparsity_max,
            )
            prices = parse_prices(arguments.price)
            config = load_config(arguments.config, arguments.local_config)
            targets = resolve_targets(
                arguments.model,
                llm_config=config.llm,
                provider=arguments.provider,
                temperature=arguments.temperature,
                timeout_seconds=arguments.timeout,
                prices=prices,
            )
        estimate = estimate_formal_cost(
            question_count=len(manifest.questions),
            variants=variants,
            targets=targets,
            estimated_input_tokens_per_attempt=arguments.estimated_input_tokens,
            repetitions=repetitions,
            include_relaxed=enable_relaxed,
        )
        if estimate.estimated_cost_usd > arguments.cost_cap:
            raise VisionExamError(
                f"预计花费 ${estimate.estimated_cost_usd:.6f} 超过 "
                f"--cost-cap ${arguments.cost_cap:.6f}"
            )
        if resolution is not None and not os.environ.get(OPENROUTER_API_KEY_ENV, "").strip():
            print(f"未设置 {OPENROUTER_API_KEY_ENV}；未尝试模型调用。可复制命令：")
            print(
                render_copy_command(
                    manifest_path=manifest.path,
                    resolution=resolution,
                    cost_cap_usd=arguments.cost_cap,
                    timeout_seconds=arguments.timeout,
                    speed_round=arguments.speed_round,
                )
            )
            return 0
        files = upload_files(manifest, variants)
        print_upload_plan(
            files,
            targets,
            variants,
            estimate,
            cost_cap_usd=arguments.cost_cap,
            repetitions=repetitions,
        )
        if not confirm_upload(assume_yes=arguments.yes):
            print("未确认上传，考卷未执行。")
            return 2

        started_at = datetime.now().astimezone()
        outcome = run_formal_exam(
            manifest=manifest,
            variants=variants,
            targets=targets,
            client_factory=_default_client_factory,
            cost_cap_usd=arguments.cost_cap,
            enable_relaxed=enable_relaxed,
            repetitions=repetitions,
            streaming=streaming,
        )
        ended_at = datetime.now().astimezone()
        output_directory = _run_directory(DEFAULT_OUTPUT_ROOT, started_at)
        write_outputs(
            output_directory=output_directory,
            records=outcome.records,
            answer_key_path=arguments.answer_key,
            run_payload={
                "manifest": str(manifest.path),
                "arguments": vars(arguments)
                | {
                    "manifest": str(arguments.manifest),
                    "prompts": {
                        mode: str(path)
                        for mode, path in OUTPUT_PROMPT_PATHS.items()
                    },
                    "config": str(arguments.config),
                    "local_config": str(arguments.local_config),
                    "answer_key": str(arguments.answer_key),
                },
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "targets": [asdict(target) for target in targets],
                "model_resolution": (
                    asdict(resolution) if resolution is not None else None
                ),
                "cost_estimate": asdict(estimate),
                "cost_cap_usd": arguments.cost_cap,
                "actual_cost_usd": outcome.actual_cost_usd,
                "cost_guard_stopped": outcome.cost_guard_stopped,
                "relaxed_models": list(outcome.relaxed_models),
                "variants": [
                    asdict(variant)
                    | {"name": variant.name, "max_tokens": variant.max_tokens}
                    for variant in variants
                ],
                "uploaded_files": [str(path) for path in files],
                "base_expected_rows": (
                    len(targets)
                    * len(variants)
                    * len(manifest.questions)
                    * repetitions
                ),
                "attempts": len(outcome.records),
                "repetitions": repetitions,
                "streaming_requested": streaming,
                "provider_lock_verification": _provider_lock_verification(
                    outcome.records, targets
                ),
            },
            speed_round=arguments.speed_round,
        )
        print(f"考卷完成：{output_directory}")
        print(
            f"调用 {len(outcome.records)} 次，成功 "
            f"{sum(record.succeeded for record in outcome.records)} 次。"
        )
        print(f"实际总花费：${outcome.actual_cost_usd:.9f}")
        if outcome.relaxed_models:
            for model in outcome.relaxed_models:
                print(
                    f"警告：{model} 的快线受推理预算影响，60 token 档不可比；"
                    "已完成 fast-relaxed 重跑。"
                )
        if outcome.cost_guard_stopped:
            print("运行中实际花费超过上限的 1.5 倍，已中止并保留已有结果。")
            return 3
        return 0
    except VisionExamError as error:
        print(f"M5-T2 无法执行：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
