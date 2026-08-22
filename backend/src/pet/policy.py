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
    "deferred",
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
        self._pending_multi_kill: GameEvent | None = None

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
        self._pending_multi_kill = None

    def decide(
        self,
        events: Sequence[GameEvent],
        game: GameState,
        *,
        now: float,
        muted: bool,
    ) -> PolicyBatchDecision:
        """Select no more than one event and explain every disposition."""
        batch_round = next(
            (event.round_number for event in events if event.round_number is not None),
            game.round,
        )
        if batch_round != self._counted_round_number:
            self._counted_round_number = batch_round
            self._lines_this_round = 0

        decisions_by_id: dict[str, PolicyDecision] = {}
        decision_events = list(events)
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
                if event.type == "multi_kill" and rejection.reason_code == "deferred":
                    self._defer_multi_kill(event)
                decisions_by_id[event.id] = rejection

        # Fold every new escalation into the pending streak before deciding
        # whether it has settled.  A higher count in this snapshot is direct
        # evidence that the streak is still active, even if the old pending
        # timestamp happened to cross the settle threshold just beforehand.
        pending = self._pending_candidate(now=now, round_number=batch_round)
        pending_is_candidate = False
        if pending is not None and self._pending_is_releasable(
            pending,
            events,
            now=now,
            muted=muted,
        ):
            # Give the pending follow-up the earlier order when priorities tie:
            # it describes the already-settled escalation that was held.
            candidates.append((event_priority(pending), -1, pending))
            pending_is_candidate = True
            if all(event.id != pending.id for event in decision_events):
                decision_events.append(pending)

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
            # A pending streak is consumed when it competes, whether it wins
            # or loses.  A newly deferred streak that has not settled remains
            # available across ordinary events in the same snapshot.
            if pending_is_candidate:
                self._pending_multi_kill = None

        return PolicyBatchDecision(
            selected_event=selected_event,
            decisions=tuple(decisions_by_id[event.id] for event in decision_events),
        )

    def _pending_candidate(
        self, *, now: float, round_number: int | None
    ) -> GameEvent | None:
        """Return a still-timely deferred multi-kill for normal filtering."""
        pending = self._pending_multi_kill
        if pending is None:
            return None
        if (
            pending.round_number is None
            or round_number is None
            or pending.round_number != round_number
            or now - pending.ts > self._config.follow_up_max_age_seconds
        ):
            self._pending_multi_kill = None
            return None
        return pending

    def _pending_is_releasable(
        self,
        pending: GameEvent,
        events: Sequence[GameEvent],
        *,
        now: float,
        muted: bool,
    ) -> bool:
        """Release one settled streak, or release it when the round ends."""
        if muted:
            return False
        if self._last_selected_at is not None and (
            now - self._last_selected_at < self._config.minimum_gap_seconds
        ):
            return False
        has_terminal_event = any(
            event.type
            in {"death", "death_after_kill", "death_thrown_away", "round_win", "round_loss"}
            for event in events
        )
        return (
            now - pending.ts >= self._config.streak_settle_seconds
            or has_terminal_event
        )

    def _defer_multi_kill(self, event: GameEvent) -> None:
        """Keep only the highest observed multi-kill escalation for this round."""
        pending = self._pending_multi_kill
        if pending is None or _multi_kill_count(event) > _multi_kill_count(pending):
            self._pending_multi_kill = event

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
        if (
            event.type != "multi_kill"
            and self._lines_this_round >= self._config.max_lines_per_round
        ):
            return _rejected(
                event,
                priority,
                "round_limit",
                limit=self._config.max_lines_per_round,
            )
        if event.type == "multi_kill":
            return _rejected(event, priority, "deferred")
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


def _multi_kill_count(event: GameEvent) -> int:
    """Read a valid multi-kill count without treating booleans as integers."""
    count = event.facts.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return 0


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
