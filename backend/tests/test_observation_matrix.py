"""Standing visual matrix enforces locked profiles and manifest boundaries."""

from pathlib import Path

import pytest

from pet.core.config import LlmConfig, LlmProfileConfig
from pet.games.generic.eval.observation_matrix import (
    _live_profile,
    _load_roles,
    build_parser,
)
from pet.games.generic.eval.observation_replay import ObservationReplayError


def test_matrix_parser_defaults_to_production_width_pacing_and_three_dollar_cap() -> None:
    arguments = build_parser().parse_args(["--profile", "fixture"])
    assert arguments.send_width is None
    assert arguments.dispatch_interval == 1.0
    assert arguments.cost_cap == 3.0


def test_manifest_loads_independent_role_segments(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    manifest = tmp_path / "segments.toml"
    manifest.write_text(
        "[[segments]]\n"
        'role = "one"\n'
        'category = "fast"\n'
        'session = "session"\n'
        "start = 30.0\n"
        "end = 180.0\n",
        encoding="utf-8",
    )
    roles = _load_roles(manifest, None)
    assert len(roles) == 1
    assert roles[0].session == session.resolve()
    assert roles[0].segment is not None
    assert roles[0].segment.start == 30.0 and roles[0].segment.end == 180.0


def test_default_endpoint_profile_without_locked_provider_is_rejected() -> None:
    configuration = LlmConfig(
        enabled=True,
        model="fixture-default",
        profiles={
            "fixture": LlmProfileConfig(
                enabled=True,
                model="fixture-model",
                provider="",
            )
        },
    )
    with pytest.raises(ObservationReplayError, match="未锁定单一上游"):
        _live_profile(configuration, "fixture")
