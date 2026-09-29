"""Tests for the LLM tool surface, scripted agent, and controller dispatch."""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import replace

import pytest
from conftest import build_scenario
from pydantic import JsonValue

from bunnyland.core import (
    ActionArgument,
    ActionDefinition,
    ActionPattern,
    ActionPointsComponent,
    CharacterComponent,
    CommandCost,
    ContainerComponent,
    ContainmentMode,
    Contains,
    IdentityComponent,
    Lane,
    LLMControllerComponent,
    PortableComponent,
    SayHandler,
    TakeHandler,
    container_of,
    replace_component,
    spawn_entity,
)
from bunnyland.core.events import (
    CommandCancelledEvent,
    CommandExecutedEvent,
    CommandExpiredEvent,
    CommandRejectedEvent,
    DomainEvent,
    EventVisibility,
    SpeechSaidEvent,
    event_base,
)
from bunnyland.foundation.persona.mechanics import GoalComponent
from bunnyland.foundation.social.mechanics import SocialBond
from bunnyland.llm_agents import (
    DISCOVER_ACTION_TOOL,
    AssistantResponseView,
    BehaviorProfileAgent,
    ChatAgentReply,
    ControllerDispatch,
    GoalDirectedAgent,
    InvalidAgentResponse,
    OpenRouterAgent,
    ProviderRouterAgent,
    ResponseFilterContext,
    ResponseFilterRejection,
    ScriptedAgent,
    ToolCall,
    action_discovery_schema,
    contextual_action_definitions,
    did_you_mean,
    name_candidates,
    persona_contradictions,
    register_autonomous_controller,
    resolve_reference,
    suggest_names,
)
from bunnyland.llm_agents import (
    command_from_tool_call as _command_from_tool_call,
)
from bunnyland.llm_agents import (
    parse_natural_command as _parse_natural_command,
)
from bunnyland.llm_agents import (
    resolve_reference_args as _resolve_reference_args,
)
from bunnyland.llm_agents import (
    tool_names as _tool_names,
)
from bunnyland.llm_agents import (
    tool_schemas as _tool_schemas,
)
from bunnyland.llm_agents.agent import (
    CHARACTER_SYSTEM_PROMPT,
    DEFAULT_MODEL,
    DEFAULT_RESPONSE_FILTERS,
    PROSE_REPLY_CORRECTION_PROMPT,
    TEXT_REPLY_CORRECTION_PROMPT,
    TEXT_TOOL_CALL_CORRECTION_PROMPT,
    LLMRequestSettings,
    OllamaAgent,
    _AutonomySignals,
    _call_provider_with_retries,
    _message_to_history,
    _ollama_response_json,
    _openrouter_arguments,
    _openrouter_enriched_usage,
    _openrouter_response_json,
    _openrouter_usage,
    contains_text_tool_call,
    llm_request_settings,
    normalize_model,
)
from bunnyland.llm_agents.dispatch import (
    Decision,
    _EventBatch,
    _governing_pressure,
    _memory_ids,
    _PerceivedEventBuffer,
)
from bunnyland.memory import InMemoryStore, configure_memory_recall, install_memory
from bunnyland.plugins import PluginRegistry, bunnyland_plugins, collect_persona_fragments
from bunnyland.plugins.ids import CORE_VERBS
from bunnyland.prompts import PerceivedPromptEvent
from bunnyland.prompts.builder import PromptBuilder, PromptContext


def test_default_response_filters_cover_invalid_empty_and_token_limited_outputs():
    assert [type(response_filter).__name__ for response_filter in DEFAULT_RESPONSE_FILTERS] == [
        "InvalidMarkupResponseFilter",
        "EmptyResponseFilter",
        "TokenLimitedResponseFilter",
    ]
    assert contains_text_tool_call("<infer>select wait</infer>") is True


class _PromptMessageEvent(DomainEvent):
    message: str


def test_decision_trace_helpers_keep_safe_memory_ids_and_pressure_precedence():
    context = types.SimpleNamespace(
        recall=("[memory:first] remembered", "[memory:first] again [memory:second]"),
        warnings=(),
        persona=(),
        conditions=(),
        social_cues=(),
    )

    assert _memory_ids(context) == ("first", "second")
    assert _governing_pressure(context) == "memory"
    context.recall = ()
    context.social_cues = ("a neighbor is nearby",)
    assert _governing_pressure(context) == "social"
    decision = Decision(character_id="character-1", tool=None, summary="waiting")
    assert decision.with_receipt(None) == decision


def test_prompt_event_summaries_keep_observations_and_action_outcomes_only():
    scenario = build_scenario()
    dispatch = ControllerDispatch(
        scenario.actor,
        PromptBuilder(scenario.actor.world),
        _GatedAsyncAgent(),
    )
    def prompt_event_base(event_id: str) -> dict[str, object]:
        return event_base(
            scenario.actor.epoch,
            event_id=event_id,
            actor_id=str(scenario.character),
            room_id=str(scenario.room_a),
        )

    events = (
        (
            SpeechSaidEvent(**prompt_event_base("speech"), text="Hello."),
            'You said, "Hello."',
        ),
        (
            CommandRejectedEvent(
                **prompt_event_base("rejected"),
                command_id="command-1",
                command_type="move",
                reason="That way is blocked",
            ),
            "Your move action was rejected: That way is blocked.",
        ),
        (
            CommandRejectedEvent(
                **prompt_event_base("internal-rejection"),
                command_id="command-internal",
                command_type="move",
                reason="mutation failed: internal invariant",
                character_actionable=False,
            ),
            None,
        ),
        (
            CommandExecutedEvent(
                **prompt_event_base("executed"),
                command_id="command-2",
                command_type="look",
            ),
            "Your look action completed.",
        ),
        (
            CommandCancelledEvent(
                **prompt_event_base("cancelled"),
                command_id="command-3",
                command_type="wait",
            ),
            "Your wait action was cancelled.",
        ),
        (
            CommandExpiredEvent(
                **prompt_event_base("expired"),
                command_id="command-4",
                command_type="say",
            ),
            "Your say action expired.",
        ),
        (
            _PromptMessageEvent(
                **prompt_event_base("message"),
                message="  A bell rings nearby.  ",
            ),
            "A bell rings nearby.",
        ),
        (
            DomainEvent(**prompt_event_base("internal")),
            None,
        ),
    )

    character = scenario.actor.world.get_entity(scenario.character)
    for event, expected in events:
        assert dispatch._prompt_event_summary(character, event) == expected

    dispatch.close()


def test_ollama_response_without_message_object_still_redacts_top_level_thinking():
    response = _ollama_response_json(
        {"message": "finished", "thinking": "private"}, include_thinking=False
    )

    assert response == {"message": "finished"}


def test_perceived_event_buffer_bounds_drains_and_restores_batches():
    def prompt_event(event_id: str, epoch: int, salience: int = 0) -> PerceivedPromptEvent:
        return PerceivedPromptEvent(event_id, "TestEvent", epoch, event_id, salience)

    buffer = _PerceivedEventBuffer(max_events=2, max_chars=1_000)
    assert buffer.has_content is False
    buffer.append(prompt_event("old-low", 1))
    buffer.append(prompt_event("important", 2, salience=100))
    buffer.append(prompt_event("new-low", 3))
    assert buffer.has_content is True

    batch = buffer.drain()
    assert [event.event_id for event in batch.events] == ["important", "new-low"]
    assert batch.omitted == 1
    assert batch.omitted_epoch_range == (1, 1)
    assert buffer.has_content is False

    buffer.append(prompt_event("later", 4))
    buffer.restore(batch)
    restored = buffer.drain()
    assert [event.event_id for event in restored.events] == ["important", "later"]
    assert restored.omitted == 2
    assert restored.omitted_epoch_range == (1, 3)

    char_limited = _PerceivedEventBuffer(max_events=10, max_chars=1)
    char_limited.append(prompt_event("too-large", 5))
    assert char_limited.drain() == _EventBatch(omitted=1, omitted_epoch_range=(5, 5))

ALL_ACTION_DEFINITIONS = tuple(
    definition for _owner, definition in PluginRegistry(bunnyland_plugins()).actions.values()
)
CORE_ACTION_DEFINITIONS = next(
    plugin.commands.action_definitions
    for plugin in bunnyland_plugins()
    if plugin.id == CORE_VERBS
)


def tool_names(definitions=ALL_ACTION_DEFINITIONS):
    return _tool_names(definitions)


def tool_schemas(definitions=ALL_ACTION_DEFINITIONS):
    return _tool_schemas(definitions)


def command_from_tool_call(call, *, definitions=ALL_ACTION_DEFINITIONS, **kwargs):
    return _command_from_tool_call(call, definitions=definitions, **kwargs)


def parse_natural_command(text, definitions=ALL_ACTION_DEFINITIONS):
    return _parse_natural_command(text, definitions)


def resolve_reference_args(world, character, arguments, *, keys=None, suggestions=3):
    if keys is None:
        keys = frozenset(
            key for definition in ALL_ACTION_DEFINITIONS for key in definition.reference_arg_keys
        )
    return _resolve_reference_args(world, character, arguments, keys=keys, suggestions=suggestions)


def _add_item(scenario, name, *, container=False):
    world = scenario.actor.world
    components = [IdentityComponent(name=name, kind="item"), PortableComponent(can_pick_up=True)]
    if container:
        components.append(ContainerComponent(open=True))
    entity = spawn_entity(world, components)
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), entity.id
    )
    return entity.id


def test_tool_schemas_cover_every_verb():
    names = {s["function"]["name"] for s in tool_schemas()}
    assert names == set(tool_names())
    assert {
        "move",
        "say",
        "take",
        "adopt_child",
        "accept_quest",
        "buy_item",
        "charge_rent",
        "claim_home",
        "claim_ownership",
        "claim_room",
        "complete_objective",
        "create_spell",
        "cast_spell",
        "discover_location",
        "drop",
        "enchant_item",
        "fertilize",
        "harvest",
        "join_household",
        "plant",
        "pickpocket",
        "join_faction",
        "leave_faction",
        "release_ownership",
        "open_business",
        "pay_bill",
        "sell_item",
        "take_note",
        "till",
        "water_crop",
        "remember",
        "forget",
        "reflect",
        "wait",
    } <= names


def test_command_from_tool_call_keeps_drop_as_public_command():
    call = ToolCall(name="drop", arguments={"item_id": "item_1"})
    command = command_from_tool_call(
        call, character_id="char_1", controller_id="ctrl_1", controller_generation=0
    )
    assert command.command_type == "drop"
    assert command.lane is Lane.WORLD
    assert command.cost == CommandCost(action=1)
    assert command.payload == {"item_id": "item_1"}


def test_command_from_tool_call_drops_unknown_arguments():
    call = ToolCall(name="move", arguments={"direction": "north", "bogus": "x"})
    command = command_from_tool_call(
        call, character_id="char_1", controller_id="ctrl_1", controller_generation=0
    )
    assert command.payload == {"direction": "north"}


def test_action_argument_json_schema_includes_only_declared_metadata():
    assert ActionArgument().json_schema() == {"type": "string"}
    assert ActionArgument(description="How many.").json_schema() == {
        "type": "string",
        "description": "How many.",
    }
    assert ActionArgument(title="Count", kind="number").json_schema() == {
        "type": "number",
        "title": "Count",
    }
    assert ActionArgument(title="Enabled", kind="boolean").json_schema() == {
        "type": "boolean",
        "title": "Enabled",
    }


def test_custom_action_definition_drives_tool_schema_and_command_mapping():
    definition = ActionDefinition(
        command_type="wave",
        tool_name="wave",
        description="Wave to a reachable character.",
        arguments={
            "target_id": ActionArgument(
                title="Target",
                description="The character to wave at.",
                kind="entity",
                required=True,
            )
        },
    )

    schema = next(
        item["function"]
        for item in tool_schemas((definition,))
        if item["function"]["name"] == "wave"
    )
    command = command_from_tool_call(
        ToolCall("wave", {"target_id": "char_1", "bogus": "ignored"}),
        character_id="char_2",
        controller_id="ctrl_1",
        controller_generation=0,
        definitions=(definition,),
    )

    assert schema["description"] == "Wave to a reachable character."
    assert schema["parameters"]["properties"]["target_id"] == {
        "type": "string",
        "title": "Target",
        "description": "The character to wave at.",
    }
    assert schema["parameters"]["required"] == ["target_id"]
    assert command.command_type == "wave"
    assert command.payload == {"target_id": "char_1"}


def test_note_tools_accept_shared_collection_arguments():
    call = ToolCall(
        name="take_note",
        arguments={"text": "shared", "scope": "shared", "collection": "burrow-board"},
    )
    command = command_from_tool_call(
        call, character_id="char_1", controller_id="ctrl_1", controller_generation=0
    )
    assert command.payload == {
        "text": "shared",
        "scope": "shared",
        "collection": "burrow-board",
    }


def test_parse_natural_command_maps_common_phrases_to_tool_calls():
    assert parse_natural_command("go north") == ToolCall("move", {"direction": "north"})
    assert parse_natural_command("take the brass key") == ToolCall(
        "take", {"item_id": "the brass key"}
    )
    assert parse_natural_command('say "hello there"') == ToolCall("say", {"text": "hello there"})
    assert parse_natural_command("tell Hazel meet me outside") == ToolCall(
        "tell", {"target_id": "Hazel", "text": "meet me outside"}
    )
    assert parse_natural_command("pickpocket Hazel brass key") == ToolCall(
        "pickpocket", {"target_id": "Hazel", "item_id": "brass key"}
    )
    assert parse_natural_command("buy radish seeds from Marigold") == ToolCall(
        "buy_item", {"item_id": "radish seeds", "seller_id": "Marigold"}
    )
    assert parse_natural_command("sell radish x2 to Marigold") == ToolCall(
        "sell_item", {"item_id": "radish x2", "customer_id": "Marigold"}
    )
    assert parse_natural_command("open business Hazel's Farm Stand") == ToolCall(
        "open_business", {"name": "Hazel's Farm Stand"}
    )
    assert parse_natural_command("adopt Clover") == ToolCall("adopt_child", {"child_id": "Clover"})
    assert parse_natural_command("claim oak chest") == ToolCall(
        "claim_ownership", {"target_id": "oak chest"}
    )
    assert parse_natural_command("till garden bed") == ToolCall("till", {"soil_id": "garden bed"})
    assert parse_natural_command("plant turnip seeds in garden bed") == ToolCall(
        "plant", {"seed_id": "turnip seeds", "soil_id": "garden bed"}
    )
    assert parse_natural_command("water garden bed") == ToolCall(
        "water_crop", {"soil_id": "garden bed"}
    )
    assert parse_natural_command("harvest garden bed") == ToolCall(
        "harvest", {"target_id": "garden bed"}
    )
    assert parse_natural_command("discover old watchtower") == ToolCall(
        "discover_location", {"location_id": "old watchtower"}
    )
    assert parse_natural_command("accept quest lost ring") == ToolCall(
        "accept_quest", {"quest_id": "lost ring"}
    )
    assert parse_natural_command("complete objective find the ring") == ToolCall(
        "complete_objective", {"objective_id": "find the ring"}
    )
    assert parse_natural_command("join faction Moss Wardens") == ToolCall(
        "join_faction", {"faction_id": "Moss Wardens"}
    )
    assert parse_natural_command("join household moss-burrow") == ToolCall(
        "join_household", {"household_id": "moss-burrow", "name": "moss-burrow"}
    )
    assert parse_natural_command("leave faction Moss Wardens") == ToolCall(
        "leave_faction", {"faction_id": "Moss Wardens"}
    )
    assert parse_natural_command("claim home North Tunnel") == ToolCall(
        "claim_home", {"room_id": "North Tunnel"}
    )
    assert parse_natural_command("claim room North Tunnel") == ToolCall(
        "claim_room", {"room_id": "North Tunnel"}
    )
    assert parse_natural_command("release ownership oak chest") == ToolCall(
        "release_ownership", {"target_id": "oak chest"}
    )
    assert parse_natural_command("charge rent Hazel 12") == ToolCall(
        "charge_rent", {"tenant_id": "Hazel", "amount": "12"}
    )
    assert parse_natural_command("pay bill") == ToolCall("pay_bill", {})
    assert parse_natural_command("pay bill bill-123") == ToolCall(
        "pay_bill", {"bill_id": "bill-123"}
    )
    assert parse_natural_command("enchant moss charm with Mend Moss") == ToolCall(
        "enchant_item", {"item_id": "moss charm", "spell_id": "Mend Moss"}
    )
    assert parse_natural_command("cast moss charm on Juniper") == ToolCall(
        "cast_spell", {"spell_id": "moss charm", "target_id": "Juniper"}
    )
    assert parse_natural_command("take note the basin is cold") == ToolCall(
        "take_note", {"text": "the basin is cold"}
    )
    assert parse_natural_command("reflect on the basin") == ToolCall(
        "reflect", {"text": "on the basin"}
    )
    assert parse_natural_command("forget note-123") == ToolCall("forget", {"note_id": "note-123"})
    assert parse_natural_command("wait") == ToolCall("wait", {})


