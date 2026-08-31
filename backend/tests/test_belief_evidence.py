from __future__ import annotations

from datetime import datetime, timezone
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from pet.core.belief import (
    EvidenceEvent,
    EvidenceStore,
    FastObservationPayload,
    FrameMetricsPayload,
    KeyWindowPayload,
    MouseMotionPayload,
    OcrFramePayload,
    ObservationsMarkdownWriter,
    Scope,
    TextObservedPayload,
    render_observations_markdown,
)
from pet.games.generic.adapter import _focus_geometry, _focus_scope


def _scope() -> Scope:
    return _focus_scope(("r2c3", "r3c3"))


def _events() -> tuple[EvidenceEvent, EvidenceEvent, EvidenceEvent]:
    scope = _scope()
    metrics = EvidenceEvent(
        evidence_id="f1:detector:1",
        source="detector",
        kind="frame_metrics",
        root_capture_id="f1",
        observed_at=0.0,
        learned_at=0.0,
        scope=scope,
        payload=FrameMetricsPayload(
            reason="sparse",
            change_ratio=2 / 144,
            global_change=6.25,
            region_area_ratio=scope.area_ratio,
            region_intensity=60.4,
            heartbeat=False,
            wall="2026-08-29T12:34:56+00:00",
        ),
        derived_from=[],
        context_version=None,
        outcome="ok",
    )
    key_window = EvidenceEvent(
        evidence_id="f1:input:1",
        source="input",
        kind="key_window",
        root_capture_id="f1",
        observed_at=0.0,
        learned_at=0.0,
        scope=None,
        payload=KeyWindowPayload(
            summary="W 按住 0.6 秒",
            window_start=None,
            window_end=0.0,
        ),
        derived_from=[],
        context_version=None,
        outcome="ok",
    )
    fast = EvidenceEvent(
        evidence_id="f1:fast:1",
        source="fast",
        kind="fast_observation",
        root_capture_id="f1",
        observed_at=0.0,
        learned_at=0.125,
        scope=scope,
        payload=FastObservationPayload(
            text="【画面】主区域保持清晰\n【局部】左上对象正在移动\n【推测】似乎正在查看界面",
            scene="主区域保持清晰",
            local="左上对象正在移动",
            speculation="似乎正在查看界面",
            game="Fixture",
            latency_ms=125.0,
            ttft_ms=80.0,
            visible_output_tokens=20,
            input_tokens=100,
            output_tokens=20,
            truncated=False,
            cost_usd=0.00014,
            actual_model="fixture-model",
            actual_provider="fixture-provider",
            user_prompt="fixture prompt",
            drop_reason=None,
        ),
        derived_from=[],
        context_version=None,
        outcome="ok",
    )
    return metrics, key_window, fast


