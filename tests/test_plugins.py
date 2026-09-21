"""Tests for the plugin system: loading, dependency ordering, and application."""

from __future__ import annotations

import pytest
from conftest import build_scenario

from bunnyland.core import (
    ActionArgument,
    ActionDefinition,
    ActionExample,
    ActionPattern,
    AdminComponent,
    AllowsMembersOf,
    CommandCost,
    Contains,
    ControllerOutboxMessageComponent,
    DiscordControllerComponent,
    HandlerContext,
    HandlerResult,
    Lane,
    MemoryProfileComponent,
    MutationPlan,
    RestingComponent,
    RoomGateComponent,
    SubmittedCommand,
    WorldActor,
    build_submitted_command,
    spawn_entity,
)
from bunnyland.core.consequences import ConversationConsequence
from bunnyland.core.events import (
    CharacterWokeEvent,
    NoteTakenEvent,
    RestEndedEvent,
    RestStartedEvent,
    SleepStartedEvent,
)
from bunnyland.core.handlers import planned
from bunnyland.discord.plugin import bunnyland_plugins as discord_plugins
from bunnyland.foundation.checkpoints.mechanics import SaveCheckpointComponent
from bunnyland.foundation.checkpoints.plugin import plugin as checkpoints_plugin
from bunnyland.foundation.core_verbs.plugin import plugin as core_verbs_plugin
from bunnyland.foundation.environment.mechanics import MoistureComponent, ShelterComponent
from bunnyland.foundation.storyteller.mechanics import IncidentSpawned
from bunnyland.foundation.storyteller.plugin import plugin as storyteller_plugin
from bunnyland.llm_agents.tools import tool_schemas
from bunnyland.memory import configure_memory_recall
from bunnyland.plugins import (
    CommandContribution,
    ContentContribution,
    DependencyContribution,
    EcsContribution,
    Plugin,
    PluginError,
    PluginRegistry,
    PolicyContribution,
    apply_plugins,
    bunnyland_plugins,
    resolve_order,
    select,
)
from bunnyland.plugins.contributions import collect_content_items, collect_ecs_types
from bunnyland.plugins.ids import (
    BARBARIANSIM,
    CHECKPOINTS,
    COLONYSIM,
    CORE_VERBS,
    DAGGERSIM,
    DINOSIM,
    DISCORD,
    DRAGONSIM,
    ENVIRONMENT,
    GARDENSIM,
    HISTORY,
    IMAGEGEN,
    LIFESIM,
    MCP,
    MECHANISMS,
    MEDIA,
    MEMORY,
    NEONSIM,
    NUKESIM,
    PERSONA,
    POLICY,
    PROMPT_FILTERS,
    SOCIAL,
    STORYTELLER,
    TOONSIM,
    VOIDSIM,
    WORLD_HEALTH,
    WORLDGEN,
)
from bunnyland.plugins.loader import _match_plugin_id, discover_plugins
from bunnyland.sandbox.plugin import SANDBOX_PLUGIN_ID


def test_builtin_plugins_declared():
    ids = {p.id for p in bunnyland_plugins()}
    assert ids == {
        BARBARIANSIM,
        COLONYSIM,
        CORE_VERBS,
        LIFESIM,
        MEMORY,
        WORLDGEN,
        ENVIRONMENT,
        MECHANISMS,
        HISTORY,
        SOCIAL,
        POLICY,
        PROMPT_FILTERS,
        SANDBOX_PLUGIN_ID,
        PERSONA,
        GARDENSIM,
        DRAGONSIM,
        DAGGERSIM,
        DINOSIM,
        DISCORD,
        MCP,
        MEDIA,
        VOIDSIM,
        NUKESIM,
        NEONSIM,
        TOONSIM,
        STORYTELLER,
        IMAGEGEN,
        CHECKPOINTS,
        WORLD_HEALTH,
    }
    assert discord_plugins()[0].id == DISCORD


def test_select_defaults_to_default_enabled():
    plugins = bunnyland_plugins()
    assert len(select(plugins, None)) == 26
    assert [p.id for p in select(plugins, [MEMORY])] == [MEMORY]
    assert CHECKPOINTS not in {p.id for p in select(plugins, None)}
    assert WORLD_HEALTH not in {p.id for p in select(plugins, None)}
    assert [p.id for p in select(plugins, [CHECKPOINTS])] == [CHECKPOINTS]
    assert [p.id for p in select(plugins, [WORLD_HEALTH])] == [WORLD_HEALTH]


def test_prompt_filter_plugin_registers_typed_async_definitions():
    from bunnyland.foundation.prompt_filters.plugin import bunnyland_plugins as filter_plugins

    plugin = next(plugin for plugin in bunnyland_plugins() if plugin.id == PROMPT_FILTERS)
    registry = PluginRegistry((plugin,))

    assert set(registry.prompt_filters) == {
        "bunnyland.prompt_filters.corrupted",
        "bunnyland.prompt_filters.recall",
        "bunnyland.prompt_filters.redacted",
        "bunnyland.prompt_filters.storyteller",
    }
    assert all(
        definition.component_type in plugin.ecs.components
        for _owner, definition in registry.prompt_filters.values()
    )
    assert filter_plugins() == [plugin]


def test_prompt_filter_registration_rejects_invalid_contracts():
    from pydantic.dataclasses import dataclass
    from relics import Component

    from bunnyland.prompts import PromptFilterDefinition

    @dataclass(frozen=True)
    class FilterComponent(Component):
        value: str = ""

    async def valid(text, context, component):
        del context, component
        return text

    def sync(text, context, component):
        del context, component
        return text

    def definition(filter_id, handler=valid):
        return PromptFilterDefinition(filter_id, FilterComponent, handler)

    with pytest.raises(PluginError, match="must be namespaced"):
        PluginRegistry(
            (
                Plugin(
                    id="example.filters",
                    name="Filters",
                    ecs=EcsContribution(components=(FilterComponent,)),
                    content=ContentContribution(prompt_filters=(definition("wrong.filter"),)),
                ),
            )
        )
    with pytest.raises(PluginError, match="not exported"):
        PluginRegistry(
            (
                Plugin(
                    id="example.filters",
                    name="Filters",
                    content=ContentContribution(
                        prompt_filters=(definition("example.filters.valid"),)
                    ),
                ),
            )
        )
    with pytest.raises(PluginError, match="handler must be async"):
        PluginRegistry(
            (
                Plugin(
                    id="example.filters",
                    name="Filters",
                    ecs=EcsContribution(components=(FilterComponent,)),
                    content=ContentContribution(
                        prompt_filters=(definition("example.filters.sync", sync),)
                    ),
                ),
            )
        )
    with pytest.raises(PluginError, match="duplicate prompt filter name"):
        PluginRegistry(
            (
                Plugin(
                    id="example.filters",
                    name="Filters",
                    ecs=EcsContribution(components=(FilterComponent,)),
                    content=ContentContribution(
                        prompt_filters=(
                            definition("example.filters.same"),
                            definition("example.filters.same"),
                        )
                    ),
                ),
            )
        )
    with pytest.raises(PluginError, match="prompt filter component"):
        PluginRegistry(
            (
                Plugin(
                    id="example.filters",
                    name="Filters",
                    ecs=EcsContribution(components=(FilterComponent,)),
                    content=ContentContribution(
                        prompt_filters=(
                            definition("example.filters.one"),
                            definition("example.filters.two"),
                        )
                    ),
                ),
            )
        )


