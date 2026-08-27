"""Offline observation replay stays deterministic and never mutates recordings."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path

from PIL import Image

from pet.games.generic.adapter import TitleRule, WindowTitleMap
from pet.games.generic.eval.observation_replay import (
    SegmentRange,
    _extract_just_now,
    _just_now_entries,
    _just_now_statistics,
    _prepare_replay,
    _recording_hash,
    build_parser,
    character_similarity,
)


def _write_session(root: Path) -> None:
    raw = root / "raw"
    raw.mkdir(parents=True)
    for sequence, color in enumerate(((0, 0, 0), (255, 255, 255), (255, 255, 255)), start=1):
        Image.new("RGB", (640, 360), color).save(
            raw / f"raw-{sequence:06d}-20260826T12000{sequence}.000000Z.jpg",
            quality=70,
        )
    (root / "session.json").write_text(
        json.dumps(
            {
                "标签": "fixture replay",
                "启动参数": {"title": "Fixture Game"},
                "时钟锚点": {
                    "perf_counter秒": 100.0,
                    "UTC墙钟": datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc).isoformat(),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with (root / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("序号", "时间", "单调秒", "时间来源"))
        for sequence in range(1, 4):
            writer.writerow(
                (
                    sequence,
                    datetime(2026, 8, 26, 12, 0, sequence, tzinfo=timezone.utc).isoformat(),
                    100.0 + sequence,
                    "capture",
                )
            )


def test_prepare_replay_uses_final_selector_segment_and_never_upscales(tmp_path: Path) -> None:
    _write_session(tmp_path)
    before = _recording_hash(tmp_path)
    prepared = _prepare_replay(
        tmp_path,
        896,
        SegmentRange(1.0, 3.0),
        WindowTitleMap((TitleRule("Fixture", ("fixture",), ()),)),
        0.25,
    )
    after = _recording_hash(tmp_path)
    assert prepared.frame_count == 2
    assert prepared.actual_send_width == 640
    assert prepared.source_width == 640
    assert prepared.game == "Fixture"
    assert prepared.selected
    assert prepared.selected[0].baseline_monotonic_seconds <= (
        prepared.selected[0].timing.monotonic_seconds
    )
    assert prepared.input_csv_missing is True
    assert prepared.input_context.summarize_window(None, 200.0) == "此窗口内无玩家输入"
    assert before == after


def test_character_similarity_flags_repetition_without_semantic_model() -> None:
    assert character_similarity("玩家站在门边。", "玩家仍站在门边。") > 0.6
    assert character_similarity("打开地图。", "进入战斗并抽了一张牌。") < 0.6


def test_just_now_metrics_use_only_observation_body_and_exclude_effect_markers() -> None:
    rows: list[dict[str, object]] = [
        {
            "frame_ts": 123.4,
            "wall": "2026-08-26T12:34:56Z",
            "text": "【画面】室内场景\n【刚刚】仅亮度变化",
            "dropped": None,
        },
        {
            "frame_ts": 124.4,
            "wall": "2026-08-26T12:34:57Z",
            "text": "【画面】状态面板\n【刚刚】中央数值为42",
            "dropped": None,
        },
        {
            "frame_ts": 125.4,
            "wall": "2026-08-26T12:34:58Z",
            "text": "【画面】状态面板\n【刚刚】中央数值为43",
            "dropped": None,
        },
        {
            "frame_ts": 126.4,
            "wall": "2026-08-26T12:34:59Z",
            "text": "【画面】没有数字的观察",
            "dropped": None,
        },
        {
            "frame_ts": 127.4,
            "wall": "2026-08-26T12:35:00Z",
            "text": "【画面】失败项\n【刚刚】数值99",
            "dropped": "timeout",
        },
    ]
    assert _extract_just_now(str(rows[0]["text"])) == "仅亮度变化"
    assert _just_now_statistics(rows) == (3, 1, 2, 19 / 3)
    informative = [
        body for _row, body in _just_now_entries(rows) if not body.startswith("仅")
    ]
    assert informative == ["中央数值为42", "中央数值为43"]
    assert character_similarity(*informative) > 0.6


def test_dispatch_interval_defaults_to_zero_and_accepts_production_pacing() -> None:
    parser = build_parser()
    common = ["--session", "fixture", "--profile", "vision_fast"]
    parsed = parser.parse_args(common)
    assert parsed.dispatch_interval == 0.0
    assert not hasattr(parsed, "context_lines")
    assert parser.parse_args([*common, "--dispatch-interval", "1.0"]).dispatch_interval == 1.0
