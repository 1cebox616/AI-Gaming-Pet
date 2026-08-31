from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
import logging
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace

import numpy as np
from PIL import Image
from pydantic import ValidationError
import pytest

from pet.core.belief import EvidenceStore, OcrFramePayload, TextObservedPayload
from pet.core.config import (
    AdapterConfig,
    GenericVisionConfig,
    LlmConfig,
    LlmProfileConfig,
    OcrConfig,
    SceneConfig,
)
from pet.core.llm import LlmResult, LlmUsage
from pet.core.ocr_probe import OcrFrameResult, OcrLine
from pet.core.ocr_rapid import (
    DETECTOR_MODEL_NAME,
    RECOGNIZER_MODEL_NAME,
    RapidOcrEngine,
)
from pet.core.ocr_selective import TextLineCache
from pet.games.generic.adapter import GenericVisionAdapter, WindowTitleMap


@dataclass(frozen=True)
class Metadata:
    window_title: str
    process_name: str
    captured_at: datetime
    monotonic_seconds: float


@dataclass(frozen=True)
class Frame:
    bitmap: Image.Image
    metadata: Metadata


class Client:
    def complete_with_images_stream(self, **_kwargs: object) -> LlmResult:
        return LlmResult(
            text="【画面】fixture",
            usage=LlmUsage(10, 2, None),
            latency_seconds=0.001,
            model="fixture",
            provider="fixture",
            finish_reason="stop",
        )

    def close(self) -> None:
        return None


class FakeEngine:
    def __init__(self, results: list[OcrFrameResult], delay: float = 0.0) -> None:
        self.results = results
        self.delay = delay
        self.calls = 0
        self.started = 0
        self.closed = 0

    def start(self) -> None:
        self.started += 1

    def recognize(self, _image: np.ndarray, /) -> OcrFrameResult:
        index = self.calls
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.results[min(index, len(self.results) - 1)]

    def close(self) -> None:
        self.closed += 1


class MissingEngine(FakeEngine):
    def start(self) -> None:
        raise FileNotFoundError("missing fixture models")


def _line(text: str = "Start", *, x: float = 0.1) -> OcrLine:
    return OcrLine(
        text=text,
        x=x,
        y=0.2,
        width=0.2,
        height=0.05,
        confidence=0.9,
        quad=((x, 0.2), (x + 0.2, 0.2), (x + 0.2, 0.25), (x, 0.25)),
    )


def _result(*lines: OcrLine, duration_ms: float = 1.0) -> OcrFrameResult:
    return OcrFrameResult(
        None,
        160,
        90,
        duration_ms,
        duration_ms / 3,
        tuple(lines),
        det_ms=duration_ms / 2,
        rec_ms=duration_ms / 3,
        cpu_core_seconds=duration_ms / 2000,
    )


def _frame(sequence: int) -> Frame:
    return Frame(
        Image.new("RGB", (160, 90), (sequence, 20, 30)),
        Metadata(
            "Fixture",
            "fixture.exe",
            datetime(2026, 8, 30, 12, 0, sequence, tzinfo=timezone.utc),
            time.perf_counter() + sequence,
        ),
    )


def _adapter(tmp_path: Path, engine: FakeEngine, *, interval: float = 0.03) -> GenericVisionAdapter:
    configuration = AdapterConfig(
        generic=GenericVisionConfig(
            enabled=True,
            poll_interval_seconds=interval,
            input_context=False,
            observation_log_dir=str(tmp_path),
            ocr=OcrConfig(enabled=True),
            scene=SceneConfig(enabled=False),
        )
    )
    llm = LlmConfig(
        profiles={
            "vision_fast": LlmProfileConfig(
                enabled=True,
                model="fixture",
                provider="fixture",
                input_price_per_million_usd=1.0,
                output_price_per_million_usd=1.0,
            )
        }
    )
    return GenericVisionAdapter(
        configuration,
        llm,
        capture_backend_factory=lambda: pytest.fail("capture not expected"),
        selector_factory=lambda _value: pytest.fail("selector not expected"),
        client_factory=lambda *_args: Client(),
        title_map=WindowTitleMap(()),
        ocr_engine=engine,
    )


async def _submit(adapter: GenericVisionAdapter, frame: Frame) -> None:
    await adapter.submit_replay_frame(
        frame,
        "Fixture",
        ("r1c1",),
        frame.metadata.monotonic_seconds - 1.0,
        confirmed_region=("r1c1",),
        change_ratio=1 / 144,
        global_change=5.0,
        region_intensity=50.0,
        forced=False,
    )


