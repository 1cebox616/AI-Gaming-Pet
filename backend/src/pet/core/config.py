"""Read and validate the backend's one-time startup configuration."""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError


DEFAULT_LLM_API_KEY_ENV = "OPENROUTER_API_KEY"
ENVIRONMENT_VARIABLE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
DEFAULT_REGION_FOCUS_MAX = 0.50

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config.toml"
LOCAL_CONFIG_PATH = DEFAULT_CONFIG_PATH.with_name("config.local.toml")


class ConfigError(ValueError):
    """One or more configuration problems rejected by strict loading."""


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


class ActiveConfig(BaseModel):
    """Select the adapter loaded for this process."""

    model_config = ConfigDict(strict=True, extra="forbid")

    game: str = "default"


class GsiConfig(BaseModel):
    """CS2 Game State Integration recording settings."""

    model_config = ConfigDict(strict=True, extra="forbid")

    record: bool = False


class EventsConfig(BaseModel):
    """Thresholds used to classify facts inferred from CS2 snapshots."""

    model_config = ConfigDict(strict=True, extra="forbid")

    thrown_away_max_survival_seconds: float = Field(default=15.0, ge=1, le=120)
    thrown_away_min_equip_value: int = Field(default=3000, ge=0, le=20000)
    death_after_kill_max_seconds: float = Field(default=8.0, ge=1, le=60)


class PolicyConfig(BaseModel):
    """Limits that decide when a detected game event may interrupt the player."""

    model_config = ConfigDict(strict=True, extra="forbid")

    cooldown_seconds: float = Field(default=6.0, ge=0, le=60)
    max_lines_per_round: int = Field(default=4, ge=1, le=20)
    alive_priority_threshold: int = Field(default=0, ge=0, le=100)
    cooldown_override_priority: int = Field(default=70, ge=0, le=101)
    minimum_gap_seconds: float = Field(default=2.0, ge=0, le=10)
    # Five seconds matches the player-approved multi-kill streak window: a
    # delayed follow-up beyond that point is no longer timely commentary.
    follow_up_max_age_seconds: float = Field(default=5.0, ge=0, le=30)
    # Synthetic streaks arrive about 1.8 seconds apart; wait 2.5 seconds to
    # make one final multi-kill callout instead of interrupting each upgrade.
    streak_settle_seconds: float = Field(default=2.5, ge=0, le=10)


PersonalityStyle = Literal["brother", "caster"]


class PersonalityConfig(BaseModel):
    """Dialogue style selected once when the backend starts."""

    model_config = ConfigDict(strict=True, extra="forbid")

    style: PersonalityStyle = "brother"


class LlmProfileConfig(BaseModel):
    """Optional overrides applied to one speech request's model profile."""

    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key_env: str = Field(
        default=DEFAULT_LLM_API_KEY_ENV,
        pattern=ENVIRONMENT_VARIABLE_PATTERN,
    )
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout_seconds: float | None = Field(default=None, gt=0, le=30)
    max_tokens: int | None = Field(default=None, ge=1, le=2048)
    input_price_per_million_usd: float | None = Field(default=None, ge=0)
    output_price_per_million_usd: float | None = Field(default=None, ge=0)


class LlmConfig(BaseModel):
    """Optional live-model settings; credentials never belong in this file."""

    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool = False
    model: str = ""
    provider: str = ""
    base_url: str | None = None
    api_key_env: str = Field(
        default=DEFAULT_LLM_API_KEY_ENV,
        pattern=ENVIRONMENT_VARIABLE_PATTERN,
    )
    temperature: float = Field(default=0.9, ge=0, le=2)
    # M3-T10: 3 seconds is over three times the offline 0.8-second event P95.
    timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    max_tokens: int = Field(default=256, ge=1, le=2048)
    profiles: dict[str, LlmProfileConfig] = Field(default_factory=dict)


def resolve_llm_profile(
    configuration: LlmConfig,
    profile_id: str | None,
) -> LlmConfig:
    """Return one effective model profile without mutating shared configuration."""
    if profile_id is None:
        return configuration
    profile = configuration.profiles.get(profile_id)
    if profile is None:
        raise ValueError(f"未知模型档位：{profile_id}")
    return configuration.model_copy(update=profile.model_dump(exclude_none=True))


class OcrConfig(BaseModel):
    """One fixed production OCR implementation with bounded CPU threads."""

    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool = True
    engine: Literal["rapidocr-ppocrv6-tiny-openvino"] = (
        "rapidocr-ppocrv6-tiny-openvino"
    )
    num_threads: int = Field(default=2, ge=1)
    det_limit_side_len: int = Field(default=1280, ge=64, le=8192)
    language: Literal["zh-Hans-CN"] = "zh-Hans-CN"
    model_dir: str = "models/ocr"


