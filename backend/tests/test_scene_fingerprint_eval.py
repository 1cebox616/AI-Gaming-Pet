"""M5-B-T2-3 all-frame duration-calibration regression tests."""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from pet.core.config import SceneConfig
from pet.games.generic.eval.scene_fingerprint_eval import (
    CARD_MIN_DWELL_SECONDS,
    STABLE_MIN_SECONDS,
    _derive_stable_min_seconds,
    _noise_floor_distances,
    _threshold_interval,
)


def test_scene_defaults_match_manual_full_frame_review() -> None:
    settings = SceneConfig()

    assert (
        settings.hash_kind,
        settings.hash_bits,
        settings.hamming_threshold,
        settings.stable_min_seconds,
        settings.card_min_dwell_seconds,
    ) == ("phash", 64, 8, 4.0, 8.0)
    assert (STABLE_MIN_SECONDS, CARD_MIN_DWELL_SECONDS) == (4.0, 8.0)


def test_noise_floor_uses_only_no_change_frame_and_its_predecessor() -> None:
    hashes = (
        "0000000000000000",
        "0000000000000001",
        "ffffffffffffffff",
        "fffffffffffffffe",
    )
    reasons = ("initial", "no_change", "persistent_change", "no_change")

    assert _noise_floor_distances(hashes, reasons) == (1, 1)


def test_threshold_interval_eliminates_variant_when_noise_floor_exceeds_cap() -> None:
    lower, upper, reason = _threshold_interval((17, 17, 17), 64)

    assert (lower, upper) == (17, 16)
    assert reason == "噪声底 P95 下限 17 > 25% 上限 16"


def test_single_peak_duration_histogram_keeps_zero_duration_p75_without_clamp() -> None:
    result = _derive_stable_min_seconds(
        (0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        1.0,
    )

    assert result.temporary is True
    assert result.stable_min_seconds == 0.0
    assert result.zero_duration_count == 5
    assert "P75" in result.method


@pytest.mark.parametrize("legacy_key", ("stable_min_frames", "card_min_visits"))
def test_removed_scene_configuration_keys_are_rejected(legacy_key: str) -> None:
    with pytest.raises(ValidationError, match=legacy_key):
        SceneConfig.model_validate({legacy_key: 3})
