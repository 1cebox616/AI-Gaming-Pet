"""Offline observation replay stays deterministic and never mutates recordings."""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone
import json
from pathlib import Path

from PIL import Image
import pytest

from pet.core.belief import EvidenceStore
from pet.core.capture import CapturedFrame, FrameMetadata
from pet.core.config import LlmConfig, LlmProfileConfig
from pet.core.llm import LlmResult, LlmUsage
from pet.games.generic.adapter import GenericVisionAdapter, TitleRule, WindowTitleMap
from pet.games.generic.eval.observation_replay import (
    SegmentRange,
    _adapter_configuration,
    _extract_scene,
    _extract_speculation,
    _echoed_metric_values,
    _extract_local,
    _input_attribution_violation,
    _local_entries,
    _local_statistics,
    _prepare_replay,
    _recording_hash,
    _retrospective_violation,
    build_parser,
    character_similarity,
)


class LocalReplayFakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete_with_images_stream(self, **_kwargs: object) -> LlmResult:
        self.calls += 1
        return LlmResult(
            text="【画面】本地假客户端观察",
            usage=LlmUsage(100, 20, None),
            latency_seconds=0.001,
            model="fixture-model",
            provider="fixture-provider",
            finish_reason="stop",
        )

    def close(self) -> None:
        return None


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
        0.50,
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
    assert prepared.selected[0].forced is True
    assert prepared.selected[0].change_ratio == 0.0
    assert prepared.selected[0].global_change == 0.0
    assert prepared.selected[0].region_intensity == 0.0
    assert prepared.selected[0].confirmed_region == ()
    assert prepared.input_csv_missing is True
    assert prepared.input_context.summarize_window(None, 200.0) == "此窗口内无玩家输入"
    assert before == after


def test_character_similarity_flags_repetition_without_semantic_model() -> None:
    assert character_similarity("玩家站在门边。", "玩家仍站在门边。") > 0.6
    assert character_similarity("打开地图。", "进入战斗并抽了一张牌。") < 0.6


def test_local_metrics_use_only_observation_body_and_exclude_effect_markers() -> None:
    rows: list[dict[str, object]] = [
        {
            "frame_ts": 123.4,
            "wall": "2026-08-26T12:34:56Z",
            "text": "【画面】室内场景\n【局部】仅亮度变化",
            "dropped": None,
        },
        {
            "frame_ts": 124.4,
            "wall": "2026-08-26T12:34:57Z",
            "text": "【画面】状态面板\n【局部】中央数值为42",
            "dropped": None,
        },
        {
            "frame_ts": 125.4,
            "wall": "2026-08-26T12:34:58Z",
            "text": "【画面】状态面板\n【局部】中央数值为43",
            "dropped": None,
        },
        {
            "frame_ts": 126.4,
            "wall": "2026-08-26T12:34:59Z",
            "text": "【画面】没有数字的观察\n【局部】r3c5区域时间12:34",
            "dropped": None,
        },
        {
            "frame_ts": 127.4,
            "wall": "2026-08-26T12:35:00Z",
            "text": "【画面】失败项\n【局部】数值99",
            "dropped": "timeout",
        },
    ]
    assert _extract_local(str(rows[0]["text"])) == "仅亮度变化"
    metrics = _local_statistics(rows)
    assert metrics.total == 4
    assert metrics.only_prefix_old == 1
    assert metrics.only_compliant == 1
    assert metrics.only_expanded == 0
    assert metrics.numeric == 2
    assert metrics.grid_leaked == 1
    assert metrics.average_length == 8.0
    informative = [
        body for _row, body in _local_entries(rows) if not body.startswith("仅")
    ]
    assert informative[:2] == ["中央数值为42", "中央数值为43"]
    assert character_similarity(*informative[:2]) > 0.6
    assert _extract_scene(str(rows[1]["text"])) == "状态面板"


def test_only_compliance_requires_prefix_and_four_to_six_chinese_characters() -> None:
    rows: list[dict[str, object]] = [
        {"text": "【局部】仅亮度变化", "dropped": None},
        {"text": "【局部】仅光效", "dropped": None},
        {"text": "【局部】仅区域内亮度正在发生变化", "dropped": None},
        {"text": "【局部】亮度变化", "dropped": None},
    ]
    result = _local_statistics(rows)
    assert result.only_prefix_old == 3
    assert result.only_compliant == 1
    assert result.only_expanded == 1


