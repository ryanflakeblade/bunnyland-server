"""Drive LLM-controlled characters one decision at a time (spec 25.4).

Each ``run_once`` walks the active, LLM-controlled characters, builds each one's
foundation prompt, asks its agent for a single tool call, and submits the resulting
command. Decisions are logged as observable summaries (the verb and target), never hidden
chain-of-thought (spec 25.4). The engine still validates and costs everything on the next
tick — dispatch only proposes.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from relics import Entity, EntityId, World
from relics.errors import EntityNotFoundError

from .. import telemetry
from ..core.commands import CommitReceipt
from ..core.components import (
    ActionPointsComponent,
    CharacterComponent,
    DeadComponent,
    DownedComponent,
    IdentityComponent,
    RoomComponent,
    SleepingComponent,
    SuspendedComponent,
)
from ..core.controllers import (
    BehaviorControllerComponent,
    LLMControllerComponent,
    ScriptedControllerComponent,
)
from ..core.ecs import container_of, contents, parse_entity_id, reachable_ids
from ..core.edges import ControlledBy, ExitTo
from ..core.events import (
    CommandCancelledEvent,
    CommandExecutedEvent,
    CommandExpiredEvent,
    CommandRejectedEvent,
    DomainEvent,
)
from ..core.world_actor import WorldActor
from ..narration.projection import event_salience, event_summary, event_visible_to
from ..prompts.builder import (
    PerceivedPromptEvent,
    PromptBuilder,
    PromptContext,
    render_compact_prompt,
)
from ..prompts.filters import PromptFilterRuntime, apply_prompt_filters
from .agent import (
    AgentDecision,
    CharacterAgent,
    InvalidAgentResponse,
    LLMRequestSettings,
    ScriptedAgent,
    call_with_llm_request_settings,
)
from .behavior_tree import BehaviorTree, BehaviorTreeAgent, resolve_behavior_tree
from .scripts import resolve_script
from .tools import (
    DISCOVER_ACTION_TOOL,
    ToolCall,
    action_discovery_schema,
    command_from_tool_call,
    contextual_action_definitions,
    reference_arg_keys,
    tool_schemas,
)

logger = logging.getLogger("bunnyland.dispatch")

ControllerAgentFactory = Callable[
    ["ControllerDispatch", str, object],
    tuple[CharacterAgent, str | None, str | None],
]


def _llm_agent_factory(
    dispatch: ControllerDispatch, _character_id: str, component: object
) -> tuple[CharacterAgent, str | None, str | None]:
    controller = component
    assert isinstance(controller, LLMControllerComponent)
    return dispatch.agent, controller.model, controller.provider


def _behavior_agent_factory(
    dispatch: ControllerDispatch, _character_id: str, component: object
) -> tuple[CharacterAgent, str | None, str | None]:
    controller = component
    assert isinstance(controller, BehaviorControllerComponent)
    return dispatch._behavior_agent(controller.behavior_name), None, None


def _scripted_agent_factory(
    dispatch: ControllerDispatch, character_id: str, component: object
) -> tuple[CharacterAgent, str | None, str | None]:
    controller = component
    assert isinstance(controller, ScriptedControllerComponent)
    return (
        dispatch._scripted_agent(character_id, controller.script_name, controller.loop),
        None,
        None,
    )


#: Controller components whose actions the engine proposes (as opposed to human/external
#: controllers like Discord/web/MCP, or the suspended no-op controller).
_CONTROLLER_AGENT_FACTORIES: dict[type, ControllerAgentFactory] = {
    LLMControllerComponent: _llm_agent_factory,
    BehaviorControllerComponent: _behavior_agent_factory,
    ScriptedControllerComponent: _scripted_agent_factory,
}
_AUTONOMOUS_COMPONENTS = tuple(_CONTROLLER_AGENT_FACTORIES)
AutonomousController = object

DEFAULT_MAX_BUFFERED_EVENTS = 100
DEFAULT_MAX_BUFFERED_EVENT_CHARS = 8_000
DEFAULT_INTERACTIVE_DECISION_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_CONCURRENT_LLM_DECISIONS = 4


def _autonomous_controller_kind(component: AutonomousController) -> str:
    if isinstance(component, LLMControllerComponent):
        return "llm"
    if isinstance(component, BehaviorControllerComponent):
        return "behavior"
    if isinstance(component, ScriptedControllerComponent):
        return "scripted"
    return "unknown"


def register_autonomous_controller(
    component_type: type,
    agent_factory: ControllerAgentFactory,
) -> None:
    """Register an engine-driven controller component contributed by a plugin."""

    _CONTROLLER_AGENT_FACTORIES[component_type] = agent_factory
    global _AUTONOMOUS_COMPONENTS
    _AUTONOMOUS_COMPONENTS = tuple(_CONTROLLER_AGENT_FACTORIES)


def name_candidates(world: World, character: Entity) -> list[tuple[str, EntityId]]:
    """(name, id) pairs the character could be referring to: reachable entities plus the
    rooms its exits lead to. Objects/characters use their identity name; rooms use title."""
    ids = reachable_ids(world, character)
    room_id = container_of(character)
    if room_id is not None:
        ids.add(room_id)
        for _edge, target in world.get_entity(room_id).get_relationships(ExitTo):
            ids.add(target)
    # Every id collected above is a live entity: reachable_ids pre-filters, container_of follows
    # a live edge, and Relics cascades dangling ExitTo edges on world.remove(), so no has_entity
    # guard is needed before expanding contents.
    for entity_id in tuple(ids):
        ids.update(contents(world.get_entity(entity_id)))

    candidates: list[tuple[str, EntityId]] = []
    for entity_id in ids:
        if entity_id == character.id:
            continue
        entity = world.get_entity(entity_id)
        if entity.has_component(IdentityComponent):
            candidates.append((entity.get_component(IdentityComponent).name, entity_id))
        elif entity.has_component(RoomComponent):
            candidates.append((entity.get_component(RoomComponent).title, entity_id))
    return candidates


def resolve_reference(value: str, candidates: list[tuple[str, EntityId]], *, world: World) -> str:
    """Resolve a human-readable name to an entity id (case-insensitive prefix match).

    Already-valid entity ids pass through. An exact (case-insensitive) name wins; otherwise
    the shortest candidate whose name starts with the query is chosen ("Mar" -> "marsh").
    Unresolvable values are returned unchanged so the handler rejects them observably.
    """
    parsed = parse_entity_id(value)
    if parsed is not None and world.has_entity(parsed):
        return value
    query = value.strip().lower()
    if not query:
        return value
    for name, entity_id in candidates:
        if name.lower() == query:
            return str(entity_id)
    matches = sorted(
        (nc for nc in candidates if nc[0].lower().startswith(query)),
        key=lambda nc: (len(nc[0]), nc[0].lower()),
    )
    if matches:
        return str(matches[0][1])
    return value


def suggest_names(
    query: str, candidates: list[tuple[str, EntityId]], *, limit: int = 3
) -> list[str]:
    """Candidate names nearest an unresolvable query, for a 'did you mean...' hint.

    Prefers prefix then substring matches, then fuzzy (difflib) matches, de-duplicated and
    capped at ``limit``."""
    names = [name for name, _ in candidates]
    q = query.strip().lower()
    prefix = [n for n in names if n.lower().startswith(q)]
    substring = [n for n in names if q and q in n.lower()]
    fuzzy = difflib.get_close_matches(query, names, n=limit, cutoff=0.5)

    ordered: list[str] = []
    for name in [*prefix, *substring, *fuzzy]:
        if name not in ordered:
            ordered.append(name)
    return ordered[:limit]


def resolve_reference_args(
    world: World,
    character: Entity,
    arguments: Mapping[str, object],
    *,
    keys: frozenset[str] | None = None,
    suggestions: int = 3,
) -> tuple[dict, dict[str, list[str]]]:
    """Resolve entity-reference args to ids.

    Returns ``(resolved, unresolved)`` where ``resolved`` is a copy of ``arguments`` with
    reference keys mapped to entity ids where possible, and ``unresolved`` maps each
    reference key that did not resolve to a list of suggested names.
    """
    candidates = name_candidates(world, character)
    resolved = dict(arguments)
    unresolved: dict[str, list[str]] = {}
    for key in keys or reference_arg_keys():
        value = resolved.get(key)
        if not isinstance(value, str):
            continue
        if not value.strip():
            resolved.pop(key, None)
            continue
        mapped = resolve_reference(value, candidates, world=world)
        resolved[key] = mapped
        parsed = parse_entity_id(mapped)
        if parsed is None or not world.has_entity(parsed):
            unresolved[key] = suggest_names(value, candidates, limit=suggestions)
    return resolved, unresolved


def did_you_mean(arguments: Mapping[str, object], unresolved: Mapping[str, list[str]]) -> str:
    """Build a 'did you mean...' message for reference args that did not resolve.

    Used for both humans (Discord reply) and LLM agents (fed back as a prompt warning), so
    the two get identical guidance."""
    parts: list[str] = []
    for key, names in unresolved.items():
        typed = arguments.get(key)
        label = key.replace("_id", "").replace("_", " ")
        if names:
            parts.append(
                f"I don't see {typed!r} ({label}) here. Did you mean: " + ", ".join(names) + "?"
            )
        else:
            parts.append(f"I don't see {typed!r} ({label}) here, and nothing similar.")
    return " ".join(parts)


@dataclass(frozen=True)
class Decision:
    """An observable record of what an agent chose (no hidden reasoning)."""

    character_id: str
    tool: str | None
    summary: str
    persona_issues: tuple[str, ...] = ()
    input_epoch: int = 0
    governing_pressure: str = "exploration"
    memory_ids: tuple[str, ...] = ()
    candidate_actions: tuple[str, ...] = ()
    policy_rejections: tuple[str, ...] = ()
    selected_action: str | None = None
    command_id: str | None = None
    submission_accepted: bool | None = None
    submission_reason: str = ""
    receipt_status: str | None = None
    receipt_reason: str = ""
    result_event_ids: tuple[str, ...] = ()
    prompt_event_ids: tuple[str, ...] = ()
    omitted_prompt_events: int = 0

    def with_receipt(self, receipt: CommitReceipt | None) -> Decision:
        if receipt is None:
            return self
        return replace(
            self,
            receipt_status=receipt.status.value,
            receipt_reason=receipt.reason,
            result_event_ids=receipt.event_ids,
        )


def _decision_outcome(decision: Decision) -> str:
    if decision.summary == "error":
        return "error"
    if decision.policy_rejections or decision.submission_accepted is False:
        return "rejected"
    if decision.tool is None:
        return "wait"
    if decision.submission_accepted:
        return "accepted"
    return "completed"


@dataclass(frozen=True)
class _EventBatch:
    events: tuple[PerceivedPromptEvent, ...] = ()
    omitted: int = 0
    omitted_epoch_range: tuple[int, int] | None = None

    @property
    def has_content(self) -> bool:
        return bool(self.events or self.omitted)


class _PerceivedEventBuffer:
    def __init__(self, max_events: int, max_chars: int) -> None:
        self.max_events = max(1, max_events)
        self.max_chars = max(1, max_chars)
        self.events: list[PerceivedPromptEvent] = []
        self.omitted = 0
        self.omitted_epoch_range: tuple[int, int] | None = None

    @property
    def has_content(self) -> bool:
        return bool(self.events or self.omitted)

    def append(self, event: PerceivedPromptEvent) -> None:
        self.events.append(event)
        self._trim()

    def drain(self) -> _EventBatch:
        batch = _EventBatch(
            events=tuple(self.events),
            omitted=self.omitted,
            omitted_epoch_range=self.omitted_epoch_range,
        )
        self.events = []
        self.omitted = 0
        self.omitted_epoch_range = None
        return batch

    def restore(self, batch: _EventBatch) -> None:
        self.events = [*batch.events, *self.events]
        self.omitted += batch.omitted
        if batch.omitted_epoch_range is not None:
            self._record_omitted_range(*batch.omitted_epoch_range, increment=False)
        self._trim()

    def _trim(self) -> None:
        while len(self.events) > self.max_events or self._chars() > self.max_chars:
            index = min(
                range(len(self.events)),
                key=lambda candidate: (self.events[candidate].salience, candidate),
            )
            event = self.events.pop(index)
            self._record_omitted_range(event.world_epoch, event.world_epoch)

    def _chars(self) -> int:
        return sum(
            len(event.summary) + len(event.event_type) + len(event.event_id)
            for event in self.events
        )

    def _record_omitted_range(
        self, start: int, end: int, *, increment: bool = True
    ) -> None:
        if increment:
            self.omitted += 1
        if self.omitted_epoch_range is None:
            self.omitted_epoch_range = (start, end)
            return
        self.omitted_epoch_range = (
            min(self.omitted_epoch_range[0], start),
            max(self.omitted_epoch_range[1], end),
        )


@dataclass(frozen=True)
class _PromptProjection:
    context: PromptContext
    schemas: tuple[dict, ...]
    controller_id: EntityId
    generation: int
    agent: CharacterAgent
    model: str | None
    provider: str | None
    prompted: bool
    controller_kind: str
    started_at: float
    request_settings: LLMRequestSettings


@dataclass
class _CharacterDispatchState:
    events: _PerceivedEventBuffer
    active_task: asyncio.Task[Decision] | None = None
    active_events: _EventBatch = _EventBatch()
    pending_projection: _PromptProjection | None = None


def _tool_text(call: ToolCall) -> str:
    return " ".join(value.strip() for value in call.arguments.values() if isinstance(value, str))


def persona_contradictions(context, call: ToolCall) -> tuple[str, ...]:
    """Detect deterministic contradictions against stable prompt persona facts."""

    text = _tool_text(call)
    if not text:
        return ()
    lowered = text.lower()
    issues: list[str] = []
    visible_names = {name.lower(): name for name in context.visible_characters}
    for pattern in (
        r"\bmy name is\s+([A-Za-z][A-Za-z0-9 _'-]{1,40})",
        r"\bcall me\s+([A-Za-z][A-Za-z0-9 _'-]{1,40})",
        r"\bi am\s+([A-Za-z][A-Za-z0-9 _'-]{1,40})",
        r"\bi'm\s+([A-Za-z][A-Za-z0-9 _'-]{1,40})",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match is None:
            continue
        claimed = match.group(1).strip(" .,!?:;\"'")
        claimed_key = claimed.lower()
        if claimed_key != context.name.lower() and claimed_key in visible_names:
            issues.append(f"name contradiction: claimed to be {visible_names[claimed_key]}")
            break

    status = context.status.split(",", 1)[0].strip().lower()
    impossible_statuses = {
        "dead": ("i am dead", "i'm dead"),
        "asleep": ("i am asleep", "i'm asleep"),
        "downed": ("i am downed", "i'm downed"),
    }
    for claimed_status, phrases in impossible_statuses.items():
        if status != claimed_status and any(phrase in lowered for phrase in phrases):
            issues.append(f"impossible self-claim: claimed {claimed_status}")

    for line in context.persona:
        relationship = re.fullmatch(r"(.+) is your ([A-Za-z0-9 _'-]+)\.", line)
        if relationship is not None:
            name, relation = relationship.groups()
            name_key = name.lower()
            relation_key = relation.lower()
            if (
                f"{name_key} is not my {relation_key}" in lowered
                or f"{name_key} is not your {relation_key}" in lowered
            ):
                issues.append(f"relationship contradiction: denied {name}'s {relation} status")
        partner = re.fullmatch(r"You are partners with (.+)\.", line)
        if partner is not None:
            name = partner.group(1)
            name_key = name.lower()
            if f"not partners with {name_key}" in lowered:
                issues.append(f"relationship contradiction: denied partnership with {name}")
        bond = re.fullmatch(r"You (are fond of|know|fear|resent|dislike) (.+)\.", line)
        if bond is not None:
            descriptor, name = bond.groups()
            name_key = name.lower()
            descriptor_key = descriptor.lower()
            if descriptor_key == "are fond of":
                denials = (
                    f"i am not fond of {name_key}",
                    f"i'm not fond of {name_key}",
                )
            else:
                denials = (
                    f"i do not {descriptor_key} {name_key}",
                    f"i don't {descriptor_key} {name_key}",
                )
            if any(denial in lowered for denial in denials):
                issues.append(f"relationship contradiction: denied bond with {name}")
    return tuple(dict.fromkeys(issues))


class ControllerDispatch:
    """Turns agent tool calls into submitted commands for engine-driven characters.

    Drives every autonomous controller kind: ``llm`` (the injected ``agent``), ``behavioral``
    (a behavior tree resolved by name), and ``scripted`` (a named tool-call sequence). Human
    and external controllers (Discord/web/MCP) and the suspended no-op controller are left
    alone — the engine never proposes actions for them.
    """

    def __init__(
        self,
        actor: WorldActor,
        builder: PromptBuilder,
        agent: CharacterAgent,
        *,
        prompt_filter_runtime: PromptFilterRuntime | None = None,
        behavior_resolver: Callable[[str], BehaviorTree] = resolve_behavior_tree,
        script_resolver: Callable[[str], tuple[ToolCall, ...]] = resolve_script,
        max_buffered_events: int = DEFAULT_MAX_BUFFERED_EVENTS,
        max_buffered_event_chars: int = DEFAULT_MAX_BUFFERED_EVENT_CHARS,
        interactive_decision_timeout_seconds: float = (
            DEFAULT_INTERACTIVE_DECISION_TIMEOUT_SECONDS
        ),
        max_concurrent_llm_decisions: int = DEFAULT_MAX_CONCURRENT_LLM_DECISIONS,
    ) -> None:
        self.actor = actor
        self.builder = builder
        self.agent = agent
        self.prompt_filter_runtime = prompt_filter_runtime or PromptFilterRuntime.from_actor(
            actor, llm=agent
        )
        actor.prompt_filter_runtime = self.prompt_filter_runtime
        self._behavior_resolver = behavior_resolver
        self._script_resolver = script_resolver
        self._max_buffered_events = max(1, max_buffered_events)
        self._max_buffered_event_chars = max(1, max_buffered_event_chars)
        self._interactive_decision_timeout_seconds = max(
            0.001, interactive_decision_timeout_seconds
        )
        self._llm_decision_slots = asyncio.Semaphore(max(1, max_concurrent_llm_decisions))
        # character_id -> a "did you mean..." note to surface on its next prompt, so an
        # agent that named something unreachable gets the same guidance a human does.
        self._feedback: dict[str, str] = {}
        # Full action definitions explicitly requested through the progressive discovery
        # tool, retained per character so later decisions receive their native schemas.
        self._discovered_actions: dict[str, set[str]] = {}
        # Counts dispatch turns so a controller can act only every N ticks (the world still
        # ticks every iteration; only the agent's decisions are throttled).
        self._tick = 0
        # Behavior trees are stateless per tick, so cache one agent per tree name.
        self._behavior_agents: dict[str, BehaviorTreeAgent] = {}
        # Scripts advance across turns, so cache a replaying agent per character; rebuild it
        # if the controller's script or loop setting changes. Keyed by character id.
        self._scripted_agents: dict[str, tuple[str, bool, ScriptedAgent]] = {}
        # Async (LLM) decisions run as background tasks so a slow prompt never blocks the
        # game loop's world ticks. Keyed by character id: a character with a task still
        # running is never re-prompted, so each character has at most one decision pending.
        self._inflight: dict[str, asyncio.Task[Decision]] = {}
        self._inflight_kinds: dict[str, str] = {}
        self._character_states: dict[str, _CharacterDispatchState] = {}
        # Decisions completed by background tasks since the last ``run_once``, surfaced (and
        # cleared) on the next pass so observers still see what autonomous agents chose.
        self._completed: list[Decision] = []
        self._event_reaction_id = f"bunnyland.dispatch:perceived-events:{id(self)}"
        actor.bus.subscribe(
            DomainEvent,
            self._record_perceived_event,
            reaction_id=self._event_reaction_id,
        )

    async def run_once(self) -> list[Decision]:
        self._tick += 1
        # A live world regeneration (admin) swaps actor.world for a brand-new World object.
        # The builder captured the old world at construction; repoint it at the current one
        # so prompts are built against the live world rather than the replaced one.
        self.builder.rebind(self.actor.world)
        decisions: list[Decision] = []
        with telemetry.span("controller.run_once", {"dispatch.tick": self._tick}) as run_span:
            # Surface decisions finished by background (LLM) tasks since the last pass.
            decisions.extend(self._drain_completed())
            actable = self._actable_characters()
            run_span.set_attribute("dispatch.actable_count", len(actable))
            for character_id in actable:
                # Prompt building can still remove a character through an in-place edit;
                # guard that boundary so the dispatch loop continues safely.
                try:
                    decision = await self._decide_for(character_id)
                except EntityNotFoundError:
                    logger.debug("skipping character %s removed mid-dispatch", character_id)
                    continue
                # ``None`` means the decision was handed to a background task and will be
                # surfaced on a later pass once it finishes.
                if decision is not None:
                    decisions.append(decision)
            run_span.set_attribute("dispatch.decision_count", len(decisions))
        # Give every newly scheduled decision one event-loop turn to begin. Immediate
        # deterministic agents can finish before the next world tick, while provider-backed
        # agents suspend naturally without delaying this dispatch pass.
        await asyncio.sleep(0)
        return decisions

    def _has_pending(self, cid: str) -> bool:
        task = self._inflight.get(cid)
        return task is not None and not task.done()

    def _drain_completed(self) -> list[Decision]:
        if not self._completed:
            return []
        drained = self._completed
        self._completed = []
        return drained

    async def await_pending(self) -> list[Decision]:
        """Await every in-flight decision and return the decisions that completed.

        The game loop never waits like this — it lets the world keep ticking — but callers
        that need a decision applied within a bounded run (offline advancement, live tests)
        use this to block until background tasks finish.
        """
        tasks = list(self._inflight.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return self._drain_completed()

    def cancel_pending(self) -> None:
        """Cancel any in-flight decision tasks (used when the game loop stops)."""
        for cid, task in list(self._inflight.items()):
            state = self._character_states.get(cid)
            if state is not None and state.active_events.has_content:
                state.events.restore(state.active_events)
                state.active_events = _EventBatch()
            task.cancel()
        self._inflight.clear()
        self._inflight_kinds.clear()
        for state in self._character_states.values():
            state.active_task = None

    def close(self) -> None:
        """Cancel provider work and detach the dispatch event observer."""
        self.cancel_pending()
        self.actor.bus.unsubscribe(DomainEvent, self._record_perceived_event)

    def inflight_decision_counts(self) -> dict[str, int]:
        """Return current decision tasks grouped by the fixed controller kind."""

        counts = {"llm": 0, "behavior": 0, "scripted": 0, "unknown": 0}
        for kind in self._inflight_kinds.values():
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def _state_for(self, cid: str) -> _CharacterDispatchState:
        state = self._character_states.get(cid)
        if state is None:
            state = _CharacterDispatchState(
                events=_PerceivedEventBuffer(
                    self._max_buffered_events,
                    self._max_buffered_event_chars,
                )
            )
            self._character_states[cid] = state
        return state

    def _record_perceived_event(self, event: DomainEvent) -> None:
        for cid, state in tuple(self._character_states.items()):
            character_id = parse_entity_id(cid)
            if character_id is None or not self.actor.world.has_entity(character_id):
                continue
            character = self.actor.world.get_entity(character_id)
            if not event_visible_to(self.actor.world, character, event):
                continue
            summary = self._prompt_event_summary(character, event)
            if summary is None:
                continue
            state.events.append(
                PerceivedPromptEvent(
                    event_id=event.event_id,
                    event_type=event.__class__.__name__,
                    world_epoch=event.world_epoch,
                    summary=summary,
                    salience=event_salience(event),
                )
            )

    def _prompt_event_summary(self, character: Entity, event: DomainEvent) -> str | None:
        if isinstance(event, CommandRejectedEvent) and not event.character_actionable:
            # Internal failures remain available to operators through logs, telemetry, and
            # receipts. A character cannot repair them, so do not turn them into prompt state.
            return None
        summary = event_summary(self.actor.world, character, event)
        if summary:
            return summary
        if isinstance(event, CommandRejectedEvent):
            return f"Your {event.command_type} action was rejected: {event.reason}."
        if isinstance(event, CommandExecutedEvent):
            return f"Your {event.command_type} action completed."
        if isinstance(event, CommandCancelledEvent):
            return f"Your {event.command_type} action was cancelled."
        if isinstance(event, CommandExpiredEvent):
            return f"Your {event.command_type} action expired."
        message = getattr(event, "message", None)
        if isinstance(message, str) and message.strip():
            return message.strip()
        return None

    def _actable_characters(self) -> list[EntityId]:
        query = (
            self.actor.world.query()
            .with_all([CharacterComponent])
            .with_none([SuspendedComponent, DeadComponent, DownedComponent, SleepingComponent])
        )
        actable: list[EntityId] = []
        for entity in query.execute_entities():
            if not self._has_action_point(entity):
                continue
            controller = self._autonomous_controller(entity.id)
            if controller is None:
                continue
            interval = max(1, controller[2].act_every_ticks)
            if self._tick % interval == 0:
                actable.append(entity.id)
        return actable

    @staticmethod
    def _has_action_point(entity) -> bool:
        if not entity.has_component(ActionPointsComponent):
            return False
        return entity.get_component(ActionPointsComponent).current >= 1.0

    def _autonomous_controller(
        self, character_id: EntityId
    ) -> tuple[EntityId, int, AutonomousController] | None:
        """The character's engine-driven controller: ``(id, generation, component)``."""
        character = self.actor.world.get_entity(character_id)
        for edge, controller_id in character.get_relationships(ControlledBy):
            controller = self.actor.world.get_entity(controller_id)
            for component_type in _AUTONOMOUS_COMPONENTS:
                if controller.has_component(component_type):
                    component = controller.get_component(component_type)
                    return (controller_id, edge.generation, component)
        return None

    def _agent_for(
        self, character_id: str, component: AutonomousController
    ) -> tuple[CharacterAgent, str | None, str | None]:
        """Resolve the agent (and model/provider) for a controller component.

        Raises ``ValueError`` if a behavior/script name is not registered, so the caller can
        skip the character for this turn rather than crash the dispatch loop.
        """
        factory = _CONTROLLER_AGENT_FACTORIES.get(type(component))
        if factory is None:
            raise ValueError(f"unregistered autonomous controller {type(component).__name__}")
        return factory(self, character_id, component)

    def _behavior_agent(self, behavior_name: str) -> BehaviorTreeAgent:
        agent = self._behavior_agents.get(behavior_name)
        if agent is None:
            agent = BehaviorTreeAgent(self._behavior_resolver(behavior_name))
            self._behavior_agents[behavior_name] = agent
        return agent

    def _scripted_agent(self, character_id: str, script_name: str, loop: bool) -> ScriptedAgent:
        cached = self._scripted_agents.get(character_id)
        if cached is not None and cached[0] == script_name and cached[1] == loop:
            return cached[2]
        agent = ScriptedAgent(self._script_resolver(script_name), loop=loop)
        self._scripted_agents[character_id] = (script_name, loop, agent)
        return agent

    async def _decide_for(self, character_id: EntityId) -> Decision | None:
        """Build or coalesce one character projection, then start it when eligible."""
        projection = self._build_projection(character_id)
        if isinstance(projection, Decision):
            return projection

        cid = str(character_id)
        if not projection.prompted:
            if self._has_pending(cid):
                return None
            self._launch_projection(character_id, projection, _EventBatch())
            return None

        state = self._state_for(cid)
        if state.active_task is not None and not state.active_task.done():
            # A dispatch pass represents the character's next turn even when its visible
            # state is unchanged. Keep one slot and overwrite it on every pass; provider
            # history and newly buffered events still make the eventual request meaningful.
            state.pending_projection = projection
            return None

        state.pending_projection = None
        event_batch = state.events.drain()
        self._launch_projection(character_id, projection, event_batch)
        return None

    def _build_projection(self, character_id: EntityId) -> _PromptProjection | Decision:
        started_at = time.perf_counter()
        controller = self._autonomous_controller(character_id)
        assert controller is not None  # filtered in _actable_characters
        controller_id, generation, controller_component = controller
        cid = str(character_id)

        try:
            agent, model, provider = self._agent_for(cid, controller_component)
        except ValueError as exc:
            logger.warning("character %s has an unresolvable controller: %s", character_id, exc)
            return Decision(cid, None, f"wait: {exc}")

        prompted = isinstance(controller_component, LLMControllerComponent)
        controller_kind = _autonomous_controller_kind(controller_component)
        metric_attrs = {"controller_kind": controller_kind}
        with (
            telemetry.record_duration(telemetry.record_prompt_build, metric_attrs),
            telemetry.span("agent.prompt.build", {"character.id": cid}),
        ):
            context = self.builder.build(character_id, epoch=self.actor.epoch)
            policy = self.actor.memory_recall_policy
            store = self.actor.memory_store
            if policy is not None and store is not None:
                try:
                    context = self.builder.with_memory(
                        context,
                        character_id,
                        store=store,
                        limit=policy.limit,
                        min_score=policy.min_score,
                    )
                except Exception:  # noqa: BLE001 - memory backend failure is fail-soft.
                    logger.exception(
                        "memory context failed for character %s; omitting private memory",
                        character_id,
                    )
            pending = self._feedback.get(cid)
            if pending is not None:
                context = replace(context, warnings=(*context.warnings, pending))

        character = self.actor.world.get_entity(character_id)
        all_definitions = self.actor.action_definitions()
        contextual = contextual_action_definitions(self.actor, character)
        contextual_names = {definition.name for definition in contextual}
        discovered_names = self._discovered_actions.get(cid, set())
        discovered = tuple(
            definition
            for definition in all_definitions
            if definition.name in discovered_names and definition.name not in contextual_names
        )
        offered = (*contextual, *discovered)
        # Scene membership survives reloads; do not depend on the launch generator name.
        from ..worldgen.wild_boar_forest import FOREST_ALLOWED_TOOLS, ForestResident

        forest_resident = bool(character.get_relationships(ForestResident))
        if forest_resident:
            offered = tuple(d for d in offered if d.name in FOREST_ALLOWED_TOOLS)
        offered_names = {definition.name for definition in offered}
        schemas = tool_schemas(offered)
        if prompted and not forest_resident:
            discovery = action_discovery_schema(
                tuple(
                    definition
                    for definition in all_definitions
                    if definition.name not in offered_names
                )
            )
            schemas.extend(filter(None, (discovery,)))
        return _PromptProjection(
            context=context,
            schemas=tuple(schemas),
            controller_id=controller_id,
            generation=generation,
            agent=agent,
            model=model,
            provider=provider,
            prompted=prompted,
            controller_kind=controller_kind,
            started_at=started_at,
            request_settings=(
                LLMRequestSettings(
                    temperature=controller_component.temperature,
                    max_output_tokens=controller_component.max_tokens,
                    system_style=controller_component.system_style,
                    tool_policy=controller_component.tool_policy,
                )
                if isinstance(controller_component, LLMControllerComponent)
                else LLMRequestSettings()
            ),
        )

    def _launch_projection(
        self,
        character_id: EntityId,
        projection: _PromptProjection,
        event_batch: _EventBatch,
    ) -> None:
        cid = str(character_id)
        context = replace(
            projection.context,
            perceived_events=event_batch.events,
            omitted_perceived_events=event_batch.omitted,
            omitted_event_epoch_range=event_batch.omitted_epoch_range,
        )
        prompt = render_compact_prompt(context)
        metric_attrs = {
            "provider": projection.provider or "local",
            "model": projection.model or "unknown",
            "agent.kind": type(projection.agent).__name__,
            "controller_kind": projection.controller_kind,
        }
        span_attrs = {
            **metric_attrs,
            "character.id": cid,
            "decision.prompted": projection.prompted,
            "decision.prompt_event_count": len(event_batch.events),
            "decision.omitted_prompt_events": event_batch.omitted,
        }
        if isinstance(projection.agent, BehaviorTreeAgent):
            span_attrs["behavior_tree.name"] = projection.agent.tree.name
        if projection.prompted and telemetry.enabled():
            span_attrs["decision.prompt_chars"] = len(prompt)
            if telemetry.content_capture_enabled():
                span_attrs["decision.prompt"] = telemetry.attr_text(prompt)
        telemetry.record_prompt_characters(
            len(prompt),
            {
                "controller_kind": projection.controller_kind,
                "provider": projection.provider or "local",
                "model": projection.model or "unknown",
            },
        )

        feedback = self._feedback.get(cid)
        if feedback is not None and feedback in context.warnings:
            self._feedback.pop(cid, None)

        task = asyncio.ensure_future(
            self._await_decision(
                character_id,
                projection.controller_id,
                projection.generation,
                context,
                projection.agent,
                prompt,
                projection.model,
                projection.provider,
                list(projection.schemas),
                span_attrs,
                metric_attrs,
                self.actor.epoch,
                event_batch,
                projection.controller_kind,
                projection.started_at,
                projection.request_settings,
            )
        )
        self._inflight[cid] = task
        self._inflight_kinds[cid] = projection.controller_kind
        if projection.prompted:
            state = self._state_for(cid)
            state.active_task = task
            state.active_events = event_batch
        task.add_done_callback(lambda finished, key=cid: self._forget(key, finished))

    async def _await_decision(
        self,
        character_id: EntityId,
        controller_id: EntityId,
        generation: int,
        context,
        agent: CharacterAgent,
        prompt: str,
        model: str | None,
        provider: str | None,
        tools: list[dict],
        span_attrs: dict,
        metric_attrs: dict,
        input_epoch: int,
        event_batch: _EventBatch,
        controller_kind: str,
        started_at: float,
        request_settings: LLMRequestSettings,
    ) -> Decision:
        """Await one character decision and finalize it without blocking other characters."""
        cid = str(character_id)

        async def filter_prompt() -> str:
            with (
                telemetry.record_duration(
                    telemetry.record_prompt_filter,
                    {"controller_kind": controller_kind},
                ),
                telemetry.span("agent.prompt.filter", {"character.id": cid}),
            ):
                return await apply_prompt_filters(
                    prompt,
                    runtime=self.prompt_filter_runtime,
                    character=self.actor.world.get_entity(character_id),
                    context=context,
                    epoch=input_epoch,
                    include_automatic=controller_kind == "llm",
                )

        try:
            self.actor.world.get_entity(character_id)
            with (
                telemetry.record_duration(telemetry.record_llm_decision, metric_attrs),
                telemetry.span("agent.decide", span_attrs) as dspan,
            ):
                if controller_kind == "llm":
                    async with asyncio.timeout(self._interactive_decision_timeout_seconds):
                        prompt = await filter_prompt()
                        if telemetry.enabled():
                            dspan.set_attribute("decision.prompt_chars", len(prompt))
                            if telemetry.content_capture_enabled():
                                dspan.set_attribute(
                                    "decision.prompt", telemetry.attr_text(prompt)
                                )
                        async with self._llm_decision_slots:
                            call = await call_with_llm_request_settings(
                                request_settings,
                                agent.decide,
                                prompt,
                                context,
                                character_id=cid,
                                model=model,
                                provider=provider,
                                tools=tools,
                            )
                else:
                    prompt = await filter_prompt()
                    if telemetry.enabled():
                        dspan.set_attribute("decision.prompt_chars", len(prompt))
                        if telemetry.content_capture_enabled():
                            dspan.set_attribute("decision.prompt", telemetry.attr_text(prompt))
                    call = await agent.decide(
                        prompt,
                        context,
                        character_id=cid,
                        model=model,
                        provider=provider,
                        tools=tools,
                    )
                self._annotate_decision_span(dspan, call)
            decision = await self._finalize_decision(
                character_id, controller_id, generation, context, call
            )
        except EntityNotFoundError:
            logger.debug("character %s removed before its decision applied", character_id)
            decision = Decision(cid, None, "skipped: removed before decision applied")
        except TimeoutError:
            logger.warning("character %s decision timed out", character_id)
            self._restore_event_batch(cid, event_batch)
            decision = Decision(
                cid,
                None,
                "error: decision timed out",
                policy_rejections=("decision_timeout",),
                receipt_status="policy_rejected",
                receipt_reason="the provider did not return before the interactive deadline",
            )
        except Exception:  # noqa: BLE001 - a background task must not crash the game loop
            logger.exception("character %s decision task failed", character_id)
            self._restore_event_batch(cid, event_batch)
            decision = Decision(cid, None, "error")
        decision = replace(
            decision,
            input_epoch=input_epoch,
            governing_pressure=_governing_pressure(context),
            memory_ids=_memory_ids(context),
            candidate_actions=tuple(
                sorted(
                    str(tool.get("function", {}).get("name", ""))
                    for tool in tools
                    if tool.get("function", {}).get("name")
                )
            ),
            selected_action=decision.tool,
            receipt_status=decision.receipt_status or ("wait" if decision.tool is None else None),
            prompt_event_ids=tuple(event.event_id for event in event_batch.events),
            omitted_prompt_events=event_batch.omitted,
        )
        telemetry.record_controller_turn(
            time.perf_counter() - started_at,
            {
                "controller_kind": controller_kind,
                "outcome": _decision_outcome(decision),
            },
        )
        self._completed.append(decision)
        return decision

    def _forget(self, cid: str, task: asyncio.Task[Decision]) -> None:
        if self._inflight.get(cid) is task:
            del self._inflight[cid]
            self._inflight_kinds.pop(cid, None)
        state = self._character_states.get(cid)
        if state is not None and state.active_task is task:
            state.active_task = None
            state.active_events = _EventBatch()

    def _restore_event_batch(self, cid: str, event_batch: _EventBatch) -> None:
        if not event_batch.has_content:
            return
        state = self._character_states[cid]
        state.events.restore(event_batch)
        state.active_events = _EventBatch()

    @staticmethod
    def _annotate_decision_span(dspan, call: AgentDecision) -> None:
        if isinstance(call, ToolCall):
            dspan.set_attribute("decision.tool", call.name)
            if telemetry.content_capture_enabled():
                encoded = json.dumps(call.arguments, sort_keys=True, default=str)
                dspan.set_attribute("decision.arguments", telemetry.attr_text(encoded))
        elif isinstance(call, InvalidAgentResponse):
            dspan.set_attribute("decision.tool", "invalid_response")
            dspan.set_attribute("decision.error", call.reason)
        else:
            dspan.set_attribute("decision.tool", "wait")

    async def _finalize_decision(
        self,
        character_id: EntityId,
        controller_id: EntityId,
        generation: int,
        context,
        call: AgentDecision,
    ) -> Decision:
        cid = str(character_id)
        if isinstance(call, InvalidAgentResponse):
            self._feedback[cid] = call.feedback
            logger.info(
                "character %s returned an invalid agent response: %s",
                character_id,
                call.reason,
            )
            return Decision(
                cid,
                None,
                f"invalid response: {call.reason}",
                policy_rejections=("invalid_agent_response",),
                receipt_status="policy_rejected",
                receipt_reason=call.feedback,
            )
        if call is None:
            logger.info("character %s decided to wait", character_id)
            return Decision(cid, None, "wait")
        if call.name == DISCOVER_ACTION_TOOL:
            requested = call.arguments.get("action_name")
            available = {
                definition.name for definition in self.actor.action_definitions()
            }
            if not isinstance(requested, str) or requested not in available:
                self._feedback[cid] = "Choose an action_name from the discovery tool enum."
                return Decision(cid, call.name, "invalid action discovery")
            self._discovered_actions.setdefault(cid, set()).add(requested)
            self._feedback[cid] = (
                f"The {requested} tool is now available. Call it on the next decision if it fits."
            )
            return Decision(cid, call.name, f"discovered {requested}")
        persona_issues = persona_contradictions(context, call)
        if persona_issues:
            logger.info(
                "character %s persona contradiction(s): %s",
                character_id,
                "; ".join(persona_issues),
            )

        # Resolve names exactly as the Discord bot does. If a reference can't be resolved,
        # don't submit a doomed command — feed the agent the same "did you mean..." hint
        # on its next turn (spec 25: tools enforce the same rules for humans and LLMs).
        character = self.actor.world.get_entity(character_id)
        resolved, unresolved = resolve_reference_args(
            self.actor.world,
            character,
            call.arguments,
            keys=reference_arg_keys(self.actor.action_definitions()),
        )
        if unresolved:
            message = did_you_mean(call.arguments, unresolved)
            self._feedback[cid] = message
            logger.info("character %s named something unreachable: %s", character_id, message)
            return Decision(
                cid,
                call.name,
                f"unresolved: {message}",
                persona_issues,
                policy_rejections=("unresolved_reference",),
                receipt_status="policy_rejected",
            )

        try:
            command = command_from_tool_call(
                ToolCall(name=call.name, arguments=resolved),
                character_id=cid,
                controller_id=str(controller_id),
                controller_generation=generation,
                submitted_at_epoch=self.actor.epoch,
                definitions=self.actor.action_definitions(),
            )
        except ValueError as exc:
            message = str(exc)
            self._feedback[cid] = f"{message}. Choose one of the available tools exactly as named."
            logger.info("character %s chose an unavailable tool: %s", character_id, message)
            return Decision(
                cid,
                call.name,
                message,
                persona_issues,
                policy_rejections=("unavailable_action",),
                receipt_status="policy_rejected",
            )
        outcome = await self.actor.submit(command)
        summary = f"{call.name} {resolved}".strip()
        logger.info("character %s chose %s", character_id, summary)
        receipt = outcome.receipt
        return Decision(
            cid,
            call.name,
            summary,
            persona_issues,
            command_id=command.command_id,
            submission_accepted=outcome.accepted,
            submission_reason=outcome.reason,
            receipt_status=receipt.status.value if receipt is not None else None,
            receipt_reason=receipt.reason if receipt is not None else "",
            result_event_ids=receipt.event_ids if receipt is not None else (),
        )


def _memory_ids(context) -> tuple[str, ...]:
    ids = []
    for line in context.recall:
        ids.extend(re.findall(r"\[memory:([^\s\]]+)", line))
    return tuple(dict.fromkeys(ids))


def _governing_pressure(context) -> str:
    if context.warnings:
        return "rejection_feedback"
    if any("goal:" in line.lower() for line in context.persona):
        return "goal"
    if context.conditions:
        return "condition"
    if context.recall:
        return "memory"
    if context.social_cues:
        return "social"
    return "exploration"


__all__ = [
    "ControllerDispatch",
    "Decision",
    "did_you_mean",
    "name_candidates",
    "persona_contradictions",
    "resolve_reference",
    "resolve_reference_args",
    "suggest_names",
]
