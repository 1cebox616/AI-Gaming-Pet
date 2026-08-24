"""Mechanically derive region grids from adjacent captured frames."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re
import sys
import tomllib

import numpy as np
from PIL import Image

from pet.core.capture import FrameChangeDetector

BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
DEFAULT_MANIFEST_PATH = (
    BACKEND_DIRECTORY / "data" / "generic" / "vision-exam" / "manifest.toml"
)
FRAME_NAME_PATTERN = re.compile(r"^frame-(\d{6})-.+\.png$")
GRID_ROWS = 16
GRID_COLUMNS = 9


class RegionAssetError(Exception):
    """A local manifest or frame error safe to show to the operator."""


@dataclass(frozen=True, slots=True)
class RegionAssetResult:
    """One reproducible region-grid result for an exam question."""

    question_id: str
    current_frame: str
    previous_frame: str | None
    region_grid: tuple[str, ...]
    reason: str | None


def _frame_index(path: Path) -> int:
    match = FRAME_NAME_PATTERN.fullmatch(path.name)
    if match is None:
        raise RegionAssetError(f"无法从截图文件名读取帧号：{path.name}")
    return int(match.group(1))


def _resolve_frames(
    raw: Mapping[str, object], manifest_directory: Path
) -> tuple[Path, ...]:
    frames = raw.get("frames")
    if not isinstance(frames, list) or not frames:
        raise RegionAssetError("题目 frames 必须是非空列表")
    resolved: list[Path] = []
    for value in frames:
        if not isinstance(value, str):
            raise RegionAssetError("题目 frames 含非字符串路径")
        path = (manifest_directory / value).resolve()
        if not path.is_file():
            raise RegionAssetError(f"截图不存在：{path}")
        resolved.append(path)
    return tuple(resolved)


def _comparison_pair(
    raw: Mapping[str, object], manifest_directory: Path
) -> tuple[Path | None, Path, str | None]:
    frames = _resolve_frames(raw, manifest_directory)
    question_type = raw.get("type")
    current = frames[-1]
    current_index = _frame_index(current)
    if question_type == "sequence":
        previous = frames[-2]
        if _frame_index(previous) + 1 != current_index:
            return None, current, "sequence 题内最后两帧的帧号不相邻"
        return previous, current, None
    if question_type != "single":
        raise RegionAssetError(f"不支持的题型：{question_type!r}")
    candidates = sorted(current.parent.glob(f"frame-{current_index - 1:06d}-*.png"))
    if not candidates:
        return None, current, "原会话未落盘紧邻的前一帧"
    if len(candidates) != 1:
        return None, current, "原会话存在多个同帧号候选，无法唯一确定前一帧"
    return candidates[0], current, None


def changed_region_grid(
    detector: FrameChangeDetector,
    previous: Image.Image,
    current: Image.Image,
) -> tuple[str, ...]:
    """Return cells whose detector-defined block mean exceeds its threshold."""
    previous_prepared, current_prepared = detector.prepare_pair(previous, current)
    difference = np.abs(previous_prepared.area_gray - current_prepared.area_gray)
    columns, rows = detector.block_grid
    cells = tuple(
        f"r{row_index}c{column_index}"
        for row_index, row in enumerate(np.array_split(difference, rows, axis=0), start=1)
        for column_index, block in enumerate(
            np.array_split(row, columns, axis=1), start=1
        )
        if float(block.mean()) > detector.block_delta_threshold
    )
    metrics = detector.compare_prepared(previous_prepared, current_prepared)
    expected_ratio = len(cells) / (columns * rows)
    if not math.isclose(metrics.block_change, expected_ratio, abs_tol=1e-12):
        raise RegionAssetError("逐块结果与 FrameChangeDetector.block_change 不一致")
    return cells


def generate_region_assets(manifest_path: Path) -> tuple[RegionAssetResult, ...]:
    """Calculate every question from its adjacent frame without semantic input."""
    try:
        with manifest_path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RegionAssetError(f"无法读取 manifest：{error}") from error
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        raise RegionAssetError("manifest 缺少 questions 列表")

    detector = FrameChangeDetector(block_grid=(GRID_COLUMNS, GRID_ROWS))
    manifest_directory = manifest_path.resolve().parent
    results: list[RegionAssetResult] = []
    for raw in raw_questions:
        if not isinstance(raw, Mapping):
            raise RegionAssetError("questions 含非表项目")
        question_id = raw.get("id")
        if not isinstance(question_id, str) or not question_id:
            raise RegionAssetError("题目缺少 id")
        previous_path, current_path, reason = _comparison_pair(raw, manifest_directory)
        if previous_path is None:
            results.append(
                RegionAssetResult(
                    question_id,
                    current_path.name,
                    None,
                    (),
                    reason,
                )
            )
            continue
        with Image.open(previous_path) as previous_source:
            previous = previous_source.convert("RGB")
        with Image.open(current_path) as current_source:
            current = current_source.convert("RGB")
        cells = changed_region_grid(detector, previous, current)
        if not cells:
            results.append(
                RegionAssetResult(
                    question_id,
                    current_path.name,
                    previous_path.name,
                    (),
                    "没有格子的平均差超过检测器块阈值",
                )
            )
            continue
        results.append(
            RegionAssetResult(
                question_id,
                current_path.name,
                previous_path.name,
                cells,
                None,
            )
        )
    return tuple(results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M5-T2.7 机械变化区域生成器")
    parser.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST_PATH)
    return parser


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console_encoding()
    arguments = build_parser().parse_args(argv)
    try:
        results = generate_region_assets(arguments.manifest)
    except RegionAssetError as error:
        print(f"M5-T2.6 无法生成：{error}", file=sys.stderr)
        return 2
    print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
