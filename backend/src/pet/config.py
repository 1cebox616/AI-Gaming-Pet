"""Read and validate the backend's one-time startup configuration."""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"
LOCAL_CONFIG_PATH = DEFAULT_CONFIG_PATH.with_name("config.local.toml")


class SpeechConfig(BaseModel):
    """Speech behavior selected once when the backend starts."""

    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool = True
    voice_name: str = ""


class IdleConfig(BaseModel):
    """Randomized idle-dialogue interval settings."""

    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool = True
    min_interval_seconds: int = Field(default=180, ge=10, le=3600)
    max_interval_seconds: int = Field(default=300, ge=10, le=3600)


class GsiConfig(BaseModel):
    """CS2 Game State Integration recording settings."""

    model_config = ConfigDict(strict=True, extra="forbid")

    record: bool = False


class EventsConfig(BaseModel):
    """Thresholds used to classify facts inferred from CS2 snapshots."""

    model_config = ConfigDict(strict=True, extra="forbid")

    thrown_away_max_survival_seconds: float = Field(default=15.0, ge=1, le=120)
    thrown_away_min_equip_value: int = Field(default=3000, ge=0, le=20000)


class PolicyConfig(BaseModel):
    """Limits that decide when a detected game event may interrupt the player."""

    model_config = ConfigDict(strict=True, extra="forbid")

    cooldown_seconds: float = Field(default=6.0, ge=0, le=60)
    max_lines_per_round: int = Field(default=4, ge=1, le=20)
    alive_priority_threshold: int = Field(default=0, ge=0, le=100)
    cooldown_override_priority: int = Field(default=70, ge=0, le=101)
    minimum_gap_seconds: float = Field(default=2.0, ge=0, le=10)


class PetConfig(BaseModel):
    """The complete runtime configuration for the local pet backend."""

    model_config = ConfigDict(strict=True, extra="forbid")

    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    idle: IdleConfig = Field(default_factory=IdleConfig)
    gsi: GsiConfig = Field(default_factory=GsiConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)


ConfigSection = TypeVar(
    "ConfigSection",
    SpeechConfig,
    IdleConfig,
    GsiConfig,
    EventsConfig,
    PolicyConfig,
)


def load_config(
    default_path: Path = DEFAULT_CONFIG_PATH,
    local_path: Path = LOCAL_CONFIG_PATH,
) -> PetConfig:
    """Load defaults, optionally overlay local settings, and recover safely on errors."""
    default_data = _read_toml(default_path, required=True)
    local_data = _read_toml(local_path, required=False)
    merged_data = _merge_sections(default_data, local_data)
    _warn_for_missing_fields(merged_data)

    speech = _validate_section("speech", SpeechConfig, merged_data.get("speech", {}))
    idle = _validate_section("idle", IdleConfig, merged_data.get("idle", {}))
    gsi = _validate_section("gsi", GsiConfig, merged_data.get("gsi", {}))
    events = _validate_section("events", EventsConfig, merged_data.get("events", {}))
    policy = _validate_section("policy", PolicyConfig, merged_data.get("policy", {}))
    configuration = PetConfig(
        speech=speech,
        idle=idle,
        gsi=gsi,
        events=events,
        policy=policy,
    )

    if configuration.idle.max_interval_seconds < configuration.idle.min_interval_seconds:
        logger.warning(
            "idle max_interval_seconds is below min_interval_seconds; swapping the values"
        )
        configuration.idle.min_interval_seconds, configuration.idle.max_interval_seconds = (
            configuration.idle.max_interval_seconds,
            configuration.idle.min_interval_seconds,
        )

    return configuration


def _validate_section(
    section_name: str,
    model_type: type[ConfigSection],
    section_data: Any,
) -> ConfigSection:
    """Validate one section without discarding valid settings in the other section."""
    try:
        return model_type.model_validate(section_data)
    except ValidationError as error:
        defaults = model_type()
        invalid_fields = ", ".join(
            f"{section_name}.{'.'.join(str(part) for part in item['loc'])}"
            if item["loc"]
            else section_name
            for item in error.errors()
        )
        logger.warning(
            "invalid backend configuration section %s at %s; "
            "using section defaults: %s",
            section_name,
            invalid_fields,
            defaults.model_dump(),
        )
        return defaults


def _warn_for_missing_fields(configuration_data: Mapping[str, Any]) -> None:
    """Record each missing known setting before Pydantic fills in its default."""
    default_values = {
        "speech": SpeechConfig().model_dump(),
        "idle": IdleConfig().model_dump(),
        "gsi": GsiConfig().model_dump(),
        "events": EventsConfig().model_dump(),
        "policy": PolicyConfig().model_dump(),
    }
    expected_fields = {
        "speech": ("enabled", "voice_name"),
        "idle": (
            "enabled",
            "min_interval_seconds",
            "max_interval_seconds",
        ),
        "gsi": ("record",),
        "events": (
            "thrown_away_max_survival_seconds",
            "thrown_away_min_equip_value",
        ),
        "policy": (
            "cooldown_seconds",
            "max_lines_per_round",
            "alive_priority_threshold",
            "cooldown_override_priority",
            "minimum_gap_seconds",
        ),
    }
    for section_name, field_names in expected_fields.items():
        section = configuration_data.get(section_name)
        if not isinstance(section, Mapping):
            logger.warning(
                "backend configuration section %s is missing; using default values: %s",
                section_name,
                default_values[section_name],
            )
            continue
        for field_name in field_names:
            if field_name not in section:
                logger.warning(
                    "backend configuration field %s.%s is missing; using default value %r",
                    section_name,
                    field_name,
                    default_values[section_name][field_name],
                )


def _read_toml(path: Path, *, required: bool) -> dict[str, Any]:
    """Read one TOML document, returning no values when it is unavailable or invalid."""
    if not path.is_file():
        if required:
            logger.warning("backend configuration file is missing; using built-in defaults: %s", path)
        return {}

    try:
        with path.open("rb") as config_file:
            content = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        logger.warning("could not read backend configuration %s; using defaults: %s", path, error)
        return {}

    if not isinstance(content, Mapping):
        logger.warning("backend configuration %s is not a TOML table; using defaults", path)
        return {}
    return dict(content)


def _merge_sections(
    default_data: Mapping[str, Any],
    local_data: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay top-level configuration sections without introducing a config framework."""
    merged: dict[str, Any] = dict(default_data)
    for section_name, local_value in local_data.items():
        default_value = merged.get(section_name)
        if isinstance(default_value, Mapping) and isinstance(local_value, Mapping):
            merged[section_name] = {**default_value, **local_value}
        else:
            merged[section_name] = local_value
    return merged