def test_collect_prompt_fragments_gathers_providers():
    from bunnyland.plugins import collect_prompt_fragments

    providers = collect_prompt_fragments(bunnyland_plugins())
    # needs, environment, and sim packs contribute generic prompt state.
    assert len(providers) >= 3
    assert all(callable(p) for p in providers)


def test_collect_prompt_filters_gathers_definitions():
    from bunnyland.foundation.prompt_filters.mechanics import BUILTIN_PROMPT_FILTERS
    from bunnyland.plugins import collect_prompt_filters

    plugin = next(plugin for plugin in bunnyland_plugins() if plugin.id == PROMPT_FILTERS)
    assert collect_prompt_filters((plugin,)) == list(BUILTIN_PROMPT_FILTERS)


def test_registry_collects_validated_plugin_boundary_scopes():
    plugin = Plugin(
        id="example.boundaries",
        name="Boundaries",
        policy=PolicyContribution(boundary_tags=frozenset({"adult:custom"})),
    )
    assert PluginRegistry((plugin,)).boundary_tags == frozenset({"adult:custom"})
    with pytest.raises(ValueError, match="boundary scopes"):
        PolicyContribution(boundary_tags=frozenset({"Adult:Invalid"}))


def test_apply_plugins_activates_contributed_boundary_scopes_in_world_policy():
    from bunnyland.foundation.policy.mechanics import WorldPolicyComponent
    from bunnyland.foundation.policy.plugin import plugin as policy_plugin

    custom = Plugin(
        id="example.boundaries",
        name="Boundaries",
        policy=PolicyContribution(boundary_tags=frozenset({"adult:custom"})),
    )
    actor = WorldActor()
    apply_plugins((core_verbs_plugin(), policy_plugin(), custom), actor)
    entity = next(actor.world.query().with_all([WorldPolicyComponent]).execute_entities())
    assert "adult:custom" in entity.get_component(WorldPolicyComponent).available


def test_collect_persona_fragments_gathers_stable_persona_providers():
    from bunnyland.plugins import collect_persona_fragments

    providers = collect_persona_fragments(bunnyland_plugins())
    assert len(providers) >= 3
    assert all(callable(p) for p in providers)


def test_collect_content_items_preserves_plugin_order():
    first = object()
    second = object()
    plugins = [
        Plugin(
            id="one",
            name="One",
            content=ContentContribution(
                prompt_fragments=(first,),
                persona_fragments=(first,),
            ),
        ),
        Plugin(
            id="two",
            name="Two",
            content=ContentContribution(
                prompt_fragments=(second,),
                persona_fragments=(second,),
            ),
        ),
    ]

    assert collect_content_items(plugins, "prompt_fragments") == (first, second)
    assert collect_content_items(plugins, "persona_fragments") == (first, second)
    assert collect_content_items([], "prompt_fragments") == ()


def test_collect_ecs_types_preserves_plugin_order():
    plugins = [
        Plugin(
            id="one",
            name="One",
            ecs=EcsContribution(components=(MemoryProfileComponent,), edges=(Contains,)),
        )
    ]

    assert collect_ecs_types(plugins) == ((MemoryProfileComponent,), (Contains,))
    assert collect_ecs_types([]) == ((), ())


def test_builtin_admin_and_storyteller_ecs_types_are_registered():
    assert AdminComponent in core_verbs_plugin().ecs.components
    assert RestingComponent in core_verbs_plugin().ecs.components
    assert RoomGateComponent in core_verbs_plugin().ecs.components
    assert AllowsMembersOf in core_verbs_plugin().ecs.edges
    assert {
        RestStartedEvent,
        RestEndedEvent,
        SleepStartedEvent,
        CharacterWokeEvent,
    } <= set(core_verbs_plugin().commands.typed_events)
    assert IncidentSpawned in storyteller_plugin().ecs.edges
    assert SaveCheckpointComponent in checkpoints_plugin().ecs.components
    assert {
        definition.command_type for definition in checkpoints_plugin().commands.action_definitions
    } == {"save-checkpoint", "reload-checkpoint"}


def test_worldgen_plugin_contributes_named_generators():
    from bunnyland.worldgen import collect_generators

    registry = collect_generators(bunnyland_plugins())
    assert {
        "wild-boar-forest",
        "empty",
        "waiting-room",
        "halloween",
        "holiday",
        "tower-debate",
        "clue-snack-demo",
        "dive-scheme-demo",
        "star-opera-demo",
        "gothic-count-demo",
        "oneshot",
        "recursive",
    } <= set(registry)
    # generators are selected by name and disappear if their plugin is dropped
    without = collect_generators([p for p in bunnyland_plugins() if p.id != WORLDGEN])
    assert "empty" not in without
    assert "wild-boar-forest" not in without
    assert "waiting-room" not in without
    assert "halloween" not in without
    assert "holiday" not in without
    assert "tower-debate" not in without
    assert "apple-crossing" not in without
    assert "bell-green" not in without
    assert "clover-city" not in without
    assert "oneshot" not in without
    assert "recursive" not in without
    # Each sim plugin contributes all of its own demo worlds, independently of Worldgen.
    assert "clue-snack-demo" in without
    assert "dive-scheme-demo" in without
    assert "star-opera-demo" in without
    assert "gothic-count-demo" in without
    assert "voidsim-demo" in registry
    assert "nukesim-demo" in registry
    assert "dinosim-demo" in registry
    assert registry["empty"].uses_seed is False
    assert registry["empty"].description == "Blank ECS world with only the world clock."
    assert registry["empty"].group == "administrative"
    assert registry["waiting-room"].uses_seed is False
    assert registry["waiting-room"].group == "scene demo"
    assert registry["halloween"].uses_seed is False
    assert registry["halloween"].group == "seasonal"
    assert registry["holiday"].uses_seed is False
    assert registry["tower-debate"].uses_seed is False
    assert registry["clue-snack-demo"].uses_seed is False
    assert registry["clue-snack-demo"].group == "pop culture"
    assert registry["dive-scheme-demo"].uses_seed is False
    assert registry["star-opera-demo"].uses_seed is False
    assert registry["gothic-count-demo"].uses_seed is False
    assert registry["recursive"].uses_seed is True
    assert registry["recursive"].description == "Breadth-first graph, grown room-by-room."
    assert registry["recursive"].group == "algorithmic"
    assert registry["voidsim-demo"].uses_seed is False
    assert registry["voidsim-demo"].group == "simpack sandbox"
    assert registry["nukesim-demo"].uses_seed is False
    assert registry["dinosim-demo"].uses_seed is False
    assert registry["dungeon-vault-demo"].group == "dungeon"
    assert registry["storm-lighthouse-demo"].group == "scene demo"
    for generator in registry.values():
        assert generator.group
        first = generator.description[0]
        assert first.isupper() or first.lower() == first.upper()
        assert generator.description.endswith((".", "。"))
    without_void = collect_generators([p for p in bunnyland_plugins() if p.id != VOIDSIM])
    assert "voidsim-demo" not in without_void
    without_nuke = collect_generators([p for p in bunnyland_plugins() if p.id != NUKESIM])
    assert "nukesim-demo" not in without_nuke
    without_dino = collect_generators([p for p in bunnyland_plugins() if p.id != DINOSIM])
    assert "dinosim-demo" not in without_dino


