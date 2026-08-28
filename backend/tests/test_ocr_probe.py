from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path

from PIL import Image

from pet.core.ocr_probe import (
    OcrFrameResult,
    OcrLanguage,
    OcrLine,
    SampledFrame,
    SegmentRange,
    _language_available,
    _prepare_samples,
    _recording_hash,
    _write_outputs,
    build_parser,
    confirmed_overlap_ratio,
    enumerate_ocr_languages,
    run_winrt_ocr,
)
from pet.core.capture import ReplayFrameTime


def _write_raw_session(root: Path) -> Path:
    raw = root / "raw"
    raw.mkdir(parents=True)
    for sequence, color in enumerate(
        ((0, 0, 0), (255, 255, 255), (255, 255, 255), (0, 0, 0)), start=1
    ):
        Image.new("RGB", (90, 160), color).save(
            raw / f"raw-{sequence:06d}-20260828T12000{sequence}.000000Z.jpg"
        )
    with (root / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("序号", "时间", "单调秒", "时间来源"))
        for sequence in range(1, 5):
            writer.writerow(
                (
                    sequence,
                    datetime(2026, 8, 28, 12, 0, sequence, tzinfo=timezone.utc).isoformat(),
                    100.0 + sequence,
                    "capture",
                )
            )
    return raw


def test_parser_accepts_required_shape_and_repeatable_languages() -> None:
    arguments = build_parser().parse_args(
        [
            "--session",
            "fixture/raw",
            "--sample-stride",
            "3",
            "--lang",
            "zh-Hans",
            "--lang",
            "en-US",
        ]
    )
    assert arguments.sample_stride == 3
    assert arguments.lang == ["zh-Hans", "en-US"]


def test_language_enumeration_and_prefix_availability_use_winrt_result() -> None:
    payload = json.dumps(
        [
            {
                "language_tag": "en-US",
                "display_name": "English",
                "native_name": "English",
            },
            {
                "language_tag": "zh-Hans-CN",
                "display_name": "Chinese",
                "native_name": "中文",
            },
        ]
    ).encode()
    languages = enumerate_ocr_languages(lambda _script: payload)
    assert [item.language_tag for item in languages] == ["en-US", "zh-Hans-CN"]
    assert _language_available("zh-Hans", languages)
    assert not _language_available("ja-JP", languages)


def test_prepare_samples_applies_segment_stride_and_preserves_recording_hash(
    tmp_path: Path,
) -> None:
    raw = _write_raw_session(tmp_path)
    before = _recording_hash(raw)
    samples, included = _prepare_samples(raw, 2, SegmentRange(1.0, 4.0))
    after = _recording_hash(raw)
    assert included == 3
    assert [item.frame_number for item in samples] == [2, 4]
    assert samples[0].confirmed_grid
    assert before == after


def test_confirmed_overlap_uses_line_area_not_grid_count() -> None:
    line = OcrLine("42", 0.0, 0.0, 2.0 / 9.0, 1.0 / 16.0)
    assert confirmed_overlap_ratio(line, ("r1c1",)) == 0.5
    assert confirmed_overlap_ratio(line, ("r2c1",)) == 0.0


def test_winrt_result_parser_keeps_null_confidence_and_frame_order(tmp_path: Path) -> None:
    path = tmp_path / "raw-000001-fixture.jpg"
    path.write_bytes(b"fixture")
    sample = SampledFrame(
        path,
        1,
        ReplayFrameTime(
            datetime(2026, 8, 28, tzinfo=timezone.utc), 10.0, "capture", False
        ),
        0.0,
        ("r1c1",),
    )
    payload = {
        "available_languages": {
            "language_tag": "zh-Hans-CN",
            "display_name": "Chinese",
            "native_name": "中文",
        },
        "max_image_dimension": 10000,
        "frames": {
            "path": str(path),
            "width": 1920,
            "height": 1080,
            "duration_ms": 42.5,
            "recognize_ms": 30.0,
            "lines": {
                "text": "生命 42",
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.04,
                "confidence": None,
            },
            "error": None,
        },
    }
    languages, maximum, results = run_winrt_ocr(
        (sample,), "zh-Hans-CN", tmp_path, lambda _script: json.dumps(payload).encode()
    )
    assert languages[0].language_tag == "zh-Hans-CN"
    assert maximum == 10000
    assert results[0].lines[0].text == "生命 42"
    assert results[0].lines[0].confidence is None


def test_outputs_include_empty_frames_manual_ten_and_overlap_columns(tmp_path: Path) -> None:
    raw = tmp_path / "session" / "raw"
    raw.mkdir(parents=True)
    samples: list[SampledFrame] = []
    results: list[OcrFrameResult] = []
    for index in range(10):
        path = raw / f"raw-{index + 1:06d}-fixture.jpg"
        path.write_bytes(b"fixture")
        samples.append(
            SampledFrame(
                path,
                index + 1,
                ReplayFrameTime(
                    datetime(2026, 8, 28, tzinfo=timezone.utc),
                    10.0 + index,
                    "capture",
                    False,
                ),
                float(index),
                ("r1c1",) if index == 0 else (),
            )
        )
        lines = (OcrLine("数值42", 0.0, 0.0, 0.1, 0.03),) if index == 0 else ()
        results.append(OcrFrameResult(path, 90, 160, 20.0 + index, 10.0, lines))
    output = tmp_path / "output"
    stats = _write_outputs(
        output,
        label="fixture",
        session_directory=raw.parent,
        raw_directory=raw,
        language="zh-Hans-CN",
        available_languages=(OcrLanguage("zh-Hans-CN", "Chinese", "中文"),),
        max_image_dimension=10000,
        samples=samples,
        included_frame_count=10,
        results=results,
        stride=1,
        segment=None,
        answer_key=None,
        hash_before="same",
        hash_after="same",
    )
    assert stats["text_line_count"] == 1
    assert stats["text_character_count"] == 4
    assert stats["text_characters_overlapping_confirmed"] == 4
    assert stats["recording_hash_matches"] is True
    csv_text = (output / "ocr-probe.csv").read_text(encoding="utf-8-sig")
    assert "confidence" in csv_text and "confirmed_overlap_ratio" in csv_text
    assert len((output / "manual-review.md").read_text(encoding="utf-8").split("|\n")) >= 10