def test_parse_natural_command_returns_none_for_ambiguous_text():
    assert parse_natural_command("") is None
    assert parse_natural_command("maybe Hazel knows") is None


def test_parse_natural_command_covers_alternate_command_shapes():
    assert parse_natural_command("go old gate") == ToolCall("move", {"exit_id": "old gate"})
    assert parse_natural_command("north") == ToolCall("move", {"direction": "north"})
    assert parse_natural_command("drop brass key") == ToolCall("drop", {"item_id": "brass key"})
    assert parse_natural_command("put brass key into oak chest") == ToolCall(
        "put", {"item_id": "brass key", "target_container_id": "oak chest"}
    )
    assert parse_natural_command("put brass key onto oak shelf") == ToolCall(
        "put", {"item_id": "brass key", "target_container_id": "oak shelf"}
    )
    assert parse_natural_command("put brass key away") == ToolCall(
        "drop", {"item_id": "brass key away"}
    )
    assert parse_natural_command("put brass key in") == ToolCall(
        "drop", {"item_id": "brass key in"}
    )
    assert parse_natural_command("use brass key with old lock") == ToolCall(
        "use", {"item_id": "brass key", "target_id": "old lock"}
    )
    assert parse_natural_command("use brass key") == ToolCall("use", {"item_id": "brass key"})
    assert parse_natural_command("enchant moss charm with Mend Moss") == ToolCall(
        "enchant_item", {"item_id": "moss charm", "spell_id": "Mend Moss"}
    )
    assert parse_natural_command("cast ember charm at Hazel") == ToolCall(
        "cast_spell", {"spell_id": "ember charm", "target_id": "Hazel"}
    )
    assert parse_natural_command("cast ember charm on") == ToolCall(
        "cast_spell", {"spell_id": "ember charm on"}
    )
    assert parse_natural_command("cast ember charm") == ToolCall(
        "cast_spell", {"spell_id": "ember charm"}
    )
    assert parse_natural_command("plant carrot seed into garden bed") == ToolCall(
        "plant", {"seed_id": "carrot seed", "soil_id": "garden bed"}
    )
    assert parse_natural_command("fertilize garden bed with compost") == ToolCall(
        "fertilize", {"soil_id": "garden bed", "fertilizer_id": "compost"}
    )
    assert parse_natural_command("eat stew") == ToolCall("eat", {"item_id": "stew"})
    assert parse_natural_command("drink spring") == ToolCall("drink", {"source_id": "spring"})
    assert parse_natural_command("remember basin") == ToolCall("remember", {"query": "basin"})
    assert parse_natural_command("write hello on slate") == ToolCall(
        "write", {"target_id": "slate", "text": "hello"}
    )
    assert parse_natural_command('say "unterminated') == ToolCall("say", {"text": '"unterminated'})


def test_parse_natural_command_rejects_incomplete_command_shapes():
    assert parse_natural_command("go") is None
    assert parse_natural_command("drop") is None
    assert parse_natural_command("use brass key with") == ToolCall(
        "use", {"item_id": "brass key with"}
    )
    assert parse_natural_command("enchant moss charm with Mend Moss") == ToolCall(
        "enchant_item", {"item_id": "moss charm", "spell_id": "Mend Moss"}
    )
    assert parse_natural_command("enchant moss charm") is None
    assert parse_natural_command("enchant moss charm with") is None
    assert parse_natural_command("plant carrot seed") is None
    assert parse_natural_command("plant carrot seed in") is None
    assert parse_natural_command("fertilize garden bed") is None
    assert parse_natural_command("fertilize garden bed with") is None
    assert parse_natural_command("buy radish seeds") is None
    assert parse_natural_command("buy radish seeds near Marigold") is None
    assert parse_natural_command("buy radish seeds from") is None
    assert parse_natural_command("sell radish seeds") is None
    assert parse_natural_command("sell radish seeds near Marigold") is None
    assert parse_natural_command("sell radish seeds to") is None
    assert parse_natural_command("charge rent Hazel twelve") is None
    assert parse_natural_command("eat") is None
    assert parse_natural_command("drink") is None
    assert parse_natural_command("remember") is None
    assert parse_natural_command("write hello") is None
    assert parse_natural_command("write hello on") is None


def test_message_to_history_uses_model_dump_or_message_attributes():
    class DumpableMessage:
        def model_dump(self, **kwargs):
            assert kwargs == {"mode": "json", "exclude_none": True}
            return {"role": "assistant", "content": "dumped"}

    class AttributeMessage:
        role = "assistant"
        content = "plain"
        tool_calls = [{"function": {"name": "wait", "arguments": {}}}]

    class EmptyMessage:
        pass

    assert _message_to_history(DumpableMessage()) == {
        "role": "assistant",
        "content": "dumped",
    }
    assert _message_to_history(AttributeMessage()) == {
        "role": "assistant",
        "content": "plain",
        "tool_calls": [{"function": {"name": "wait", "arguments": {}}}],
    }
    assert _message_to_history(EmptyMessage()) == {"role": "assistant"}


def test_openrouter_arguments_accept_json_mapping_and_other_values():
    assert _openrouter_arguments('{"item_id": "basket"}') == {"item_id": "basket"}
    assert _openrouter_arguments("") == {}
    assert _openrouter_arguments({"direction": "north"}) == {"direction": "north"}
    assert _openrouter_arguments(("direction", "north")) == {}


def test_parse_natural_command_uses_action_definition_patterns():
    definition = ActionDefinition(
        command_type="wave",
        tool_name="wave",
        arguments={"target_id": ActionArgument(kind="entity")},
        natural_patterns=(ActionPattern("wave to {target_id}"),),
    )

    assert parse_natural_command("wave to Hazel", (definition,)) == ToolCall(
        "wave", {"target_id": "Hazel"}
    )


def test_parse_natural_command_rejects_adjacent_pattern_slots():
    definition = ActionDefinition(
        command_type="give",
        tool_name="give",
        arguments={
            "item_id": ActionArgument(kind="entity"),
            "target_id": ActionArgument(kind="entity"),
        },
        natural_patterns=(ActionPattern("give {item_id} {target_id}"),),
    )

    assert parse_natural_command("give carrot Hazel", (definition,)) is None


def test_parse_natural_command_rejects_trailing_separator_patterns():
    definition = ActionDefinition(
        command_type="use",
        tool_name="use",
        arguments={"item_id": ActionArgument(kind="entity")},
        natural_patterns=(ActionPattern("use {item_id} "),),
    )

    assert parse_natural_command("use brass key", (definition,)) is None


def test_parse_natural_command_ignores_patterns_without_slots():
    definition = ActionDefinition(
        command_type="wave",
        tool_name="wave",
        arguments={},
        natural_patterns=(ActionPattern("wave"),),
    )

    assert parse_natural_command("wave", (definition,)) is None


def test_parse_natural_command_supports_leading_slot_patterns():
    # A pattern that begins with a slot (no leading literal) compiles and matches; this
    # exercises the split path where the first slot starts at position 0.
    definition = ActionDefinition(
        command_type="move",
        tool_name="move",
        arguments={"direction": ActionArgument(kind="direction")},
        natural_patterns=(ActionPattern("{direction}"),),
    )

    assert parse_natural_command("north", (definition,)) == ToolCall("move", {"direction": "north"})


def test_parse_natural_command_drops_whitespace_only_slot_captures():
    # A middle text slot wrapped in literals becomes optional; matching with only
    # whitespace in that slot leaves it out of the arguments (only fixed args remain).
    definition = ActionDefinition(
        command_type="say",
        tool_name="say",
        arguments={"text": ActionArgument(kind="text")},
        natural_patterns=(ActionPattern("say{text}please", fixed_arguments={"mode": "polite"}),),
    )

    assert parse_natural_command("say  please", (definition,)) == ToolCall(
        "say", {"mode": "polite"}
    )


def test_parse_natural_command_ignores_unsatisfiable_argument_aliases():
    # An alias whose source argument is never captured leaves the target absent rather
    # than raising; the alias loop simply skips it.
    definition = ActionDefinition(
        command_type="greet",
        tool_name="greet",
        arguments={"name": ActionArgument(kind="entity")},
        natural_patterns=(
            ActionPattern("greet {name}", argument_aliases={"target_id": "missing"}),
        ),
    )

    assert parse_natural_command("greet Hazel", (definition,)) == ToolCall(
        "greet", {"name": "Hazel"}
    )


async def test_scripted_agent_replays_then_waits():
    agent = ScriptedAgent([ToolCall("wait", {})])
    first = await agent.decide("prompt", None, character_id="char_1")
    assert first is not None and first.name == "wait"
    assert await agent.decide("prompt", None, character_id="char_1") is None


async def test_goal_directed_agent_takes_goal_relevant_visible_object():
    scenario = build_scenario()
    world = scenario.actor.world
    character = world.get_entity(scenario.character)
    character.add_component(GoalComponent(active_goals=("find the silver key",)))
    _add_item(scenario, "silver key")
    builder = PromptBuilder(
        world,
        persona_providers=collect_persona_fragments(bunnyland_plugins()),
    )
    context = builder.build(scenario.character)

    call = await GoalDirectedAgent().decide("", context, character_id=str(scenario.character))

    assert call == ToolCall("take", {"item_id": "silver key"})


async def test_goal_directed_agent_uses_recall_to_address_visible_character():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    context = PromptBuilder(world).build(scenario.character)
    context = replace(
        context,
        recall=("Hazel hid the basin key under the woven basket. [memory:m1 source:note]",),
    )

    call = await GoalDirectedAgent().decide("", context, character_id=str(scenario.character))

    assert call is not None
    assert call.name == "say"
    assert call.arguments["text"].startswith("Hazel, I remember Hazel hid the basin key")


async def test_goal_directed_agent_uses_goal_to_address_visible_character():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    character = world.get_entity(scenario.character)
    character.add_component(GoalComponent(active_goals=("ask Hazel about the bridge",)))
    context = PromptBuilder(
        world,
        persona_providers=collect_persona_fragments(bunnyland_plugins()),
    ).build(scenario.character)

    call = await GoalDirectedAgent().decide("", context, character_id=str(scenario.character))

    assert call == ToolCall("say", {"text": "Hazel, I am working on ask Hazel about the bridge"})


async def test_goal_directed_agent_speaks_from_condition_signal_without_private_goal():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    context = replace(
        PromptBuilder(world).build(scenario.character),
        conditions=("Hazel looks distressed.",),
    )

    call = await GoalDirectedAgent().decide("", context, character_id=str(scenario.character))

    assert call == ToolCall("say", {"text": "Hazel, I need to talk with you."})


async def test_goal_directed_agent_moves_only_when_goal_points_to_exploration():
    scenario = build_scenario()
    world = scenario.actor.world
    character = world.get_entity(scenario.character)
    character.add_component(GoalComponent(active_goals=("search north for shelter",)))
    builder = PromptBuilder(
        world,
        persona_providers=collect_persona_fragments(bunnyland_plugins()),
    )
    context = builder.build(scenario.character)

    assert await GoalDirectedAgent().decide("", context, character_id=str(scenario.character)) == (
        ToolCall("move", {"direction": "north"})
    )


async def test_goal_directed_agent_records_memory_goal_when_no_target_matches():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        persona=("Your goal: remember the cellar glyph.",),
        visible_objects=(),
        visible_characters=(),
        exits=(),
        commands=("take note",),
    )

    assert await GoalDirectedAgent().decide("", context, character_id=str(scenario.character)) == (
        ToolCall("take_note", {"text": "Goal matters: remember the cellar glyph"})
    )


async def test_goal_directed_agent_records_recall_when_no_target_matches():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_objects=(),
        visible_characters=(),
        exits=(),
        recall=("The cellar glyph opened the bridge. [memory:m1 source:note]",),
        commands=("take note",),
    )

    assert await GoalDirectedAgent().decide("", context, character_id=str(scenario.character)) == (
        ToolCall("take_note", {"text": "Recall matters: The cellar glyph opened the bridge"})
    )


async def test_goal_directed_agent_does_not_move_through_locked_exploration_exit():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        persona=("Your goal: explore the burrow.",),
        visible_objects=("plain stone",),
        exits=("north (locked)",),
        commands=("take plain stone", "take note"),
    )

    assert (
        await GoalDirectedAgent().decide("", context, character_id=str(scenario.character))
        is None
    )


async def test_goal_directed_agent_waits_without_goal_or_recall_signal():
    scenario = build_scenario()
    context = PromptBuilder(scenario.actor.world).build(scenario.character)

    assert (
        await GoalDirectedAgent().decide("", context, character_id=str(scenario.character))
        is None
    )


def test_behavior_profile_agent_rejects_unknown_profile():
    with pytest.raises(ValueError, match="unknown background profile"):
        BehaviorProfileAgent("wanderer")


async def test_behavior_profile_agent_idle_waits_without_goal_signal():
    scenario = build_scenario()
    context = PromptBuilder(scenario.actor.world).build(scenario.character)

    assert (
        await BehaviorProfileAgent("idle").decide("", context, character_id=str(scenario.character))
        is None
    )


async def test_behavior_profile_agent_social_speaks_to_visible_character():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    context = PromptBuilder(world).build(scenario.character)

    assert await BehaviorProfileAgent("social").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall(
        "say",
        {"text": "Hazel, good to see you.", "intent": "praise", "approach": "friendly"},
    )


async def test_behavior_profile_agent_social_waits_without_visible_character():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_characters=(),
    )

    assert (
        await BehaviorProfileAgent("social").decide(
            "", context, character_id=str(scenario.character)
        )
        is None
    )


async def test_behavior_profile_agent_relationship_fear_prefers_avoidance():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_characters=("Hazel",),
        persona=("You fear Hazel.",),
        exits=("north",),
        commands=("move north", "say something to the room"),
    )

    assert await BehaviorProfileAgent("social").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall("move", {"direction": "north"})


async def test_behavior_profile_agent_relationship_fondness_prefers_warm_speech():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_characters=("Hazel",),
        persona=("You are fond of Hazel.",),
        commands=("say something to the room",),
    )

    assert await BehaviorProfileAgent("social").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall(
        "say",
        {
            "text": "Hazel, I am glad you are here.",
            "intent": "praise",
            "approach": "warm",
        },
    )


async def test_behavior_profile_agent_relationship_resentment_prefers_cold_warning():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_characters=("Hazel",),
        persona=("You resent Hazel.",),
        commands=("say something to the room",),
    )

    assert await BehaviorProfileAgent("social").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall(
        "say",
        {"text": "Hazel, keep your distance.", "intent": "threat", "approach": "cold"},
    )


async def test_behavior_profile_agent_relationship_fear_falls_back_to_request_speech():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_characters=("Hazel",),
        persona=("You fear Hazel.",),
        exits=("north (locked)",),  # no unlocked exit -> cannot flee
        commands=("say something to the room",),
    )

    assert await BehaviorProfileAgent("social").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall(
        "say",
        {
            "text": "Hazel, I need space.",
            "intent": "request",
            "approach": "cautious",
        },
    )


async def test_behavior_profile_agent_fear_without_exit_or_speech_falls_through():
    scenario = build_scenario()
    # Fear line matches, but there is no unlocked exit and no speech command, so the fear
    # branch returns nothing (298->307) and the profile fallback runs.
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_characters=("Hazel",),
        persona=("You fear Hazel.",),
        visible_objects=("work crate",),
        exits=("north (locked)",),
        commands=("take work crate",),
    )

    assert await BehaviorProfileAgent("worker").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall("take", {"item_id": "work crate"})


async def test_behavior_profile_agent_relationship_lines_without_speech_command_fall_through():
    scenario = build_scenario()
    # Fond and resentment lines both match the visible character, but no speech command is
    # available, so neither relationship branch returns; the profile fallback runs instead.
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_characters=("Hazel",),
        persona=("You are fond of Hazel.", "You resent Hazel."),
        visible_objects=("work crate",),
        commands=("take work crate",),
    )

    assert await BehaviorProfileAgent("worker").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall("take", {"item_id": "work crate"})


def test_autonomy_signals_note_text_defaults_without_recall_or_goal():
    signals = _AutonomySignals(
        goals=(),
        recall=(),
        conditions=("You are tired.",),
        recent=(),
        notes=(),
    )

    assert signals.note_text() == "Something nearby may matter."


def test_autonomy_signals_speech_skips_lines_not_mentioning_name():
    signals = _AutonomySignals(
        goals=(),
        recall=("Hazel guards the gate.", "Briar tends the garden."),
        conditions=(),
        recent=(),
        notes=(),
    )

    # First recall line does not mention Briar (476->475 continue), second does.
    assert signals.speech_for("Briar") == "Briar, I remember Briar tends the garden"


