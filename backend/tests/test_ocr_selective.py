from __future__ import annotations

import threading
import time

from pet.core.ocr_probe import OcrLine
from pet.core.ocr_selective import (
    DiffLine,
    CropGroup,
    PersistentWinRtOcrWorker,
    SceneResetRamp,
    TextLineCache,
    build_crop_groups,
    crop_fits_remaining_budget,
    prove_thread_submission_is_nonblocking,
    select_crop_budget,
    simulate_nonblocking_cadence,
)


def _line(text: str, x: float, y: float, *, height: float = 0.02) -> OcrLine:
    return OcrLine(text, x, y, 0.12, height)


def test_cache_classifies_added_changed_disappeared_and_stable() -> None:
    cache = TextLineCache()
    first = cache.update((_line("生命 52", 0.1, 0.1),))
    assert [item.kind for item in first.lines] == ["added"]
    assert first.stable_count == 0

    second = cache.update((_line("生命 52", 0.102, 0.101),))
    assert [item.kind for item in second.lines] == ["unchanged"]
    assert second.lines[0].streak == 2
    assert second.stable_count == 1

    third = cache.update((_line("生命 45", 0.101, 0.102),))
    assert [item.kind for item in third.lines] == ["changed"]
    assert third.lines[0].streak == 1
    assert third.stable_count == 0

    fourth = cache.update(())
    assert fourth.disappeared == ("生命 45",)
    assert cache.lines == ()


def test_scene_reset_invalidates_cache_without_pairing_disappearances() -> None:
    cache = TextLineCache()
    cache.update((_line("旧界面", 0.1, 0.1),))
    reset = cache.update(
        (_line("新界面一", 0.6, 0.1), _line("新界面二", 0.6, 0.2)),
        scene_reset=True,
    )
    assert reset.scene_reset is True
    assert [item.kind for item in reset.lines] == ["added", "added"]
    assert reset.disappeared == ()
    assert reset.stable_count == 0


def test_cache_ignores_punctuation_jitter_but_not_alphanumeric_changes() -> None:
    cache = TextLineCache()
    cache.update((_line("生命：52 / 75", 0.70, 0.10),))
    punctuation_only = cache.update((_line("生命 52／75。", 0.68, 0.101),))
    assert punctuation_only.lines[0].kind == "unchanged"
    assert punctuation_only.lines[0].streak == 2
    value_change = cache.update((_line("生命 45／75", 0.69, 0.102),))
    assert value_change.lines[0].kind == "changed"
    assert value_change.lines[0].streak == 1


def test_crop_groups_merge_adjacent_lines_and_use_pure_geometry_order() -> None:
    changes = (
        DiffLine("added", "a", _line("a", 0.1, 0.10), 1),
        DiffLine("changed", "b", _line("b", 0.1, 0.125), 1),
        DiffLine("added", "c", _line("c", 0.7, 0.85, height=0.03), 1),
        DiffLine("unchanged", "ignored", _line("ignored", 0.4, 0.4), 3),
        DiffLine("added", "too tall", _line("too tall", 0.4, 0.5, height=0.08), 1),
    )
    groups = build_crop_groups(changes, 1920, 1080)
    assert len(groups) == 2
    assert {frozenset(member.text for member in group.members) for group in groups} == {
        frozenset(("a", "b")),
        frozenset(("c",)),
    }
    assert groups[0].score >= groups[1].score
    assert all(3 <= item.scale <= 6 for item in groups)


def test_budget_has_normal_and_three_frame_ramp_limits() -> None:
    changes = tuple(
        DiffLine("added", str(index), _line(str(index), 0.05 + index * 0.15, 0.1), 1)
        for index in range(5)
    )
    groups = build_crop_groups(changes, 1920, 1080)
    assert len(groups) == 5
    normal = select_crop_budget(groups, ramp=False)
    assert normal.max_crops == 2
    assert normal.max_ms == 120.0
    assert len(normal.selected) == 2
    assert normal.skipped_count == 3
    ramp = select_crop_budget(groups, ramp=True)
    assert ramp.max_crops == 4
    assert ramp.max_ms == 250.0
    assert len(ramp.selected) == 4
    assert ramp.skipped_count == 1

    state = SceneResetRamp(3)
    assert [state.consume(value) for value in (True, False, False, False)] == [
        True,
        True,
        True,
        False,
    ]

    small = CropGroup(groups[0].members, 0, 0, 100, 30, 3, groups[0].score)
    assert crop_fits_remaining_budget(small, 120.0)
    huge = CropGroup(small.members, 0, 0, 1920, 1080, 6, small.score)
    assert not crop_fits_remaining_budget(huge, 250.0)


def test_dead_worker_restarts_and_skips_the_restart_frame(monkeypatch) -> None:
    class DeadProcess:
        def poll(self) -> int:
            return 1

        def wait(self, timeout: float) -> int:
            return 1

    worker = PersistentWinRtOcrWorker()
    worker.process = DeadProcess()  # type: ignore[assignment]
    started: list[bool] = []
    monkeypatch.setattr(worker, "start", lambda: started.append(True) or 1.0)
    reply = worker.recognize(__file__)  # type: ignore[arg-type]
    assert reply.result is None
    assert reply.skipped_reason == "worker_restarted"
    worker.wait_for_restart(timeout_seconds=1.0)
    assert started == [True]
    assert worker.restart_count == 1
    assert worker.crash_restart_count == 1


def test_cadence_math_marks_late_without_blocking_the_tick_thread() -> None:
    result = simulate_nonblocking_cadence((50.0, 170.0, 80.0), wait_ms=150.0)
    assert result.same_frame_hits == 2
    assert result.late_frames == 1
    assert result.maximum_queue_delay_ms == 0.0

    finished = threading.Event()

    def slow_ocr() -> None:
        time.sleep(0.2)
        finished.set()

    submission_ms, thread = prove_thread_submission_is_nonblocking(slow_ocr)
    assert submission_ms < 100.0
    assert not finished.is_set()
    thread.join(timeout=1.0)
    assert finished.is_set()
