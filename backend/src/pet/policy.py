"""Choose at most one detected CS2 event that is worth speaking about."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pet.config import PolicyConfig
from pet.events import EventType, GameEvent
from pet.gsi import GameSnapshot
from pet.session import GameState

DecisionReason = Literal[
    "selected",
    "teammate_event",
    "muted",
    "alive_threshold",
    "round_limit",
    "cooldown",
    "minimum_gap",
    "higher_priority",
    "round_event",
]

_STATIC_PRIORITIES: dict[EventType, int] = {
    "kill": 20,
    "kill_headshot": 30,
    "death": 45,
    "death_after_kill": 50,
    "death_thrown_away": 60,
    "round_win": 70,
    "round_loss": 70,
    "multi_kill": 0,
}
_MULTI_KILL_PRIORITIES = {2: 50, 3: 80, 4: 90, 5: 100}
# Round results remain detectable facts, but their speech slot is reserved for
# the later OCR-backed long-memory layer.
_NON_SPEECH_EVENT_TYPES = frozenset({"round_win", "round_loss"})


class PolicyDecision(BaseModel):
    """The disposition and concrete reason for one detected event."""

    model_config = ConfigDict(frozen=True)

    event: GameEvent
    selected: bool
    priority: int
    reason_code: DecisionReason
    elapsed_seconds: float | None = None
    limit: float | int | None = None


class PolicyBatchDecision(BaseModel):
    """All dispositions for one snapshot's event batch and its single winner."""

    model_config = ConfigDict(frozen=True)

    selected_event: GameEvent | None
    decisions: tuple[PolicyDecision, ...]


class SpeechPolicy:
    """Apply ordered filters, cooldown, and per-round limits to event batches."""

    def __init__(self, config: PolicyConfig) -> None:
        self._config = config
        self._self_steamid: str | None = None
        self._self_health: int | None = None
        self._last_selected_at: float | None = None
        self._counted_round_number: int | None = None
        self._lines_this_round = 0

    def observe_snapshot(self, snapshot: GameSnapshot) -> None:
        """Remember health only when the snapshot describes the local player."""
        if snapshot.provider_steamid is not None:
            self._self_steamid = snapshot.provider_steamid
        if (
            snapshot.player_steamid is not None
            and snapshot.player_steamid == self._self_steamid
            and snapshot.health is not None
        ):
            self._self_health = snapshot.health

    def reset(self) -> None:
        """Discard every per-match health, cooldown, and quota value."""
        self._self_steamid = None
        self._self_health = None
        self._last_selected_at = None
        self._counted_round_number = None
        self._lines_this_round = 0

    def decide(
        self,
        events: Sequence[GameEvent],
        game: GameState,
        *,
        now: float,
        muted: bool,
    ) -> PolicyBatchDecision:
        """Select no more than one event and explain every disposition."""
        if not events:
            return PolicyBatchDecision(selected_event=None, decisions=())

        batch_round = next(
            (event.round_number for event in events if event.round_number is not None),
            game.round,
        )
        if batch_round != self._counted_round_number:
            self._counted_round_number = batch_round
            self._lines_this_round = 0

        decisions_by_id: dict[str, PolicyDecision] = {}
        candidates: list[tuple[int, int, GameEvent]] = []
        for order, event in enumerate(events):
            priority = event_priority(event)
            rejection = self._filter_event(
                event,
                game,
                priority=priority,
                now=now,
                muted=muted,
            )
            if rejection is None:
                candidates.append((priority, order, event))
            else:
                decisions_by_id[event.id] = rejection

        selected_event: GameEvent | None = None
        if candidates:
            priority, _, selected_event = max(
                candidates,
                key=lambda candidate: (candidate[0], -candidate[1]),
            )
            for candidate_priority, _, event in candidates:
                if event.id == selected_event.id:
                    decisions_by_id[event.id] = PolicyDecision(
                        event=event,
                        selected=True,
                        priority=candidate_priority,
                        reason_code="selected",
                    )
                else:
                    decisions_by_id[event.id] = PolicyDecision(
                        event=event,
                        selected=False,
                        priority=candidate_priority,
                        reason_code="higher_priority",
                    )
            self._last_selected_at = now
            self._lines_this_round += 1

        return PolicyBatchDecision(
            selected_event=selected_event,
            decisions=tuple(decisions_by_id[event.id] for event in events),
        )

    def _filter_event(
        self,
        event: GameEvent,
        game: GameState,
        *,
        priority: int,
        now: float,
        muted: bool,
    ) -> PolicyDecision | None:
        if not event.subject_is_self:
            return _rejected(event, priority, "teammate_event")
        if event.type in _NON_SPEECH_EVENT_TYPES:
            return _rejected(event, priority, "round_event")
        if muted:
            return _rejected(event, priority, "muted")
        if (
            game.state == "playing"
            and self._self_health is not None
            and self._self_health > 0
            and priority < self._config.alive_priority_threshold
        ):
            return _rejected(
                event,
                priority,
                "alive_threshold",
                limit=self._config.alive_priority_threshold,
            )
        if self._lines_this_round >= self._config.max_lines_per_round:
            return _rejected(
                event,
                priority,
                "round_limit",
                limit=self._config.max_lines_per_round,
            )
        if self._last_selected_at is not None:
            elapsed = max(0.0, now - self._last_selected_at)
            if elapsed < self._config.cooldown_seconds:
                if priority >= self._config.cooldown_override_priority:
                    if elapsed < self._config.minimum_gap_seconds:
                        return _rejected(
                            event,
                            priority,
                            "minimum_gap",
                            elapsed_seconds=elapsed,
                            limit=self._config.minimum_gap_seconds,
                        )
                    return None
                return _rejected(
                    event,
                    priority,
                    "cooldown",
                    elapsed_seconds=elapsed,
                    limit=self._config.cooldown_seconds,
                )
        return None


def event_priority(event: GameEvent) -> int:
    """Return the policy-owned priority for one detected fact."""
    if event.type == "multi_kill":
        count = event.facts.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            return _MULTI_KILL_PRIORITIES.get(count, 0)
        return 0
    return _STATIC_PRIORITIES[event.type]


def _rejected(
    event: GameEvent,
    priority: int,
    reason_code: DecisionReason,
    *,
    elapsed_seconds: float | None = None,
    limit: float | int | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        event=event,
        selected=False,
        priority=priority,
        reason_code=reason_code,
        elapsed_seconds=elapsed_seconds,
        limit=limit,
    )