def test_select_unknown_id_raises():
    with pytest.raises(PluginError):
        select(bunnyland_plugins(), ["nope"])


def test_resolve_order_places_dependencies_first():
    ordered = resolve_order(bunnyland_plugins())
    ids = [p.id for p in ordered]
    assert ids.index(CORE_VERBS) < ids.index(LIFESIM)
    assert ids.index(LIFESIM) < ids.index(COLONYSIM)
    assert ids.index(COLONYSIM) < ids.index(GARDENSIM)
    assert ids.index(CORE_VERBS) < ids.index(BARBARIANSIM)
    assert ids.index(LIFESIM) < ids.index(DRAGONSIM)
    assert ids.index(BARBARIANSIM) < ids.index(VOIDSIM)
    assert ids.index(VOIDSIM) < ids.index(NUKESIM)
    assert ids.index(LIFESIM) < ids.index(DINOSIM)
    assert ids.index(COLONYSIM) < ids.index(DINOSIM)
    assert ids.index(CORE_VERBS) < ids.index(MEMORY)


def test_builtin_sim_dependencies_match_layering_contracts():
    plugins = {plugin.id: plugin for plugin in bunnyland_plugins()}

    assert plugins[LIFESIM].dependencies.requires == (CORE_VERBS,)
    assert plugins[COLONYSIM].dependencies.requires == (CORE_VERBS, LIFESIM)
    assert plugins[GARDENSIM].dependencies.requires == (
        CORE_VERBS,
        LIFESIM,
        COLONYSIM,
    )
    assert plugins[BARBARIANSIM].dependencies.requires == (CORE_VERBS, ENVIRONMENT)
    assert plugins[DRAGONSIM].dependencies.requires == (CORE_VERBS, LIFESIM)
    assert plugins[DAGGERSIM].dependencies.requires == (CORE_VERBS, DRAGONSIM)
    assert plugins[VOIDSIM].dependencies.requires == (
        CORE_VERBS,
        COLONYSIM,
        BARBARIANSIM,
    )
    assert plugins[NUKESIM].dependencies.requires == (
        CORE_VERBS,
        COLONYSIM,
        BARBARIANSIM,
    )
    assert plugins[DINOSIM].dependencies.requires == (CORE_VERBS, LIFESIM, COLONYSIM)


def test_selecting_later_sim_without_required_layers_fails_clearly():
    with pytest.raises(PluginError, match="depends on missing"):
        resolve_order([select(bunnyland_plugins(), [NUKESIM])[0]])


def test_environment_solely_owns_canonical_shelter_and_room_moisture():
    from bunnyland.simpacks.barbariansim.mechanics import ShelterComponent as LegacyShelter

    plugins = {plugin.id: plugin for plugin in bunnyland_plugins()}
    registry = PluginRegistry(plugins.values())

    assert LegacyShelter is ShelterComponent
    assert ShelterComponent in plugins[ENVIRONMENT].ecs.components
    assert MoistureComponent in plugins[ENVIRONMENT].ecs.components
    assert ShelterComponent not in plugins[BARBARIANSIM].ecs.components
    assert registry.components["ShelterComponent"] == (ENVIRONMENT, ShelterComponent)


def test_missing_dependency_raises():
    orphan = Plugin(
        id="x",
        name="X",
        dependencies=DependencyContribution(requires=("does.not.exist",)),
    )
    with pytest.raises(PluginError):
        resolve_order([orphan])


def test_dependency_cycle_raises():
    a = Plugin(id="a", name="A", dependencies=DependencyContribution(requires=("b",)))
    b = Plugin(id="b", name="B", dependencies=DependencyContribution(requires=("a",)))
    with pytest.raises(PluginError):
        resolve_order([a, b])


def test_three_plugin_dependency_cycle_raises():
    a = Plugin(id="a", name="A", dependencies=DependencyContribution(requires=("b",)))
    b = Plugin(id="b", name="B", dependencies=DependencyContribution(requires=("c",)))
    c = Plugin(id="c", name="C", dependencies=DependencyContribution(requires=("a",)))
    with pytest.raises(PluginError, match="dependency cycle"):
        resolve_order([a, b, c])


def test_missing_recommendation_warns_but_continues(caplog):
    plugin = Plugin(
        id="a",
        name="A",
        dependencies=DependencyContribution(recommends=("missing",)),
    )

    assert resolve_order([plugin]) == [plugin]
    assert "recommends missing" in caplog.text


class _WaveHandler:
    command_type = "wave"

    def execute(self, ctx: HandlerContext, command: SubmittedCommand) -> HandlerResult:
        del ctx, command
        return planned(MutationPlan())


def test_plugin_action_definitions_register_with_actor_and_tool_schema():
    definition = ActionDefinition(
        command_type="wave",
        tool_name="wave",
        title="Wave",
        description="Wave to a reachable character.",
        arguments={
            "target_id": ActionArgument(
                title="Target",
                description="The character to wave at.",
                kind="entity",
                required=True,
            )
        },
        examples=(ActionExample("wave to Hazel", natural=True),),
        natural_patterns=(ActionPattern("wave to {target_id}"),),
    )
    actor = WorldActor()
    apply_plugins(
        [
            Plugin(
                id="wave",
                name="Wave",
                commands=CommandContribution(
                    action_handlers=(_WaveHandler,),
                    action_definitions=(definition,),
                ),
            )
        ],
        actor,
    )

    assert actor.action_definitions() == (definition,)
    schema = next(
        item["function"]
        for item in tool_schemas(actor.action_definitions())
        if item["function"]["name"] == "wave"
    )
    assert schema["description"] == "Wave to a reachable character. Example: wave to Hazel."
    assert schema["parameters"]["properties"]["target_id"]["description"] == (
        "The character to wave at."
    )


