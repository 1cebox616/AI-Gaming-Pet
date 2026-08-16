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
    assert configuration.idle.min_interval_seconds == 180
    assert configuration.idle.max_interval_seconds == 300
    assert configuration.gsi.record is False
    assert configuration.events.thrown_away_max_survival_seconds == 15.0
    assert configuration.events.thrown_away_min_equip_value == 3000
    assert configuration.events.death_after_kill_max_seconds == 8.0
    assert configuration.policy.cooldown_seconds == 6.0
    assert configuration.policy.max_lines_per_round == 4
    assert configuration.policy.alive_priority_threshold == 0
    assert configuration.policy.cooldown_override_priority == 70
    assert configuration.policy.minimum_gap_seconds == 2.0
    assert configuration.personality.style == "brother"
    assert configuration.llm.enabled is False
    assert configuration.llm.model == ""
    assert configuration.llm.timeout_seconds == 3.0
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
    assert configuration.idle.min_interval_seconds == 180
    assert "configuration section speech at speech.enabled" in caplog.text
    assert "configuration section idle at idle.min_interval_seconds" in caplog.text
    assert "'min_interval_seconds': 180" in caplog.text


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
    assert configuration.idle.min_interval_seconds == 180
    assert configuration.idle.max_interval_seconds == 300
    assert "configuration section idle at idle.min_interval_seconds" in caplog.text
    assert "'min_interval_seconds': 180" in caplog.text
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


def test_unknown_top_level_section_warns_without_discarding_known_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[speech]\nenabled = false\nvoice_name = \"\"\n\n"
        "[policy]\ncooldown_seconds = 11\nmax_lines_per_round = 7\n"
        "alive_priority_threshold = 25\ncooldown_override_priority = 80\n"
        "minimum_gap_seconds = 3\n\n"
        "[unknown_top]\nvalue = 42\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="pet.config")

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.speech.enabled is False
    assert configuration.policy.cooldown_seconds == 11
    assert configuration.policy.max_lines_per_round == 7
    assert "unknown backend configuration top-level section unknown_top" in caplog.text
    assert "configuration section speech" not in caplog.text
    assert "configuration section policy" not in caplog.text


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
        "thrown_away_min_equip_value = 4567\n"
        "death_after_kill_max_seconds = 6.5\n",
        encoding="utf-8",
    )

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.events.thrown_away_max_survival_seconds == 22.5
    assert configuration.events.thrown_away_min_equip_value == 4567
    assert configuration.events.death_after_kill_max_seconds == 6.5


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
        "thrown_away_max_survival_seconds = 15\n"
        "thrown_away_min_equip_value = 3000\n"
        "death_after_kill_max_seconds = 61\n",
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
    assert configuration.events.death_after_kill_max_seconds == 8.0
    assert configuration.speech.enabled is False
    assert configuration.idle.enabled is False
    assert configuration.idle.min_interval_seconds == 30
    assert configuration.idle.max_interval_seconds == 60
    assert configuration.gsi.record is True
    assert "configuration section events at events." in caplog.text
    assert "configuration section speech" not in caplog.text
    assert "configuration section idle" not in caplog.text
    assert "configuration section gsi" not in caplog.text