async def test_behavior_profile_agent_timid_leaves_when_someone_is_nearby():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    context = PromptBuilder(world).build(scenario.character)

    assert await BehaviorProfileAgent("timid").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall("move", {"direction": "north"})


async def test_behavior_profile_agent_timid_waits_without_visible_character():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_characters=(),
    )

    assert (
        await BehaviorProfileAgent("timid").decide(
            "", context, character_id=str(scenario.character)
        )
        is None
    )


async def test_behavior_profile_agent_timid_waits_without_unlocked_exit():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_characters=("Hazel",),
        exits=("north (locked)",),
        commands=("say something to the room",),
    )

    assert (
        await BehaviorProfileAgent("timid").decide(
            "", context, character_id=str(scenario.character)
        )
        is None
    )


async def test_behavior_profile_agent_aggressive_warns_visible_character():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    context = PromptBuilder(world).build(scenario.character)

    assert await BehaviorProfileAgent("aggressive").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall(
        "say",
        {"text": "Hazel, back away.", "intent": "threat", "approach": "confrontational"},
    )


async def test_behavior_profile_agent_aggressive_waits_without_speech_command():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_characters=("Hazel",),
        commands=(),
    )

    assert (
        await BehaviorProfileAgent("aggressive").decide(
            "", context, character_id=str(scenario.character)
        )
        is None
    )


async def test_behavior_profile_agent_worker_takes_available_object():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_objects=("work crate",),
        commands=("take work crate",),
    )

    assert await BehaviorProfileAgent("worker").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall("take", {"item_id": "work crate"})


async def test_behavior_profile_agent_worker_moves_when_no_object_is_available():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_objects=("fixed anvil",),
        exits=("north",),
        commands=("move north",),
    )

    assert await BehaviorProfileAgent("worker").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall("move", {"direction": "north"})


async def test_behavior_profile_agent_worker_waits_without_work_or_exit():
    scenario = build_scenario()
    context = replace(
        PromptBuilder(scenario.actor.world).build(scenario.character),
        visible_objects=("fixed anvil",),
        exits=("north (locked)",),
        commands=(),
    )

    assert (
        await BehaviorProfileAgent("worker").decide(
            "", context, character_id=str(scenario.character)
        )
        is None
    )


async def test_behavior_profile_agent_prefers_goal_over_profile_fallback():
    scenario = build_scenario()
    world = scenario.actor.world
    character = world.get_entity(scenario.character)
    character.add_component(GoalComponent(active_goals=("find the silver key",)))
    _add_item(scenario, "work crate")
    _add_item(scenario, "silver key")
    context = PromptBuilder(
        world,
        persona_providers=collect_persona_fragments(bunnyland_plugins()),
    ).build(scenario.character)

    assert await BehaviorProfileAgent("worker").decide(
        "", context, character_id=str(scenario.character)
    ) == ToolCall("take", {"item_id": "silver key"})


async def test_dispatch_submits_behavior_profile_agent_command():
    scenario = build_scenario()
    scenario.actor.register_handler(TakeHandler())
    _add_item(scenario, "work crate")
    builder = PromptBuilder(scenario.actor.world)
    dispatch = ControllerDispatch(scenario.actor, builder, BehaviorProfileAgent("worker"))

    assert await dispatch.run_once() == []
    decisions = await dispatch.await_pending()

    assert len(decisions) == 1
    assert decisions[0].tool == "take"
    assert not scenario.actor._inbox.empty()


async def test_dispatch_submits_goal_directed_agent_command():
    scenario = build_scenario()
    scenario.actor.register_handler(TakeHandler())
    world = scenario.actor.world
    world.get_entity(scenario.character).add_component(
        GoalComponent(active_goals=("find the silver key",))
    )
    key_id = _add_item(scenario, "silver key")
    builder = PromptBuilder(
        world,
        persona_providers=collect_persona_fragments(bunnyland_plugins()),
    )
    dispatch = ControllerDispatch(scenario.actor, builder, GoalDirectedAgent())

    assert await dispatch.run_once() == []
    decisions = await dispatch.await_pending()

    assert len(decisions) == 1
    assert decisions[0].tool == "take"
    assert str(key_id) in decisions[0].summary
    assert not scenario.actor._inbox.empty()


async def test_dispatch_submits_a_command_for_an_llm_character():
    scenario = build_scenario()
    builder = PromptBuilder(scenario.actor.world)
    agent = ScriptedAgent([ToolCall("move", {"direction": "north"})])
    dispatch = ControllerDispatch(scenario.actor, builder, agent)

    assert await dispatch.run_once() == []
    decisions = await dispatch.await_pending()

    assert len(decisions) == 1
    assert decisions[0].tool == "move"
    assert decisions[0].selected_action == "move"
    assert decisions[0].command_id is not None
    assert decisions[0].submission_accepted is True
    assert "move" in decisions[0].candidate_actions
    assert decisions[0].receipt_status is None
    # The command is submitted (inbox), not yet executed.
    assert not scenario.actor._inbox.empty()


async def test_dispatch_throttles_controller_by_act_every_ticks():
    from dataclasses import replace

    from bunnyland.core import replace_component
    from bunnyland.core.controllers import LLMControllerComponent

    scenario = build_scenario()
    builder = PromptBuilder(scenario.actor.world)
    controller = scenario.actor.world.get_entity(scenario.controller)
    replace_component(
        controller,
        replace(controller.get_component(LLMControllerComponent), act_every_ticks=2),
    )
    agent = ScriptedAgent([ToolCall("move", {"direction": "north"})])
    dispatch = ControllerDispatch(scenario.actor, builder, agent)

    # Tick 1 is skipped (1 % 2 != 0); tick 2 is the controller's turn.
    assert await dispatch.run_once() == []
    assert await dispatch.run_once() == []
    second = await dispatch.await_pending()
    assert [decision.tool for decision in second] == ["move"]


async def test_dispatch_uses_registered_autonomous_controller_factory():
    from pydantic.dataclasses import dataclass
    from relics import Component

    @dataclass(frozen=True)
    class TestControllerComponent(Component):
        act_every_ticks: int = 1

    gated = _GatedAsyncAgent()

    def test_agent_factory(dispatch, character_id: str, component: object):
        assert isinstance(dispatch, ControllerDispatch)
        assert character_id
        assert isinstance(component, TestControllerComponent)
        return gated, "test-model", "test"

    scenario = build_scenario()
    controller = spawn_entity(scenario.actor.world, [TestControllerComponent()])
    scenario.actor.assign_controller(scenario.character, controller.id)
    register_autonomous_controller(TestControllerComponent, test_agent_factory)
    dispatch = ControllerDispatch(
        scenario.actor,
        PromptBuilder(scenario.actor.world),
        ScriptedAgent([]),
    )

    assert await dispatch.run_once() == []
    assert await dispatch.run_once() == []
    assert gated.prompts == [str(scenario.character)]
    gated.gate.set()
    decisions = await dispatch.await_pending()

    assert [decision.tool for decision in decisions] == ["move"]


async def test_dispatch_rejects_unregistered_autonomous_controller():
    from pydantic.dataclasses import dataclass
    from relics import Component

    @dataclass(frozen=True)
    class UnregisteredControllerComponent(Component):
        act_every_ticks: int = 1

    scenario = build_scenario()
    dispatch = ControllerDispatch(
        scenario.actor,
        PromptBuilder(scenario.actor.world),
        ScriptedAgent([]),
    )

    with pytest.raises(ValueError, match="unregistered autonomous controller"):
        dispatch._agent_for(str(scenario.character), UnregisteredControllerComponent())


class _FakeOllamaClient:
    """Records the messages sent on each chat call and replies with a fixed tool call."""

    def __init__(self, *args, **kwargs):
        self.calls: list[list[dict]] = []
        self.models: list[str] = []
        self.tools: list[list[dict] | None] = []

    async def chat(self, *, model, messages, tools):
        self.models.append(model)
        self.tools.append(tools)
        self.calls.append([dict(m) for m in messages])  # snapshot
        return {
            "message": {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [{"function": {"name": "wait", "arguments": {}}}],
            }
        }


class _FakeProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"provider failed with status code: {status_code}")
        self.status_code = status_code


class _FakeResponseProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"provider response failed with status code: {status_code}")
        self.response = types.SimpleNamespace(status_code=status_code)


@pytest.mark.parametrize(
    "exc",
    [
        _FakeProviderError(429),
        _FakeProviderError(502),
        _FakeResponseProviderError(503),
        _FakeResponseProviderError(504),
        TimeoutError("provider timed out"),
        ConnectionError("provider connection reset"),
        OSError("provider network unreachable"),
    ],
)
async def test_provider_retry_helper_retries_intermittent_network_errors(exc):
    attempts = 0

    async def request():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise exc
        return "ok"

    result = await _call_provider_with_retries(
        "test-provider", request, max_retries=2, retry_delay_seconds=0
    )

    assert result == "ok"
    assert attempts == 2


@pytest.mark.parametrize(
    "exc",
    [
        _FakeProviderError(400),
        _FakeResponseProviderError(403),
        ValueError("malformed provider response"),
    ],
)
async def test_provider_retry_helper_does_not_retry_non_transient_errors(exc):
    attempts = 0

    async def request():
        nonlocal attempts
        attempts += 1
        raise exc

    with pytest.raises(type(exc)):
        await _call_provider_with_retries(
            "test-provider", request, max_retries=2, retry_delay_seconds=0
        )

    assert attempts == 1


def _fake_ollama_response():
    return {
        "message": {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [{"function": {"name": "wait", "arguments": {}}}],
        }
    }


class _FlakyOllamaClient(_FakeOllamaClient):
    failures = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.remaining_failures = self.failures

    async def chat(self, *, model, messages, tools):
        self.models.append(model)
        self.calls.append([dict(m) for m in messages])
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise _FakeProviderError(502)
        return _fake_ollama_response()


async def test_ollama_agent_resends_prior_turns_as_context(monkeypatch):
    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = _FakeOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3")
    await agent.decide("turn one", None, character_id="char_1")
    await agent.decide("turn two", None, character_id="char_1")

    client = agent._client
    # With no visible event result, raw provider turns are discarded before the next request.
    second = client.calls[1]
    assert second[0] == {"role": "system", "content": CHARACTER_SYSTEM_PROMPT}
    assert second[1] == {"role": "user", "content": "turn two"}


async def test_ollama_agent_returns_visible_events_as_structured_tool_result(monkeypatch):
    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = _FakeOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    context = PromptContext(
        name="Hazel",
        kind="character",
        status="active",
        action=(5.0, 5.0),
        focus=(5.0, 5.0),
        location_title="Meadow",
        room_summary="Meadow",
        perceived_events=(
            PerceivedPromptEvent(
                event_id="event-1",
                event_type="CommandExecutedEvent",
                world_epoch=12,
                summary="Your move action completed.",
            ),
        ),
        omitted_perceived_events=2,
        omitted_event_epoch_range=(3, 8),
        warnings=("The destination was blocked.",),
    )
    agent = OllamaAgent(model="llama3")

    await agent.decide("turn one", context, character_id="hazel")
    await agent.decide("turn two", context, character_id="hazel")

    result = agent._client.calls[1][1]
    assert result["role"] == "user"
    assert "Your move action completed." in result["content"]
    assert "deterministic visible-event summary" in result["content"]


async def test_ollama_agent_keeps_history_per_character(monkeypatch):
    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = _FakeOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3")
    await agent.decide("hazel turn", None, character_id="hazel")
    await agent.decide("juniper turn", None, character_id="juniper")

    # Juniper's first call must not contain Hazel's history.
    juniper_call = agent._client.calls[1]
    assert juniper_call == [
        {"role": "system", "content": CHARACTER_SYSTEM_PROMPT},
        {"role": "user", "content": "juniper turn"},
    ]


async def test_ollama_agent_sends_system_prompt_and_tool_schemas(monkeypatch):
    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = _FakeOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    tools = _tool_schemas(CORE_ACTION_DEFINITIONS)
    agent = OllamaAgent(model="llama3")
    await agent.decide("turn one", None, character_id="hazel", tools=tools)

    assert agent._client.calls[0][0] == {"role": "system", "content": CHARACTER_SYSTEM_PROMPT}
    assert agent._client.tools == [tools]
    by_name = {schema["function"]["name"]: schema["function"] for schema in tools}
    assert {"move", "wait"} <= by_name.keys()
    assert "Example: go north." in by_name["move"]["description"]
    assert "Example: wait." in by_name["wait"]["description"]


async def test_ollama_agent_uses_configured_system_prompt(monkeypatch):
    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = _FakeOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    requests: list[dict[str, JsonValue]] = []
    agent = OllamaAgent(
        model="llama3",
        system_prompt="You are player one.",
        request_observer=requests.append,
    )
    await agent.decide("turn one", None, character_id="hazel")

    assert agent._client.calls[0][0] == {
        "role": "system",
        "content": "You are player one.",
    }
    assert requests == [
        {
            "provider": "ollama",
            "model": "deepseek-v4-flash",
            "messages": agent._client.calls[0],
            "tools": agent._client.tools[0],
            "options": {},
        }
    ]


