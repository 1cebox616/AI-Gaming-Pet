"""Run an explicitly confirmed, offline-authored visual-model exam."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import re
import statistics
import sys
import tomllib
from typing import Literal, cast

from pet.core.config import LlmConfig, load_config
from pet.core.llm import (
    LlmError,
    LlmImage,
    LlmResult,
    LlmVisionClientProtocol,
    OpenRouterClient,
    image_upload_metadata,
)

BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
DEFAULT_PROMPT_PATH = BACKEND_DIRECTORY / "prompts" / "generic" / "observation.md"
DEFAULT_OUTPUT_ROOT = BACKEND_DIRECTORY / "eval-reports"
DEFAULT_CONFIG_PATH = BACKEND_DIRECTORY / "config.toml"
DEFAULT_LOCAL_CONFIG_PATH = BACKEND_DIRECTORY / "config.local.toml"
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
DEFAULT_REGION_SPARSITY_MAX = 0.25  # 此数待实测确定。


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
    """One upload-width and region-mode Cartesian-product choice."""

    send_width: int
    region_mode: RegionMode
    region_sparsity_max: float

    @property
    def name(self) -> str:
        width = "native" if self.send_width == 0 else str(self.send_width)
        return f"region-{self.region_mode}__width-{width}"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Configured USD prices per one million input and output tokens."""

    input_per_million_usd: float
    output_per_million_usd: float


@dataclass(frozen=True, slots=True)
class ModelTarget:
    """One requested model or resolved config profile."""

    label: str
    model: str
    provider: str | None
    temperature: float
    timeout_seconds: float
    max_tokens: int
    price: ModelPrice


@dataclass(frozen=True, slots=True)
class ExamRecord:
    """One model attempt, including invalid output or transport failure."""

    question_id: str
    question_type: str
    variant: str
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
    response_text: str
    error: str | None
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    configured_cost_usd: float | None
    upstream_cost_usd: float | None

    @property
    def succeeded(self) -> bool:
        return self.error is None


ClientFactory = Callable[[ModelTarget], LlmVisionClientProtocol]


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
            "游戏上下文（由窗口标题与进程名确定）："
            f"{question.game_context}。请在 game_guess 中填写这个名称，不要另行猜测。"
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
    return tuple(
        ExamVariant(width, mode, region_sparsity_max)
        for width in widths
        for mode in modes
    )


def validate_observation_json(text: str) -> None:
    """Reject answers that cannot be handed to the human scoring sheet as specified."""
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise VisionExamError(f"非法 JSON 输出：{error}") from error
    if not isinstance(payload, Mapping):
        raise VisionExamError("非法 JSON 输出：顶层必须是对象")
    required = {"scene", "notable_events", "game_guess", "confidence"}
    if set(payload) != required:
        raise VisionExamError("非法 JSON 输出：字段必须恰为 scene、notable_events、game_guess、confidence")
    if not isinstance(payload["scene"], str) or not payload["scene"].strip():
        raise VisionExamError("非法 JSON 输出：scene 必须是非空字符串")
    events = payload["notable_events"]
    if not isinstance(events, list) or any(not isinstance(item, str) for item in events):
        raise VisionExamError("非法 JSON 输出：notable_events 必须是字符串数组")
    if not isinstance(payload["game_guess"], str) or not payload["game_guess"].strip():
        raise VisionExamError("非法 JSON 输出：game_guess 必须是非空字符串")
    confidence = payload["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= float(confidence) <= 1
    ):
        raise VisionExamError("非法 JSON 输出：confidence 必须是 0–1 数字")


def run_exam(
    *,
    manifest: ExamManifest,
    variants: Sequence[ExamVariant],
    targets: Sequence[ModelTarget],
    client_factory: ClientFactory,
) -> tuple[ExamRecord, ...]:
    """Run all targets and keep going after every individual failure."""
    system_prompt = _read_prompt(DEFAULT_PROMPT_PATH)
    records: list[ExamRecord] = []
    for target in targets:
        client: LlmVisionClientProtocol | None = None
        initialization_error: str | None = None
        try:
            client = client_factory(target)
        except Exception as error:
            initialization_error = f"客户端初始化失败：{error}"
        try:
            for question in manifest.questions:
                for variant in variants:
                    if initialization_error is not None or client is None:
                        records.append(
                            _failed_record(
                                question,
                                variant,
                                target,
                                initialization_error or "客户端不可用",
                            )
                        )
                        continue
                    records.append(
                        _run_attempt(
                            client=client,
                            question=question,
                            variant=variant,
                            target=target,
                            system_prompt=system_prompt,
                        )
                    )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    return tuple(records)