def test_policy_section_loads_custom_limits(tmp_path: Path) -> None:
    """Valid policy values are exposed exactly once at backend startup."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[policy]\ncooldown_seconds = 12.5\nmax_lines_per_round = 7\n"
        "alive_priority_threshold = 90\ncooldown_override_priority = 85\n"
        "minimum_gap_seconds = 3.5\n",
        encoding="utf-8",
    )

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.policy.cooldown_seconds == 12.5
    assert configuration.policy.max_lines_per_round == 7
    assert configuration.policy.alive_priority_threshold == 90
    assert configuration.policy.cooldown_override_priority == 85
    assert configuration.policy.minimum_gap_seconds == 3.5


@pytest.mark.parametrize(
    "policy_section",
    (
        'cooldown_seconds = "8"\nmax_lines_per_round = 3\n'
        "alive_priority_threshold = 75\n",
        "cooldown_seconds = 61\nmax_lines_per_round = 3\n"
        "alive_priority_threshold = 75\n",
        "cooldown_seconds = 8\nmax_lines_per_round = 0\n"
        "alive_priority_threshold = 75\n",
        "cooldown_seconds = 8\nmax_lines_per_round = 3\n"
        "alive_priority_threshold = 101\n",
        "cooldown_seconds = 8\nmax_lines_per_round = 3\n"
        "alive_priority_threshold = 75\ncooldown_override_priority = 102\n",
        "cooldown_seconds = 8\nmax_lines_per_round = 3\n"
        "alive_priority_threshold = 75\nminimum_gap_seconds = 11\n",
        "cooldown_seconds = 8\nmax_lines_per_round = 3\n"
        "alive_priority_threshold = 75\nminimum_gap_seconds = \"2\"\n",
        "cooldown_seconds = 8\nmax_lines_per_round = 3\n"
        "alive_priority_threshold = 75\nunknown_limit = 1\n",
    ),
)
def test_invalid_policy_section_falls_back_alone_and_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    policy_section: str,
) -> None:
    """Policy type, range, and unknown-field errors preserve every other section."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[speech]\nenabled = false\nvoice_name = \"\"\n\n"
        "[idle]\nenabled = false\nmin_interval_seconds = 30\n"
        "max_interval_seconds = 60\n\n"
        "[gsi]\nrecord = true\n\n"
        "[events]\nthrown_away_max_survival_seconds = 22\n"
        "thrown_away_min_equip_value = 4567\n\n"
        "[policy]\n"
        + policy_section,
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="pet.config")

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.policy.cooldown_seconds == 6.0
    assert configuration.policy.max_lines_per_round == 4
    assert configuration.policy.alive_priority_threshold == 0
    assert configuration.policy.cooldown_override_priority == 70
    assert configuration.policy.minimum_gap_seconds == 2.0
    assert configuration.speech.enabled is False
    assert configuration.idle.enabled is False
    assert configuration.gsi.record is True
    assert configuration.events.thrown_away_max_survival_seconds == 22.0
    assert configuration.events.thrown_away_min_equip_value == 4567
    assert "configuration section policy at policy." in caplog.text
    assert "configuration section speech" not in caplog.text
    assert "configuration section idle" not in caplog.text
    assert "configuration section gsi" not in caplog.text
    assert "configuration section events" not in caplog.text


def test_personality_section_switches_to_caster(tmp_path: Path) -> None:
    """A valid startup style is retained without runtime mutation machinery."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        '[personality]\nstyle = "caster"\n',
        encoding="utf-8",
    )

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.personality.style == "caster"


@pytest.mark.parametrize(
    "personality_section",
    (
        'style = "coach"\n',
        "style = 1\n",
        'style = "caster"\nextra = true\n',
    ),
)
def test_invalid_personality_falls_back_only_that_section_and_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    personality_section: str,
) -> None:
    """Invalid style data cannot discard valid policy and idle sections."""
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[idle]\nenabled = false\nmin_interval_seconds = 200\n"
        "max_interval_seconds = 400\n\n"
        "[policy]\ncooldown_seconds = 9\nmax_lines_per_round = 8\n"
        "alive_priority_threshold = 25\ncooldown_override_priority = 90\n"
        "minimum_gap_seconds = 4\n\n"
        "[personality]\n"
        + personality_section,
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="pet.config")

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.personality.style == "brother"
    assert configuration.idle.min_interval_seconds == 200
    assert configuration.policy.cooldown_seconds == 9
    assert configuration.policy.cooldown_override_priority == 90
    assert "configuration section personality at personality." in caplog.text
    assert "configuration section idle" not in caplog.text
    assert "configuration section policy" not in caplog.text


def test_llm_section_loads_runtime_tuning_without_accepting_credentials(tmp_path: Path) -> None:
    default_path = tmp_path / "config.toml"
    default_path.write_text(
        "[llm]\nenabled = true\nmodel = \"vendor/model\"\n"
        "provider = \"provider\"\ntemperature = 1.1\n"
        "timeout_seconds = 2.5\nmax_tokens = 128\n",
        encoding="utf-8",
    )

    configuration = load_config(default_path, tmp_path / "missing-local.toml")

    assert configuration.llm.enabled is True
    assert configuration.llm.model == "vendor/model"
    assert configuration.llm.provider == "provider"
    assert configuration.llm.temperature == 1.1
    assert configuration.llm.timeout_seconds == 2.5
    assert configuration.llm.max_tokens == 128