async def test_ollama_agent_sends_configured_thinking_and_temperature(monkeypatch):
    class ConfiguredOllamaClient(_FakeOllamaClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.settings: list[tuple[str, dict[str, float | int]]] = []

        async def chat(self, *, model, messages, tools, think, options):
            self.settings.append((think, options))
            return await super().chat(model=model, messages=messages, tools=tools)

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = ConfiguredOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(
        model="reasoner", think="high", temperature=1.0, max_output_tokens=8192
    )
    await agent.decide("turn one", None, character_id="hazel")
    await agent.chat([{"role": "user", "content": "hello"}], character_id="hazel")

    assert agent._client.settings == [
        ("high", {"temperature": 1.0, "num_predict": 8192}),
        ("high", {"temperature": 1.0, "num_predict": 8192}),
    ]


@pytest.mark.parametrize(
    ("system_style", "expected_style"),
    [
        ("concise", "concise and direct"),
        ("expressive", "vivid but grounded"),
        ("unknown", None),
    ],
)
async def test_ollama_agent_applies_task_local_controller_settings(
    monkeypatch, system_style, expected_style
):
    class ControllerSettingsClient(_FakeOllamaClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.settings: list[dict[str, float | int]] = []

        async def chat(self, *, model, messages, tools, options):
            self.settings.append(options)
            return await super().chat(model=model, messages=messages, tools=tools)

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = ControllerSettingsClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    agent = OllamaAgent(model="reasoner", temperature=1.0, max_output_tokens=8192)

    with llm_request_settings(
        LLMRequestSettings(
            temperature=0.25,
            max_output_tokens=256,
            system_style=system_style,
        )
    ):
        await agent.decide("turn one", None, character_id="hazel")

    assert agent._client.settings == [{"temperature": 0.25, "num_predict": 256}]
    system = agent._client.calls[0][0]["content"]
    if expected_style is None:
        assert system == CHARACTER_SYSTEM_PROMPT
    else:
        assert expected_style in system


async def test_ollama_agent_limits_concurrent_provider_requests(monkeypatch):
    release = asyncio.Event()

    class LimitedClient(_FakeOllamaClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.active = 0
            self.maximum = 0

        async def chat(self, *, model, messages, tools):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await release.wait()
                return await super().chat(model=model, messages=messages, tools=tools)
            finally:
                self.active -= 1

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = LimitedClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    agent = OllamaAgent(max_concurrent_requests=1)
    tasks = [
        asyncio.create_task(
            agent.chat([{"role": "user", "content": "hello"}], character_id=str(index))
        )
        for index in range(3)
    ]

    await asyncio.sleep(0)
    assert agent._client.maximum == 1
    release.set()
    await asyncio.gather(*tasks)

    assert agent._client.maximum == 1


def test_ollama_agent_clamps_provider_concurrency_to_one(monkeypatch):
    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = _FakeOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(max_concurrent_requests=0)

    assert agent._request_slots._value == 1


async def test_ollama_agent_configures_provider_request_timeout(monkeypatch):
    configured: list[tuple[str, str, float]] = []
    closed: list[bool] = []

    class TimeoutOllamaClient:
        def __init__(self, *, host, headers, timeout):
            configured.append((host, headers["Authorization"], timeout))

        async def close(self):
            closed.append(True)

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TimeoutOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(
        model="reasoner",
        host="https://ollama.example",
        api_key="cloud-secret",
        request_timeout_seconds=3600,
    )
    await agent.close()

    assert configured == [
        ("https://ollama.example", "Bearer cloud-secret", 3600),
    ]
    assert closed == [True]


async def test_ollama_agent_configures_timeout_without_host(monkeypatch):
    configured: list[float] = []

    class TimeoutOllamaClient:
        def __init__(self, *, timeout):
            configured.append(timeout)

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TimeoutOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    OllamaAgent(model="reasoner", request_timeout_seconds=600)

    assert configured == [600]


async def test_ollama_agent_configures_host_without_timeout(monkeypatch):
    configured: list[tuple[str, str]] = []

    class HostedOllamaClient:
        def __init__(self, *, host, headers):
            configured.append((host, headers["Authorization"]))

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = HostedOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    OllamaAgent(
        model="reasoner",
        host="https://ollama.example",
        api_key="cloud-secret",
    )

    assert configured == [("https://ollama.example", "Bearer cloud-secret")]


async def test_ollama_agent_observes_full_response_and_optionally_thinking(monkeypatch):
    class ThinkingOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            del messages, tools
            return {
                "model": model,
                "eval_count": 12,
                "message": {
                    "role": "assistant",
                    "content": "done",
                    "thinking": "reasoning trace",
                    "tool_calls": [{"function": {"name": "wait", "arguments": {}}}],
                },
            }

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = ThinkingOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    without_thinking: list[dict[str, JsonValue]] = []
    with_thinking: list[dict[str, JsonValue]] = []

    await OllamaAgent(
        model="reasoner", response_observer=without_thinking.append
    ).decide("turn one", None, character_id="hazel")
    await OllamaAgent(
        model="reasoner",
        response_observer=with_thinking.append,
        log_thinking=True,
    ).decide("turn one", None, character_id="hazel")

    assert without_thinking[0]["model"] == "reasoner"
    assert without_thinking[0]["eval_count"] == 12
    assert "thinking" not in without_thinking[0]["message"]
    assert with_thinking[0]["message"]["thinking"] == "reasoning trace"


async def test_ollama_agent_can_override_model_per_decision(monkeypatch):
    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = _FakeOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="fallback")
    await agent.decide("turn one", None, character_id="hazel", model="controller-model")

    assert agent._client.models == ["controller-model"]


async def test_ollama_agent_maps_legacy_default_model_to_flash(monkeypatch):
    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = _FakeOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="fallback")
    await agent.decide("turn one", None, character_id="hazel", model="llama3")

    assert normalize_model("llama3") == DEFAULT_MODEL
    assert agent._client.models == [DEFAULT_MODEL]


async def test_ollama_agent_rejects_plain_assistant_reply_and_trims_history(monkeypatch):
    class PlainOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            del tools
            self.models.append(model)
            self.calls.append([dict(m) for m in messages])
            return {"message": {"role": "assistant", "content": "waiting"}}

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = PlainOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3", history_turns=1)

    first = await agent.decide("turn one", None, character_id="hazel")
    assert isinstance(first, InvalidAgentResponse)
    assert "no structured tool call" in first.feedback
    assert '"waiting"' in first.feedback
    assert agent._history["hazel"] == [
        {"role": "user", "content": "turn one"},
        {"role": "assistant", "content": "waiting"},
    ]

    await agent.decide("turn two", None, character_id="hazel")

    assert agent._history["hazel"] == [
        {"role": "user", "content": "turn two"},
        {"role": "assistant", "content": "waiting"},
    ]


async def test_ollama_agent_retries_tagged_autonomous_reply_as_structured_call(
    monkeypatch,
):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedThenValidOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                return {"message": {"role": "assistant", "content": malformed}}
            return _fake_ollama_response()

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TaggedThenValidOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3")
    result = await agent.decide("turn one", None, character_id="hazel")

    assert result == ToolCall("wait", {})
    assert len(agent._client.calls) == 2
    assert agent._client.calls[1][-2] == {
        "role": "assistant",
        "content": malformed,
    }
    assert agent._client.calls[1][-1] == {
        "role": "user",
        "content": TEXT_TOOL_CALL_CORRECTION_PROMPT,
    }
    assert malformed not in str(agent._history["hazel"])


async def test_ollama_agent_can_disable_tagged_autonomous_retry(monkeypatch):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            return {"message": {"role": "assistant", "content": malformed}}

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TaggedOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3", reject_text_tool_calls=False)
    result = await agent.decide("turn one", None, character_id="hazel")

    assert isinstance(result, InvalidAgentResponse)
    assert result.reason == "provider response wrote a tool call as message text"
    assert len(agent._client.calls) == 1
    assert agent._history["hazel"][-1]["content"] == ""


async def test_ollama_agent_rejects_tagged_reply_when_corrective_request_fails(
    monkeypatch,
):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedThenFailingOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                return {"message": {"role": "assistant", "content": malformed}}
            raise _FakeProviderError(502)

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TaggedThenFailingOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3", retry_delay_seconds=0)
    result = await agent.decide("turn one", None, character_id="hazel")

    assert isinstance(result, InvalidAgentResponse)
    assert result.reason == "provider response wrote a tool call as message text"
    assert len(agent._client.calls) == 3


async def test_ollama_agent_bounds_invalid_response_feedback(monkeypatch):
    class LongReplyOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            del model, messages, tools
            return {"message": {"role": "assistant", "content": "x" * 1_001}}

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = LongReplyOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    result = await OllamaAgent(model="llama3").decide(
        "turn one", None, character_id="hazel"
    )

    assert isinstance(result, InvalidAgentResponse)
    assert f'{"x" * 1_000}...' in result.feedback
    assert "x" * 1_001 not in result.feedback


async def test_ollama_agent_retries_transient_provider_errors(monkeypatch):
    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = _FlakyOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3", retry_delay_seconds=0)
    call = await agent.decide("turn one", None, character_id="hazel")

    assert call == ToolCall("wait", {})
    assert len(agent._client.calls) == 2
    assert agent._history["hazel"][0] == {"role": "user", "content": "turn one"}


async def test_ollama_agent_retries_empty_responses_before_recording_history(monkeypatch):
    class EmptyThenValidOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) < 3:
                return {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [],
                    }
                }
            return _fake_ollama_response()

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = EmptyThenValidOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    observed: list[dict[str, JsonValue]] = []

    agent = OllamaAgent(
        model="llama3",
        retry_delay_seconds=0,
        response_observer=observed.append,
    )
    call = await agent.decide("turn one", None, character_id="hazel")

    assert call == ToolCall("wait", {})
    assert len(agent._client.calls) == 3
    assert len(observed) == 3
    assert agent._history["hazel"] == [
        {"role": "user", "content": "turn one"},
        _fake_ollama_response()["message"],
    ]


async def test_ollama_agent_rejects_empty_response_after_shared_retry_budget(monkeypatch):
    class AlwaysEmptyOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [],
                }
            }

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = AlwaysEmptyOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    observed: list[dict[str, JsonValue]] = []

    agent = OllamaAgent(
        model="llama3",
        retry_delay_seconds=0,
        response_observer=observed.append,
    )
    call = await agent.decide("turn one", None, character_id="hazel")

    assert isinstance(call, InvalidAgentResponse)
    assert call.reason == "provider returned empty response after retries"
    assert "after 3 attempt(s)" in call.feedback
    assert len(agent._client.calls) == 3
    assert len(observed) == 3
    assert observed[0]["message"]["content"] == ""
    assert agent._history["hazel"] == []


async def test_ollama_agent_retries_token_limited_response(monkeypatch):
    class TokenLimitedThenValidOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                return {
                    "done_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": "still explaining",
                        "tool_calls": [],
                    },
                }
            return _fake_ollama_response()

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TokenLimitedThenValidOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3", retry_delay_seconds=0)
    call = await agent.decide("turn one", None, character_id="hazel")

    assert call == ToolCall("wait", {})
    assert len(agent._client.calls) == 2
    assert "reached the output token limit" in agent._client.calls[1][-1]["content"]


async def test_ollama_agent_mixed_filtered_outputs_share_one_retry_budget(monkeypatch):
    class MixedInvalidOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                return {
                    "message": {
                        "role": "assistant",
                        "content": "<infer>wait</infer>",
                    }
                }
            if len(self.calls) == 2:
                return {"message": {"role": "assistant", "content": ""}}
            if len(self.calls) == 3:
                return {
                    "done_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": "still explaining",
                    },
                }
            return _fake_ollama_response()

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = MixedInvalidOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    observed: list[dict[str, JsonValue]] = []

    agent = OllamaAgent(
        model="llama3",
        retry_delay_seconds=0,
        response_observer=observed.append,
    )
    call = await agent.decide("turn one", None, character_id="hazel")

    assert isinstance(call, InvalidAgentResponse)
    assert call.reason == "provider response reached its output token limit"
    assert len(agent._client.calls) == 3
    assert len(observed) == 3
    assert agent._history["hazel"] == []


async def test_ollama_agent_rejects_missing_response_after_transient_provider_retries(
    monkeypatch,
):
    class AlwaysFailOllamaClient(_FlakyOllamaClient):
        failures = 99

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = AlwaysFailOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3", retry_delay_seconds=0)
    call = await agent.decide("turn one", None, character_id="hazel")

    assert isinstance(call, InvalidAgentResponse)
    assert call.reason == "provider returned no response after retries"
    assert "after 3 attempt(s)" in call.feedback
    assert len(agent._client.calls) == 3
    assert agent._history["hazel"] == []


async def test_ollama_agent_chat_returns_content_and_tool(monkeypatch):
    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = _FakeOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    tools = [{"type": "function", "function": {"name": "wait"}}]
    agent = OllamaAgent(model="llama3")
    reply = await agent.chat(
        [{"role": "system", "content": "chat"}, {"role": "user", "content": "hello"}],
        character_id="hazel",
        model="controller-model",
        tools=tools,
    )

    assert reply.content == "ok"
    assert reply.tool_call == ToolCall("wait", {})
    assert agent._client.models == ["controller-model"]
    assert agent._client.tools == [tools]


async def test_ollama_agent_chat_returns_content_without_tool(monkeypatch):
    class PlainChatOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(m) for m in messages])
            return {"message": {"role": "assistant", "content": "just text"}}

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = PlainChatOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3")
    reply = await agent.chat([{"role": "user", "content": "hello"}], character_id="hazel")

    assert reply.content == "just text"
    assert reply.tool_call is None


@pytest.mark.parametrize(
    ("tools", "correction"),
    [
        (None, "Return a concise prose reply."),
        (
            [{"type": "function", "function": {"name": "wait"}}],
            "native structured tool call or a concise prose reply",
        ),
    ],
)
async def test_ollama_agent_chat_retries_empty_reply_through_response_filters(
    monkeypatch,
    tools,
    correction,
):
    class EmptyThenPlainOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                return {"message": {"role": "assistant", "content": "  "}}
            return {"message": {"role": "assistant", "content": "Clean prose."}}

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = EmptyThenPlainOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3", max_retries=1, retry_delay_seconds=0)
    reply = await agent.chat(
        [{"role": "user", "content": "hello"}],
        character_id="hazel",
        tools=tools,
    )

    assert reply == ChatAgentReply(content="Clean prose.")
    assert len(agent._client.calls) == 2
    assert "completely empty" in agent._client.calls[1][-1]["content"]
    assert correction in agent._client.calls[1][-1]["content"]


async def test_ollama_agent_accepts_additional_content_filter(monkeypatch):
    class InappropriateContentFilter:
        def reject(
            self,
            response: AssistantResponseView,
            context: ResponseFilterContext,
        ) -> ResponseFilterRejection | None:
            del context
            if "blocked topic" not in response.content:
                return None
            return ResponseFilterRejection(
                filter_name="content_safety",
                reason="provider response violated content policy",
                correction_prompt="Answer without the blocked topic.",
                telemetry_attribute="llm.content_safety.rejected",
            )

    class FilteredThenPlainOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                return {"message": {"role": "assistant", "content": "blocked topic"}}
            return {"message": {"role": "assistant", "content": "Clean prose."}}

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = FilteredThenPlainOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(
        model="llama3",
        max_retries=1,
        retry_delay_seconds=0,
        response_filters=(*DEFAULT_RESPONSE_FILTERS, InappropriateContentFilter()),
    )
    reply = await agent.chat([{"role": "user", "content": "hello"}], character_id="hazel")

    assert reply == ChatAgentReply(content="Clean prose.")
    assert agent._client.calls[1][-1]["content"] == "Answer without the blocked topic."


async def test_ollama_agent_chat_retries_tagged_reply_as_clean_prose(monkeypatch):
    malformed = "<｜DSML｜ name=\"target_id\">crate</｜DSML｜>"

    class TaggedThenPlainOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                return {"message": {"role": "assistant", "content": malformed}}
            return {"message": {"role": "assistant", "content": "Clean prose."}}

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TaggedThenPlainOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3")
    reply = await agent.chat([{"role": "user", "content": "hello"}], character_id="hazel")

    assert reply == ChatAgentReply(content="Clean prose.")
    assert agent._client.calls[1][-1]["content"] == PROSE_REPLY_CORRECTION_PROMPT


async def test_ollama_agent_chat_keeps_native_call_and_drops_tagged_content(
    monkeypatch,
):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedThenMixedOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                return {"message": {"role": "assistant", "content": malformed}}
            response = _fake_ollama_response()
            response["message"]["content"] = malformed
            return response

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TaggedThenMixedOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    tools = [{"type": "function", "function": {"name": "wait"}}]

    agent = OllamaAgent(model="llama3")
    reply = await agent.chat(
        [{"role": "user", "content": "hello"}],
        character_id="hazel",
        tools=tools,
    )

    assert reply == ChatAgentReply(tool_call=ToolCall("wait", {}))
    assert agent._client.calls[1][-1]["content"] == TEXT_REPLY_CORRECTION_PROMPT


async def test_ollama_agent_chat_drops_tagged_content_from_initial_native_call(
    monkeypatch,
):
    malformed = '<invoke name="wait"></invoke>'

    class MixedOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            response = _fake_ollama_response()
            response["message"]["content"] = malformed
            return response

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = MixedOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3")
    reply = await agent.chat(
        [{"role": "user", "content": "hello"}],
        character_id="hazel",
    )

    assert reply == ChatAgentReply(tool_call=ToolCall("wait", {}))
    assert len(agent._client.calls) == 1


async def test_ollama_agent_chat_suppresses_tagged_reply_when_retry_fails(
    monkeypatch,
):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedThenFailingOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                return {"message": {"role": "assistant", "content": malformed}}
            raise _FakeProviderError(502)

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TaggedThenFailingOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3", max_retries=0)
    reply = await agent.chat(
        [{"role": "user", "content": "hello"}],
        character_id="hazel",
    )

    assert reply == ChatAgentReply()
    assert len(agent._client.calls) == 1


async def test_ollama_agent_chat_suppresses_repeated_tagged_prose(monkeypatch):
    malformed = '<tool_call>{"name": "wait"}</tool_call>'

    class TaggedOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            return {"message": {"role": "assistant", "content": malformed}}

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TaggedOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3")
    reply = await agent.chat([{"role": "user", "content": "hello"}], character_id="hazel")

    assert reply == ChatAgentReply()
    assert len(agent._client.calls) == 3


async def test_ollama_agent_chat_can_allow_tagged_prose_for_debugging(monkeypatch):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedOllamaClient(_FakeOllamaClient):
        async def chat(self, *, model, messages, tools):
            self.models.append(model)
            self.tools.append(tools)
            self.calls.append([dict(message) for message in messages])
            return {"message": {"role": "assistant", "content": malformed}}

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = TaggedOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3", reject_text_tool_calls=False)
    reply = await agent.chat([{"role": "user", "content": "hello"}], character_id="hazel")

    assert reply == ChatAgentReply(content=malformed)
    assert len(agent._client.calls) == 1


async def test_ollama_agent_chat_returns_empty_after_transient_provider_retries(monkeypatch):
    class AlwaysFailOllamaClient(_FlakyOllamaClient):
        failures = 99

    fake_module = types.ModuleType("ollama")
    fake_module.AsyncClient = AlwaysFailOllamaClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    agent = OllamaAgent(model="llama3", retry_delay_seconds=0)
    reply = await agent.chat([], character_id="hazel")

    assert reply.content == ""
    assert reply.tool_call is None
    assert len(agent._client.calls) == 3


class _FakeOpenRouterChat:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_async(self, *, model, messages, tools):
        self.calls.append({"model": model, "messages": [dict(m) for m in messages], "tools": tools})
        return _fake_openrouter_response()


def _fake_openrouter_response(
    *,
    content: str = "ok",
    include_tool_call: bool = True,
    finish_reason: str | None = None,
):
    tool_calls = None
    serialized_tool_calls = None
    if include_tool_call:
        function = types.SimpleNamespace(name="wait", arguments='{"reason": "rest"}')
        tool_call = types.SimpleNamespace(id="call_wait", function=function)
        tool_calls = [tool_call]
        serialized_tool_calls = [
            {
                "id": "call_wait",
                "type": "function",
                "function": {"name": "wait", "arguments": '{"reason": "rest"}'},
            }
        ]
    message = types.SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        model_dump=lambda **_: {
            "role": "assistant",
            "content": content,
            "tool_calls": serialized_tool_calls,
        },
    )
    usage = types.SimpleNamespace(
        prompt_tokens=14,
        completion_tokens=6,
        total_tokens=20,
        cost=0.0012,
    )
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=message,
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
    )


