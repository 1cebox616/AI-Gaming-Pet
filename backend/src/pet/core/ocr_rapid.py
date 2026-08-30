"""Fixed PP-OCRv6 tiny/OpenVINO engine used by the generic OCR sensor."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import time
from typing import Any

import numpy as np

from pet.core.ocr_probe import OcrFrameResult, OcrLine


ENGINE_NAME = "rapidocr-ppocrv6-tiny-openvino"
DETECTOR_MODEL_NAME = "PP-OCRv6_det_tiny.onnx"
RECOGNIZER_MODEL_NAME = "PP-OCRv6_rec_tiny.onnx"


def set_current_thread_below_normal() -> None:
    """Lower only the dedicated OCR worker thread on Windows."""

    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThread.restype = ctypes.c_void_p
    kernel32.SetThreadPriority.argtypes = (ctypes.c_void_p, ctypes.c_int)
    kernel32.SetThreadPriority.restype = ctypes.c_int
    thread_priority_below_normal = -1
    if not kernel32.SetThreadPriority(
        kernel32.GetCurrentThread(), thread_priority_below_normal
    ):
        raise OSError(ctypes.get_last_error(), "SetThreadPriority failed")


class RapidOcrEngine:
    """Persistent, explicitly model-pinned RapidOCR OpenVINO implementation."""

    name = ENGINE_NAME

    def __init__(
        self,
        *,
        model_dir: Path,
        num_threads: int,
        det_limit_side_len: int,
    ) -> None:
        if num_threads < 1:
            raise ValueError("num_threads must be at least one")
        self.model_dir = model_dir
        self.num_threads = num_threads
        self.det_limit_side_len = det_limit_side_len
        self._engine: Any = None

    def start(self) -> None:
        if self._engine is not None:
            return
        detector = self.model_dir / DETECTOR_MODEL_NAME
        recognizer = self.model_dir / RECOGNIZER_MODEL_NAME
        missing = [path for path in (detector, recognizer) if not path.is_file()]
        if missing:
            names = ", ".join(str(path) for path in missing)
            raise FileNotFoundError(f"OCR model file missing: {names}")

        os.environ["OMP_NUM_THREADS"] = str(self.num_threads)
        import cv2
        from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion
        from rapidocr.main import CalRecBoxes, LoadImage, RapidOCR, TextDetector, TextRecognizer

        cv2.setNumThreads(self.num_threads)
        params = {
            "Global.use_cls": False,
            "Global.log_level": "error",
            "Det.engine_type": EngineType.OPENVINO,
            "Det.lang_type": LangDet.CH,
            "Det.model_type": ModelType.TINY,
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Det.model_path": str(detector),
            "Det.limit_type": "max",
            "Det.limit_side_len": self.det_limit_side_len,
            "Rec.engine_type": EngineType.OPENVINO,
            "Rec.lang_type": LangRec.CH,
            "Rec.model_type": ModelType.TINY,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_path": str(recognizer),
            "EngineConfig.openvino.inference_num_threads": self.num_threads,
        }
        class RapidOcrWithoutClassifier(RapidOCR):
            """RapidOCR 3.9.2 otherwise constructs Cls even when use_cls=false."""

            def _initialize(self, cfg: Any) -> None:
                self.text_score = cfg.Global.text_score
                self.min_height = cfg.Global.min_height
                self.width_height_ratio = cfg.Global.width_height_ratio
                self.use_det = cfg.Global.use_det
                cfg.Det.engine_cfg = cfg.EngineConfig[cfg.Det.engine_type.value]
                cfg.Det.model_root_dir = cfg.Global.model_root_dir
                self.text_det = TextDetector(cfg.Det)
                self.use_cls = False
                self.text_cls = None
                self.use_rec = cfg.Global.use_rec
                cfg.Rec.engine_cfg = cfg.EngineConfig[cfg.Rec.engine_type.value]
                cfg.Rec.font_path = cfg.Global.font_path
                cfg.Rec.model_root_dir = cfg.Global.model_root_dir
                self.text_rec = TextRecognizer(cfg.Rec)
                self.load_img = LoadImage()
                self.max_side_len = cfg.Global.max_side_len
                self.min_side_len = cfg.Global.min_side_len
                self.cal_rec_boxes = CalRecBoxes()
                self.return_word_box = cfg.Global.return_word_box
                self.return_single_char_box = cfg.Global.return_single_char_box
                self.cfg = cfg

        self._engine = RapidOcrWithoutClassifier(params=params)
        # Initialization and graph compilation finish before gameplay. The blank
        # result is intentionally discarded.
        self._recognize(np.zeros((64, 64, 3), dtype=np.uint8))

    def recognize(self, image: np.ndarray, /) -> OcrFrameResult:
        if self._engine is None:
            raise RuntimeError("RapidOCR engine is not started")
        if not isinstance(image, np.ndarray) or image.ndim != 3:
            raise TypeError("RapidOCR expects an HxWxC numpy array")
        return self._recognize(image)

    def _recognize(self, image: np.ndarray) -> OcrFrameResult:
        import psutil

        process = psutil.Process(os.getpid())
        before = process.cpu_times()
        started = time.perf_counter()
        result = self._engine(image, use_cls=False)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        after = process.cpu_times()
        cpu_core_seconds = max(
            0.0,
            (after.user + after.system) - (before.user + before.system),
        )

        height, width = image.shape[:2]
        boxes = result.boxes if result.boxes is not None else ()
        texts = result.txts if result.txts is not None else ()
        scores = result.scores if result.scores is not None else ()
        lines: list[OcrLine] = []
        for box, text, confidence in zip(boxes, texts, scores, strict=True):
            quad = tuple(
                (
                    min(1.0, max(0.0, float(point[0]) / width)),
                    min(1.0, max(0.0, float(point[1]) / height)),
                )
                for point in box
            )
            xs = [point[0] for point in quad]
            ys = [point[1] for point in quad]
            lines.append(
                OcrLine(
                    text=str(text),
                    x=min(xs),
                    y=min(ys),
                    width=max(xs) - min(xs),
                    height=max(ys) - min(ys),
                    confidence=float(confidence),
                    quad=quad,
                )
            )
        stages = result.elapse_list or (None, None, None)
        return OcrFrameResult(
            path=None,
            width=width,
            height=height,
            duration_ms=elapsed_ms,
            recognize_ms=float(stages[2] or 0.0) * 1000.0,
            lines=tuple(lines),
            det_ms=float(stages[0] or 0.0) * 1000.0,
            rec_ms=float(stages[2] or 0.0) * 1000.0,
            cpu_core_seconds=cpu_core_seconds,
        )

    def close(self) -> None:
        self._engine = None