def _run_attempt(
    *,
    client: LlmVisionClientProtocol,
    question: ExamQuestion,
    variant: ExamVariant,
    target: ModelTarget,
    system_prompt: str,
) -> ExamRecord:
    images = build_images(question, variant)
    region_fraction = region_grid_fraction(question)
    region_injected = should_inject_region(question, variant)
    upload_metadata = ()
    try:
        upload_metadata = tuple(
            image_upload_metadata(image, max_image_edge=None) for image in images
        )
        result = client.complete_with_images(
            model=target.model,
            provider=target.provider,
            system_prompt=system_prompt,
            user_prompt=build_user_prompt(question, variant),
            images=images,
            max_image_edge=None,
            max_tokens=target.max_tokens,
            temperature=target.temperature,
        )
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
        )

    validation_error: str | None = None
    try:
        validate_observation_json(result.text)
    except VisionExamError as error:
        validation_error = str(error)
    return _record_from_result(
        question,
        variant,
        target,
        result,
        error=validation_error,
        region_fraction=region_fraction,
        region_injected=region_injected,
        image_dimensions=tuple(
            f"{metadata.width}x{metadata.height}" for metadata in upload_metadata
        ),
        image_byte_sizes=tuple(metadata.byte_size for metadata in upload_metadata),
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
) -> ExamRecord:
    return ExamRecord(
        question_id=question.question_id,
        question_type=question.question_type,
        variant=variant.name,
        upload_width=variant.send_width,
        region_mode=variant.region_mode,
        region_grid_fraction=region_fraction,
        region_injected=region_injected,
        image_dimensions=image_dimensions,
        image_byte_sizes=image_byte_sizes,
        target_label=target.label,
        requested_model=target.model,
        actual_model=result.model,
        provider=result.provider,
        response_text=result.text,
        error=error,
        latency_ms=result.latency_seconds * 1000,
        input_tokens=result.usage.prompt_tokens,
        output_tokens=result.usage.completion_tokens,
        configured_cost_usd=_configured_cost(result, target.price),
        upstream_cost_usd=result.usage.cost_usd,
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
) -> ExamRecord:
    return ExamRecord(
        question_id=question.question_id,
        question_type=question.question_type,
        variant=variant.name,
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
        provider=target.provider,
        response_text="",
        error=error,
        latency_ms=latency_ms,
        input_tokens=None,
        output_tokens=None,
        configured_cost_usd=None,
        upstream_cost_usd=None,
    )


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
    "回答原文",
    "错误原文",
    "往返毫秒",
    "输入token",
    "输出token",
    "配置折算花费美元",
    "上游报告花费美元",
)


def write_outputs(
    *,
    output_directory: Path,
    records: Sequence[ExamRecord],
    run_payload: Mapping[str, object],
) -> None:
    """Write the machine records, human marking table, and self-contained run data."""
    output_directory.mkdir(parents=True, exist_ok=False)
    _write_csv(output_directory / "results.csv", records)
    summary = summarize(records)
    (output_directory / "report.md").write_text(
        render_report(records, summary),
        encoding="utf-8",
    )
    complete_payload = dict(run_payload)
    complete_payload["summary"] = summary
    (output_directory / "run.json").write_text(
        json.dumps(complete_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
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
                    "回答原文": record.response_text,
                    "错误原文": record.error or "",
                    "往返毫秒": _optional_number(record.latency_ms, 3),
                    "输入token": record.input_tokens if record.input_tokens is not None else "",
                    "输出token": record.output_tokens if record.output_tokens is not None else "",
                    "配置折算花费美元": _optional_number(record.configured_cost_usd, 9),
                    "上游报告花费美元": _optional_number(record.upstream_cost_usd, 9),
                }
            )