class _FakeOpenRouterClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = _FakeOpenRouterChat()


def test_openrouter_agent_clamps_provider_concurrency_to_one(monkeypatch):
    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = _FakeOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(max_concurrent_requests=0)

    assert agent._request_slots._value == 1


async def test_openrouter_agent_closes_sdk_http_clients(monkeypatch):
    closed: list[str] = []

    class SyncClient:
        def close(self):
            closed.append("sync")

    class AsyncClient:
        async def aclose(self):
            closed.append("async")

    class ClosableOpenRouter:
        def __init__(self, **kwargs):
            del kwargs
            self.sdk_configuration = types.SimpleNamespace(
                client=SyncClient(),
                async_client=AsyncClient(),
            )

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = ClosableOpenRouter
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="openai/gpt-4.1-mini", api_key="key")
    await agent.close()

    assert closed == ["sync", "async"]


class _FlakyOpenRouterChat(_FakeOpenRouterChat):
    failures = 1

    def __init__(self):
        super().__init__()
        self.remaining_failures = self.failures

    async def send_async(self, *, model, messages, tools):
        self.calls.append({"model": model, "messages": [dict(m) for m in messages]})
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise _FakeProviderError(502)
        function = types.SimpleNamespace(name="wait", arguments='{"reason": "rest"}')
        tool_call = types.SimpleNamespace(id="call_wait", function=function)
        message = types.SimpleNamespace(
            role="assistant",
            content="ok",
            tool_calls=[tool_call],
            model_dump=lambda **_: {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [
                    {
                        "id": "call_wait",
                        "type": "function",
                        "function": {"name": "wait", "arguments": '{"reason": "rest"}'},
                    }
                ],
            },
        )
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _FlakyOpenRouterClient(_FakeOpenRouterClient):
    chat_type = _FlakyOpenRouterChat

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = self.chat_type()


async def test_openrouter_agent_parses_tool_arguments_json(monkeypatch):
    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = _FakeOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="openai/gpt-4.1-mini", api_key="key")
    call = await agent.decide("turn one", None, character_id="hazel")

    assert call == ToolCall("wait", {"reason": "rest"})
    assert agent._client.kwargs == {"api_key": "key"}
    assert agent._client.chat.calls[0]["model"] == "openai/gpt-4.1-mini"


async def test_openrouter_agent_uses_configured_system_prompt(monkeypatch):
    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = _FakeOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    requests: list[dict[str, JsonValue]] = []
    agent = OpenRouterAgent(
        model="openai/gpt-4.1-mini",
        api_key="key",
        system_prompt="You are player two.",
        request_observer=requests.append,
    )
    await agent.decide("turn one", None, character_id="hazel")

    assert agent._client.chat.calls[0]["messages"][0] == {
        "role": "system",
        "content": "You are player two.",
    }
    assert requests[0] == {
        "provider": "openrouter",
        "model": "openai/gpt-4.1-mini",
        "messages": agent._client.chat.calls[0]["messages"],
        "tools": agent._client.chat.calls[0]["tools"],
        "options": {},
    }


async def test_openrouter_agent_passes_reasoning_options_and_observes_response(
    monkeypatch,
):
    class ConfiguredOpenRouterChat:
        def __init__(self):
            self.options: dict[str, object] = {}

        async def send_async(
            self,
            *,
            model,
            messages,
            tools,
            reasoning=None,
            temperature=None,
            max_completion_tokens=None,
        ):
            del model, messages, tools
            self.options = {
                "reasoning": reasoning,
                "temperature": temperature,
                "max_completion_tokens": max_completion_tokens,
            }
            function = types.SimpleNamespace(name="wait", arguments="{}")
            tool_call = types.SimpleNamespace(id="call_wait", function=function)
            message = types.SimpleNamespace(
                role="assistant",
                content="",
                reasoning="private trace",
                tool_calls=[tool_call],
                model_dump=lambda **_: {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "private trace",
                    "tool_calls": [
                        {
                            "id": "call_wait",
                            "type": "function",
                            "function": {"name": "wait", "arguments": "{}"},
                        }
                    ],
                },
            )
            response_json = {
                "id": "generation",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning": "private trace",
                            "tool_calls": [
                                {
                                    "id": "call_wait",
                                    "type": "function",
                                    "function": {
                                        "name": "wait",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
            }
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message)],
                model_dump=lambda **_: response_json,
            )

    class ConfiguredOpenRouterClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = ConfiguredOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = ConfiguredOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)
    responses: list[dict[str, JsonValue]] = []
    agent = OpenRouterAgent(
        model="openai/gpt-5.6-terra",
        api_key="key",
        reasoning="medium",
        temperature=0.4,
        max_output_tokens=4096,
        response_observer=responses.append,
        log_thinking=True,
    )

    call = await agent.decide("turn one", None, character_id="hazel")

    assert call == ToolCall("wait", {})
    assert agent._client.chat.options == {
        "reasoning": {"effort": "medium"},
        "temperature": 0.4,
        "max_completion_tokens": 4096,
    }
    with llm_request_settings(
        LLMRequestSettings(temperature=0.2, max_output_tokens=256)
    ):
        await agent.decide("turn one", None, character_id="bramble")
    assert agent._client.chat.options == {
        "reasoning": {"effort": "medium"},
        "temperature": 0.2,
        "max_completion_tokens": 256,
    }
    assert responses[0]["choices"][0]["message"]["reasoning"] == "private trace"
    sanitized = _openrouter_response_json(
        types.SimpleNamespace(model_dump=lambda **_: responses[0]),
        include_thinking=False,
    )
    assert "reasoning" not in sanitized["choices"][0]["message"]


def test_openrouter_response_redaction_handles_partial_provider_shapes():
    without_choices = _openrouter_response_json(
        {"reasoning": "private trace"},
        include_thinking=False,
    )
    mixed_choices = _openrouter_response_json(
        {
            "choices": [
                None,
                {},
                {"message": None},
                {"message": {"reasoning_details": ["private trace"]}},
            ]
        },
        include_thinking=False,
    )

    assert without_choices == {}
    assert mixed_choices["choices"][-1] == {"message": {}}


async def test_openrouter_agent_rejects_tool_call_without_correlation_id(monkeypatch):
    class MissingIdOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append({"model": model, "messages": messages, "tools": tools})
            function = types.SimpleNamespace(name="wait", arguments="{}")
            tool_call = types.SimpleNamespace(function=function)
            message = types.SimpleNamespace(
                role="assistant",
                content="",
                tool_calls=[tool_call],
                model_dump=lambda **_: {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"type": "function", "function": {"name": "wait", "arguments": "{}"}}
                    ],
                },
            )
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    class MissingIdOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = MissingIdOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = MissingIdOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    result = await OpenRouterAgent(model="model", api_key="key").decide(
        "turn one", None, character_id="hazel"
    )

    assert isinstance(result, InvalidAgentResponse)
    assert result.reason == "provider tool call contained no tool call id"
    assert "cannot be correlated" in result.feedback


async def test_openrouter_agent_resends_prior_turns_as_context(monkeypatch):
    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = _FakeOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="openai/gpt-4.1-mini", api_key="key")
    await agent.decide("turn one", None, character_id="hazel")
    await agent.decide("turn two", None, character_id="hazel")

    second = agent._client.chat.calls[1]["messages"]
    assert second[0] == {"role": "system", "content": CHARACTER_SYSTEM_PROMPT}
    assert second[1] == {"role": "user", "content": "turn two"}


async def test_openrouter_agent_sends_system_prompt_and_tool_schemas(monkeypatch):
    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = _FakeOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    tools = _tool_schemas(CORE_ACTION_DEFINITIONS)
    agent = OpenRouterAgent(model="openai/gpt-4.1-mini", api_key="key")
    await agent.decide("turn one", None, character_id="hazel", tools=tools)

    call = agent._client.chat.calls[0]
    assert call["messages"][0] == {"role": "system", "content": CHARACTER_SYSTEM_PROMPT}
    assert call["tools"] == tools
    by_name = {schema["function"]["name"]: schema["function"] for schema in call["tools"]}
    assert {"move", "wait"} <= by_name.keys()
    assert "Example: go north." in by_name["move"]["description"]
    assert "Example: wait." in by_name["wait"]["description"]


def test_openrouter_usage_includes_total_tokens_and_provider_cost():
    usage = types.SimpleNamespace(
        prompt_tokens=3,
        completion_tokens=4,
        total_tokens=7,
        cost=0.0042,
    )
    parsed = _openrouter_usage(types.SimpleNamespace(usage=usage))

    assert parsed.prompt_tokens == 3
    assert parsed.completion_tokens == 4
    assert parsed.total_tokens == 7
    assert parsed.cost == 0.0042


async def test_openrouter_usage_enriches_cost_from_generation_metadata():
    class FakeGenerations:
        async def get_generation_async(self, *, id):
            assert id == "gen_1"
            data = types.SimpleNamespace(
                tokens_prompt=11,
                tokens_completion=5,
                total_cost=0.006,
            )
            return types.SimpleNamespace(data=data)

    client = types.SimpleNamespace(generations=FakeGenerations())
    usage = types.SimpleNamespace(prompt_tokens=11, completion_tokens=5, total_tokens=16)
    response = types.SimpleNamespace(id="gen_1", usage=usage)

    parsed = await _openrouter_enriched_usage(client, response)

    assert parsed.prompt_tokens == 11
    assert parsed.completion_tokens == 5
    assert parsed.total_tokens == 16
    assert parsed.cost == 0.006


async def test_openrouter_usage_handles_missing_generation_metadata():
    class FakeGenerations:
        async def get_generation_async(self, *, id):
            assert id == "gen_1"
            return types.SimpleNamespace(data=None)

    client = types.SimpleNamespace(generations=FakeGenerations())
    usage = types.SimpleNamespace(prompt_tokens=11, completion_tokens=5, total_tokens=16)
    response = types.SimpleNamespace(id="gen_1", usage=usage)

    parsed = await _openrouter_enriched_usage(client, response)

    assert parsed.prompt_tokens == 11
    assert parsed.completion_tokens == 5
    assert parsed.total_tokens == 16
    assert parsed.cost == 0.0


async def test_openrouter_usage_falls_back_when_generation_lookup_fails(monkeypatch):
    class FakeGenerations:
        def __init__(self) -> None:
            self.calls = 0

        async def get_generation_async(self, *, id):
            assert id == "gen_1"
            self.calls += 1
            raise RuntimeError("metadata unavailable")

    async def no_sleep(_seconds):
        return None

    generations = FakeGenerations()
    client = types.SimpleNamespace(generations=generations)
    usage = types.SimpleNamespace(prompt_tokens=11, completion_tokens=5, total_tokens=16)
    response = types.SimpleNamespace(id="gen_1", usage=usage)
    monkeypatch.setattr("bunnyland.llm_agents.agent.asyncio.sleep", no_sleep)

    parsed = await _openrouter_enriched_usage(client, response)

    assert generations.calls == 3
    assert parsed.prompt_tokens == 11
    assert parsed.completion_tokens == 5
    assert parsed.total_tokens == 16
    assert parsed.cost == 0.0


async def test_openrouter_agent_retries_transient_provider_errors(monkeypatch):
    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = _FlakyOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="openai/gpt-4.1-mini", api_key="key", retry_delay_seconds=0)
    call = await agent.decide("turn one", None, character_id="hazel")

    assert call == ToolCall("wait", {"reason": "rest"})
    assert len(agent._client.chat.calls) == 2
    assert agent._history["hazel"][0] == {"role": "user", "content": "turn one"}


async def test_openrouter_agent_retries_empty_responses_before_recording_history(
    monkeypatch,
):
    class EmptyThenValidOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            if len(self.calls) >= 2:
                return await super().send_async(
                    model=model,
                    messages=messages,
                    tools=tools,
                )
            self.calls.append(
                {
                    "model": model,
                    "messages": [dict(message) for message in messages],
                    "tools": tools,
                }
            )
            message = types.SimpleNamespace(
                role="assistant",
                content="",
                tool_calls=None,
                model_dump=lambda **_: {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": None,
                },
            )
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message)],
                model_dump=lambda **_: {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": None,
                            }
                        }
                    ]
                },
            )

    class EmptyThenValidOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = EmptyThenValidOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = EmptyThenValidOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(
        model="openai/gpt-4.1-mini",
        api_key="key",
        retry_delay_seconds=0,
    )
    call = await agent.decide("turn one", None, character_id="hazel")

    assert call == ToolCall("wait", {"reason": "rest"})
    assert len(agent._client.chat.calls) == 3
    assert len(agent._history["hazel"]) == 2
    assert agent._history["hazel"][0] == {"role": "user", "content": "turn one"}
    assert agent._history["hazel"][1]["content"] == "ok"


async def test_openrouter_agent_rejects_empty_response_after_shared_retry_budget(
    monkeypatch,
):
    class AlwaysEmptyOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {
                    "model": model,
                    "messages": [dict(message) for message in messages],
                    "tools": tools,
                }
            )
            message = types.SimpleNamespace(
                role="assistant",
                content="",
                tool_calls=None,
                model_dump=lambda **_: {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": None,
                },
            )
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message)],
                model_dump=lambda **_: {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": None,
                            }
                        }
                    ]
                },
            )

    class AlwaysEmptyOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = AlwaysEmptyOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = AlwaysEmptyOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)
    observed: list[dict[str, JsonValue]] = []

    agent = OpenRouterAgent(
        model="openai/gpt-4.1-mini",
        api_key="key",
        retry_delay_seconds=0,
        response_observer=observed.append,
    )
    call = await agent.decide("turn one", None, character_id="hazel")

    assert isinstance(call, InvalidAgentResponse)
    assert call.reason == "provider returned empty response after retries"
    assert "after 3 attempt(s)" in call.feedback
    assert len(agent._client.chat.calls) == 3
    assert len(observed) == 3
    choices = observed[0]["choices"]
    assert isinstance(choices, list)
    assert choices[0]["message"]["content"] == ""
    assert agent._history["hazel"] == []


async def test_openrouter_agent_retries_token_limited_response(monkeypatch):
    class TokenLimitedThenValidOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {
                    "model": model,
                    "messages": [dict(message) for message in messages],
                    "tools": tools,
                }
            )
            if len(self.calls) == 1:
                return _fake_openrouter_response(
                    content="still explaining",
                    include_tool_call=False,
                    finish_reason="length",
                )
            return _fake_openrouter_response()

    class TokenLimitedThenValidOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = TokenLimitedThenValidOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = TokenLimitedThenValidOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(
        model="model",
        api_key="key",
        retry_delay_seconds=0,
    )
    call = await agent.decide("turn one", None, character_id="hazel")

    assert call == ToolCall("wait", {"reason": "rest"})
    assert len(agent._client.chat.calls) == 2
    assert (
        "reached the output token limit"
        in agent._client.chat.calls[1]["messages"][-1]["content"]
    )


async def test_openrouter_agent_rejects_plain_assistant_reply_and_trims_history(monkeypatch):
    class PlainOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            del tools
            self.calls.append({"model": model, "messages": [dict(m) for m in messages]})
            message = types.SimpleNamespace(role="assistant", content="waiting", tool_calls=None)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    class PlainOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = PlainOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = PlainOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="openai/gpt-4.1-mini", api_key="key", history_turns=1)

    first = await agent.decide("turn one", None, character_id="hazel")
    assert isinstance(first, InvalidAgentResponse)
    assert "no structured tool call" in first.feedback
    assert agent._history["hazel"] == [
        {"role": "user", "content": "turn one"},
        {"role": "assistant", "content": "waiting"},
    ]

    await agent.decide("turn two", None, character_id="hazel")

    assert agent._history["hazel"] == [
        {"role": "user", "content": "turn two"},
        {"role": "assistant", "content": "waiting"},
    ]


async def test_openrouter_agent_retries_tagged_autonomous_reply_as_structured_call(
    monkeypatch,
):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedThenValidOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {"model": model, "messages": [dict(m) for m in messages], "tools": tools}
            )
            if len(self.calls) == 1:
                return _fake_openrouter_response(
                    content=malformed,
                    include_tool_call=False,
                )
            return _fake_openrouter_response()

    class TaggedThenValidOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = TaggedThenValidOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = TaggedThenValidOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="model", api_key="key")
    result = await agent.decide("turn one", None, character_id="hazel")

    assert result == ToolCall("wait", {"reason": "rest"})
    assert len(agent._client.chat.calls) == 2
    assert agent._client.chat.calls[1]["messages"][-2] == {
        "role": "assistant",
        "content": malformed,
    }
    assert agent._client.chat.calls[1]["messages"][-1] == {
        "role": "user",
        "content": TEXT_TOOL_CALL_CORRECTION_PROMPT,
    }
    assert malformed not in str(agent._history["hazel"])


