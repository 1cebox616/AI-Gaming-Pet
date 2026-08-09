"""Startup configuration tests using real TOML files."""

import logging
from pathlib import Path

import pytest

from pet.config import load_config


def test_missing_configuration_file_uses_validated_defaults(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing default file leaves the backend usable with model defaults."""
    caplog.set_level(logging.WARNING, logger="pet.config")

    configuration = load_config(
        tmp_path / "missing-config.toml",
        tmp_path / "missing-local.toml",
    )

    assert configuration.speech.enabled is True
    assert configuration.idle.enabled is True
    assert configuration.idle.min_interval_seconds < configuration.idle.max_interval_seconds
    assert configuration.gsi.record is False
    assert configuration.events.thrown_away_max_survival_seconds == 15.0
    assert configuration.events.thrown_away_min_equip_value == 3000
    assert "configuration file is missing" in caplog.text


def test_local_configuration_overrides_only_its_explicit_values(tmp_path: Path) -> None:
    """An optional local file overlays matching settings without replacing others."""
    default_path = tmp_path / "config.toml"
    local_path = tmp_path / "config.local.toml"
    default_path.write_text(
        "[speech]\nenabled = true\nvoice_name = \"\"\n\n"
        "[idle]\nenabled = true\nmin_interval_seconds = 45\nmax_interval_seconds = 120\n\n"
        "[gsi]\nrecord = false\n",
        encoding="utf-8",
    )
    local_path.write_text("[idle]\nenabled = false\n", encoding="utf-8")

    configuration = load_config(default_path, local_path)

    assert configuration.speech.enabled is True
    assert configuration.idle.enabled is False
    assert configuration.idle.min_interval_seconds == 45
    assert configuration.idle.max_interval_seconds == 120
    assert configuration.gsi.record is False


def test_local_configuration_can_enable_gsi_recording(tmp_path: Path) -> None:
    """The ignored local override can enable raw GSI recording alone."""
    default_path = tmp_path / "config.toml"
    local_path = tmp_path / "config.local.toml"
    default_path.write_text(
        "[speech]\nenabled = true\nvoice_name = \"\"\n\n"
        "[idle]\nenabled = true\nmin_interval_seconds = 45\nmax_interval_seconds = 120\n\n"
        "[gsi]\nrecord = false\n",
        encoding="utf-8",
    )
    local_path.write_text("[gsi]\nrecord = true\n", encoding="utf-8")

    configuration = load_config(default_path, local_path)

    assert configuration.gsi.record is True


def test_missing_field_uses_its_default_and_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A partial default document retains a default for its omitted setting."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[speech]\nenabled = false\n\n"
        "[idle]\nenabled = false\nmin_interval_seconds = 45\nmax_interval_seconds = 120\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="pet.config")

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.speech.enabled is False
    assert configuration.speech.voice_name == ""
    assert "speech.voice_name is missing" in caplog.text


def test_invalid_sections_fall_back_independently_and_log_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A type or range failure cannot prevent backend startup."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[speech]\nenabled = \"not-a-boolean\"\nvoice_name = \"\"\n\n"
        "[idle]\nenabled = true\nmin_interval_seconds = 4\nmax_interval_seconds = 120\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="pet.config")

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.speech.enabled is True
    assert configuration.idle.min_interval_seconds == 45
    assert "configuration section speech at speech.enabled" in caplog.text
    assert "configuration section idle at idle.min_interval_seconds" in caplog.text
    assert "'min_interval_seconds': 45" in caplog.text


def test_invalid_idle_section_preserves_valid_speech_customization(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An idle validation failure cannot discard a valid speech override."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[speech]\nenabled = false\nvoice_name = \"\"\n\n"
        "[idle]\nenabled = true\nmin_interval_seconds = 5\nmax_interval_seconds = 120\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="pet.config")

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.speech.enabled is False
    assert configuration.idle.enabled is True
    assert configuration.idle.min_interval_seconds == 45
    assert configuration.idle.max_interval_seconds == 120
    assert "configuration section idle at idle.min_interval_seconds" in caplog.text
    assert "'min_interval_seconds': 45" in caplog.text
    assert "configuration section speech" not in caplog.text


def test_invalid_speech_section_preserves_valid_idle_customization(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A speech validation failure cannot discard valid idle overrides."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[speech]\nenabled = \"invalid\"\nvoice_name = \"\"\n\n"
        "[idle]\nenabled = false\nmin_interval_seconds = 30\nmax_interval_seconds = 60\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="pet.config")

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.speech.enabled is True
    assert configuration.speech.voice_name == ""
    assert configuration.idle.enabled is False
    assert configuration.idle.min_interval_seconds == 30
    assert configuration.idle.max_interval_seconds == 60
    assert "configuration section speech at speech.enabled" in caplog.text
    assert "'enabled': True" in caplog.text
    assert "configuration section idle" not in caplog.text


def test_unknown_field_falls_back_only_its_section_and_logs_field_name(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A misspelled setting is visible and cannot discard another valid section."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[speech]\nenabeld = false\nvoice_name = \"Microsoft Yaoyao\"\n\n"
        "[idle]\nenabled = false\nmin_interval_seconds = 30\nmax_interval_seconds = 60\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="pet.config")

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.speech.enabled is True
    assert configuration.speech.voice_name == ""
    assert configuration.idle.enabled is False
    assert configuration.idle.min_interval_seconds == 30
    assert configuration.idle.max_interval_seconds == 60
    assert "configuration section speech at speech.enabeld" in caplog.text
    assert "configuration section idle" not in caplog.text


def test_reversed_idle_interval_is_swapped(tmp_path: Path) -> None:
    """A valid but reversed interval remains usable in the intended range."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[speech]\nenabled = true\nvoice_name = \"\"\n\n"
        "[idle]\nenabled = true\nmin_interval_seconds = 120\nmax_interval_seconds = 45\n",
        encoding="utf-8",
    )

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.idle.min_interval_seconds == 45
    assert configuration.idle.max_interval_seconds == 120