def summarize(records: Sequence[ExamRecord]) -> dict[str, object]:
    """Compute model, question, variant, and width groups from recorded attempts."""
    return {
        "models": _group_summary(records, lambda item: item.target_label),
        "questions": _group_summary(records, lambda item: item.question_id),
        "variants": _group_summary(records, lambda item: item.variant),
        "upload_widths": _group_summary(
            records,
            lambda item: "native" if item.upload_width == 0 else str(item.upload_width),
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
    successful = [item for item in records if item.succeeded]
    latencies = [item.latency_ms for item in records if item.latency_ms is not None]
    token_totals = [
        item.input_tokens + item.output_tokens
        for item in records
        if item.input_tokens is not None and item.output_tokens is not None
    ]
    input_tokens = [
        item.input_tokens for item in records if item.input_tokens is not None
    ]
    costs = [
        item.configured_cost_usd
        for item in records
        if item.configured_cost_usd is not None
    ]
    return {
        "attempts": len(records),
        "successes": len(successful),
        "failures": len(records) - len(successful),
        "latency_median_ms": statistics.median(latencies) if latencies else None,
        "latency_p90_ms": _percentile(latencies, 0.9) if latencies else None,
        "average_tokens_per_attempt": statistics.fmean(token_totals) if token_totals else None,
        "average_input_tokens_per_attempt": (
            statistics.fmean(input_tokens) if input_tokens else None
        ),
        "average_configured_cost_usd_per_attempt": statistics.fmean(costs) if costs else None,
    }


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


def render_report(
    records: Sequence[ExamRecord],
    summary: Mapping[str, object],
) -> str:
    """Render summary tables and one blank human-scoring row per attempt."""
    lines = [
        "# M5-T2 视觉模型考卷判卷表",
        "",
        "人工列由产品负责人填写；工具不对模型质量作结论。",
        "",
        "## 模型汇总",
        "",
        "| 模型/档位 | 成功/总数 | 延迟中位(ms) | 延迟P90(ms) | 每次平均token | 每次平均配置花费(USD) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    model_summary = summary.get("models", {})
    if isinstance(model_summary, Mapping):
        for name, raw in model_summary.items():
            if isinstance(raw, Mapping):
                lines.append(_summary_markdown_row(str(name), raw))
    lines.extend(
        (
            "",
            "## 题目汇总",
            "",
            "| 题号 | 成功/总数 | 延迟中位(ms) | 延迟P90(ms) | 每次平均token | 每次平均配置花费(USD) |",
            "|---|---:|---:|---:|---:|---:|",
        )
    )
    question_summary = summary.get("questions", {})
    if isinstance(question_summary, Mapping):
        for name, raw in question_summary.items():
            if isinstance(raw, Mapping):
                lines.append(_summary_markdown_row(str(name), raw))
    lines.extend(
        (
            "",
            "## 变体轴同类对比",
            "",
            "| 变体 | 成功/总数 | 延迟中位(ms) | 延迟P90(ms) | 每次平均token | 每次平均配置花费(USD) |",
            "|---|---:|---:|---:|---:|---:|",
        )
    )
    variant_summary = summary.get("variants", {})
    if isinstance(variant_summary, Mapping):
        for name, raw in variant_summary.items():
            if isinstance(raw, Mapping):
                lines.append(_summary_markdown_row(str(name), raw))
    lines.extend(
        (
            "",
            "## 上传宽度同类对比",
            "",
            "| 上传宽度 | 成功/总数 | 延迟中位(ms) | 延迟P90(ms) | 每次平均输入token |",
            "|---|---:|---:|---:|---:|",
        )
    )
    width_summary = summary.get("upload_widths", {})
    if isinstance(width_summary, Mapping):
        for name, raw in width_summary.items():
            if isinstance(raw, Mapping):
                lines.append(
                    f"| {_markdown_cell(str(name))} | "
                    f"{raw.get('successes', 0)}/{raw.get('attempts', 0)} | "
                    f"{_display_number(raw.get('latency_median_ms'), 3)} | "
                    f"{_display_number(raw.get('latency_p90_ms'), 3)} | "
                    f"{_display_number(raw.get('average_input_tokens_per_attempt'), 2)} |"
                )
    lines.extend(
        (
            "",
            "## 逐题判卷",
            "",
            "| 题号 | 变体 | 上传宽度 | 区域提示模式 | 变化格子占比 | 实际注入 | 图像像素尺寸 | 图像字节数 | 模型/档位 | 回答原文 | 错误原文 | 准确性判定 | 漏了什么 | 编造了什么 |",
            "|---|---|---:|---|---:|---|---|---|---|---|---|---|---|---|",
        )
    )
    for record in records:
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(record.question_id),
                    _markdown_cell(record.variant),
                    str(record.upload_width),
                    _markdown_cell(record.region_mode),
                    _display_number(record.region_grid_fraction, 9),
                    str(record.region_injected).lower(),
                    _markdown_cell(";".join(record.image_dimensions)),
                    _markdown_cell(
                        ";".join(str(value) for value in record.image_byte_sizes)
                    ),
                    _markdown_cell(record.target_label),
                    _markdown_cell(record.response_text),
                    _markdown_cell(record.error or ""),
                    "",
                    "",
                    "",
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _summary_markdown_row(name: str, raw: Mapping[str, object]) -> str:
    return (
        f"| {_markdown_cell(name)} | {raw.get('successes', 0)}/{raw.get('attempts', 0)} | "
        f"{_display_number(raw.get('latency_median_ms'), 3)} | "
        f"{_display_number(raw.get('latency_p90_ms'), 3)} | "
        f"{_display_number(raw.get('average_tokens_per_attempt'), 2)} | "
        f"{_display_number(raw.get('average_configured_cost_usd_per_attempt'), 9)} |"
    )


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _display_number(value: object, digits: int) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "未取得"
    return f"{float(value):.{digits}f}"


def _optional_number(value: float | None, digits: int) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def resolve_targets(
    model_arguments: Sequence[str],
    *,
    llm_config: LlmConfig,
    provider: str | None,
    temperature: float,
    timeout_seconds: float,
    max_tokens: int,
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
                    max_tokens=(
                        profile.max_tokens
                        if profile.max_tokens is not None
                        else llm_config.max_tokens
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
                max_tokens=max_tokens,
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
) -> None:
    print("=" * 68)
    print("警告：M5-T2 将把下面列出的本地图像发送到网络模型服务。")
    print("只有这次前台命令会上传；实时宠物与截屏探针不会上传。")
    print("目标模型/档位：")
    for target in targets:
        print(
            f"  - {target.label} -> {target.model}；"
            f"输入/输出百万 token 单价 ${target.price.input_per_million_usd:g}/"
            f"${target.price.output_per_million_usd:g}"
        )
    print("变体：")
    for variant in variants:
        print(f"  - {variant.name}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M5-T2 离线视觉模型考卷")
    parser.add_argument("manifest", type=Path, help="TOML 考卷清单")
    parser.add_argument("--model", action="append", default=[], help="模型 ID 或 profile:<档位名>；可重复")
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
        "--region-sparsity-max",
        type=float,
        default=DEFAULT_REGION_SPARSITY_MAX,
        help="sparse 模式的最大变化格子占比，默认 0.25（待实测）",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-tokens", type=int, default=512)
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
        if arguments.max_tokens <= 0:
            raise VisionExamError("--max-tokens 必须大于 0")
        manifest = load_manifest(arguments.manifest)
        variants = build_variants(
            send_widths=arguments.send_widths or (),
            region_modes=arguments.region_modes or (),
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
            max_tokens=arguments.max_tokens,
            prices=prices,
        )
        files = upload_files(manifest, variants)
        print_upload_plan(files, targets, variants)
        if not confirm_upload(assume_yes=arguments.yes):
            print("未确认上传，考卷未执行。")
            return 2

        started_at = datetime.now().astimezone()
        records = run_exam(
            manifest=manifest,
            variants=variants,
            targets=targets,
            client_factory=_default_client_factory,
        )
        ended_at = datetime.now().astimezone()
        output_directory = _run_directory(DEFAULT_OUTPUT_ROOT, started_at)
        write_outputs(
            output_directory=output_directory,
            records=records,
            run_payload={
                "manifest": str(manifest.path),
                "arguments": vars(arguments)
                | {
                    "manifest": str(arguments.manifest),
                    "prompt": str(DEFAULT_PROMPT_PATH),
                    "config": str(arguments.config),
                    "local_config": str(arguments.local_config),
                },
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "targets": [asdict(target) for target in targets],
                "variants": [
                    asdict(variant) | {"name": variant.name}
                    for variant in variants
                ],
                "uploaded_files": [str(path) for path in files],
                "attempts": len(records),
            },
        )
        print(f"考卷完成：{output_directory}")
        print(f"调用 {len(records)} 次，成功 {sum(record.succeeded for record in records)} 次。")
        return 0
    except VisionExamError as error:
        print(f"M5-T2 无法执行：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
