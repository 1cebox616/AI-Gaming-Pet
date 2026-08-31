"""Append-only JSONL persistence for typed evidence events."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import TextIO

from pet.core.belief.models import EvidenceEvent


class EvidenceStore:
    """The only writer for evidence.jsonl; existing rows are never rewritten."""

    def __init__(self, path: Path, stream: TextIO) -> None:
        self._path = path
        self._stream = stream
        self._counters: dict[tuple[str, str], int] = {}
        self._non_frame_sequence = 0

    @classmethod
    def open(cls, directory: Path) -> EvidenceStore:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "evidence.jsonl"
        stream = path.open("w", encoding="utf-8", newline="\n")
        return cls(path, stream)

    def append(self, event: EvidenceEvent) -> None:
        if self._stream.closed:
            raise RuntimeError("evidence store is closed")
        value = event.model_dump(mode="json")
        self._stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        self._stream.flush()

    def new_evidence_id(self, root_capture_id: str | None, source: str) -> str:
        if not source or ":" in source:
            raise ValueError("source must be a non-empty evidence source name")
        if root_capture_id is None:
            self._non_frame_sequence += 1
            return f"n{self._non_frame_sequence:012d}:{source}"
        if (
            not root_capture_id.startswith("f")
            or not root_capture_id[1:].isdigit()
            or int(root_capture_id[1:]) < 1
        ):
            raise ValueError("root_capture_id must use the form f<positive sequence>")
        key = (root_capture_id, source)
        sequence = self._counters.get(key, 0) + 1
        self._counters[key] = sequence
        return f"{root_capture_id}:{source}:{sequence}"

    @staticmethod
    def read(path: Path) -> Iterator[EvidenceEvent]:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield EvidenceEvent.model_validate_json(line)

    def close(self) -> None:
        self._stream.close()
