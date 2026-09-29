"""Typed domain events and a lightweight event bus (spec section 18).

Events are typed Pydantic models. Handlers subscribe to concrete event classes (or a
base class to receive a whole hierarchy). The bus is owned by the world actor and
dispatched synchronously within a tick; async handlers are awaited.

These are bunnyland's own domain events, distinct from Relics' internal ``CustomEvent``
observer system. The world actor is the single emitter, so ordering is deterministic.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .components import GenerationIntentComponent

type EventValue = (
    None
    | bool
    | int
    | float
    | str
    | tuple["EventValue", ...]
    | list["EventValue"]
    | dict[str, "EventValue"]
)


class EventVisibility(StrEnum):
    PUBLIC = "public"
    ROOM = "room"
    DIRECTED = "directed"
    PRIVATE = "private"
    SYSTEM = "system"


class DomainEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    world_epoch: int
    created_at: datetime

    visibility: EventVisibility = EventVisibility.SYSTEM
    actor_id: str | None = None
    room_id: str | None = None
    target_ids: tuple[str, ...] = ()

    causation_id: str | None = None
    correlation_id: str | None = None


def serialized_event_visible_to(
    event: Mapping[str, object],
    *,
    character_id: str,
    room_of: Callable[[str], str | None],
) -> bool:
    """Return whether a serialized event is visible to one character.

    This is the transport-safe counterpart to narration projection visibility.  It accepts
    only the common serialized event fields so plugin events follow the same rules without
    importing their concrete classes.
    """
    if not character_id:
        return False
    actor_id = event.get("actor_id")
    raw_targets = event.get("target_ids")
    targets = raw_targets if isinstance(raw_targets, (list, tuple, set, frozenset)) else ()
    if actor_id == character_id:
        return True
    if character_id in targets:
        return True
    visibility = event.get("visibility")
    if visibility == EventVisibility.PUBLIC.value:
        return True
    if visibility == EventVisibility.ROOM.value:
        return bool(event.get("room_id")) and event.get("room_id") == room_of(character_id)
    if visibility == EventVisibility.DIRECTED.value:
        raw_overhearers = event.get("overhearer_ids")
        overhearers = (
            raw_overhearers
            if isinstance(raw_overhearers, (list, tuple, set, frozenset))
            else ()
        )
        return character_id in overhearers
    return False


def event_base(
    epoch: int,
    *,
    default_visibility: EventVisibility | None = None,
    **kwargs: object,
) -> dict[str, object]:
    """Create the common deterministic event payload fields."""
    base: dict[str, object] = {
        "event_id": uuid4().hex,
        "world_epoch": epoch,
        "created_at": datetime.now(UTC),
    }
    if default_visibility is not None:
        base["visibility"] = default_visibility
    base.update(kwargs)
    return base


# --------------------------------------------------------------------------------------
# Command lifecycle events (spec 18.3)
# --------------------------------------------------------------------------------------


class CommandSubmittedEvent(DomainEvent):
    command_id: str
    command_type: str


class CommandAcceptedEvent(DomainEvent):
    command_id: str
    command_type: str


class CommandRejectedEvent(DomainEvent):
    command_id: str
    command_type: str
    reason: str
    character_actionable: bool = True


class CommandQueuedEvent(DomainEvent):
    command_id: str
    command_type: str
    lane: str


class CommandCancelledEvent(DomainEvent):
    command_id: str
    command_type: str
    lane: str | None = None


class CommandExecutedEvent(DomainEvent):
    command_id: str
    command_type: str
    payload: dict[str, EventValue] = Field(default_factory=dict)
    result_events: tuple[dict[str, EventValue], ...] = ()


class CharacterReactionEvent(DomainEvent):
    """A concise player-facing summary when an addressed agent has no visible result."""

    command_type: str
    summary: str


class CommandExpiredEvent(DomainEvent):
    command_id: str
    command_type: str


# --------------------------------------------------------------------------------------
# Points and controller events
# --------------------------------------------------------------------------------------


class ActionPointsChangedEvent(DomainEvent):
    current: float
    maximum: float


class FocusPointsChangedEvent(DomainEvent):
    current: float
    maximum: float


class ControllerChangedEvent(DomainEvent):
    generation: int
    controller_kind: str


class CharacterClaimedEvent(DomainEvent):
    character_id: str
    controller_id: str
    generation: int


class CharacterChatRequestedEvent(DomainEvent):
    job_id: str
    message: str


class RecoveryEndReason(StrEnum):
    EXPLICIT = "explicit"
    DURATION = "duration"
    ACTION = "action"
    DANGER = "danger"


class RestStartedEvent(DomainEvent):
    session_id: str
    until_epoch: float | None = None


class RestEndedEvent(DomainEvent):
    session_id: str
    reason: RecoveryEndReason


class SleepStartedEvent(DomainEvent):
    session_id: str
    until_epoch: float | None = None


class CharacterWokeEvent(DomainEvent):
    session_id: str
    reason: RecoveryEndReason


class WorldPauseStatusChangedEvent(DomainEvent):
    paused: bool
    state: str
    message: str


# --------------------------------------------------------------------------------------
# Health / downed / death events (spec 18.3)
# --------------------------------------------------------------------------------------


class CharacterDownedEvent(DomainEvent):
    cause: str


class CharacterRevivedEvent(DomainEvent):
    pass


class CharacterDiedEvent(DomainEvent):
    cause: str


class EncumbranceChangedEvent(DomainEvent):
    current_load: float
    capacity: float
    overburdened: bool
    speed_multiplier: float


class InjuryAddedEvent(DomainEvent):
    injury_id: str
    body_part: str
    severity: float
    bleeding_rate: float = 0.0


class PainChangedEvent(DomainEvent):
    current: float


class BleedingChangedEvent(DomainEvent):
    rate: float
    accumulated_loss: float


class EntitySeenEvent(DomainEvent):
    entity_id: str


class NoiseHeardEvent(DomainEvent):
    noise_id: str
    source_entity_id: str | None = None
    text: str = ""


class AttentionShiftedEvent(DomainEvent):
    focus_entity_id: str | None = None
    focus_room_id: str | None = None
    score: float


class AffectChangedEvent(DomainEvent):
    labels: tuple[str, ...] = ()


class PartnershipStartedEvent(DomainEvent):
    partner_id: str


class PartnershipEndedEvent(DomainEvent):
    partner_id: str


class PregnancyStartedEvent(DomainEvent):
    pregnant_id: str
    co_parent_ids: tuple[str, ...] = ()
    due_at_epoch: int


class BirthDueEvent(DomainEvent):
    pregnant_id: str
    due_since_epoch: int


class BirthResolvedEvent(DomainEvent):
    child_id: str
    parent_ids: tuple[str, ...] = ()


class AdoptionCompletedEvent(DomainEvent):
    child_id: str
    parent_id: str


class ReservationCreatedEvent(DomainEvent):
    target_id: str


class ReservationReleasedEvent(DomainEvent):
    target_id: str


class ResourceGatheredEvent(DomainEvent):
    node_id: str
    resource_type: str
    quantity: int
    stack_id: str


class StockpileCreatedEvent(DomainEvent):
    stockpile_id: str
    capacity: int


class StorageFilterChangedEvent(DomainEvent):
    stockpile_id: str
    allowed_types: tuple[str, ...] = ()


class ItemForbiddenEvent(DomainEvent):
    item_id: str
    forbidden: bool


class ItemHauledEvent(DomainEvent):
    item_id: str
    target_container_id: str


class StackSplitEvent(DomainEvent):
    source_stack_id: str
    new_stack_id: str
    quantity: int


class StackMergedEvent(DomainEvent):
    source_stack_id: str
    target_stack_id: str
    quantity: int


class ItemCraftedEvent(DomainEvent):
    recipe_id: str
    output_ids: tuple[str, ...] = ()


class JobAssignedEvent(DomainEvent):
    job_id: str


class JobCompletedEvent(DomainEvent):
    job_id: str


class OwnershipClaimedEvent(DomainEvent):
    target_id: str


class OwnershipReleasedEvent(DomainEvent):
    target_id: str


class CharacterAttackedEvent(DomainEvent):
    target_id: str
    weapon_id: str | None = None
    damage: float
    lethal: bool = False
    sparring: bool = False


class CharacterDefendedEvent(DomainEvent):
    reduction: float


class CombatChallengeEvent(DomainEvent):
    target_id: str
    terms: str = ""


class FortificationBuiltEvent(DomainEvent):
    target_id: str
    durability: float
    rating: float


class RaidStartedEvent(DomainEvent):
    target_id: str
    intensity: float
    damage: float


class CharacterPickpocketedEvent(DomainEvent):
    target_id: str
    item_id: str


# --------------------------------------------------------------------------------------
# Movement / world events
# --------------------------------------------------------------------------------------


class ActorMovedEvent(DomainEvent):
    from_room_id: str
    to_room_id: str
    direction: str | None = None
    arrival_summary: str = ""


class RoomLookedEvent(DomainEvent):
    room_title: str
    summary: str


class EntityInspectedEvent(DomainEvent):
    entity_id: str
    name: str
    kind: str | None = None
    description: str = ""
    text: str = ""
    state: str = ""
    facts: tuple[dict[str, object], ...] = ()


# --------------------------------------------------------------------------------------
# Inventory / object events (spec 18.3)
# --------------------------------------------------------------------------------------


class ItemTakenEvent(DomainEvent):
    item_id: str
    from_container_id: str


class ItemPutEvent(DomainEvent):
    item_id: str
    to_container_id: str


class ItemDroppedEvent(DomainEvent):
    item_id: str
    room_id_dropped: str


class ItemHeldEvent(DomainEvent):
    item_id: str
    slot: str


class ItemUnheldEvent(DomainEvent):
    item_id: str
    slot: str


class ItemWornEvent(DomainEvent):
    item_id: str
    slot: str


class ItemRemovedEvent(DomainEvent):
    item_id: str
    slot: str


class ItemUsedEvent(DomainEvent):
    item_id: str
    affordance: str
    tool_id: str | None = None


class ContainerOpenedEvent(DomainEvent):
    target_id: str


class ContainerClosedEvent(DomainEvent):
    target_id: str


class DoorOpenedEvent(DomainEvent):
    target_id: str


class DoorClosedEvent(DomainEvent):
    target_id: str


class EntityLockedEvent(DomainEvent):
    target_id: str
    tool_id: str | None = None


class EntityUnlockedEvent(DomainEvent):
    target_id: str
    tool_id: str | None = None


class PhysicalWriteEvent(DomainEvent):
    item_id: str
    text: str


# --------------------------------------------------------------------------------------
# Speech events (spec 14, 18.3). Visibility distinguishes room speech from directed.
# --------------------------------------------------------------------------------------


class SpeechSaidEvent(DomainEvent):
    text: str
    author_intent: str | None = None
    inferred_intent: str | None = None
    final_interpretation: str | None = None
    approach: str | None = None


class SpeechToldEvent(DomainEvent):
    text: str
    author_intent: str | None = None
    inferred_intent: str | None = None
    final_interpretation: str | None = None
    approach: str | None = None
    overhearer_ids: tuple[str, ...] = ()


class ConversationStartedEvent(DomainEvent):
    conversation_id: str
    participant_ids: tuple[str, ...] = ()
    topic: str = ""
    active_participant_id: str | None = None
    expires_at_epoch: int = 0


class ConversationLineEvent(DomainEvent):
    conversation_id: str
    speaker_id: str
    text: str
    turn_index: int
    next_participant_id: str | None = None
    author_intent: str | None = None
    inferred_intent: str | None = None
    final_interpretation: str | None = None
    approach: str | None = None


class ConversationEndedEvent(DomainEvent):
    conversation_id: str
    participant_ids: tuple[str, ...] = ()
    reason: str = "ended"


# --------------------------------------------------------------------------------------
# Private notes / memory events (spec 15, 18.4). Always private to the character.
# --------------------------------------------------------------------------------------


class NoteTakenEvent(DomainEvent):
    note_id: str
    text: str
    scope: str = "private"
    collection: str | None = None


class NotesSearchedEvent(DomainEvent):
    query: str | None
    mode: str
    results: tuple[str, ...] = ()
    note_ids: tuple[str, ...] = ()
    scope: str = "private"
    collection: str | None = None


class NoteForgottenEvent(DomainEvent):
    note_id: str
    scope: str = "private"
    collection: str | None = None


class ReflectionCreatedEvent(DomainEvent):
    note_id: str
    text: str
    source_note_ids: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------
# World generation events (spec 18.3, 22)
# --------------------------------------------------------------------------------------


class WorldGeneratedEvent(DomainEvent):
    seed: str
    room_count: int
    character_count: int


class GeneratedEntityEvent(DomainEvent):
    seed: str
    entity_id: str
    entity_key: str
    entity_kind: str
    generation: GenerationIntentComponent = Field(default_factory=GenerationIntentComponent)

    @property
    def intent(self) -> str:
        return self.generation.description

    @property
    def tags(self) -> tuple[str, ...]:
        return self.generation.tags

    @property
    def wants(self) -> tuple[str, ...]:
        return self.generation.wants

    @property
    def needs(self) -> tuple[str, ...]:
        return self.generation.needs


class RoomGeneratedEvent(GeneratedEntityEvent):
    room_key: str
    biome: str = "unknown"
    indoor: bool = False


class ObjectGeneratedEvent(GeneratedEntityEvent):
    object_key: str
    room_id: str | None = None
    container_id: str | None = None
    containment_mode: str = "room_content"


class CharacterGeneratedEvent(GeneratedEntityEvent):
    character_key: str
    room_id: str
    species: str = "bunny"


class WorldGenerationStartedEvent(DomainEvent):
    job_id: str
    seed: str
    generator: str


class WorldGenerationCompletedEvent(DomainEvent):
    job_id: str
    seed: str
    generator: str
    room_count: int
    character_count: int


class WorldGenerationFailedEvent(DomainEvent):
    job_id: str
    seed: str
    generator: str
    error: str


class ReactionCascadeLimitedEvent(DomainEvent):
    """Diagnostic emitted when a causal chain exceeds the configured hop limit."""

    source_event_id: str
    hops: int
    reason: str = "causal hop limit exceeded"


# --------------------------------------------------------------------------------------
# Event bus
# --------------------------------------------------------------------------------------

E = TypeVar("E", bound=DomainEvent)
Handler = Callable[[DomainEvent], None | Awaitable[None]]


_PLACEMENT_ORDER = {"core": 0, "foundation": 1, "inner": 2, "outer": 3, "addon": 4}


@dataclass(frozen=True)
class _Subscription:
    event_type: type[DomainEvent]
    handler: Handler
    reaction_id: str
    plugin_id: str
    placement: str
    external: bool
    sequence: int

    @property
    def order(self) -> tuple[int, str, str, int]:
        return (
            _PLACEMENT_ORDER.get(self.placement, _PLACEMENT_ORDER["outer"]),
            self.plugin_id,
            self.reaction_id,
            self.sequence,
        )


class EventBus:
    """Breadth-first deterministic dispatch with support for async handlers.

    Subscriptions are keyed by event class. Subscribing to a base class (e.g.
    ``DomainEvent``) receives every subclass, enabling audit/logging sinks.
    """

    def __init__(
        self,
        *,
        max_deliveries: int = 256,
        max_causal_hops: int = 16,
    ) -> None:
        self._handlers: dict[type[DomainEvent], list[Handler]] = defaultdict(list)
        self._subscriptions: list[_Subscription] = []
        self._events: deque[tuple[DomainEvent, int]] = deque()
        self._deliveries: deque[tuple[_Subscription, DomainEvent, int]] = deque()
        self._external: deque[tuple[_Subscription, DomainEvent]] = deque()
        self._delivered: set[tuple[str, str, str]] = set()
        self._dispatching = False
        self._flushing_external = False
        self._transaction_depth = 0
        self._sequence = 0
        self._registration_plugin_id = "bunnyland.core"
        self._registration_placement = "core"
        self._current_event: DomainEvent | None = None
        self._current_depth = 0
        self._remaining_deliveries = max_deliveries
        self.max_deliveries = max_deliveries
        self.max_causal_hops = max_causal_hops
        self.diagnostics: list[ReactionCascadeLimitedEvent] = []

    def subscribe(
        self,
        event_type: type[E],
        handler: Handler,
        *,
        reaction_id: str | None = None,
        plugin_id: str | None = None,
        placement: str | None = None,
        external: bool = False,
    ) -> None:
        reaction_id = reaction_id or (
            f"{getattr(handler, '__module__', type(handler).__module__)}:"
            f"{getattr(handler, '__qualname__', type(handler).__qualname__)}:{self._sequence}"
        )
        self._handlers[event_type].append(handler)
        self._subscriptions.append(
            _Subscription(
                event_type=event_type,
                handler=handler,
                reaction_id=reaction_id,
                plugin_id=plugin_id or self._registration_plugin_id,
                placement=placement or self._registration_placement,
                external=external,
                sequence=self._sequence,
            )
        )
        self._sequence += 1

    def begin_registration(self, plugin_id: str, placement: str) -> tuple[str, str]:
        """Set defaults for subscriptions installed by one plugin factory."""

        previous = (self._registration_plugin_id, self._registration_placement)
        self._registration_plugin_id = plugin_id
        self._registration_placement = placement
        return previous

    def end_registration(self, previous: tuple[str, str]) -> None:
        self._registration_plugin_id, self._registration_placement = previous

    def unsubscribe(self, event_type: type[E], handler: Handler) -> None:
        handlers = self._handlers.get(event_type)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return
        self._subscriptions = [
            subscription
            for subscription in self._subscriptions
            if not (subscription.event_type is event_type and subscription.handler == handler)
        ]

    def begin_transaction(self) -> None:
        """Defer external sinks until the actor-owned transaction has committed."""

        self._transaction_depth += 1
        if self._transaction_depth == 1:
            self._remaining_deliveries = self.max_deliveries

    async def end_transaction(self) -> None:
        if self._transaction_depth == 0:
            return
        self._transaction_depth -= 1
        if self._transaction_depth == 0:
            await self.flush_external()

    def _derived_event(self, event: DomainEvent) -> tuple[DomainEvent, int] | None:
        source = self._current_event
        if source is None:
            return event, 0
        depth = self._current_depth + 1
        if depth > self.max_causal_hops:
            diagnostic = ReactionCascadeLimitedEvent(
                **event_base(
                    source.world_epoch,
                    correlation_id=source.correlation_id or source.event_id,
                    causation_id=source.event_id,
                    source_event_id=source.event_id,
                    hops=depth,
                )
            )
            self.diagnostics.append(diagnostic)
            self._events.append((diagnostic, 0))
            return None
        updates: dict[str, str] = {}
        if event.causation_id is None:
            updates["causation_id"] = source.event_id
        if event.correlation_id is None:
            updates["correlation_id"] = source.correlation_id or source.event_id
        return (event.model_copy(update=updates) if updates else event), depth

    def _schedule_deliveries(self, event: DomainEvent, depth: int) -> None:
        matching = sorted(
            (
                subscription
                for subscription in self._subscriptions
                if isinstance(event, subscription.event_type)
            ),
            key=lambda subscription: subscription.order,
        )
        for subscription in matching:
            if subscription.external:
                self._external.append((subscription, event))
            else:
                self._deliveries.append((subscription, event, depth))

    async def drain(self) -> None:
        if self._dispatching:
            return
        self._dispatching = True
        try:
            while self._remaining_deliveries > 0 and (self._deliveries or self._events):
                if not self._deliveries:
                    event, depth = self._events.popleft()
                    self._schedule_deliveries(event, depth)
                    if not self._deliveries:
                        continue
                subscription, event, depth = self._deliveries.popleft()
                delivery_key = (
                    event.event_id,
                    subscription.plugin_id,
                    subscription.reaction_id,
                )
                if delivery_key in self._delivered:
                    continue
                events_before = deque(self._events)
                deliveries_before = deque(self._deliveries)
                external_before = deque(self._external)
                diagnostics_before = len(self.diagnostics)
                self._delivered.add(delivery_key)
                self._remaining_deliveries -= 1
                self._current_event = event
                self._current_depth = depth
                try:
                    result = subscription.handler(event)
                    if isinstance(result, Awaitable):
                        await result
                except Exception:
                    self._events = events_before
                    self._deliveries = deliveries_before
                    self._deliveries.appendleft((subscription, event, depth))
                    self._external = external_before
                    del self.diagnostics[diagnostics_before:]
                    self._delivered.discard(delivery_key)
                    self._remaining_deliveries += 1
                    raise
        finally:
            self._current_event = None
            self._current_depth = 0
            self._dispatching = False

    async def flush_external(self) -> None:
        if self._transaction_depth or self._flushing_external:
            return
        self._flushing_external = True
        try:
            while self._external:
                subscription, event = self._external.popleft()
                delivery_key = (
                    event.event_id,
                    subscription.plugin_id,
                    subscription.reaction_id,
                )
                if delivery_key in self._delivered:
                    continue
                self._delivered.add(delivery_key)
                result = subscription.handler(event)
                if isinstance(result, Awaitable):
                    await result
        finally:
            self._flushing_external = False

    async def publish(self, event: DomainEvent) -> None:
        scheduled = self._derived_event(event)
        if scheduled is not None:
            self._events.append(scheduled)
        if self._dispatching:
            return
        if self._transaction_depth == 0:
            self._remaining_deliveries = self.max_deliveries
        await self.drain()
        if self._transaction_depth == 0:
            await self.flush_external()


__all__ = [
    "ActionPointsChangedEvent",
    "AdoptionCompletedEvent",
    "ActorMovedEvent",
    "AffectChangedEvent",
    "AttentionShiftedEvent",
    "BleedingChangedEvent",
    "BirthDueEvent",
    "BirthResolvedEvent",
    "CharacterDiedEvent",
    "CharacterDownedEvent",
    "CharacterAttackedEvent",
    "CharacterDefendedEvent",
    "CharacterPickpocketedEvent",
    "CharacterClaimedEvent",
    "CharacterChatRequestedEvent",
    "CharacterReactionEvent",
    "CharacterRevivedEvent",
    "CombatChallengeEvent",
    "CommandAcceptedEvent",
    "CommandCancelledEvent",
    "CommandExecutedEvent",
    "CommandExpiredEvent",
    "CommandQueuedEvent",
    "CommandRejectedEvent",
    "CommandSubmittedEvent",
    "ContainerClosedEvent",
    "ContainerOpenedEvent",
    "ControllerChangedEvent",
    "DomainEvent",
    "DoorClosedEvent",
    "DoorOpenedEvent",
    "EncumbranceChangedEvent",
    "EntityInspectedEvent",
    "EntityLockedEvent",
    "EntitySeenEvent",
    "EntityUnlockedEvent",
    "EventBus",
    "EventVisibility",
    "event_base",
    "FortificationBuiltEvent",
    "FocusPointsChangedEvent",
    "GeneratedEntityEvent",
    "InjuryAddedEvent",
    "ItemDroppedEvent",
    "ItemCraftedEvent",
    "ItemHeldEvent",
    "ItemRemovedEvent",
    "ItemPutEvent",
    "ItemTakenEvent",
    "ItemUnheldEvent",
    "ItemUsedEvent",
    "ItemWornEvent",
    "JobAssignedEvent",
    "JobCompletedEvent",
    "ItemForbiddenEvent",
    "ItemHauledEvent",
    "NoiseHeardEvent",
    "NoteTakenEvent",
    "NotesSearchedEvent",
    "OwnershipClaimedEvent",
    "OwnershipReleasedEvent",
    "PainChangedEvent",
    "PartnershipEndedEvent",
    "PartnershipStartedEvent",
    "PregnancyStartedEvent",
    "ReservationCreatedEvent",
    "ReservationReleasedEvent",
    "RaidStartedEvent",
    "ResourceGatheredEvent",
    "StackMergedEvent",
    "StackSplitEvent",
    "StockpileCreatedEvent",
    "StorageFilterChangedEvent",
    "PhysicalWriteEvent",
    "ReflectionCreatedEvent",
    "RoomGeneratedEvent",
    "ReactionCascadeLimitedEvent",
    "RoomLookedEvent",
    "SpeechSaidEvent",
    "SpeechToldEvent",
    "WorldPauseStatusChangedEvent",
    "WorldGenerationCompletedEvent",
    "WorldGenerationFailedEvent",
    "WorldGenerationStartedEvent",
    "WorldGeneratedEvent",
    "ObjectGeneratedEvent",
    "CharacterGeneratedEvent",
]