def test_core_and_memory_plugins_expose_native_agent_tools_with_examples():
    actor = WorldActor()
    apply_plugins(
        [plugin for plugin in bunnyland_plugins() if plugin.id in (CORE_VERBS, MEMORY)], actor
    )

    schemas = {
        schema["function"]["name"]: schema["function"]
        for schema in tool_schemas(actor.action_definitions())
    }

    assert {
        "move",
        "rest",
        "sleep",
        "wait",
        "take_note",
        "remember",
        "forget",
        "reflect",
    } <= schemas.keys()
    assert set(schemas["move"]["parameters"]["properties"]) == {"direction", "exit_id"}
    assert schemas["wait"]["parameters"] == {"type": "object", "properties": {}}
    assert schemas["rest"]["parameters"]["properties"]["duration_seconds"]["type"] == (
        "number"
    )
    assert schemas["sleep"]["parameters"]["properties"]["duration_seconds"]["type"] == (
        "number"
    )
    assert "Example: go north." in schemas["move"]["description"]
    assert "Example: wait." in schemas["wait"]["description"]
    assert "Example: take note the north tunnel is flooded." in schemas["take_note"]["description"]


def test_builtin_action_catalogue_uses_reviewed_lanes_and_effort_tiers():
    actor = WorldActor()
    apply_plugins(bunnyland_plugins(), actor)
    definitions = {definition.command_type: definition for definition in actor.action_definitions()}

    assert actor.plugins is not None
    assert actor.plugins.actions["rest"][0] == CORE_VERBS
    assert definitions["say"].cost == CommandCost()
    assert definitions["tell"].cost == CommandCost()
    assert definitions["inspect"].cost == CommandCost()
    assert definitions["unlock-perk"].lane is Lane.FOCUS
    assert definitions["unlock-perk"].cost == CommandCost(focus=3)
    allowed = {0, 1, 2, 3, 5}
    for definition in definitions.values():
        assert definition.cost.action in allowed
        assert definition.cost.focus in allowed - {5}


def test_plugin_handlers_without_owned_action_definitions_are_rejected():
    actor = WorldActor()
    with pytest.raises(PluginError, match="has no plugin-owned action definition"):
        apply_plugins(
            [
                Plugin(
                    id="wave",
                    name="Wave",
                    commands=CommandContribution(action_handlers=(_WaveHandler,)),
                )
            ],
            actor,
        )


def test_builtin_handler_command_types_are_in_the_shared_action_catalog():
    catalog = set(PluginRegistry(bunnyland_plugins()).actions)
    handler_types = {
        handler.command_type
        for plugin in bunnyland_plugins()
        for handler in plugin.commands.action_handlers
    }

    assert handler_types - catalog == set()


def test_action_requirement_names_resolve_to_real_components_and_edges():
    registry = PluginRegistry(bunnyland_plugins())

    for _owner, definition in registry.actions.values():
        requirement = definition.requirement
        for name in (*requirement.character_components, *requirement.reachable_components):
            assert name in registry.components, (
                f"{definition.command_type}: unknown component requirement {name!r}"
            )
        for name in requirement.character_edges:
            assert name in registry.edges, (
                f"{definition.command_type}: unknown edge requirement {name!r}"
            )


def test_speech_action_metadata_exposes_intent_and_approach_arguments():
    definitions = {
        command_type: definition
        for command_type, (_owner, definition) in PluginRegistry(
            bunnyland_plugins()
        ).actions.items()
    }

    assert definitions["say"].arg_keys == ("text", "intent", "approach")
    assert definitions["tell"].arg_keys == (
        "target_id",
        "text",
        "intent",
        "approach",
        "audible",
    )
    assert definitions["say"].arguments["intent"].kind == "string"
    assert definitions["say"].arguments["approach"].kind == "string"
    assert definitions["tell"].arguments["intent"].kind == "string"
    assert definitions["tell"].arguments["approach"].kind == "string"

    # The message body is required so clients (TUI, toon) prompt for it before
    # submitting; optional flavor arguments stay optional.
    assert definitions["say"].arguments["text"].required is True
    assert definitions["say"].arguments["intent"].required is False
    assert definitions["tell"].arguments["target_id"].required is True
    assert definitions["tell"].arguments["text"].required is True
    assert definitions["tell"].arguments["audible"].required is False
    assert definitions["start-conversation"].arguments["target_ids"].required is True
    assert definitions["conversation-line"].arguments["conversation_id"].required is True
    assert definitions["conversation-line"].arguments["text"].required is True
    assert definitions["end-conversation"].arguments["conversation_id"].required is True


def test_core_verbs_installs_conversation_lifecycle_consequence():
    actor = WorldActor()

    apply_plugins([core_verbs_plugin()], actor)

    assert any(
        isinstance(consequence, ConversationConsequence)
        for consequence in actor._consequences
    )


async def test_applying_core_verbs_enables_move():
    # An actor with no plugins cannot move; applying core_verbs registers the handler.
    scenario = _bare_scenario()
    apply_plugins([p for p in bunnyland_plugins() if p.id == CORE_VERBS], scenario.actor)

    await scenario.actor.submit(_move(scenario))
    await scenario.actor.tick(3600.0)
    assert scenario.character_room() == scenario.room_b


async def test_applying_memory_plugin_enables_notes():
    scenario = _bare_scenario()
    apply_plugins([p for p in bunnyland_plugins() if p.id in (CORE_VERBS, MEMORY)], scenario.actor)
    char = scenario.actor.world.get_entity(scenario.character)
    char.add_component(MemoryProfileComponent(vector_collection="c"))

    notes = []
    scenario.actor.bus.subscribe(NoteTakenEvent, notes.append)
    note = build_submitted_command(
        character_id=str(scenario.character),
        controller_id=str(scenario.controller),
        controller_generation=scenario.generation,
        command_type="take-note",
        cost=CommandCost(focus=1),
        lane=Lane.FOCUS,
        payload={"text": "a private thought"},
    )
    await scenario.actor.submit(note)
    await scenario.actor.tick(0.0)
    assert len(notes) == 1


def test_memory_and_prompt_filter_plugins_register_runtime_only_automatic_recall():
    scenario = _bare_scenario()
    apply_plugins(
        [
            plugin
            for plugin in bunnyland_plugins()
            if plugin.id in (CORE_VERBS, MEMORY, PROMPT_FILTERS)
        ],
        scenario.actor,
    )

    assert len(scenario.actor.automatic_prompt_filters) == 1
    automatic = scenario.actor.automatic_prompt_filters[0]
    assert automatic.definition_id == "bunnyland.prompt_filters.recall"
    assert automatic.required_component is MemoryProfileComponent
    assert automatic.component_factory() is not None
    with pytest.raises(ValueError, match="already registered"):
        scenario.actor.register_automatic_prompt_filter(automatic)
    configure_memory_recall(scenario.actor, limit=0)
    assert automatic.component_factory() is None
    scenario.actor.memory_recall_policy = None
    assert automatic.component_factory() is None