def test_ocr_payload_geometry_and_changed_previous_text_validation() -> None:
    valid = dict(
        text="start",
        bbox=(0.1, 0.2, 0.3, 0.25),
        quad=((0.1, 0.2), (0.3, 0.2), (0.3, 0.25), (0.1, 0.25)),
        change="changed",
        previous_text="stop",
        streak=1,
        engine="fixture",
        engine_confidence=0.9,
    )
    TextObservedPayload(**valid)
    for override in (
        {"previous_text": None},
        {"streak": 0},
        {"bbox": (-0.1, 0.2, 0.3, 0.25)},
        {"quad": ((0.1, 0.2), (1.1, 0.2), (0.3, 0.25), (0.1, 0.25))},
    ):
        with pytest.raises(ValidationError):
            TextObservedPayload(**(valid | override))


def test_text_line_cache_emits_all_four_production_changes() -> None:
    cache = TextLineCache()
    # ROOT CAUSE: OCR text can jitter across frames; cross-frame diffing is the
    # only source of new/changed/gone/stable semantics in the sensor stream.
    new = cache.update((_line("Start"),))
    stable = cache.update((_line("Start"),))
    changed = cache.update((_line("Stop"),))
    gone = cache.update(())
    assert new.lines[0].kind == "added"
    assert stable.lines[0].kind == "unchanged" and stable.lines[0].streak == 2
    assert changed.lines[0].kind == "changed"
    assert changed.lines[0].previous_text == "Start"
    assert gone.gone[0].text == "Stop"


def test_multiple_same_frame_ocr_events_round_trip_without_id_collision(tmp_path: Path) -> None:
    store = EvidenceStore.open(tmp_path)
    payloads = [
        TextObservedPayload(
            text=text,
            bbox=(0.1, 0.2, 0.3, 0.25),
            quad=None,
            change="new",
            previous_text=None,
            streak=1,
            engine="fixture",
            engine_confidence=None,
        )
        for text in ("one", "two")
    ]
    from pet.core.belief import EvidenceEvent

    events = []
    for payload in payloads:
        events.append(
            EvidenceEvent(
                evidence_id=store.new_evidence_id("f1", "ocr"),
                source="ocr",
                kind="text_observed",
                root_capture_id="f1",
                observed_at=0.0,
                learned_at=0.1,
                scope=None,
                payload=payload,
                derived_from=[],
                context_version=None,
                outcome="ok",
            )
        )
        store.append(events[-1])
    store.close()
    assert [event.evidence_id for event in events] == ["f1:ocr:1", "f1:ocr:2"]
    assert list(EvidenceStore.read(tmp_path / "evidence.jsonl")) == events


def test_slow_ocr_is_nonblocking_late_and_never_queues(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeEngine, Path, float]:
        engine = FakeEngine([_result(_line(), duration_ms=100.0)], delay=0.12)
        adapter = _adapter(tmp_path, engine, interval=0.03)
        output = tmp_path / "run"
        adapter.start_replay(output, input_context=None)
        started = time.perf_counter()
        await _submit(adapter, _frame(1))
        await _submit(adapter, _frame(2))
        await _submit(adapter, _frame(3))
        dispatch_elapsed = time.perf_counter() - started
        await adapter.finish_replay()
        return engine, output, dispatch_elapsed

    engine, output, dispatch_elapsed = asyncio.run(scenario())
    assert dispatch_elapsed < 0.08
    assert engine.calls == 1
    events = list(EvidenceStore.read(output / "evidence.jsonl"))
    frames = [event.payload for event in events if isinstance(event.payload, OcrFramePayload)]
    assert len(frames) == 3
    assert all(payload.outcome_detail == "late" for payload in frames)
    assert not any(isinstance(event.payload, TextObservedPayload) for event in events)


def test_skipped_poll_does_not_advance_diff_state(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeEngine, Path]:
        engine = FakeEngine([_result(_line("Same")), _result(_line("Same"))])
        adapter = _adapter(tmp_path, engine, interval=0.05)
        output = tmp_path / "run"
        adapter.start_replay(output, input_context=None)
        await _submit(adapter, _frame(1))
        await asyncio.sleep(0.01)
        # ROOT CAUSE: no-change polling is skipped to save CPU, but the OCR cache
        # must span that skipped frame so stability does not restart at streak=1.
        await asyncio.sleep(0.01)
        await _submit(adapter, _frame(3))
        await adapter.finish_replay()
        return engine, output

    engine, output = asyncio.run(scenario())
    assert engine.calls == 2
    events = list(EvidenceStore.read(output / "evidence.jsonl"))
    texts = [event.payload for event in events if isinstance(event.payload, TextObservedPayload)]
    assert [(payload.change, payload.streak) for payload in texts] == [("new", 1), ("stable", 2)]