async def test_openrouter_agent_can_disable_tagged_autonomous_retry(monkeypatch):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {"model": model, "messages": [dict(m) for m in messages], "tools": tools}
            )
            return _fake_openrouter_response(
                content=malformed,
                include_tool_call=False,
            )

    class TaggedOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = TaggedOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = TaggedOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(
        model="model",
        api_key="key",
        reject_text_tool_calls=False,
    )
    result = await agent.decide("turn one", None, character_id="hazel")

    assert isinstance(result, InvalidAgentResponse)
    assert result.reason == "provider response wrote a tool call as message text"
    assert len(agent._client.chat.calls) == 1
    assert agent._history["hazel"][-1]["content"] == ""


async def test_openrouter_agent_rejects_tagged_reply_when_corrective_request_fails(
    monkeypatch,
):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedThenFailingOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {"model": model, "messages": [dict(m) for m in messages], "tools": tools}
            )
            if len(self.calls) == 1:
                return _fake_openrouter_response(
                    content=malformed,
                    include_tool_call=False,
                )
            raise _FakeProviderError(502)

    class TaggedThenFailingOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = TaggedThenFailingOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = TaggedThenFailingOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(
        model="model",
        api_key="key",
        retry_delay_seconds=0,
    )
    result = await agent.decide("turn one", None, character_id="hazel")

    assert isinstance(result, InvalidAgentResponse)
    assert result.reason == "provider response wrote a tool call as message text"
    assert len(agent._client.chat.calls) == 3


async def test_openrouter_agent_rejects_missing_response_after_transient_provider_retries(
    monkeypatch,
):
    class AlwaysFailOpenRouterChat(_FlakyOpenRouterChat):
        failures = 99

    class AlwaysFailOpenRouterClient(_FlakyOpenRouterClient):
        chat_type = AlwaysFailOpenRouterChat

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = AlwaysFailOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="openai/gpt-4.1-mini", api_key="key", retry_delay_seconds=0)
    call = await agent.decide("turn one", None, character_id="hazel")

    assert isinstance(call, InvalidAgentResponse)
    assert call.reason == "provider returned no response after retries"
    assert "after 3 attempt(s)" in call.feedback
    assert len(agent._client.chat.calls) == 3
    assert agent._history["hazel"] == []


async def test_openrouter_agent_chat_returns_content_and_tool(monkeypatch):
    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = _FakeOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    tools = [{"type": "function", "function": {"name": "wait"}}]
    agent = OpenRouterAgent(model="openai/gpt-4.1-mini", api_key="key")
    reply = await agent.chat(
        [{"role": "system", "content": "chat"}, {"role": "user", "content": "hello"}],
        character_id="hazel",
        model="controller-model",
        tools=tools,
    )

    assert reply.content == "ok"
    assert reply.tool_call == ToolCall("wait", {"reason": "rest"})
    assert agent._client.chat.calls[0]["model"] == "controller-model"
    assert agent._client.chat.calls[0]["tools"] == tools


async def test_openrouter_agent_chat_returns_content_without_tool(monkeypatch):
    class PlainChatOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {"model": model, "messages": [dict(m) for m in messages], "tools": tools}
            )
            message = types.SimpleNamespace(role="assistant", content="just text", tool_calls=None)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    class PlainChatOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = PlainChatOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = PlainChatOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="openai/gpt-4.1-mini", api_key="key")
    reply = await agent.chat([{"role": "user", "content": "hello"}], character_id="hazel")

    assert reply.content == "just text"
    assert reply.tool_call is None


async def test_openrouter_agent_chat_retries_tagged_reply_as_clean_prose(monkeypatch):
    malformed = "<｜DSML｜ name=\"target_id\">crate</｜DSML｜>"

    class TaggedThenPlainOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {"model": model, "messages": [dict(m) for m in messages], "tools": tools}
            )
            if len(self.calls) == 1:
                return _fake_openrouter_response(
                    content=malformed,
                    include_tool_call=False,
                )
            return _fake_openrouter_response(
                content="Clean prose.",
                include_tool_call=False,
            )

    class TaggedThenPlainOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = TaggedThenPlainOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = TaggedThenPlainOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="model", api_key="key")
    reply = await agent.chat(
        [{"role": "user", "content": "hello"}],
        character_id="hazel",
    )

    assert reply == ChatAgentReply(content="Clean prose.")
    assert (
        agent._client.chat.calls[1]["messages"][-1]["content"]
        == PROSE_REPLY_CORRECTION_PROMPT
    )


async def test_openrouter_agent_chat_keeps_native_call_and_drops_tagged_content(
    monkeypatch,
):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedThenMixedOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {"model": model, "messages": [dict(m) for m in messages], "tools": tools}
            )
            if len(self.calls) == 1:
                return _fake_openrouter_response(
                    content=malformed,
                    include_tool_call=False,
                )
            return _fake_openrouter_response(content=malformed)

    class TaggedThenMixedOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = TaggedThenMixedOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = TaggedThenMixedOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)
    tools = [{"type": "function", "function": {"name": "wait"}}]

    agent = OpenRouterAgent(model="model", api_key="key")
    reply = await agent.chat(
        [{"role": "user", "content": "hello"}],
        character_id="hazel",
        tools=tools,
    )

    assert reply == ChatAgentReply(
        tool_call=ToolCall("wait", {"reason": "rest"})
    )
    assert (
        agent._client.chat.calls[1]["messages"][-1]["content"]
        == TEXT_REPLY_CORRECTION_PROMPT
    )


async def test_openrouter_agent_chat_drops_tagged_content_from_initial_native_call(
    monkeypatch,
):
    malformed = '<invoke name="wait"></invoke>'

    class MixedOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {"model": model, "messages": [dict(m) for m in messages], "tools": tools}
            )
            return _fake_openrouter_response(content=malformed)

    class MixedOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = MixedOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = MixedOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="model", api_key="key")
    reply = await agent.chat(
        [{"role": "user", "content": "hello"}],
        character_id="hazel",
    )

    assert reply == ChatAgentReply(
        tool_call=ToolCall("wait", {"reason": "rest"})
    )
    assert len(agent._client.chat.calls) == 1


async def test_openrouter_agent_chat_suppresses_tagged_reply_when_retry_fails(
    monkeypatch,
):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedThenFailingOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {"model": model, "messages": [dict(m) for m in messages], "tools": tools}
            )
            if len(self.calls) == 1:
                return _fake_openrouter_response(
                    content=malformed,
                    include_tool_call=False,
                )
            raise _FakeProviderError(502)

    class TaggedThenFailingOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = TaggedThenFailingOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = TaggedThenFailingOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="model", api_key="key", max_retries=0)
    reply = await agent.chat(
        [{"role": "user", "content": "hello"}],
        character_id="hazel",
    )

    assert reply == ChatAgentReply()
    assert len(agent._client.chat.calls) == 1


async def test_openrouter_agent_chat_suppresses_repeated_tagged_prose(monkeypatch):
    malformed = '<tool_call>{"name": "wait"}</tool_call>'

    class TaggedOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {"model": model, "messages": [dict(m) for m in messages], "tools": tools}
            )
            return _fake_openrouter_response(
                content=malformed,
                include_tool_call=False,
            )

    class TaggedOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = TaggedOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = TaggedOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="model", api_key="key")
    reply = await agent.chat(
        [{"role": "user", "content": "hello"}],
        character_id="hazel",
    )

    assert reply == ChatAgentReply()
    assert len(agent._client.chat.calls) == 3


async def test_openrouter_agent_chat_can_allow_tagged_prose_for_debugging(monkeypatch):
    malformed = '<invoke name="wait"></invoke>'

    class TaggedOpenRouterChat(_FakeOpenRouterChat):
        async def send_async(self, *, model, messages, tools):
            self.calls.append(
                {"model": model, "messages": [dict(m) for m in messages], "tools": tools}
            )
            return _fake_openrouter_response(
                content=malformed,
                include_tool_call=False,
            )

    class TaggedOpenRouterClient(_FakeOpenRouterClient):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = TaggedOpenRouterChat()

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = TaggedOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(
        model="model",
        api_key="key",
        reject_text_tool_calls=False,
    )
    reply = await agent.chat(
        [{"role": "user", "content": "hello"}],
        character_id="hazel",
    )

    assert reply == ChatAgentReply(content=malformed)
    assert len(agent._client.chat.calls) == 1


async def test_openrouter_agent_chat_returns_empty_after_transient_provider_retries(monkeypatch):
    class AlwaysFailOpenRouterChat(_FlakyOpenRouterChat):
        failures = 99

    class AlwaysFailOpenRouterClient(_FlakyOpenRouterClient):
        chat_type = AlwaysFailOpenRouterChat

    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = AlwaysFailOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(model="openai/gpt-4.1-mini", api_key="key", retry_delay_seconds=0)
    reply = await agent.chat([], character_id="hazel")

    assert reply.content == ""
    assert reply.tool_call is None
    assert len(agent._client.chat.calls) == 3


async def test_dispatch_records_wait_when_agent_passes():
    scenario = build_scenario()
    builder = PromptBuilder(scenario.actor.world)
    dispatch = ControllerDispatch(scenario.actor, builder, ScriptedAgent([]))

    assert await dispatch.run_once() == []
    decisions = await dispatch.await_pending()

    assert len(decisions) == 1
    assert decisions[0].tool is None
    assert scenario.actor._inbox.empty()


async def test_dispatch_rejects_invalid_agent_response_and_returns_feedback():
    class RecoveringAgent:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def decide(
            self, prompt, context, *, character_id, model=None, provider=None, tools=None
        ):
            del context, character_id, model, provider, tools
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return InvalidAgentResponse(
                    reason="provider response contained no structured tool call",
                    feedback=(
                        "Invalid action response: no structured tool call was returned. "
                        "Return the action through assistant.tool_calls."
                    ),
                )
            return ToolCall("wait", {})

    scenario = build_scenario()
    agent = RecoveringAgent()
    dispatch = ControllerDispatch(
        scenario.actor, PromptBuilder(scenario.actor.world), agent
    )

    assert await dispatch.run_once() == []
    first = await dispatch.await_pending()
    assert first[0].receipt_status == "policy_rejected"
    assert first[0].policy_rejections == ("invalid_agent_response",)
    assert "no structured tool call" in first[0].receipt_reason
    assert scenario.actor._inbox.empty()

    assert await dispatch.run_once() == []
    second = await dispatch.await_pending()
    assert second[0].tool == "wait"
    assert "Invalid action response" in agent.prompts[1]


async def test_dispatch_skips_a_character_despawned_mid_run():
    from bunnyland.core import (
        ActionPointsComponent,
        FocusPointsComponent,
        InitiativeComponent,
        LLMControllerComponent,
    )

    scenario = build_scenario()
    world = scenario.actor.world

    # A second LLM-controlled, actable character sharing the room.
    other = spawn_entity(
        world,
        [
            IdentityComponent(name="Bramble", kind="character"),
            CharacterComponent(species="bunny"),
            ActionPointsComponent(current=5.0, maximum=5.0, regen_per_hour=1.0),
            FocusPointsComponent(current=3.0, maximum=3.0, regen_per_hour=0.5),
            InitiativeComponent(score=1.0),
        ],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), other.id
    )
    other_controller = spawn_entity(
        world, [LLMControllerComponent(profile_name="default", model="claude")]
    )
    scenario.actor.assign_controller(other.id, other_controller.id)

    # Deciding for the first actable character despawns the second, mimicking an entity
    # removed mid-loop (e.g. by another actor's tick) before its prompt is built.
    class DespawningAgent:
        def __init__(self) -> None:
            self.decided: list[str] = []

        async def decide(
            self, prompt, context, *, character_id, model=None, provider=None, tools=None
        ):
            del prompt, context, model, provider, tools
            self.decided.append(character_id)
            for cid in (scenario.character, other.id):
                if str(cid) != character_id and world.has_entity(cid):
                    world.remove(cid)
            return None

    agent = DespawningAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(world), agent)

    # The despawned character is skipped rather than crashing the loop.
    assert await dispatch.run_once() == []
    decisions = await dispatch.await_pending()

    assert len(agent.decided) == 1
    assert len(decisions) == 2
    assert any(decision.character_id == agent.decided[0] for decision in decisions)
    assert any("removed" in decision.summary for decision in decisions)


async def test_dispatch_follows_live_world_replacement():
    # Regenerating the world on a running server swaps actor.world for a brand-new World
    # object (admin.generate_replacement_world). The dispatch reads the live actor.world to
    # pick actable characters, but the builder captured the old world at construction, so
    # building a new-world character's prompt would dereference its id against the stale,
    # replaced world and crash the game loop. The builder must follow the swap.
    from relics import World

    new = build_scenario()
    # The builder holds a different (old) world that does not contain the live character,
    # exactly as after a regeneration swaps actor.world for a brand-new World.
    stale_world = World()
    assert not stale_world.has_entity(new.character)
    builder = PromptBuilder(stale_world)
    dispatch = ControllerDispatch(new.actor, builder, ScriptedAgent([]))

    assert await dispatch.run_once() == []
    decisions = await dispatch.await_pending()

    # The builder followed the swap and drove the live character (waits, here) instead of
    # crashing on build against the stale world.
    assert builder.world is new.actor.world
    assert len(decisions) == 1
    assert decisions[0].character_id == str(new.character)
    assert decisions[0].tool is None


async def test_dispatch_skips_a_character_removed_during_build():
    # A character can pass the has_entity guard and then be removed before its prompt is
    # built (an in-place admin patch or player interaction at an await point). The per-
    # character EntityNotFoundError guard must skip it instead of crashing the loop.
    scenario = build_scenario()

    class LosingBuilder(PromptBuilder):
        def build(self, character_id, *, epoch=0):
            self.world.remove(character_id)
            return super().build(character_id, epoch=epoch)

    dispatch = ControllerDispatch(
        scenario.actor, LosingBuilder(scenario.actor.world), ScriptedAgent([])
    )

    decisions = await dispatch.run_once()

    assert decisions == []
    assert not scenario.actor.world.has_entity(scenario.character)


async def test_dispatch_uses_controller_model_for_character_decision():
    scenario = build_scenario()
    agent = _RecordingAgent([])
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    await dispatch.run_once()
    await dispatch.await_pending()

    assert agent.models == ["claude"]


async def test_dispatch_uses_controller_provider_for_character_decision():
    from dataclasses import replace

    from bunnyland.core import replace_component
    from bunnyland.core.controllers import LLMControllerComponent

    scenario = build_scenario()
    controller = scenario.actor.world.get_entity(scenario.controller)
    replace_component(
        controller,
        replace(controller.get_component(LLMControllerComponent), provider="openrouter"),
    )
    agent = _RecordingAgent([])
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    await dispatch.run_once()
    await dispatch.await_pending()

    assert agent.providers == ["openrouter"]


async def test_dispatch_populates_structured_memory_for_autonomy_and_telemetry():
    from bunnyland.core import MemoryProfileComponent

    scenario = build_scenario()
    character = scenario.actor.world.get_entity(scenario.character)
    character.add_component(MemoryProfileComponent(vector_collection="juniper-private"))
    store = install_memory(scenario.actor, InMemoryStore())
    configure_memory_recall(scenario.actor, limit=2, min_score=0.0)
    memory = store.add(
        "juniper-private",
        text="The Mosslit Burrow north tunnel is the goal.",
        source="manual",
    )

    class ContextAgent:
        contexts: list[PromptContext] = []

        async def decide(
            self, prompt, context, *, character_id, model=None, provider=None, tools=None
        ):
            del prompt, character_id, model, provider, tools
            self.contexts.append(context)
            return None

    agent = ContextAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    await dispatch.run_once()
    decisions = await dispatch.await_pending()

    assert agent.contexts[0].notes
    assert agent.contexts[0].recall
    assert decisions[0].memory_ids == (memory.id,)


async def test_dispatch_omits_private_memory_when_recall_backend_fails(caplog):
    from bunnyland.core import MemoryProfileComponent

    class FailingStore(InMemoryStore):
        def search(self, collection, *, query=None, mode="recent", limit=5):
            del collection, query, mode, limit
            raise RuntimeError("private backend detail")

    class ContextAgent:
        contexts: list[PromptContext] = []

        async def decide(
            self, prompt, context, *, character_id, model=None, provider=None, tools=None
        ):
            del prompt, character_id, model, provider, tools
            self.contexts.append(context)
            return None

    scenario = build_scenario()
    scenario.actor.world.get_entity(scenario.character).add_component(
        MemoryProfileComponent(vector_collection="juniper-private")
    )
    install_memory(scenario.actor, FailingStore())
    configure_memory_recall(scenario.actor, limit=2, min_score=0.0)
    agent = ContextAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    await dispatch.run_once()
    decisions = await dispatch.await_pending()

    assert decisions[0].tool is None
    assert agent.contexts[0].notes == ()
    assert agent.contexts[0].recall == ()
    assert "memory context failed" in caplog.text


