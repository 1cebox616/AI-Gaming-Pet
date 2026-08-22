"""Explicit registry of built-in game adapters."""

from collections.abc import Callable

from pet.core.adapter_api import GameAdapter
from pet.core.config import AdapterConfig
from pet.games.cs2.adapter import create_adapter as create_cs2_adapter

AdapterFactory = Callable[[AdapterConfig], GameAdapter]


def built_in_adapters() -> dict[str, AdapterFactory]:
    """Return the fixed built-in registry; no plugin discovery is performed."""
    return {"cs2": create_cs2_adapter}