async def test_applying_lifesim_plugin_enables_skill_progression():
    scenario = _bare_scenario()
    apply_plugins(
        [p for p in bunnyland_plugins() if p.id in (CORE_VERBS, LIFESIM)],
        scenario.actor,
    )
    from bunnyland.simpacks.lifesim.mechanics import SkillSetComponent

    command = build_submitted_command(
        character_id=str(scenario.character),
        controller_id=str(scenario.controller),
        controller_generation=scenario.generation,
        command_type="practice-skill",
        cost=CommandCost(action=1),
        lane=Lane.WORLD,
        payload={"skill": "cooking", "xp": 100},
    )
    await scenario.actor.submit(command)
    await scenario.actor.tick(3600.0)

    character = scenario.actor.world.get_entity(scenario.character)
    assert character.get_component(SkillSetComponent).levels["cooking"] == 1


async def test_disabled_plugin_leaves_its_verbs_unhandled():
    # Without the memory plugin, take-note has no handler and is rejected.
    scenario = _bare_scenario()
    apply_plugins([p for p in bunnyland_plugins() if p.id == CORE_VERBS], scenario.actor)
    from bunnyland.core import OnInsufficientPoints
    from bunnyland.core.events import CommandRejectedEvent

    rejects = []
    scenario.actor.bus.subscribe(CommandRejectedEvent, rejects.append)
    note = build_submitted_command(
        character_id=str(scenario.character),
        controller_id=str(scenario.controller),
        controller_generation=scenario.generation,
        command_type="take-note",
        cost=CommandCost(focus=1),
        lane=Lane.FOCUS,
        on_insufficient_points=OnInsufficientPoints.DENY,
        payload={"text": "x"},
    )
    await scenario.actor.submit(note)
    await scenario.actor.tick(0.0)
    assert any("no handler for take-note" in r.reason for r in rejects)