async def test_dispatch_times_out_and_cancels_a_stuck_provider_decision():
    cancelled = asyncio.Event()

    class StuckAgent:
        async def decide(
            self, prompt, context, *, character_id, model=None, provider=None, tools=None
        ):
            del prompt, context, character_id, model, provider, tools
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    scenario = build_scenario()
    dispatch = ControllerDispatch(
        scenario.actor,
        PromptBuilder(scenario.actor.world),
        StuckAgent(),
        interactive_decision_timeout_seconds=0.01,
    )

    await dispatch.run_once()
    decisions = await dispatch.await_pending()

    assert cancelled.is_set()
    assert decisions[0].summary == "error: decision timed out"
    assert decisions[0].policy_rejections == ("decision_timeout",)


async def test_dispatch_deadline_includes_semaphore_admission():
    class ContextAgent:
        def __init__(self):
            self.contexts = []

        async def decide(
            self, prompt, context, *, character_id, model=None, provider=None, tools=None
        ):
            del prompt, character_id, model, provider, tools
            self.contexts.append(context)
            return None

    scenario = build_scenario()
    agent = ContextAgent()
    dispatch = ControllerDispatch(
        scenario.actor,
        PromptBuilder(scenario.actor.world),
        agent,
        interactive_decision_timeout_seconds=0.01,
        max_concurrent_llm_decisions=1,
    )

    await dispatch.run_once()
    await dispatch.await_pending()
    await scenario.actor.bus.publish(
        _PromptMessageEvent(
            **event_base(
                scenario.actor.epoch,
                event_id="retry-after-admission-timeout",
                visibility=EventVisibility.ROOM,
                room_id=str(scenario.room_a),
            ),
            message="Keep this event while provider capacity is unavailable.",
        )
    )
    await dispatch._llm_decision_slots.acquire()

    await dispatch.run_once()
    decisions = await dispatch.await_pending()

    assert decisions[0].summary == "error: decision timed out"
    assert decisions[0].policy_rejections == ("decision_timeout",)

    dispatch._llm_decision_slots.release()
    await dispatch.run_once()
    await dispatch.await_pending()

    assert [event.event_id for event in agent.contexts[-1].perceived_events] == [
        "retry-after-admission-timeout"
    ]


def test_llm_controller_rejects_unknown_tool_policy():
    with pytest.raises(ValueError, match="tool_policy"):
        LLMControllerComponent(
            profile_name="invalid",
            model="model",
            tool_policy="unrestricted",
        )


def test_persona_contradiction_guard_flags_name_relationship_and_status_claims():
    scenario = build_scenario()
    world = scenario.actor.world
    character = world.get_entity(scenario.character)
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    character.add_relationship(SocialBond(affinity=0.5, familiarity=0.5), hazel.id)
    builder = PromptBuilder(
        world,
        persona_providers=collect_persona_fragments(bunnyland_plugins()),
    )
    context = builder.build(scenario.character)

    issues = persona_contradictions(
        context,
        ToolCall(
            "say",
            {
                "text": "My name is Hazel. I am not fond of Hazel. I am dead.",
            },
        ),
    )

    assert "name contradiction: claimed to be Hazel" in issues
    assert "relationship contradiction: denied bond with Hazel" in issues
    assert "impossible self-claim: claimed dead" in issues


def test_persona_contradiction_guard_covers_relationship_line_shapes():
    scenario = build_scenario()
    context = PromptBuilder(scenario.actor.world).build(scenario.character)
    context = replace(
        context,
        persona=(
            *context.persona,
            "Hazel is your friend.",
            "You are partners with Hazel.",
            "You know Hazel.",
        ),
    )

    issues = persona_contradictions(
        context,
        ToolCall(
            "say",
            {"text": ("Hazel is not my friend. I am not partners with Hazel. I don't know Hazel.")},
        ),
    )

    assert "relationship contradiction: denied Hazel's friend status" in issues
    assert "relationship contradiction: denied partnership with Hazel" in issues
    assert "relationship contradiction: denied bond with Hazel" in issues
    assert persona_contradictions(context, ToolCall("wait", {})) == ()


def test_persona_contradiction_guard_ignores_consistent_relationship_speech():
    # Persona declares relationship, partnership, and bond lines, but the spoken text
    # affirms them rather than denying — every relationship branch matches its regex yet
    # appends no issue (the no-contradiction arc of each shape).
    scenario = build_scenario()
    context = PromptBuilder(scenario.actor.world).build(scenario.character)
    context = replace(
        context,
        persona=(
            *context.persona,
            "Hazel is your friend.",
            "You are partners with Hazel.",
            "You know Hazel.",
        ),
    )

    issues = persona_contradictions(
        context,
        ToolCall(
            "say",
            {"text": "Hazel is my friend, my partner, and someone I know well."},
        ),
    )

    assert issues == ()


async def test_dispatch_flags_persona_contradiction_without_blocking_valid_action():
    scenario = build_scenario()
    scenario.actor.register_handler(SayHandler())
    hazel = spawn_entity(
        scenario.actor.world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    scenario.actor.world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    builder = PromptBuilder(
        scenario.actor.world,
        persona_providers=collect_persona_fragments(bunnyland_plugins()),
    )
    agent = ScriptedAgent([ToolCall("say", {"text": "I am Hazel."})])
    dispatch = ControllerDispatch(scenario.actor, builder, agent)

    assert await dispatch.run_once() == []
    decisions = await dispatch.await_pending()

    assert decisions[0].tool == "say"
    assert decisions[0].persona_issues == ("name contradiction: claimed to be Hazel",)
    assert not scenario.actor._inbox.empty()


async def test_openrouter_agent_passes_server_url_to_client(monkeypatch):
    fake_module = types.ModuleType("openrouter")
    fake_module.OpenRouter = _FakeOpenRouterClient
    monkeypatch.setitem(sys.modules, "openrouter", fake_module)

    agent = OpenRouterAgent(
        model="openai/gpt-4.1-mini",
        api_key="key",
        server_url="https://router.example",
    )

    assert agent._client.kwargs == {
        "api_key": "key",
        "server_url": "https://router.example",
        "async_client": agent._client.kwargs["async_client"],
    }
    await agent._client.kwargs["async_client"].aclose()


async def test_provider_router_raises_for_unknown_provider():
    router = ProviderRouterAgent({"ollama": _RecordingAgent([])})

    with pytest.raises(RuntimeError, match="no LLM agent configured for provider 'mystery'"):
        await router.decide("prompt", None, character_id="hazel", provider="mystery")


async def test_provider_router_chat_uses_selected_agent():
    class ChatAgent:
        def __init__(self):
            self.calls = []

        async def chat(self, messages, *, character_id, model=None, provider=None, tools=None):
            self.calls.append((messages, character_id, model, provider, tools))
            return types.SimpleNamespace(content="hello", tool_call=None)

    ollama = ChatAgent()
    openrouter = ChatAgent()
    router = ProviderRouterAgent({"ollama": ollama, "openrouter": openrouter})

    reply = await router.chat(
        [{"role": "user", "content": "hi"}],
        character_id="hazel",
        model="model",
        provider="openrouter",
        tools=[],
    )

    assert reply.content == "hello"
    assert ollama.calls == []
    assert openrouter.calls[0][1:] == ("hazel", "model", "openrouter", [])


async def test_provider_router_chat_rejects_unknown_or_unsupported_provider():
    router = ProviderRouterAgent({"ollama": _RecordingAgent([])})

    with pytest.raises(RuntimeError, match="no LLM agent configured"):
        await router.chat([], character_id="hazel", provider="mystery")
    with pytest.raises(RuntimeError, match="does not support character chat"):
        await router.chat([], character_id="hazel")


async def test_provider_retry_helper_sleeps_between_retries(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    attempts = 0

    async def request():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("transient")
        return "ok"

    result = await _call_provider_with_retries(
        "test-provider", request, max_retries=2, retry_delay_seconds=0.25
    )

    assert result == "ok"
    assert sleeps == [0.25]


async def test_provider_router_uses_selected_agent():
    ollama = _RecordingAgent([])
    openrouter = _RecordingAgent([ToolCall("wait", {})])
    router = ProviderRouterAgent({"ollama": ollama, "openrouter": openrouter})

    call = await router.decide(
        "prompt",
        None,
        character_id="hazel",
        model="openai/gpt-4.1-mini",
        provider="openrouter",
    )

    assert call == ToolCall("wait", {})
    assert ollama.prompts == []
    assert openrouter.models == ["openai/gpt-4.1-mini"]


def test_resolve_reference_matches_names_case_insensitively():
    scenario = build_scenario()
    world = scenario.actor.world
    journal = _add_item(scenario, "marsh journal")
    basket = _add_item(scenario, "woven basket", container=True)
    character = world.get_entity(scenario.character)
    candidates = name_candidates(world, character)

    # prefix match: "Mar" -> "marsh journal"
    assert resolve_reference("Mar", candidates, world=world) == str(journal)
    # exact, case-insensitive
    assert resolve_reference("WOVEN BASKET", candidates, world=world) == str(basket)
    # adjacent room resolved by title ("North Tunnel")
    assert resolve_reference("North", candidates, world=world) == str(scenario.room_b)
    # an already-valid id passes through untouched
    assert resolve_reference(str(journal), candidates, world=world) == str(journal)
    # no match -> returned unchanged so the handler rejects it observably
    assert resolve_reference("dragon", candidates, world=world) == "dragon"
    # blank/whitespace-only query -> returned unchanged without matching anything
    assert resolve_reference("   ", candidates, world=world) == "   "
    assert resolve_reference("", candidates, world=world) == ""


def test_name_candidates_skips_roomless_and_unnameable_entities():
    scenario = build_scenario()
    world = scenario.actor.world
    character = world.get_entity(scenario.character)

    # An entity in the room with neither an identity nor a room component is reachable but
    # contributes no candidate (the neither-name-nor-title branch).
    blank = spawn_entity(world, [PortableComponent(can_pick_up=True)])
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), blank.id
    )
    journal = _add_item(scenario, "marsh journal")
    candidates = name_candidates(world, character)
    candidate_ids = {entity_id for _name, entity_id in candidates}
    assert journal in candidate_ids
    assert blank not in candidate_ids

    # A roomless character (no containing room) skips the exit-expansion block entirely and
    # still yields no crash — only its own inventory would appear, here empty.
    loner = spawn_entity(
        world, [CharacterComponent(), IdentityComponent(name="Loner", kind="character")]
    )
    assert container_of(world.get_entity(loner.id)) is None
    assert name_candidates(world, world.get_entity(loner.id)) == []


def test_resolve_reference_args_reports_unresolved_with_suggestions():
    scenario = build_scenario()
    world = scenario.actor.world
    journal = _add_item(scenario, "marsh journal")
    _add_item(scenario, "woven basket", container=True)
    character = world.get_entity(scenario.character)

    resolved, unresolved = resolve_reference_args(
        world, character, {"item_id": "Mar", "target_container_id": "basket"}
    )
    # "Mar" resolves to the journal id; "basket" does not prefix-match anything.
    assert resolved["item_id"] == str(journal)
    assert "item_id" not in unresolved
    assert "woven basket" in unresolved["target_container_id"]


def test_suggest_names_prefers_substring_then_fuzzy():
    candidates = [("woven basket", None), ("marsh journal", None)]
    assert suggest_names("basket", candidates) == ["woven basket"]  # substring
    assert "woven basket" in suggest_names("woven baskt", candidates)  # fuzzy typo
    assert suggest_names("dragon", candidates) == []  # nothing nearby


def test_resolve_reference_args_drops_blank_optional_sdk_arguments():
    scenario = build_scenario()
    character = scenario.actor.world.get_entity(scenario.character)

    resolved, unresolved = resolve_reference_args(
        scenario.actor.world,
        character,
        {"direction": "north", "exit_id": ""},
    )

    assert resolved == {"direction": "north"}
    assert unresolved == {}


def test_did_you_mean_message():
    msg = did_you_mean({"item_id": "baskt"}, {"item_id": ["woven basket"]})
    assert "did you mean" in msg.lower() and "woven basket" in msg
    empty = did_you_mean({"target_id": "ghost"}, {"target_id": []})
    assert "nothing" in empty.lower()


class _RecordingAgent:
    """Records the prompts it is shown and replays a fixed list of calls."""

    def __init__(self, calls):
        self.calls = list(calls)
        self.prompts: list[str] = []
        self.models: list[str | None] = []
        self.providers: list[str | None] = []
        self.tools: list[list[dict] | None] = []
        self._index = 0

    async def decide(
        self, prompt, context, *, character_id, model=None, provider=None, tools=None
    ):
        self.prompts.append(prompt)
        self.models.append(model)
        self.providers.append(provider)
        self.tools.append(tools)
        if self._index >= len(self.calls):
            return None
        call = self.calls[self._index]
        self._index += 1
        return call


async def test_dispatch_passes_registry_tools_and_state_examples_to_agent():
    scenario = build_scenario()
    agent = _RecordingAgent([None])
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    await dispatch.run_once()
    await dispatch.await_pending()

    schemas = {
        schema["function"]["name"]: schema["function"] for schema in agent.tools[0] or ()
    }
    assert {"move", "wait"} <= schemas.keys()
    assert "practice_skill" not in schemas
    assert "accept_quest" not in schemas
    assert DISCOVER_ACTION_TOOL in schemas
    discoverable = schemas[DISCOVER_ACTION_TOOL]["parameters"]["properties"]["action_name"][
        "enum"
    ]
    assert {"practice_skill", "accept_quest"} <= set(discoverable)
    assert schemas["move"] == next(
        definition.tool_schema()["function"]
        for definition in scenario.actor.action_definitions()
        if definition.name == "move"
    )
    assert schemas["wait"] == next(
        definition.tool_schema()["function"]
        for definition in scenario.actor.action_definitions()
        if definition.name == "wait"
    )
    assert "move north" in agent.prompts[0]


def test_contextual_action_definitions_add_owner_for_live_character_state():
    from bunnyland.simpacks.lifesim.mechanics import SkillSetComponent

    scenario = build_scenario()
    character = scenario.actor.world.get_entity(scenario.character)
    baseline = {
        definition.name
        for definition in contextual_action_definitions(scenario.actor, character)
    }

    assert {"move", "wait", "take_note"} <= baseline
    assert "practice_skill" not in baseline

    character.add_component(SkillSetComponent(levels={"foraging": 1}))
    expanded = {
        definition.name
        for definition in contextual_action_definitions(scenario.actor, character)
    }

    assert "practice_skill" in expanded
    assert len(expanded) > len(baseline)

    scenario.actor.world.get_entity(scenario.room_a).remove_relationship(
        Contains, scenario.character
    )
    uncontained = {
        definition.name
        for definition in contextual_action_definitions(scenario.actor, character)
    }
    assert {"move", "wait", "take_note"} <= uncontained


def test_contextual_action_definitions_preserve_manual_actor_catalogue():
    from bunnyland.core import WorldActor

    actor = WorldActor()
    definition = ActionDefinition(command_type="wave", tool_name="wave")
    actor.register_action_definition(definition)
    character = spawn_entity(actor.world, [CharacterComponent()])

    assert contextual_action_definitions(actor, character) == (definition,)


def test_action_discovery_schema_is_typed_and_omitted_when_empty():
    definition = ActionDefinition(command_type="practice-skill", tool_name="practice_skill")

    assert action_discovery_schema(()) is None
    schema = action_discovery_schema((definition,))["function"]
    assert schema["name"] == DISCOVER_ACTION_TOOL
    assert schema["parameters"]["required"] == ["action_name"]
    assert schema["parameters"]["properties"]["action_name"]["enum"] == ["practice_skill"]


async def test_dispatch_progressively_exposes_discovered_native_action_schema():
    scenario = build_scenario()
    agent = _RecordingAgent(
        [ToolCall(DISCOVER_ACTION_TOOL, {"action_name": "practice_skill"}), None]
    )
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    assert await dispatch.run_once() == []
    first = await dispatch.await_pending()
    assert first[0].summary == "discovered practice_skill"
    assert scenario.actor._inbox.empty()

    await dispatch.run_once()
    await dispatch.await_pending()
    second_tools = {
        schema["function"]["name"]: schema["function"] for schema in agent.tools[1] or ()
    }
    assert "practice_skill" in second_tools
    assert "practice_skill tool is now available" in agent.prompts[1]
    assert "practice_skill" not in second_tools[DISCOVER_ACTION_TOOL]["parameters"]["properties"][
        "action_name"
    ]["enum"]


