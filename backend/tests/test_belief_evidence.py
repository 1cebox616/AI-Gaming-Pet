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
    ObservationsMarkdownWriter,
    Scope,
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