def test_catalogue_parity_plugins_register_new_public_surfaces():
    plugins = {plugin.id: plugin for plugin in bunnyland_plugins()}

    core = plugins[CORE_VERBS]
    assert {"HoldableComponent", "WearableComponent"} <= {
        component.__name__ for component in core.ecs.components
    }
    assert {
        "look",
        "inspect",
        "drop",
        "open",
        "close",
        "lock",
        "unlock",
        "hold",
        "unhold",
        "wear",
        "remove",
    } <= {handler.command_type for handler in core.commands.action_handlers}
    assert {
        "RoomLookedEvent",
        "EntityInspectedEvent",
        "ContainerOpenedEvent",
        "ContainerClosedEvent",
        "DoorOpenedEvent",
        "DoorClosedEvent",
        "EntityLockedEvent",
        "EntityUnlockedEvent",
        "ItemHeldEvent",
        "ItemUnheldEvent",
        "ItemWornEvent",
        "ItemRemovedEvent",
    } <= {event.__name__ for event in core.commands.typed_events}

    lifesim = plugins[LIFESIM]
    assert {
        "CharacterProfileComponent",
        "WhimComponent",
        "HomeObjectComponent",
    } <= {component.__name__ for component in lifesim.ecs.components}
    assert "HasWhim" in {edge.__name__ for edge in lifesim.ecs.edges}
    assert {
        "update-profile",
        "add-whim",
        "complete-whim",
        "use-home-object",
        "maintain-home-object",
        "invite-over",
        "configure-aging",
    } <= {handler.command_type for handler in lifesim.commands.action_handlers}
    assert {
        "ProfileUpdatedEvent",
        "WhimAddedEvent",
        "HomeObjectMaintainedEvent",
        "LifesimAgingPolicyChangedEvent",
    } <= {event.__name__ for event in lifesim.commands.typed_events}

    colony = plugins[COLONYSIM]
    assert {
        "PawnProfileComponent",
        "JobBillComponent",
        "PrisonerComponent",
        "ResearchProjectComponent",
        "ColonyIncidentComponent",
        "TradeOfferComponent",
        "SurgeryBillComponent",
    } <= {component.__name__ for component in colony.ecs.components}
    assert "HasBodyPart" in {edge.__name__ for edge in colony.ecs.edges}
    assert {
        "update-pawn-profile",
        "progress-job-bill",
        "set-prisoner-policy",
        "recruit-prisoner",
        "research-project",
        "resolve-colony-incident",
        "complete-trade",
        "perform-surgery",
    } <= {handler.command_type for handler in colony.commands.action_handlers}
    assert {
        "PawnProfileUpdatedEvent",
        "JobBillProgressedEvent",
        "RecruitmentProgressedEvent",
        "TechUnlockedEvent",
        "ColonyIncidentResolvedEvent",
        "SurgeryPerformedEvent",
    } <= {event.__name__ for event in colony.commands.typed_events}

    garden = plugins[GARDENSIM]
    assert {
        "CropQualityComponent",
        "RegrowableComponent",
        "PestComponent",
        "MachineBreakdownComponent",
        "AnimalBreedingComponent",
        "GeodeComponent",
        "ShippingBinComponent",
        "CollectionComponent",
    } <= {component.__name__ for component in garden.ecs.components}
    assert {
        "inspect",
        "weed-crop",
        "treat-pests",
        "cancel-machine",
        "repair-machine",
        "breed-animal",
        "open-geode",
        "ship-items",
        "claim-reward",
    } <= {handler.command_type for handler in garden.commands.action_handlers}
    assert {
        "CropInspectedEvent",
        "MachineProcessingCancelledEvent",
        "AnimalBornEvent",
        "GeodeOpenedEvent",
        "ItemsShippedEvent",
        "CollectionUpdatedEvent",
    } <= {event.__name__ for event in garden.commands.typed_events}

    barbarian = plugins[BARBARIANSIM]
    assert {
        "BaseClaimComponent",
        "TrapComponent",
        "SurvivalGapComponent",
        "BuildingComponent",
        "PurgeWaveComponent",
        "RitualComponent",
        "DangerZoneComponent",
        "BossComponent",
        "TreasureComponent",
        "ClimbingGateComponent",
        "WetnessComponent",
    } <= {component.__name__ for component in barbarian.ecs.components}
    assert {
        "claim-base",
        "place-trap",
        "disarm-trap",
        "bridge-survival-gap",
        "decay-building",
        "upgrade-building",
        "demolish-building",
        "prepare-siege",
        "start-purge-wave",
        "perform-ritual",
        "explore-danger-zone",
        "defeat-boss",
        "unlock-treasure",
        "claim-treasure",
        "climb",
    } <= {handler.command_type for handler in barbarian.commands.action_handlers}
    assert {
        "BaseClaimedEvent",
        "TrapPlacedEvent",
        "TrapDisarmedEvent",
        "SurvivalGapBridgedEvent",
        "BuildingUpgradedEvent",
        "BuildingDecayedEvent",
        "BuildingDemolishedEvent",
        "PurgeWaveStartedEvent",
        "RitualPerformedEvent",
        "DangerZoneExploredEvent",
        "BossDefeatedEvent",
        "TreasureUnlockedEvent",
        "TreasureClaimedEvent",
        "ClimbingGatePassedEvent",
        "WetnessChangedEvent",
    } <= {event.__name__ for event in barbarian.commands.typed_events}

    dragon = plugins[DRAGONSIM]
    assert {
        "MapMarkerComponent",
        "EncounterZoneComponent",
        "PerkComponent",
        "AncientBeastComponent",
        "WordOfPowerComponent",
        "VoiceInscriptionComponent",
        "CarvableComponent",
        "LoreBookComponent",
        "LockDifficultyComponent",
        "MagicComponent",
        "SpellComponent",
        "PotionRecipeComponent",
        "ArtifactComponent",
        "SpellCooldownComponent",
        "PersuasionComponent",
        "SurrenderComponent",
        "QuestTemplateComponent",
        "QuestStateComponent",
        "QuestProvenanceComponent",
    } <= {component.__name__ for component in dragon.ecs.components}
    assert {
        "HasPerk",
        "KnowsWord",
        "KnowsSpell",
        "QuestHasObjective",
        "QuestHasReward",
        "QuestAcceptedBy",
        "TracksQuest",
        "RequiresQuest",
    } <= {edge.__name__ for edge in dragon.ecs.edges}
    assert {
        "mark-map",
        "trigger-encounter",
        "unlock-perk",
        "absorb-great-soul",
        "learn-word-of-power",
        "speak-word-of-power",
        "inscribe-voice-phrase",
        "study-voice-inscription",
        "change-faction-rank",
        "bribe",
        "serve-jail-time",
        "pick-lock",
        "read-lore-book",
        "learn-spell",
        "cast-dragon-spell",
        "brew-potion",
        "use",
        "track-quest",
        "decline-quest",
        "choose-quest-branch",
        "persuade",
        "surrender",
        "report-crime",
        "recover-magic",
        "identify",
        "appease-ancient-beast",
        "ask-for-work",
        "accept-generated-quest",
        "complete-generated-quest",
        "refuse-generated-quest",
        "abandon-generated-quest",
        "extend-generated-quest",
        "lie-about-quest",
    } <= {handler.command_type for handler in dragon.commands.action_handlers}
    assert {
        "MapMarkerAddedEvent",
        "EncounterTriggeredEvent",
        "PerkUnlockedEvent",
        "GreatSoulAbsorbedEvent",
        "WordOfPowerLearnedEvent",
        "VoicePhraseInscribedEvent",
        "VoiceInscriptionStudiedEvent",
        "FactionRankChangedEvent",
        "GuardBribedEvent",
        "JailSentenceServedEvent",
        "LockPickedEvent",
        "LoreBookReadEvent",
        "SpellLearnedEvent",
        "DragonSpellCastEvent",
        "PotionBrewedEvent",
        "ArtifactUsedEvent",
        "QuestTrackedEvent",
        "QuestDeclinedEvent",
        "QuestBranchChosenEvent",
        "PersuasionAttemptedEvent",
        "SurrenderedEvent",
        "CrimeReportedEvent",
        "ArtifactIdentifiedEvent",
        "AncientBeastAppeasedEvent",
        "QuestGeneratedEvent",
        "QuestFailedEvent",
        "QuestRefusedEvent",
        "QuestAbandonedEvent",
        "QuestExtendedEvent",
        "QuestLieToldEvent",
    } <= {event.__name__ for event in dragon.commands.typed_events}
    assert LIFESIM in dragon.dependencies.requires

    dagger = plugins[DAGGERSIM]
    assert {
        "PropertyDeedComponent",
        "InstitutionDuesComponent",
        "LetterOfCreditComponent",
        "SafeStorageComponent",
        "LodgingComponent",
        "CampingComponent",
        "PotionMakerComponent",
        "IngredientComponent",
        "AfflictionStigmaComponent",
        "CureRequestComponent",
    } <= {component.__name__ for component in dagger.ecs.components}
    assert {"OwnsProperty", "StoredIn", "HasAccessToService"} <= {
        edge.__name__ for edge in dagger.ecs.edges
    }
    assert {
        "buy-property",
        "promote-institution",
        "pay-institution-dues",
        "issue-letter-of-credit",
        "store-safe-item",
        "retrieve-safe-item",
        "send-debt-collector",
        "sentence-crime",
        "rent-lodging",
        "camp",
        "buy-travel-supplies",
        "resolve-travel-interruption",
        "make-potion",
        "recharge-enchanted-item",
        "identify",
        "progress-affliction-incubation",
        "mark-affliction-stigma",
        "request-cure-quest",
    } <= {handler.command_type for handler in dagger.commands.action_handlers}
    assert {
        "InstitutionReputationChangedEvent",
        "LegalReputationChangedEvent",
        "ServiceAccessChangedEvent",
        "PropertyPurchasedEvent",
        "InstitutionPromotedEvent",
        "InstitutionDuesPaidEvent",
        "LetterOfCreditIssuedEvent",
        "CourtSentenceIssuedEvent",
        "LodgingRentedEvent",
        "CampMadeEvent",
        "TravelInterruptionResolvedEvent",
        "PotionMadeEvent",
        "IngredientIdentifiedEvent",
        "AfflictionIncubationProgressedEvent",
        "AfflictionStigmaMarkedEvent",
        "CureRequestedEvent",
    } <= {event.__name__ for event in dagger.commands.typed_events}

    void = plugins[VOIDSIM]
    assert {
        "DutyShiftComponent",
        "CrewDutyStatusComponent",
        "ContractComponent",
        "CargoComponent",
        "SalvageClaimComponent",
        "AlienSpeciesComponent",
        "FirstContactComponent",
        "TranslationMatrixComponent",
        "QuarantineComponent",
        "DiplomaticMissionComponent",
        "AlienArtifactComponent",
        "DroneComponent",
        "ShipAIComponent",
        "AwayTeamComponent",
        "MoraleComponent",
        "MutinyComponent",
        "EmergencyComponent",
        "ReactorComponent",
        "PassengerComponent",
        "SurveySiteComponent",
        "MiningSiteComponent",
        "MortgageComponent",
    } <= {component.__name__ for component in void.ecs.components}
    assert {"WorksShift", "MemberOfAwayTeam"} <= {edge.__name__ for edge in void.ecs.edges}
    assert {
        "assign-crew-shift",
        "relieve-crew-shift",
        "accept-contract",
        "load-cargo",
        "deliver-cargo",
        "claim-salvage",
        "initiate-contact",
        "attempt-translation",
        "quarantine-sample",
        "negotiate-alien",
        "study-alien-artifact",
        "deploy-away-team",
        "boost-morale",
        "start-mutiny",
        "command",
        "hack-ship-ai",
        "salvage-data",
        "study-xenobiology",
        "accept-trade-protocol",
        "resolve-emergency",
        "stabilize-reactor",
        "adjust-gravity",
        "repel-boarders",
        "deliver-passenger",
        "survey-site",
        "mine-asteroid",
        "inspect",
        "search-smuggling-compartment",
        "claim-insurance",
        "pay-mortgage",
    } <= {handler.command_type for handler in void.commands.action_handlers}
    assert {
        "CrewShiftAssignedEvent",
        "CrewDutyChangedEvent",
        "ContractAcceptedEvent",
        "CargoLoadedEvent",
        "CargoDeliveredEvent",
        "SalvageClaimedEvent",
        "FirstContactEvent",
        "TranslationProgressedEvent",
        "QuarantineStartedEvent",
        "DiplomacyChangedEvent",
        "AlienArtifactStudiedEvent",
        "AwayTeamDeployedEvent",
        "MoraleChangedEvent",
        "MutinyStartedEvent",
        "DroneCommandedEvent",
        "ShipAIHackedEvent",
        "DataSalvagedEvent",
        "XenobiologyStudiedEvent",
        "TradeProtocolAcceptedEvent",
        "EmergencyResolvedEvent",
        "ReactorStabilizedEvent",
        "GravityAdjustedEvent",
        "BoardingRepelledEvent",
        "PassengerDeliveredEvent",
        "SurveyCompletedEvent",
        "MiningCompletedEvent",
        "CustomsInspectedEvent",
        "SmugglingCompartmentSearchedEvent",
        "InsuranceClaimedEvent",
        "MortgagePaidEvent",
    } <= {event.__name__ for event in void.commands.typed_events}

    nuke = plugins[NUKESIM]
    assert {
        "OldWorldTechComponent",
        "TechLeadComponent",
        "SettlementComponent",
        "SettlementSalvageComponent",
        "WaterPurifierComponent",
        "GeneratorComponent",
        "HotspotMarkerComponent",
        "SuppressantComponent",
        "SampleComponent",
        "LockedCrateComponent",
        "WastelandArtifactComponent",
        "FactionSalvageComponent",
        "SchematicComponent",
        "ItemModComponent",
        "BeaconComponent",
        "RaiderPressureComponent",
        "TerminalComponent",
    } <= {component.__name__ for component in nuke.ecs.components}
    assert {
        "identify",
        "restore-tech",
        "claim",
        "salvage-settlement",
        "build",
        "power-generator",
        "mark-hotspot",
        "use-suppressant",
        "harvest",
        "study-sample",
        "unlock",
        "study-wasteland-artifact",
        "claim-faction-salvage",
        "install-mod",
        "field-repair",
        "brew-chem",
        "activate-beacon",
        "open-trader-route",
        "increase-raider-pressure",
        "boot-terminal",
    } <= {handler.command_type for handler in nuke.commands.action_handlers}
    assert {
        "OldWorldTechIdentifiedEvent",
        "OldWorldTechRestoredEvent",
        "SettlementClaimedEvent",
        "SettlementSalvagedEvent",
        "PurifierBuiltEvent",
        "GeneratorPoweredEvent",
        "HotspotMarkedEvent",
        "SuppressantUsedEvent",
        "SampleHarvestedEvent",
        "SampleStudiedEvent",
        "CrateUnlockedEvent",
        "WastelandArtifactStudiedEvent",
        "FactionSalvageClaimedEvent",
        "ModInstalledEvent",
        "FieldRepairAppliedEvent",
        "ChemBrewedEvent",
        "BeaconActivatedEvent",
        "TraderRouteOpenedEvent",
        "RaiderPressureChangedEvent",
        "TerminalBootedEvent",
    } <= {event.__name__ for event in nuke.commands.typed_events}

    dino = plugins[DINOSIM]
    assert {
        "TerritoryComponent",
        "HerdComponent",
        "NestComponent",
        "FossilSurveyComponent",
        "LabIncubationComponent",
        "EggInspectionComponent",
        "ImprintComponent",
        "JuvenileCareComponent",
        "WaterCreatureComponent",
        "ContainmentPanicComponent",
    } <= {component.__name__ for component in dino.ecs.components}
    assert {
        "mark-territory",
        "track-herd",
        "prepare-nest",
        "survey-fossil",
        "excavate-fossil",
        "clean-fossil",
        "stabilize-fossil",
        "lab-incubate-egg",
        "inspect",
        "imprint-creature",
        "care-for-juvenile",
        "study-water-creature",
        "brood-egg",
        "set-incubation-temperature",
        "trigger-containment-panic",
    } <= {handler.command_type for handler in dino.commands.action_handlers}
    assert {
        "TerritoryMarkedEvent",
        "HerdTrackedEvent",
        "NestPreparedEvent",
        "FossilSurveyedEvent",
        "FossilExcavatedEvent",
        "FossilCleanedEvent",
        "FossilStabilizedEvent",
        "LabIncubationStartedEvent",
        "EggInspectedEvent",
        "CreatureImprintedEvent",
        "JuvenileCareGivenEvent",
        "WaterCreatureStudiedEvent",
        "BroodingStartedEvent",
        "IncubationTemperatureSetEvent",
        "ContainmentPanicStartedEvent",
    } <= {event.__name__ for event in dino.commands.typed_events}

    neon = plugins[NEONSIM]
    assert {
        "CyberpunkSiteComponent",
        "SecurityZoneComponent",
        "AccessLevelComponent",
        "CheckpointComponent",
        "SafehouseComponent",
        "RestrictedAreaComponent",
        "DeviceComponent",
        "CameraComponent",
        "SurveillanceCoverageComponent",
        "RecordedEvidenceComponent",
        "BlindSpotComponent",
        "HackableComponent",
        "ExploitComponent",
        "CredentialComponent",
        "DataPayloadComponent",
        "TraceTimerComponent",
        "BlackMarketComponent",
        "ContrabandComponent",
        "HeatComponent",
        "WantedLevelComponent",
        "InformantComponent",
        "ImplantComponent",
        "AugmentationSlotsComponent",
        "ClinicComponent",
        "FixerComponent",
        "RunnerContractComponent",
        "BlackmailFileComponent",
        "AssetExtractionComponent",
    } <= {component.__name__ for component in neon.ecs.components}
    assert {"InsideZone", "OwesFavor", "HasImplant"} <= {edge.__name__ for edge in neon.ecs.edges}
    assert {
        "enter-district",
        "show-credentials",
        "bribe",
        "sneak",
        "claim",
        "case-location",
        "inspect",
        "disable-camera",
        "loop-camera",
        "jam-sensor",
        "deploy-drone",
        "wipe-evidence",
        "scan-network",
        "run-exploit",
        "use-credential",
        "access-terminal",
        "escalate-privileges",
        "install-backdoor",
        "exfiltrate-data",
        "sabotage-system",
        "unlock",
        "evade-trace",
        "spoof-identity",
        "buy-contraband",
        "sell-data",
        "call-favor",
        "pay-debt",
        "post-bounty",
        "turn-informant",
        "hide-from-law",
        "clear-warrant",
        "install-implant",
        "remove-implant",
        "service-implant",
        "overclock-implant",
        "disable-implant",
        "license-implant",
        "scan-implant",
        "exploit-implant",
        "take-fixer-job",
        "meet-handler",
        "deliver-data",
        "collect-payout",
        "burn-contact",
        "plant-evidence",
        "blackmail-target",
        "leak-file",
        "extract-asset",
    } <= {handler.command_type for handler in neon.commands.action_handlers}
    assert {
        "DistrictEnteredEvent",
        "AccessGrantedEvent",
        "AccessDeniedEvent",
        "CheckpointPassedEvent",
        "TrespassDetectedEvent",
        "SafehouseClaimedEvent",
        "DeviceInspectedEvent",
        "CameraDisabledEvent",
        "CameraLoopedEvent",
        "SensorJammedEvent",
        "DroneDeployedEvent",
        "EvidenceRecordedEvent",
        "EvidenceWipedEvent",
        "NetworkScannedEvent",
        "CredentialUsedEvent",
        "HackSucceededEvent",
        "HackFailedEvent",
        "BackdoorInstalledEvent",
        "PrivilegesEscalatedEvent",
        "TraceStartedEvent",
        "DataExfiltratedEvent",
        "SystemSabotagedEvent",
        "AlarmRaisedEvent",
        "ContrabandBoughtEvent",
        "DataSoldEvent",
        "FavorCalledEvent",
        "DebtPaidEvent",
        "HeatChangedEvent",
        "WantedLevelChangedEvent",
        "WarrantClearedEvent",
        "InformantTurnedEvent",
        "LawResponseEvent",
        "ImplantInstalledEvent",
        "ImplantRemovedEvent",
        "ImplantServicedEvent",
        "ImplantExploitedEvent",
        "SideEffectTriggeredEvent",
        "FixerJobAcceptedEvent",
        "HandlerMetEvent",
        "DataDeliveredEvent",
        "PayoutCollectedEvent",
        "DoubleCrossRevealedEvent",
        "ContactBurnedEvent",
        "EvidencePlantedEvent",
        "BlackmailAppliedEvent",
        "FileLeakedEvent",
        "AssetExtractedEvent",
    } <= {event.__name__ for event in neon.commands.typed_events}