def test_unavailable_engine_logs_once_and_adapter_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> Path:
        adapter = _adapter(tmp_path, MissingEngine([_result()]))
        output = tmp_path / "run"
        adapter.start_replay(output, input_context=None)
        await _submit(adapter, _frame(1))
        await adapter.finish_replay()
        return output

    with caplog.at_level(logging.ERROR):
        output = asyncio.run(scenario())
    assert sum("OCR 引擎不可用" in record.message for record in caplog.records) == 1
    events = list(EvidenceStore.read(output / "evidence.jsonl"))
    assert not any(event.source == "ocr" for event in events)


def test_ocr_config_rejects_unknown_engine_and_zero_threads() -> None:
    with pytest.raises(ValidationError):
        OcrConfig(engine="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        OcrConfig(num_threads=0)


def test_configured_model_directory_must_contain_both_local_models(tmp_path: Path) -> None:
    configured_model_dir = tmp_path / "configured-models"
    settings = OcrConfig(model_dir=str(configured_model_dir))
    engine = RapidOcrEngine(
        model_dir=Path(settings.model_dir),
        num_threads=settings.num_threads,
        det_limit_side_len=settings.det_limit_side_len,
    )
    assert engine.model_dir == configured_model_dir
    with pytest.raises(FileNotFoundError) as caught:
        engine.start()
    message = str(caught.value)
    assert str(configured_model_dir / DETECTOR_MODEL_NAME) in message
    assert str(configured_model_dir / RECOGNIZER_MODEL_NAME) in message
    assert engine._engine is None


def test_rapidocr_392_classifier_is_never_constructed_or_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ROOT CAUSE: RapidOCR 3.9.2 constructs TextClassifier even when
    # Global.use_cls=false. The private _initialize override prevents that.
    # A failure after upgrading rapidocr means the override must be reviewed,
    # not that this regression test should be weakened.
    assert version("rapidocr") == "3.9.2"
    calls = {"classifier_constructed": 0, "classifier_called": 0}

    class FakeEngineType:
        OPENVINO = "openvino"

    class FakeLang:
        CH = "ch"

    class FakeModelType:
        TINY = "tiny"

    class FakeOcrVersion:
        PPOCRV6 = "ppocrv6"

    class FakeComponent:
        def __init__(self, _cfg: object = None) -> None:
            return None

    class FakeClassifier(FakeComponent):
        def __init__(self, _cfg: object = None) -> None:
            calls["classifier_constructed"] += 1

        def __call__(self, _images: object) -> object:
            calls["classifier_called"] += 1
            return _images

    class FakeRapidOcr:
        def __init__(self, *, params: dict[str, object]) -> None:
            del params
            engine_type = SimpleNamespace(value="openvino")
            cfg = SimpleNamespace(
                Global=SimpleNamespace(
                    text_score=0.5,
                    min_height=1,
                    width_height_ratio=8.0,
                    use_det=True,
                    use_rec=True,
                    model_root_dir=None,
                    font_path=None,
                    max_side_len=2000,
                    min_side_len=30,
                    return_word_box=False,
                    return_single_char_box=False,
                ),
                Det=SimpleNamespace(engine_type=engine_type),
                Rec=SimpleNamespace(engine_type=engine_type),
                EngineConfig={"openvino": {}},
            )
            self._initialize(cfg)

        def __call__(self, image: np.ndarray, *, use_cls: bool) -> SimpleNamespace:
            self.use_cls = use_cls
            if self.use_cls:
                self.text_cls((image,))
            return SimpleNamespace(
                boxes=None,
                txts=None,
                scores=None,
                elapse_list=(0.0, 0.0, 0.0),
            )

    rapidocr_module = ModuleType("rapidocr")
    rapidocr_module.EngineType = FakeEngineType
    rapidocr_module.LangDet = FakeLang
    rapidocr_module.LangRec = FakeLang
    rapidocr_module.ModelType = FakeModelType
    rapidocr_module.OCRVersion = FakeOcrVersion
    rapidocr_main_module = ModuleType("rapidocr.main")
    rapidocr_main_module.CalRecBoxes = FakeComponent
    rapidocr_main_module.LoadImage = FakeComponent
    rapidocr_main_module.RapidOCR = FakeRapidOcr
    rapidocr_main_module.TextClassifier = FakeClassifier
    rapidocr_main_module.TextDetector = FakeComponent
    rapidocr_main_module.TextRecognizer = FakeComponent
    monkeypatch.setitem(sys.modules, "rapidocr", rapidocr_module)
    monkeypatch.setitem(sys.modules, "rapidocr.main", rapidocr_main_module)

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / DETECTOR_MODEL_NAME).touch()
    (model_dir / RECOGNIZER_MODEL_NAME).touch()
    engine = RapidOcrEngine(model_dir=model_dir, num_threads=2, det_limit_side_len=1280)
    engine.start()

    assert engine._engine.use_cls is False
    assert engine._engine.text_cls is None
    engine.recognize(np.zeros((64, 64, 3), dtype=np.uint8))
    assert calls == {"classifier_constructed": 0, "classifier_called": 0}
