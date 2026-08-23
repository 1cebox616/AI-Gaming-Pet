"""Measure all six capture metrics on synthetic 1920x1080 frames."""

from __future__ import annotations

import statistics
import time

import numpy as np

from pet.core.capture import FrameChangeDetector, FrameComparisonTracker

WIDTH = 1920
HEIGHT = 1080
WARMUP_RUNS = 5
MEASURED_RUNS = 50


def main() -> None:
    rng = np.random.default_rng(20260823)
    tracker = FrameComparisonTracker(FrameChangeDetector())
    tracker.observe(rng.integers(0, 256, (HEIGHT, WIDTH, 3), dtype=np.uint8))
    durations_ms: list[float] = []

    for index in range(WARMUP_RUNS + MEASURED_RUNS):
        frame = rng.integers(0, 256, (HEIGHT, WIDTH, 3), dtype=np.uint8)
        started = time.perf_counter()
        tracker.observe(frame)
        duration_ms = (time.perf_counter() - started) * 1000
        if index >= WARMUP_RUNS:
            durations_ms.append(duration_ms)

    print(f"Input: {WIDTH}x{HEIGHT} synthetic RGB image")
    print(f"Warm-up runs: {WARMUP_RUNS}; measured runs: {MEASURED_RUNS}")
    print(f"Six-metric median: {statistics.median(durations_ms):.3f} ms")
    print(f"Six-metric maximum: {max(durations_ms):.3f} ms")


if __name__ == "__main__":
    main()
