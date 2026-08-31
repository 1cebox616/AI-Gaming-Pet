"""Regenerable human view over the append-only generic evidence stream."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
import logging
from pathlib import Path
import re
from typing import TextIO

from pet.core.belief.models import (
    EvidenceEvent,
    FastObservationPayload,
    FrameMetricsPayload,
    KeyWindowPayload,
)

_ROOT_PATTERN = re.compile(r"f(?P<sequence>[1-9]\d*)\Z")
logger = logging.getLogger(__name__)
_RENDERED_KINDS = {"frame_metrics", "key_window", "fast_observation"}


def _sequence(root_capture_id: str | None) -> int:
    match = _ROOT_PATTERN.fullmatch(root_capture_id or "")
    if match is None:
        raise ValueError(f"invalid frame root_capture_id: {root_capture_id!r}")
    return int(match.group("sequence"))


def _group_events(
    events: Iterable[EvidenceEvent],
) -> dict[int, dict[str, list[EvidenceEvent]]]:
    grouped: dict[int, dict[str, list[EvidenceEvent]]] = {}
    for event in events:
        if event.kind not in _RENDERED_KINDS:
            continue
        sequence = _sequence(event.root_capture_id)
        frame = grouped.setdefault(sequence, {})
        frame.setdefault(event.kind, []).append(event)
    return grouped


def _render_block(frame: dict[str, list[EvidenceEvent]]) -> str:
    missing = _RENDERED_KINDS.difference(frame)
    if missing:
        raise ValueError(f"cannot render incomplete evidence group: {sorted(missing)}")
    metrics_event = frame["frame_metrics"][0]
    key_event = frame["key_window"][0]
    fast_event = frame["fast_observation"][0]
    if not isinstance(metrics_event.payload, FrameMetricsPayload):
        raise TypeError("frame_metrics evidence has the wrong payload")
    if not isinstance(key_event.payload, KeyWindowPayload):
        raise TypeError("key_window evidence has the wrong payload")
    if not isinstance(fast_event.payload, FastObservationPayload):
        raise TypeError("fast_observation evidence has the wrong payload")
    metrics = metrics_event.payload
    fast = fast_event.payload
    relative_seconds = max(0, round(metrics_event.observed_at))
    heartbeat = "（心跳）" if metrics.heartbeat else ""
    lines = [f"T+{relative_seconds}{heartbeat}："]
    if fast_event.outcome != "ok":
        lines.append(f"[丢弃：{fast.drop_reason}]")
    else:
        scene = fast.scene
        if scene is None:
            scene = " ".join(fast.text.split())
        lines.append(f"【全局画面】（全局像素变化{metrics.global_change:.1f}%）{scene}")
        if metrics.region_area_ratio is not None and fast.local is not None:
            assert metrics.region_intensity is not None
            lines.append(
                f"【局部｜区域占比{metrics.region_area_ratio:.0f}%】"
                f"（区域像素变化{metrics.region_intensity:.0f}%）{fast.local}"
            )
        if fast.speculation:
            lines.append(f"【推测】{fast.speculation}")
    lines.extend((f"【玩家输入】{key_event.payload.summary}", ""))
    return "\n".join(lines) + "\n"


def render_observations_markdown(
    events: Iterable[EvidenceEvent],
    started_at: datetime,
) -> str:
    grouped = _group_events(events)
    header = "# 通用视觉观察日志\n\n" f"本会话始于 {started_at.isoformat()}\n\n"
    return header + "".join(_render_block(grouped[sequence]) for sequence in sorted(grouped))


class ObservationsMarkdownWriter:
    """Incrementally render complete frame groups while preserving frame order."""

    def __init__(self, path: Path, started_at: datetime) -> None:
        self._stream: TextIO = path.open("w", encoding="utf-8", newline="\n")
        self._stream.write(
            "# 通用视觉观察日志\n\n"
            f"本会话始于 {started_at.isoformat()}\n\n"
        )
        self._stream.flush()
        self._pending: dict[int, dict[str, list[EvidenceEvent]]] = {}
        self._next_sequence = 1

    def append(self, event: EvidenceEvent) -> None:
        if event.kind not in _RENDERED_KINDS:
            return
        sequence = _sequence(event.root_capture_id)
        if sequence < self._next_sequence:
            raise ValueError(f"late rendered evidence for frame f{sequence}")
        frame = self._pending.setdefault(sequence, {})
        frame.setdefault(event.kind, []).append(event)
        self._flush_ready()

    def append_many(self, events: Sequence[EvidenceEvent]) -> None:
        for event in events:
            self.append(event)

    def close(self) -> None:
        self._flush_ready()
        incomplete = [
            sequence
            for sequence, frame in sorted(self._pending.items())
            if not _RENDERED_KINDS.issubset(frame)
        ]
        if incomplete:
            logger.warning(
                "观察日志关闭时存在不完整帧组：%s",
                ", ".join(f"f{sequence}" for sequence in incomplete),
            )
        self._stream.close()

    def _flush_ready(self) -> None:
        required = _RENDERED_KINDS
        while required.issubset(self._pending.get(self._next_sequence, {})):
            frame = self._pending.pop(self._next_sequence)
            self._stream.write(_render_block(frame))
            self._stream.flush()
            self._next_sequence += 1