async def test_dispatch_rejects_invalid_action_discovery_without_submission():
    scenario = build_scenario()
    agent = _RecordingAgent([ToolCall(DISCOVER_ACTION_TOOL, {"action_name": "not_real"})])
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    assert await dispatch.run_once() == []
    decisions = await dispatch.await_pending()

    assert decisions[0].summary == "invalid action discovery"
    assert scenario.actor._inbox.empty()


async def test_dispatch_feeds_did_you_mean_back_to_the_agent():
    # An LLM agent that names something unreachable gets the same guidance a human would,
    # surfaced as a warning on its next prompt — and the doomed command is never submitted.
    scenario = build_scenario()
    _add_item(scenario, "woven basket", container=True)
    agent = _RecordingAgent([ToolCall("take", {"item_id": "basket"}), None])
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    assert await dispatch.run_once() == []
    first = await dispatch.await_pending()
    assert first[0].tool == "take"
    assert "did you mean" in first[0].summary.lower()
    assert scenario.actor._inbox.empty()  # nothing submitted

    await dispatch.run_once()  # second turn carries the feedback as a prompt warning
    await dispatch.await_pending()
    assert "woven basket" in agent.prompts[1]


async def test_dispatch_resolves_item_names_to_ids_before_submitting():
    scenario = build_scenario()
    scenario.actor.register_handler(TakeHandler())
    world = scenario.actor.world
    journal = _add_item(scenario, "marsh journal")

    builder = PromptBuilder(world)
    agent = ScriptedAgent([ToolCall("take", {"item_id": "Mar"})])
    dispatch = ControllerDispatch(scenario.actor, builder, agent)

    await dispatch.run_once()
    await dispatch.await_pending()
    await scenario.actor.tick(3600.0)

    # "Mar" resolved to the journal, which is now in the character's inventory.
    assert container_of(world.get_entity(journal)) == scenario.character


async def test_dispatch_rejects_unknown_agent_tools_without_crashing():
    scenario = build_scenario()
    agent = _RecordingAgent([ToolCall("read", {}), None])
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    assert await dispatch.run_once() == []
    first = await dispatch.await_pending()

    assert first[0].tool == "read"
    assert "unknown tool" in first[0].summary
    assert scenario.actor._inbox.empty()

    await dispatch.run_once()
    await dispatch.await_pending()
    assert "Choose one of the available tools exactly as named" in agent.prompts[1]


class _GatedAsyncAgent:
    """Async agent whose decision blocks on an event until released.

    Records the character ids and contexts it was asked about so tests can prove a
    character is not re-prompted while pending and that a later prompt is rebuilt fresh.
    """

    def __init__(self, call: ToolCall | None = None) -> None:
        self.gate = asyncio.Event()
        self.prompts: list[str] = []
        self.contexts: list[object] = []
        self._call = call if call is not None else ToolCall("move", {"direction": "north"})

    async def decide(
        self, prompt, context, *, character_id, model=None, provider=None, tools=None
    ):
        del prompt, model, provider, tools
        self.prompts.append(character_id)
        self.contexts.append(context)

        await self.gate.wait()
        return self._call


class _ConcurrencyProbeAgent:
    """Async agent that records how many of its decisions run provider work at once."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def decide(
        self, prompt, context, *, character_id, model=None, provider=None, tools=None
    ):
        del prompt, context, character_id, model, provider, tools

        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await self.gate.wait()
        self.active -= 1
        return None


class _FailingAsyncAgent:
    async def decide(
        self, prompt, context, *, character_id, model=None, provider=None, tools=None
    ):
        del prompt, context, character_id, model, provider, tools

        raise RuntimeError("provider exploded")


class _FailSecondAgent:
    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.contexts: list[object] = []

    async def decide(
        self, prompt, context, *, character_id, model=None, provider=None, tools=None
    ):
        del prompt, character_id, model, provider, tools
        self.contexts.append(context)
        if len(self.contexts) == 1:
            await self.gate.wait()
            return None
        if len(self.contexts) == 2:
            raise RuntimeError("transient provider failure")
        return None


def _add_second_llm_character(scenario, name: str = "Bramble"):
    from bunnyland.core import (
        ActionPointsComponent,
        FocusPointsComponent,
        InitiativeComponent,
        LLMControllerComponent,
    )

    world = scenario.actor.world
    other = spawn_entity(
        world,
        [
            IdentityComponent(name=name, kind="character"),
            CharacterComponent(species="bunny"),
            ActionPointsComponent(current=5.0, maximum=5.0, regen_per_hour=1.0),
            FocusPointsComponent(current=3.0, maximum=3.0, regen_per_hour=0.5),
            InitiativeComponent(score=1.0),
        ],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), other.id
    )
    other_controller = spawn_entity(
        world, [LLMControllerComponent(profile_name="default", model="claude")]
    )
    scenario.actor.assign_controller(other.id, other_controller.id)
    return other.id


async def test_dispatch_runs_async_decision_in_background_without_blocking():
    scenario = build_scenario()
    agent = _GatedAsyncAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    # The slow prompt is handed to a background task: run_once returns immediately with no
    # decision yet, and nothing is submitted while the agent is still thinking.
    assert await dispatch.run_once() == []
    await asyncio.sleep(0)  # let the background task start and block on the gate
    assert agent.prompts == [str(scenario.character)]
    assert scenario.actor._inbox.empty()

    # Once the provider responds, the decision is finalized and surfaced.
    agent.gate.set()
    decisions = await dispatch.await_pending()
    assert [decision.tool for decision in decisions] == ["move"]
    assert not scenario.actor._inbox.empty()


async def test_dispatch_does_not_reprompt_a_character_with_a_pending_decision():
    scenario = build_scenario()
    agent = _GatedAsyncAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    assert await dispatch.run_once() == []
    await asyncio.sleep(0)
    assert agent.prompts == [str(scenario.character)]

    # Several more passes while the first decision is still pending must not re-prompt it.
    assert await dispatch.run_once() == []
    assert await dispatch.run_once() == []
    assert agent.prompts == [str(scenario.character)]

    dispatch.cancel_pending()
    assert scenario.actor._inbox.empty()


async def test_await_pending_returns_empty_when_no_decisions_are_in_flight():
    scenario = build_scenario()
    dispatch = ControllerDispatch(
        scenario.actor, PromptBuilder(scenario.actor.world), ScriptedAgent([])
    )

    # Nothing has been scheduled, so the no-tasks arc returns immediately with no decisions
    # and never reaches asyncio.gather.
    assert dispatch._inflight == {}
    assert await dispatch.await_pending() == []


async def test_dispatch_rebuilds_prompt_from_latest_state_after_a_response():
    scenario = build_scenario()
    agent = _GatedAsyncAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    # First prompt is built from the starting room, then re-prompts are coalesced away.
    assert await dispatch.run_once() == []
    await asyncio.sleep(0)
    assert await dispatch.run_once() == []  # suppressed while pending
    assert len(agent.prompts) == 1
    assert agent.contexts[0].location_title == "Mosslit Burrow"

    # Let the decision land and execute, moving the character to the north tunnel.
    agent.gate.set()
    await dispatch.await_pending()
    agent.gate.clear()
    await scenario.actor.tick(1.0)  # executes the queued move
    assert scenario.character_room() == scenario.room_b

    # The next pass delivers exactly one fresh prompt reflecting the most recent state.
    assert await dispatch.run_once() == []
    await asyncio.sleep(0)
    assert len(agent.prompts) == 2
    assert agent.contexts[1].location_title == "North Tunnel"

    dispatch.cancel_pending()


async def test_dispatch_allows_concurrent_llm_calls_for_different_characters():
    scenario = build_scenario()
    _add_second_llm_character(scenario)
    agent = _ConcurrencyProbeAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    # Character locks are independent: both provider calls may run at once.
    assert await dispatch.run_once() == []
    for _ in range(8):
        await asyncio.sleep(0)
    assert agent.active == 2
    assert agent.max_active == 2

    agent.gate.set()
    await dispatch.await_pending()
    assert agent.max_active == 2


async def test_dispatch_coalesces_latest_projection_and_preserves_perceived_events():
    scenario = build_scenario()
    agent = _GatedAsyncAgent(call=None)
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    assert await dispatch.run_once() == []
    await asyncio.sleep(0)
    character = scenario.actor.world.get_entity(scenario.character)
    points = character.get_component(ActionPointsComponent)

    replace_component(character, replace(points, current=4.0))
    await scenario.actor.bus.publish(
        _PromptMessageEvent(
            **event_base(
                scenario.actor.epoch,
                event_id="visible-one",
                visibility=EventVisibility.ROOM,
                room_id=str(scenario.room_a),
            ),
            message="You hear an orchard gate creak.",
        )
    )
    assert await dispatch.run_once() == []

    scenario.actor.world.get_entity(scenario.room_a).remove_relationship(
        Contains, scenario.character
    )
    scenario.actor.world.get_entity(scenario.room_b).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), scenario.character
    )
    replace_component(character, replace(points, current=3.0))
    await scenario.actor.bus.publish(
        _PromptMessageEvent(
            **event_base(
                scenario.actor.epoch,
                event_id="visible-two",
                visibility=EventVisibility.ROOM,
                room_id=str(scenario.room_b),
            ),
            message="A breeze follows you into the tunnel.",
        )
    )
    await scenario.actor.bus.publish(
        _PromptMessageEvent(
            **event_base(
                scenario.actor.epoch,
                event_id="visible-message",
                visibility=EventVisibility.ROOM,
                room_id=str(scenario.room_b),
            ),
            message="A bell rings nearby.",
        )
    )
    await scenario.actor.bus.publish(
        DomainEvent(
            **event_base(
                scenario.actor.epoch,
                event_id="private-other",
                visibility=EventVisibility.PRIVATE,
                actor_id="somebody-else",
                room_id=str(scenario.room_b),
            )
        )
    )
    assert await dispatch.run_once() == []
    assert len(agent.contexts) == 1

    agent.gate.set()
    await dispatch.await_pending()
    assert await dispatch.run_once() == []
    await asyncio.sleep(0)

    assert len(agent.contexts) == 2
    latest = agent.contexts[1]
    assert latest.action == (3.0, 5.0)
    assert latest.location_title == "North Tunnel"
    assert [event.event_id for event in latest.perceived_events] == [
        "visible-one",
        "visible-two",
        "visible-message",
    ]
    assert latest.perceived_events[-1].summary == "A bell rings nearby."
    dispatch.cancel_pending()


async def test_dispatch_sends_one_latest_unchanged_turn_after_response():
    scenario = build_scenario()
    agent = _GatedAsyncAgent(call=None)
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    assert await dispatch.run_once() == []
    agent.gate.set()
    await dispatch.await_pending()

    assert await dispatch.run_once() == []
    await asyncio.sleep(0)
    assert len(agent.contexts) == 2


async def test_dispatch_reports_perceived_event_buffer_overflow():
    scenario = build_scenario()
    agent = _GatedAsyncAgent(call=None)
    dispatch = ControllerDispatch(
        scenario.actor,
        PromptBuilder(scenario.actor.world),
        agent,
        max_buffered_events=2,
    )

    await dispatch.run_once()
    await asyncio.sleep(0)
    for index in range(3):
        await scenario.actor.bus.publish(
            _PromptMessageEvent(
                **event_base(
                    scenario.actor.epoch + index,
                    event_id=f"visible-{index}",
                    visibility=EventVisibility.ROOM,
                    room_id=str(scenario.room_a),
                ),
                message=f"Visible event {index}.",
            )
        )
    await dispatch.run_once()
    agent.gate.set()
    await dispatch.await_pending()
    await dispatch.run_once()
    await asyncio.sleep(0)

    latest = agent.contexts[1]
    assert [event.event_id for event in latest.perceived_events] == ["visible-1", "visible-2"]
    assert latest.omitted_perceived_events == 1
    assert latest.omitted_event_epoch_range == (scenario.actor.epoch, scenario.actor.epoch)


async def test_dispatch_requeues_prompt_events_after_provider_failure():
    scenario = build_scenario()
    agent = _FailSecondAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    await dispatch.run_once()
    await asyncio.sleep(0)
    await scenario.actor.bus.publish(
        _PromptMessageEvent(
            **event_base(
                scenario.actor.epoch,
                event_id="retry-me",
                visibility=EventVisibility.ROOM,
                room_id=str(scenario.room_a),
            ),
            message="This event should be retried.",
        )
    )
    await dispatch.run_once()
    agent.gate.set()
    await dispatch.await_pending()

    await dispatch.run_once()
    failed = await dispatch.await_pending()
    assert failed[-1].summary == "error"
    assert failed[-1].prompt_event_ids == ("retry-me",)

    await dispatch.run_once()
    await dispatch.await_pending()
    assert [event.event_id for event in agent.contexts[2].perceived_events] == ["retry-me"]


async def test_dispatch_cancel_restores_events_from_active_prompt():
    scenario = build_scenario()
    agent = _GatedAsyncAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    await dispatch.run_once()
    await asyncio.sleep(0)
    await scenario.actor.bus.publish(
        _PromptMessageEvent(
            **event_base(
                scenario.actor.epoch,
                event_id="active-event",
                visibility=EventVisibility.ROOM,
                room_id=str(scenario.room_a),
            ),
            message="This event is active.",
        )
    )
    await dispatch.run_once()
    agent.gate.set()
    await dispatch.await_pending()
    agent.gate.clear()
    await dispatch.run_once()
    await asyncio.sleep(0)

    dispatch.cancel_pending()

    state = dispatch._character_states[str(scenario.character)]
    assert [event.event_id for event in state.events.events] == ["active-event"]


async def test_dispatch_close_detaches_event_observer_and_skips_stale_state_keys():
    scenario = build_scenario()
    dispatch = ControllerDispatch(
        scenario.actor, PromptBuilder(scenario.actor.world), _GatedAsyncAgent()
    )
    await dispatch.run_once()
    dispatch._state_for("not-an-entity")
    scenario.actor.world.remove(scenario.character)

    await scenario.actor.bus.publish(
        DomainEvent(
            **event_base(
                scenario.actor.epoch,
                event_id="after-removal",
                visibility=EventVisibility.PUBLIC,
            )
        )
    )
    dispatch.close()

    assert dispatch._inflight == {}


async def test_dispatch_surfaces_background_decision_on_a_later_pass():
    scenario = build_scenario()
    agent = _GatedAsyncAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    assert await dispatch.run_once() == []
    agent.gate.set()
    await asyncio.gather(*list(dispatch._inflight.values()))

    # The finished decision is reported by the next run_once, not lost.
    decisions = await dispatch.run_once()
    assert any(decision.tool == "move" for decision in decisions)
    await dispatch.await_pending()  # let the follow-up prompt this pass scheduled finish


async def test_dispatch_records_error_when_an_async_decision_raises():
    scenario = build_scenario()
    dispatch = ControllerDispatch(
        scenario.actor, PromptBuilder(scenario.actor.world), _FailingAsyncAgent()
    )

    assert await dispatch.run_once() == []
    decisions = await dispatch.await_pending()
    assert len(decisions) == 1
    assert decisions[0].tool is None
    assert decisions[0].summary == "error"
    assert scenario.actor._inbox.empty()


async def test_dispatch_skips_async_decision_when_character_removed_midflight():
    scenario = build_scenario()
    world = scenario.actor.world
    agent = _GatedAsyncAgent()
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(world), agent)

    assert await dispatch.run_once() == []
    await asyncio.sleep(0)
    world.remove(scenario.character)  # removed while the agent is still responding
    agent.gate.set()

    decisions = await dispatch.await_pending()
    assert len(decisions) == 1
    assert "removed" in decisions[0].summary
    assert scenario.actor._inbox.empty()


async def test_dispatch_cancel_pending_drops_inflight_decisions():
    scenario = build_scenario()
    agent = _GatedAsyncAgent()  # never released
    dispatch = ControllerDispatch(scenario.actor, PromptBuilder(scenario.actor.world), agent)

    assert await dispatch.run_once() == []
    await asyncio.sleep(0)
    assert dispatch._has_pending(str(scenario.character))

    dispatch.cancel_pending()
    await asyncio.sleep(0)  # let the cancellation propagate
    assert not dispatch._has_pending(str(scenario.character))
    assert scenario.actor._inbox.empty()


def test_ollama_agent_missing_extra_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "ollama", None)
    with pytest.raises(RuntimeError, match="OllamaAgent requires the 'llm' extra"):
        OllamaAgent()


def test_openrouter_agent_missing_extra_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "openrouter", None)
    with pytest.raises(RuntimeError, match="OpenRouterAgent requires the 'llm' extra"):
        OpenRouterAgent()