class GenericVisionConfig(BaseModel):
    """Disabled-by-default settings for the generic visual adapter."""

    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool = False
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    send_width: int = Field(default=896, ge=64, le=8192)
    fast_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    max_inflight: int = Field(default=4, ge=1, le=32)
    input_context: bool = True
    observation_log_dir: str = "recordings/observation"
    region_focus_max: float = Field(
        default=DEFAULT_REGION_FOCUS_MAX,
        ge=0,
        le=1,
    )
    llm_profile: str = "vision_fast"
    cost_warn_per_hour: float = Field(default=1.0, gt=0)
    ocr: OcrConfig = Field(default_factory=OcrConfig)


GENERIC_VISION_FIELDS = frozenset(GenericVisionConfig.model_fields)


class AdapterConfig(BaseModel):
    """Configuration shape consumed by the built-in adapters."""

    model_config = ConfigDict(strict=True, extra="forbid")

    gsi: GsiConfig = Field(default_factory=GsiConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    generic: GenericVisionConfig = Field(default_factory=GenericVisionConfig)


class PetConfig(BaseModel):
    """The complete runtime configuration for the local pet backend."""

    model_config = ConfigDict(strict=True, extra="forbid")

    active: ActiveConfig = Field(default_factory=ActiveConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    idle: IdleConfig = Field(default_factory=IdleConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    games: dict[str, AdapterConfig] = Field(default_factory=dict)

    @property
    def active_game(self) -> AdapterConfig:
        return self.games.get(self.active.game, AdapterConfig())

    @property
    def gsi(self) -> GsiConfig:
        return self.active_game.gsi

    @property
    def events(self) -> EventsConfig:
        return self.active_game.events

    @property
    def policy(self) -> PolicyConfig:
        return self.active_game.policy

    @property
    def personality(self) -> PersonalityConfig:
        return self.active_game.personality


ConfigSection = TypeVar(
    "ConfigSection",
    ActiveConfig,
    SpeechConfig,
    IdleConfig,
    GsiConfig,
    EventsConfig,
    PolicyConfig,
    PersonalityConfig,
    GenericVisionConfig,
    LlmProfileConfig,
    LlmConfig,
)


def load_config(
    default_path: Path = DEFAULT_CONFIG_PATH,
    local_path: Path = LOCAL_CONFIG_PATH,
    *,
    strict: bool = False,
) -> PetConfig:
    """Load defaults, optionally overlay local settings, and recover safely on errors."""
    default_data = _read_toml(default_path, required=True)
    local_data = _read_toml(local_path, required=False)
    merged_data = _merge_sections(default_data, local_data)
    problems: list[str] = []
    _collect_unknown_sections(merged_data, problems)
    _warn_for_missing_fields(merged_data)

    active = _validate_section(
        "active", ActiveConfig, merged_data.get("active", {}), problems
    )
    speech = _validate_section(
        "speech", SpeechConfig, merged_data.get("speech", {}), problems
    )
    idle = _validate_section(
        "idle", IdleConfig, merged_data.get("idle", {}), problems
    )
    llm = _validate_section("llm", LlmConfig, merged_data.get("llm", {}), problems)
    games = _load_games(merged_data, active, problems)
    configuration = PetConfig(
        active=active,
        speech=speech,
        idle=idle,
        llm=llm,
        games=games,
    )

    if problems:
        if strict:
            detail = "\n".join(problems)
            raise ConfigError(
                f"配置文件存在 {len(problems)} 处问题，严格模式下拒绝加载\n{detail}"
            )
        for problem in problems:
            logger.warning("%s", problem)

    if configuration.idle.max_interval_seconds < configuration.idle.min_interval_seconds:
        logger.warning(
            "idle max_interval_seconds is below min_interval_seconds; swapping the values"
        )
        configuration.idle.min_interval_seconds, configuration.idle.max_interval_seconds = (
            configuration.idle.max_interval_seconds,
            configuration.idle.min_interval_seconds,
        )

    return configuration


def _collect_unknown_sections(
    configuration_data: Mapping[str, Any], problems: list[str]
) -> None:
    """Collect misspelled root tables without discarding valid known tables."""
    known_sections = set(PetConfig.model_fields) | {
        "gsi",
        "events",
        "policy",
        "personality",
    }
    for section_name in sorted(set(configuration_data) - known_sections):
        problems.append(
            f"{section_name}: 未知顶层配置小节；"
            f"unknown backend configuration top-level section {section_name}; ignoring it"
        )


def _validate_section(
    section_name: str,
    model_type: type[ConfigSection],
    section_data: Any,
    problems: list[str],
) -> ConfigSection:
    """Validate one section without discarding valid settings in the other section."""
    try:
        return model_type.model_validate(section_data)
    except ValidationError as error:
        defaults = model_type()
        for item in error.errors():
            path = (
                f"{section_name}.{'.'.join(str(part) for part in item['loc'])}"
                if item["loc"]
                else section_name
            )
            reason = (
                "未知配置键"
                if item["type"] == "extra_forbidden"
                else f"配置值无效：{item['msg']}"
            )
            problems.append(
                f"{path}: {reason}; invalid backend configuration section "
                f"{section_name} at {path}; using section defaults: "
                f"{defaults.model_dump()}"
            )
    return defaults


def _load_games(
    configuration_data: Mapping[str, Any],
    active: ActiveConfig,
    problems: list[str],
) -> dict[str, AdapterConfig]:
    games_data = configuration_data.get("games")
    if isinstance(games_data, Mapping) and games_data:
        games: dict[str, AdapterConfig] = {}
        for game_id, game_data in games_data.items():
            if not isinstance(game_id, str) or not isinstance(game_data, Mapping):
                if not isinstance(game_id, str):
                    path = f"games.{game_id!r}"
                    reason = "游戏 ID 必须是字符串"
                else:
                    path = f"games.{game_id}"
                    reason = "游戏配置必须是表"
                problems.append(
                    f"{path}: {reason}; invalid backend game configuration "
                    f"{game_id!r}; ignoring it"
                )
                continue
            allowed_fields = {
                "gsi",
                "events",
                "policy",
                "personality",
            } | GENERIC_VISION_FIELDS
            for field_name in sorted(set(game_data) - allowed_fields):
                problems.append(f"games.{game_id}.{field_name}: 未知配置键")
            games[game_id] = AdapterConfig(
                gsi=_validate_section(
                    f"games.{game_id}.gsi",
                    GsiConfig,
                    game_data.get("gsi", {}),
                    problems,
                ),
                events=_validate_section(
                    f"games.{game_id}.events",
                    EventsConfig,
                    game_data.get("events", {}),
                    problems,
                ),
                policy=_validate_section(
                    f"games.{game_id}.policy",
                    PolicyConfig,
                    game_data.get("policy", {}),
                    problems,
                ),
                personality=_validate_section(
                    f"games.{game_id}.personality",
                    PersonalityConfig,
                    game_data.get("personality", {}),
                    problems,
                ),
                generic=_validate_section(
                    f"games.{game_id}",
                    GenericVisionConfig,
                    {
                        field_name: game_data[field_name]
                        for field_name in GENERIC_VISION_FIELDS
                        if field_name in game_data
                    },
                    problems,
                ),
            )
        return games

    return {
        active.game: AdapterConfig(
            gsi=_validate_section(
                "gsi", GsiConfig, configuration_data.get("gsi", {}), problems
            ),
            events=_validate_section(
                "events", EventsConfig, configuration_data.get("events", {}), problems
            ),
            policy=_validate_section(
                "policy", PolicyConfig, configuration_data.get("policy", {}), problems
            ),
            personality=_validate_section(
                "personality",
                PersonalityConfig,
                configuration_data.get("personality", {}),
                problems,
            ),
            generic=GenericVisionConfig(),
        )
    }


def _warn_for_missing_fields(configuration_data: Mapping[str, Any]) -> None:
    """Record each missing known setting before Pydantic fills in its default."""
    default_values = {
        "speech": SpeechConfig().model_dump(),
        "idle": IdleConfig().model_dump(),
        "gsi": GsiConfig().model_dump(),
        "events": EventsConfig().model_dump(),
        "policy": PolicyConfig().model_dump(),
        "personality": PersonalityConfig().model_dump(),
        "llm": LlmConfig().model_dump(),
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
            "death_after_kill_max_seconds",
        ),
        "policy": (
            "cooldown_seconds",
            "max_lines_per_round",
            "alive_priority_threshold",
            "cooldown_override_priority",
            "minimum_gap_seconds",
            "follow_up_max_age_seconds",
            "streak_settle_seconds",
        ),
        "personality": ("style",),
        "llm": (
            "enabled",
            "model",
            "provider",
            "temperature",
            "timeout_seconds",
            "max_tokens",
        ),
    }
    if isinstance(configuration_data.get("games"), Mapping):
        for game_section in ("gsi", "events", "policy", "personality"):
            expected_fields.pop(game_section)
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
    """Recursively overlay local values while retaining unspecified defaults."""
    merged: dict[str, Any] = dict(default_data)
    for section_name, local_value in local_data.items():
        default_value = merged.get(section_name)
        if isinstance(default_value, Mapping) and isinstance(local_value, Mapping):
            merged[section_name] = _merge_sections(default_value, local_value)
        else:
            merged[section_name] = local_value
    return merged