def test_plugin_observers_are_registered_with_the_world():
    from relics import Component, OnComponentAdded

    class MarkerComponent(Component):
        pass

    added = []

    class MarkerObserver(OnComponentAdded):
        component_type = MarkerComponent

        def on_component_added(self, entity, component):
            added.append(entity.id)

    actor = WorldActor()
    apply_plugins(
        [
            Plugin(
                id="observer",
                name="Observer",
                ecs=EcsContribution(observers=(MarkerObserver,)),
            )
        ],
        actor,
    )

    entity = spawn_entity(actor.world, [])
    entity.add_component(MarkerComponent())
    # Observers are queued and run when the world processes its observer queue.
    actor.world._process_observer_queue()
    assert entity.id in added


def test_ecs_systems_can_be_instances_or_classes():
    # apply should accept a system instance as well as a class.
    from bunnyland.foundation.needs.mechanics import HungerSystem

    actor = WorldActor()
    plugin = Plugin(id="t", name="T", ecs=EcsContribution(systems=(HungerSystem(),)))
    apply_plugins([plugin], actor)  # must not raise


async def test_example_motd_plugin_greets_discord_claims_with_ecs_rows():
    from bunnyland.core.events import CharacterClaimedEvent
    from examples.plugins.motd_claim import (
        HasMotdMessage,
        MotdMessageComponent,
    )
    from examples.plugins.motd_claim import (
        bunnyland_plugins as motd_plugins,
    )

    scenario = build_scenario()
    apply_plugins(motd_plugins(), scenario.actor)
    controller = spawn_entity(
        scenario.actor.world,
        [DiscordControllerComponent(discord_user_id=123, default_channel_id=456)],
    )
    generation = scenario.actor.assign_controller(scenario.character, controller.id)

    await scenario.actor.bus.publish(
        CharacterClaimedEvent(
            **scenario.actor._event_base(
                actor_id=str(scenario.character),
                character_id=str(scenario.character),
                controller_id=str(controller.id),
                generation=generation,
            )
        )
    )
    await scenario.actor.tick(0.0)

    character = scenario.actor.world.get_entity(scenario.character)
    motds = [
        scenario.actor.world.get_entity(message_id)
        for _edge, message_id in character.get_relationships(HasMotdMessage)
    ]
    assert len(motds) == 1
    message = motds[0].get_component(MotdMessageComponent)
    assert "Today's tip" in message.text
    assert message.queued_for_delivery is True
    outbox = motds[0].get_component(ControllerOutboxMessageComponent)
    assert outbox.controller_id == str(controller.id)
    assert outbox.delivered_at_epoch is None


