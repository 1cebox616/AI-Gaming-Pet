"""Explicit registry of built-in game adapters."""

from collections.abc import Callable

from pet.core.adapter_api import GameAdapter
from pet.core.capture import AdaptiveFrameSelector, WindowsGraphicsCaptureBackend
from pet.core.config import AdapterConfig, LlmConfig
from pet.core.input_telemetry import ActionInputListener
from pet.games.cs2.adapter import create_adapter as create_cs2_adapter
from pet.games.generic.adapter import create_adapter as create_generic_adapter

AdapterFactory = Callable[[AdapterConfig], GameAdapter]


def built_in_adapters(llm_configuration: LlmConfig) -> dict[str, AdapterFactory]:
    """Return the fixed built-in registry; no plugin discovery is performed."""
    return {
        "cs2": create_cs2_adapter,
        "generic": lambda configuration: create_generic_adapter(
            configuration,
            llm_configuration,
            capture_backend_factory=WindowsGraphicsCaptureBackend,
            selector_factory=lambda sparsity: AdaptiveFrameSelector(
                region_sparsity_max=sparsity
            ),
            input_listener_factory=lambda backend: ActionInputListener(
                backend.target.hwnd  # type: ignore[attr-defined]
            ),
        ),
    }