def test_evidence_store_has_only_append_only_public_api_and_round_trips(
    tmp_path: Path,
) -> None:
    public_methods = {
        name
        for name, member in inspect.getmembers(EvidenceStore)
        if not name.startswith("_") and callable(member)
    }
    assert public_methods == {"open", "append", "new_evidence_id", "read", "close"}

    store = EvidenceStore.open(tmp_path)
    metrics, key_window, fast = _events()
    assert store.new_evidence_id("f9", "fast") == "f9:fast:1"
    assert store.new_evidence_id("f9", "fast") == "f9:fast:2"
    assert store.new_evidence_id("f9", "input") == "f9:input:1"
    for expected_lines, event in enumerate((metrics, key_window, fast), start=1):
        store.append(event)
        lines = (tmp_path / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == expected_lines
    store.close()

    assert list(EvidenceStore.read(tmp_path / "evidence.jsonl")) == [
        metrics,
        key_window,
        fast,
    ]


def test_non_frame_mouse_evidence_ids_are_unique_sortable_and_round_trip(
    tmp_path: Path,
) -> None:
    store = EvidenceStore.open(tmp_path)
    first_id = store.new_evidence_id(None, "mouse")
    second_id = store.new_evidence_id(None, "mouse")
    assert first_id == "n000000000001:mouse"
    assert second_id == "n000000000002:mouse"
    assert first_id < second_id
    events = [
        EvidenceEvent(
            evidence_id=evidence_id,
            source="mouse",
            kind="mouse_motion",
            root_capture_id=None,
            observed_at=float(index),
            learned_at=float(index) + 0.01,
            scope=None,
            payload=MouseMotionPayload(
                window_start=float(index - 1),
                window_end=float(index),
                direction="right",
                magnitude="moderate",
                raw_count_total=600 + index,
                estimated_degrees=None,
            ),
            derived_from=[],
            context_version=None,
            outcome="ok",
        )
        for index, evidence_id in enumerate((first_id, second_id), start=1)
    ]
    for event in events:
        store.append(event)
    store.close()

    assert list(EvidenceStore.read(tmp_path / "evidence.jsonl")) == events


def test_non_frame_relaxation_does_not_weaken_frame_evidence_validation() -> None:
    metrics, _, _ = _events()
    with pytest.raises(ValidationError, match="frame evidence requires root_capture_id"):
        EvidenceEvent.model_validate(
            {
                **metrics.model_dump(),
                "evidence_id": "n000000000001:detector",
                "root_capture_id": None,
            }
        )
    with pytest.raises(ValidationError, match="must not have root_capture_id"):
        EvidenceEvent(
            evidence_id="f1:mouse:1",
            source="mouse",
            kind="mouse_motion",
            root_capture_id="f1",
            observed_at=1.0,
            learned_at=1.0,
            scope=None,
            payload=MouseMotionPayload(
                window_start=0.0,
                window_end=1.0,
                direction="right",
                magnitude="moderate",
                raw_count_total=600,
                estimated_degrees=None,
            ),
            derived_from=[],
            context_version=None,
            outcome="ok",
        )


def test_observations_markdown_is_regenerable_and_incremental_bytes_match(
    tmp_path: Path,
) -> None:
    started_at = datetime(2026, 8, 29, 12, 34, 56, tzinfo=timezone.utc)
    events = _events()
    expected = render_observations_markdown(events, started_at)
    writer_path = tmp_path / "observations.md"
    writer = ObservationsMarkdownWriter(writer_path, started_at)
    writer.append(events[2])
    writer.append(events[1])
    writer.append(events[0])
    writer.close()
    assert writer_path.read_bytes() == expected.encode("utf-8")


def test_ocr_events_do_not_change_observations_markdown_bytes(tmp_path: Path) -> None:
    started_at = datetime(2026, 8, 29, 12, 34, 56, tzinfo=timezone.utc)
    baseline = _events()
    ocr = (
        EvidenceEvent(
            evidence_id="f1:ocr:1",
            source="ocr",
            kind="ocr_frame",
            root_capture_id="f1",
            observed_at=0.0,
            learned_at=0.1,
            scope=None,
            payload=OcrFramePayload(
                engine="rapidocr-ppocrv6-tiny-openvino",
                num_threads=2,
                det_limit_side_len=1280,
                recognized_line_count=2,
                elapsed_ms=100.0,
                det_ms=60.0,
                rec_ms=30.0,
                cpu_core_seconds=0.2,
                trigger="detector",
                outcome_detail="ok",
            ),
            derived_from=[],
            context_version=None,
            outcome="ok",
        ),
        EvidenceEvent(
            evidence_id="f1:ocr:2",
            source="ocr",
            kind="text_observed",
            root_capture_id="f1",
            observed_at=0.0,
            learned_at=0.1,
            scope=None,
            payload=TextObservedPayload(
                text="开始游戏",
                bbox=(0.1, 0.2, 0.3, 0.25),
                quad=((0.1, 0.2), (0.3, 0.2), (0.3, 0.25), (0.1, 0.25)),
                change="new",
                previous_text=None,
                streak=1,
                engine="rapidocr-ppocrv6-tiny-openvino",
                engine_confidence=0.95,
            ),
            derived_from=[],
            context_version=None,
            outcome="ok",
        ),
    )
    expected = render_observations_markdown(baseline, started_at).encode("utf-8")
    assert render_observations_markdown((*baseline, *ocr), started_at).encode("utf-8") == expected

    path = tmp_path / "observations.md"
    writer = ObservationsMarkdownWriter(path, started_at)
    writer.append_many((*ocr, *baseline))
    writer.close()
    assert path.read_bytes() == expected


@pytest.mark.parametrize(
    ("cells", "expected_location"),
    [
        (("r1c1",), "左上"),
        (("r8c5",), "中央"),
        (("r16c9",), "右下"),
        (("r6c1", "r11c3"), "左侧"),
    ],
)
def test_scope_bbox_center_matches_focus_geometry_location(
    cells: tuple[str, ...],
    expected_location: str,
) -> None:
    scope = _focus_scope(cells)
    assert scope.location == expected_location == _focus_geometry(cells)[0]
    x0, y0, x1, y1 = scope.bbox
    horizontal = 0 if (x0 + x1) / 2 <= 1 / 3 else (1 if (x0 + x1) / 2 <= 2 / 3 else 2)
    vertical = 0 if (y0 + y1) / 2 <= 1 / 3 else (1 if (y0 + y1) / 2 <= 2 / 3 else 2)
    expected_cells = (
        ("左上", "上方", "右上"),
        ("左侧", "中央", "右侧"),
        ("左下", "下方", "右下"),
    )
    assert expected_cells[vertical][horizontal] == scope.location


def test_evidence_kind_rejects_the_wrong_payload_type() -> None:
    with pytest.raises(ValidationError, match="payload does not match evidence kind"):
        EvidenceEvent(
            evidence_id="f1:fast:1",
            source="fast",
            kind="fast_observation",
            root_capture_id="f1",
            observed_at=0.0,
            learned_at=0.0,
            scope=None,
            payload=KeyWindowPayload(summary="无输入", window_start=None, window_end=0.0),
            derived_from=[],
            context_version=None,
            outcome="ok",
        )