async def test_example_motd_plugin_ignores_non_discord_claims():
    from bunnyland.core.events import CharacterClaimedEvent
    from examples.plugins.motd_claim import HasMotdMessage
    from examples.plugins.motd_claim import bunnyland_plugins as motd_plugins

    scenario = build_scenario()
    apply_plugins(motd_plugins(), scenario.actor)

    await scenario.actor.bus.publish(
        CharacterClaimedEvent(
            **scenario.actor._event_base(
                actor_id=str(scenario.character),
                character_id=str(scenario.character),
                controller_id=str(scenario.controller),
                generation=scenario.generation,
            )
        )
    )

    character = scenario.actor.world.get_entity(scenario.character)
    assert character.get_relationships(HasMotdMessage) == []


# -- helpers ----------------------------------------------------------------------------


def _bare_scenario():
    # build_scenario registers MoveHandler already; use a fresh actor with no handlers
    # by clearing the registry so we can prove plugins add them.
    scenario = build_scenario()
    scenario.actor._handlers.clear()
    return scenario


def _move(scenario):
    return build_submitted_command(
        character_id=str(scenario.character),
        controller_id=str(scenario.controller),
        controller_generation=scenario.generation,
        command_type="move",
        cost=CommandCost(action=1),
        lane=Lane.WORLD,
        payload={"direction": "north"},
    )


def test_plugin_discovery_rejects_invalid_entrypoint_payload(monkeypatch):
    class EntryPoint:
        name = "invalid"

        def load(self):
            return object()

    monkeypatch.setattr("bunnyland.plugins.loader.entry_points", lambda **_kwargs: [EntryPoint()])
    with pytest.raises(PluginError, match="expected Plugin"):
        discover_plugins()


def test_short_plugin_id_rejects_ambiguous_suffix():
    plugins = {
        "one.shared": Plugin(id="one.shared", name="One"),
        "two.shared": Plugin(id="two.shared", name="Two"),
    }
    with pytest.raises(PluginError, match="ambiguous plugin id"):
        _match_plugin_id(plugins, "shared")
    assert _match_plugin_id({"one.shared": plugins["one.shared"]}, "shared").id == ("one.shared")