def test_events_section_loads_custom_thresholds(tmp_path: Path) -> None:
    """Valid event thresholds are exposed to the detector without coercion surprises."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[events]\nthrown_away_max_survival_seconds = 22.5\n"
        "thrown_away_min_equip_value = 4567\n",
        encoding="utf-8",
    )

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.events.thrown_away_max_survival_seconds == 22.5
    assert configuration.events.thrown_away_min_equip_value == 4567


@pytest.mark.parametrize(
    "events_section",
    (
        'thrown_away_max_survival_seconds = "15"\n'
        "thrown_away_min_equip_value = 3000\n",
        "thrown_away_max_survival_seconds = 0\n"
        "thrown_away_min_equip_value = 3000\n",
        "thrown_away_max_survival_seconds = 15\n"
        "thrown_away_min_equip_value = 20001\n",
        "thrown_away_max_survival_seconds = 15\n"
        "thrown_away_min_equip_value = 3000\nunknown_threshold = 1\n",
    ),
)
def test_invalid_events_section_falls_back_alone_and_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    events_section: str,
) -> None:
    """Type, range, and unknown-field errors cannot discard other valid sections."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[speech]\nenabled = false\nvoice_name = \"\"\n\n"
        "[idle]\nenabled = false\nmin_interval_seconds = 30\n"
        "max_interval_seconds = 60\n\n"
        "[gsi]\nrecord = true\n\n"
        "[events]\n"
        + events_section,
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="pet.config")

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.events.thrown_away_max_survival_seconds == 15.0
    assert configuration.events.thrown_away_min_equip_value == 3000
    assert configuration.speech.enabled is False
    assert configuration.idle.enabled is False
    assert configuration.idle.min_interval_seconds == 30
    assert configuration.idle.max_interval_seconds == 60
    assert configuration.gsi.record is True
    assert "configuration section events at events." in caplog.text
    assert "configuration section speech" not in caplog.text
    assert "configuration section idle" not in caplog.text
    assert "configuration section gsi" not in caplog.text
