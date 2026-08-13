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

# A single fixed window absorbs the short burst of a firefight without making
# the pet wait indefinitely for a later event.  It is intentionally not a
# configuration value: this is the product-wide speech-timing contract.
EVENT_BUFFER_SECONDS = 1.5

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


@dataclass(frozen=True, slots=True)
class BufferedEventGroup:
    """One non-nested speech window and its policy-selected focus."""

    events: tuple[GameEvent, ...]
    focus_event: GameEvent
    opened_at: float
    flushed_at: float


class SpeechBuffer:
    """Collect policy-selected events into fixed, non-extending windows."""

    def __init__(self) -> None:
        self._events: list[GameEvent] = []
        self._opened_at: float | None = None

    @property
    def has_pending(self) -> bool:
        """Return whether a speech window is waiting to be flushed."""
        return bool(self._events)

    def add(self, event: GameEvent) -> tuple[BufferedEventGroup, ...]:
        """Add one approved event, closing an already elapsed window first."""
        flushed: tuple[BufferedEventGroup, ...] = ()
        if (
            self._opened_at is not None
            and event.ts - self._opened_at >= EVENT_BUFFER_SECONDS
        ):
            flushed = (
                self._flush(flushed_at=self._opened_at + EVENT_BUFFER_SECONDS),
            )
        if not self._events:
            self._opened_at = event.ts
        self._events.append(event)
        return flushed

    def flush_due(self, *, now: float) -> tuple[BufferedEventGroup, ...]:
        """Flush once the fixed window has elapsed, without extending it."""
        if self._opened_at is None or now - self._opened_at < EVENT_BUFFER_SECONDS:
            return ()
        return (
            self._flush(flushed_at=self._opened_at + EVENT_BUFFER_SECONDS),
        )

    def flush_all(self, *, now: float) -> tuple[BufferedEventGroup, ...]:
        """Flush immediately at a round/match/program boundary."""
        if not self._events:
            return ()
        return (self._flush(flushed_at=now),)

    def reset(self) -> None:
        """Discard a pending window after its boundary has been delivered."""
        self._events.clear()
        self._opened_at = None

    def _flush(self, *, flushed_at: float) -> BufferedEventGroup:
        if not self._events or self._opened_at is None:
            raise RuntimeError("cannot flush an empty speech buffer")
        events = tuple(self._events)
        focus_event = max(
            enumerate(events),
            key=lambda item: (event_priority(item[1]), -item[0]),
        )[1]
        group = BufferedEventGroup(
            events=events,
            focus_event=focus_event,
            opened_at=self._opened_at,
            flushed_at=flushed_at,
        )
        self.reset()
        return group


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
        if event.type in {"round_win", "round_loss"}:
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