def test_violation_metrics_separate_testimony_from_speculation() -> None:
    allowed_direct: dict[str, object] = {
        "text": "【画面】视角正在向右转动，正在连续点击。",
    }
    misplaced_intent: dict[str, object] = {
        "text": "【画面】玩家似乎想要浏览模式列表。",
    }
    separated_intent: dict[str, object] = {
        "text": "【画面】选择界面占据画面。\n【推测】似乎正在浏览模式列表。",
    }
    retrospective: dict[str, object] = {
        "text": "【画面】某界面元素出现了。",
    }
    assert not _input_attribution_violation(allowed_direct)
    assert _input_attribution_violation(misplaced_intent)
    assert not _input_attribution_violation(separated_intent)
    assert _extract_speculation(str(separated_intent["text"])) == "似乎正在浏览模式列表。"
    assert _retrospective_violation(retrospective)


def test_dispatch_interval_defaults_to_zero_and_accepts_production_pacing() -> None:
    parser = build_parser()
    common = ["--session", "fixture", "--profile", "vision_fast"]
    parsed = parser.parse_args(common)
    assert parsed.dispatch_interval == 0.0
    assert not hasattr(parsed, "context_lines")
    assert parser.parse_args([*common, "--dispatch-interval", "1.0"]).dispatch_interval == 1.0


def test_metric_echo_detection_only_matches_renderer_values() -> None:
    row: dict[str, object] = {
        "text": "【画面】界面显示30%\n【局部】画面上方约38%的区域正在移动",
        "global_change": 1.6,
        "region_area_ratio": 38,
        "region_intensity": 5,
    }
    assert _echoed_metric_values(row) == ("38%",)


def test_local_1080p_replay_emits_frame_and_mouse_evidence(
    tmp_path: Path,
) -> None:
    session = (
        Path(__file__).parents[1]
        / "recordings"
        / "capture"
        / "20260827-220206"
    )
    if not session.is_dir():
        pytest.skip("local 1080p acceptance recording is not present")
    prepared = _prepare_replay(
        session,
        896,
        SegmentRange(0.0, 60.0),
        WindowTitleMap.load(),
        0.50,
    )
    # WGC records the borderless Full-HD client area as 1920x1079 on this fixture.
    assert (prepared.source_width, prepared.source_height) == (1920, 1079)
    client = LocalReplayFakeClient()
    llm = LlmConfig(
        profiles={
            "vision_fast": LlmProfileConfig(
                enabled=True,
                model="fixture-model",
                provider="fixture-provider",
                temperature=0.0,
                max_tokens=200,
                input_price_per_million_usd=1.0,
                output_price_per_million_usd=2.0,
            )
        }
    )
    adapter = GenericVisionAdapter(
        _adapter_configuration(
            profile="vision_fast",
            send_width=896,
            timeout=1.0,
            max_inflight=4,
            region_focus_max=0.50,
            ocr_enabled=False,
            scene_memory_dir=tmp_path / "memory",
        ),
        llm,
        capture_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("local replay must not initialize capture")
        ),
        selector_factory=lambda _sparsity: (_ for _ in ()).throw(
            AssertionError("local replay selection was already prepared")
        ),
        client_factory=lambda *_args: client,
        title_map=WindowTitleMap.load(),
    )
    output = tmp_path / "local-1080p"

    async def scenario() -> None:
        adapter.start_replay(
            output,
            input_context=prepared.input_context,
            input_window_start_monotonic=prepared.input_window_start_monotonic,
        )
        for item in prepared.selected:
            with Image.open(item.path) as source:
                bitmap = source.convert("RGB")
            frame = CapturedFrame(
                bitmap,
                FrameMetadata(
                    prepared.title,
                    prepared.process_name,
                    item.timing.wall_time,
                    bitmap.width,
                    bitmap.height,
                    item.timing.monotonic_seconds,
                    item.timing.source,
                ),
            )
            await adapter.submit_replay_frame(
                frame,
                prepared.game,
                item.region,
                item.baseline_monotonic_seconds,
                confirmed_region=item.confirmed_region,
                change_ratio=item.change_ratio,
                global_change=item.global_change,
                region_intensity=item.region_intensity,
                forced=item.forced,
            )
        await adapter.finish_replay()

    asyncio.run(scenario())
    events = list(EvidenceStore.read(output / "evidence.jsonl"))
    mouse_events = [event for event in events if event.kind == "mouse_motion"]
    assert mouse_events
    assert len(events) == len(prepared.selected) * 4 + len(mouse_events)
    roots: dict[str, set[str]] = {}
    for event in events:
        if event.root_capture_id is None:
            assert event.source == "mouse"
            continue
        roots.setdefault(event.root_capture_id, set()).add(event.kind)
    assert all(
        kinds
        == {
            "frame_metrics",
            "key_window",
            "scene_fingerprint",
            "fast_observation",
        }
        for kinds in roots.values()
    )
    assert client.calls == len(prepared.selected)
    assert (output / "observations.md").is_file()
    assert not (output / "observations.jsonl").exists()
