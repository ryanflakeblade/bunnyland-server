"""Tests for the foundation prompt builder (spec 16)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from conftest import build_scenario

from bunnyland.core import (
    AffectComponent,
    AffectVector,
    CharacterComponent,
    ContainmentMode,
    Contains,
    DeadComponent,
    DownedComponent,
    Holding,
    IdentityComponent,
    MemoryProfileComponent,
    PortableComponent,
    ReadableComponent,
    RestingComponent,
    RoomComponent,
    SleepingComponent,
    StudiedBy,
    SuspendedComponent,
    replace_component,
    spawn_entity,
)
from bunnyland.core.events import ActorMovedEvent, EventVisibility, SpeechSaidEvent
from bunnyland.foundation.meters.mechanics import Meter
from bunnyland.foundation.needs.mechanics import HungerComponent, ThirstComponent, need_fragments
from bunnyland.foundation.persona.mechanics import (
    GoalComponent,
    PersonaProfileComponent,
    PreferenceComponent,
    TraitSetComponent,
)
from bunnyland.foundation.policy.mechanics import BoundaryTag, CharacterBoundaryComponent
from bunnyland.foundation.social.mechanics import SocialBond
from bunnyland.memory import InMemoryStore
from bunnyland.plugins import bunnyland_plugins, collect_persona_fragments
from bunnyland.projections import RecentContextProjection, RoomSummaryProjection
from bunnyland.projections.perception import PerceivedEntity
from bunnyland.prompts import (
    ComponentPromptContext,
    PerceivedPromptEvent,
    PerspectiveName,
    PerspectivePhrase,
    PromptAccess,
    PromptBuilder,
    PromptFact,
    PromptPerspective,
    render_compact_prompt,
    render_prompt,
)
from bunnyland.prompts.builder import _status
from bunnyland.prompts.facts import (
    coerce_prompt_facts,
    collect_prompt_facts,
    visible_prompt_facts,
)
from bunnyland.simpacks.dinosim.mechanics import (
    ApexPredatorComponent,
    ArmyResponseComponent,
    BoneComponent,
    ContainmentPanicComponent,
    CreatureMilkComponent,
    CreatureProductComponent,
    EggComponent,
    EnclosureComponent,
    EscapeRiskComponent,
    FenceComponent,
    FossilFragmentComponent,
    FossilSurveyComponent,
    GateComponent,
    GuardAnimalComponent,
    HideComponent,
    ImprintComponent,
    IncubationComponent,
    LabIncubationComponent,
    RanchLaborComponent,
    SettlementDamageComponent,
    SpeciesIdentificationComponent,
    ToxinComponent,
    TrainingComponent,
    WeakPointComponent,
)
from bunnyland.simpacks.dragonsim.mechanics import (
    AncientBeastComponent,
    ArtifactComponent,
    EncounterZoneComponent,
    FactionComponent,
    GreatSoulComponent,
    JailedByFaction,
    KnowsSpell,
    LockDifficultyComponent,
    LoreBookComponent,
    MagicComponent,
    MapMarkerComponent,
    MemberOfFaction,
    PointOfInterestComponent,
    PotionRecipeComponent,
    QuestAcceptedBy,
    QuestComponent,
    QuestProvenanceComponent,
    QuestStateComponent,
    SpellComponent,
    SpellCooldownComponent,
    SurrenderComponent,
    TracksQuest,
    VoiceInscriptionComponent,
    WantedByFaction,
)


def add_item(scenario, room_id, name):
    item = spawn_entity(
        scenario.actor.world,
        [IdentityComponent(name=name, kind="item"), PortableComponent()],
    )
    scenario.actor.world.get_entity(room_id).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), item.id
    )
    return item


def test_build_context_has_core_sections():
    scenario = build_scenario()
    add_item(scenario, scenario.room_a, "three berries")
    builder = PromptBuilder(scenario.actor.world)

    ctx = builder.build(scenario.character, epoch=scenario.actor.epoch)

    assert ctx.name == "Juniper"
    assert ctx.status.startswith("active")
    assert ctx.action == (5.0, 5.0)
    assert ctx.location_title == "Mosslit Burrow"
    assert "three berries" in ctx.visible_objects
    assert "north to North Tunnel" in ctx.exits
    assert "move north" in ctx.commands
    assert "inspect three berries" in ctx.commands
    assert "take note" in ctx.commands


def test_prompt_facts_validate_keys_scores_and_numeric_cutoffs():
    facts = (
        PromptFact(key="test.essential", text="essential", detail=0),
        PromptFact(key="test.intermediate", text="intermediate", detail=15),
        PromptFact(key="test.exhaustive", text="exhaustive", detail=30),
    )

    assert [fact.key for fact in visible_prompt_facts(facts, cutoff=15)] == [
        "test.essential",
        "test.intermediate",
    ]
    with pytest.raises(ValueError, match="namespaced"):
        PromptFact(key="missing-namespace", text="invalid")
    with pytest.raises(ValueError, match="must not be empty"):
        PromptFact(key="test.empty", text="  ")
    for invalid in (-1, 1.5, True):
        with pytest.raises(ValueError, match="non-negative"):
            PromptFact(key="test.invalid", text="invalid", detail=invalid)
        with pytest.raises(ValueError, match="non-negative"):
            visible_prompt_facts(facts, cutoff=invalid)
    with pytest.raises(ValueError, match="duplicate"):
        coerce_prompt_facts(
            (
                PromptFact(key="test.same", text="one"),
                PromptFact(key="test.same", text="two"),
            ),
            namespace="test.provider",
        )
    assert coerce_prompt_facts(["legacy"], namespace="!!!")[0].key == "provider.fact-0"


def test_fact_collection_enforces_viewer_awareness_and_global_key_uniqueness():
    entity = SimpleNamespace(id="entity-1")
    other = SimpleNamespace(id="entity-2")

    def hidden(_world, _entity):
        raise AssertionError("viewer-unaware provider must not inspect another entity")

    def aware(_world, _entity, *, viewer):
        assert viewer is other
        return [PromptFact(key="test.aware", text="observable", detail=15)]

    assert collect_prompt_facts(
        None, entity, [hidden, aware], cutoff=15, viewer=other
    ) == (PromptFact(key="test.aware", text="observable", detail=15),)

    def duplicate_one(_world, _entity):
        return [PromptFact(key="test.duplicate", text="one")]

    def duplicate_two(_world, _entity):
        return [PromptFact(key="test.duplicate", text="two")]

    with pytest.raises(ValueError, match="duplicate prompt fact key"):
        collect_prompt_facts(None, entity, [duplicate_one, duplicate_two], cutoff=10)
    for invalid in (-1, 1.5, True):
        with pytest.raises(ValueError, match="non-negative"):
            collect_prompt_facts(None, entity, [], cutoff=invalid)


def test_prompt_builder_logs_and_omits_failed_fact_provider(caplog):
    scenario = build_scenario()

    def broken_provider(_world, _entity):
        raise RuntimeError("operator-only component failure")

    def healthy_provider(_world, _entity):
        return [PromptFact(key="test.healthy", text="A useful condition.")]

    def duplicate_provider(_world, _entity):
        return [PromptFact(key="test.healthy", text="Conflicting condition.")]

    builder = PromptBuilder(
        scenario.actor.world,
        fragment_providers=(broken_provider, healthy_provider, duplicate_provider),
    )
    context = builder.build(scenario.character)
    prompt = render_prompt(context)

    assert context.conditions == ("A useful condition.",)
    assert "operator-only component failure" not in prompt
    assert "Conflicting condition" not in prompt
    assert "failed for character" in caplog.text
    assert "duplicate prompt fact key" in caplog.text


def test_component_prompt_context_validates_detail_scores():
    scenario = build_scenario()
    character = scenario.actor.world.get_entity(scenario.character)
    context = ComponentPromptContext.for_entity(scenario.actor.world, character)

    assert context.includes_detail(10) is True
    assert context.includes_detail(11) is False
    for invalid in (-1, 1.5, True):
        with pytest.raises(ValueError, match="non-negative"):
            context.includes_detail(invalid)


def test_prompt_perspective_uses_grammar_and_access_enums_independently():
    scenario = build_scenario()
    character = scenario.actor.world.get_entity(scenario.character)
    observer = spawn_entity(scenario.actor.world)
    perspective = PromptPerspective(
        viewer=observer,
        perspective="third-person",
        access="admin",
    )
    context = ComponentPromptContext.for_entity(
        scenario.actor.world,
        character,
        perspective=perspective,
    )

    assert perspective.perspective is PerspectiveName.THIRD_PERSON
    assert perspective.access is PromptAccess.ADMIN
    assert perspective.choose(first="I", second="You", third="They") == "They"
    assert context.is_first_person is True
    assert context.can_view_private_state is True
    with pytest.raises(ValueError, match="not a valid PerspectiveName"):
        PromptPerspective(perspective="god")


def test_prompt_builder_uses_standard_cutoff_and_accepts_detailed_cutoff():
    scenario = build_scenario()

    def facts(_world, _character):
        return [
            PromptFact(key="test.relevant", text="relevant", detail=10),
            PromptFact(key="test.quiet", text="quiet", detail=30),
        ]

    builder = PromptBuilder(scenario.actor.world, fragment_providers=[facts])

    assert builder.build(scenario.character).conditions == ("relevant",)
    assert builder.build(scenario.character, detail_cutoff=30).conditions == (
        "relevant",
        "quiet",
    )


def test_include_entity_ids_annotates_entities_and_commands_when_enabled():
    scenario = build_scenario()
    item = add_item(scenario, scenario.room_a, "three berries")

    ctx = PromptBuilder(scenario.actor.world, include_entity_ids=True).build(
        scenario.character, epoch=scenario.actor.epoch
    )
    item_token = f"three berries [{item.id}]"
    assert item_token in ctx.visible_objects
    assert f"take {item_token}" in ctx.commands
    assert f"move north [{scenario.room_b}]" in ctx.commands

    # Default builder stays id-free so existing narrative prompts are unchanged.
    plain = PromptBuilder(scenario.actor.world).build(
        scenario.character, epoch=scenario.actor.epoch
    )
    assert "three berries" in plain.visible_objects
    assert "move north" in plain.commands
    assert f"[{item.id}]" not in " ".join(plain.commands)


def test_available_commands_match_object_affordances():
    scenario = build_scenario()
    fixed_notice = spawn_entity(
        scenario.actor.world,
        [IdentityComponent(name="fixed notice", kind="paper"), ReadableComponent(text="Read me")],
    )
    scenario.actor.world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), fixed_notice.id
    )

    commands = PromptBuilder(scenario.actor.world).build(scenario.character).commands

    assert "inspect fixed notice" in commands
    assert "take fixed notice" not in commands
    assert "use fixed notice" not in commands


def test_available_commands_skip_stale_perceived_entities():
    scenario = build_scenario()
    commands = PromptBuilder(scenario.actor.world)._available_commands(
        exits=(),
        visible_entities=(
            PerceivedEntity(id="entity_999999", name="old notice", is_character=False),
        ),
        visible_characters=(),
    )

    assert commands == ("take note", "remember/search notes", "forget note by id")


def test_prompt_surfaces_others_held_items_without_offering_take():
    scenario = build_scenario()
    world = scenario.actor.world
    add_item(scenario, scenario.room_a, "floor pebble")
    holder = spawn_entity(
        world, [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()]
    )
    body = spawn_entity(
        world,
        [
            IdentityComponent(name="Marlow", kind="character"),
            CharacterComponent(),
            DeadComponent(died_at_epoch=0, cause="test"),
        ],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), holder.id
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), body.id
    )
    held = spawn_entity(world, [IdentityComponent(name="steamed bun", kind="food")])
    pocketed = spawn_entity(world, [IdentityComponent(name="pocket key", kind="item")])
    holder.add_relationship(Contains(mode=ContainmentMode.INVENTORY), held.id)
    holder.add_relationship(Contains(mode=ContainmentMode.INVENTORY), pocketed.id)
    holder.add_relationship(Holding(slot="hand"), held.id)

    context = PromptBuilder(world, include_entity_ids=True).build(
        scenario.character, epoch=scenario.actor.epoch
    )
    rendered = render_prompt(context)

    assert context.other_held == (f"Hazel: steamed bun [{held.id}]",)
    assert "Others are holding:\n- Hazel: steamed bun" in rendered
    assert all(str(held.id) not in command for command in context.commands)
    assert "pocket key" not in rendered


def test_build_context_has_stable_persona_surface_from_plugins():
    scenario = build_scenario()
    world = scenario.actor.world
    character = world.get_entity(scenario.character)
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character")],
    )
    character.add_component(PersonaProfileComponent(voice="warm and direct", role="forager"))
    character.add_component(TraitSetComponent(traits=("curious",)))
    character.add_component(PreferenceComponent(likes=("berries",)))
    character.add_component(GoalComponent(active_goals=("find the elder",)))
    character.add_component(CharacterBoundaryComponent(denied=frozenset({BoundaryTag.PREGNANCY})))
    character.add_relationship(SocialBond(affinity=0.5, familiarity=0.5), hazel.id)
    builder = PromptBuilder(
        world,
        persona_providers=collect_persona_fragments(bunnyland_plugins()),
    )

    ctx = builder.build(scenario.character)
    prompt = render_prompt(ctx)

    assert "Your name is Juniper." in ctx.persona
    assert "Your current status is active, controlled by an agent." in ctx.persona
    assert "Your voice: warm and direct." in ctx.persona
    assert "Your current role: forager." in ctx.persona
    assert "You are curious." in ctx.persona
    assert "You like berries." in ctx.persona
    assert "Your goal: find the elder." in ctx.persona
    assert "You are fond of Hazel." in ctx.persona
    assert "Your denied boundaries: pregnancy." in ctx.persona
    assert "Persona:" in prompt
    assert "- Your voice: warm and direct." in prompt


def test_build_context_relationship_prompts_differ_by_viewer_bond():
    scenario = build_scenario()
    world = scenario.actor.world
    juniper = world.get_entity(scenario.character)
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    juniper.add_relationship(SocialBond(fear=0.5, familiarity=0.5), hazel.id)
    hazel.add_relationship(SocialBond(affinity=0.5, familiarity=0.5), juniper.id)
    builder = PromptBuilder(
        world,
        persona_providers=collect_persona_fragments(bunnyland_plugins()),
    )

    juniper_context = builder.build(juniper.id)
    hazel_context = builder.build(hazel.id)

    assert "You fear Hazel." in juniper_context.persona
    assert "You are fond of Juniper." in hazel_context.persona


def test_status_helper_uses_condition_precedence(scenario):
    character = scenario.actor.world.get_entity(scenario.character)

    assert _status(character) == "active"

    character.add_component(RestingComponent(session_id="rest"))
    assert _status(character) == "resting"
    character.remove_component(RestingComponent)

    character.add_component(SleepingComponent())
    assert _status(character) == "asleep"

    character.add_component(DownedComponent(downed_at_epoch=0, cause="test"))
    assert _status(character) == "downed"

    character.add_component(SuspendedComponent())
    assert _status(character) == "suspended"

    character.add_component(DeadComponent(died_at_epoch=0, cause="test"))
    assert _status(character) == "dead"


def test_build_context_includes_needs_and_feelings_without_retrieving_memory():
    scenario = build_scenario()
    char = scenario.actor.world.get_entity(scenario.character)
    char.add_component(HungerComponent(meter=Meter(value=75.0)))  # urgent
    char.add_component(ThirstComponent(meter=Meter(value=10.0)))  # calm -> no phrase
    char.add_component(
        AffectComponent(current=AffectVector(stress=20.0), labels=frozenset({"tense"}))
    )
    builder = PromptBuilder(
        scenario.actor.world,
        fragment_providers=[need_fragments],
    )
    ctx = builder.build(scenario.character, epoch=scenario.actor.epoch)

    assert any("hungry" in n for n in ctx.conditions)
    assert all("dry" not in n for n in ctx.conditions)  # thirst calm
    assert "tense" in ctx.feelings
    assert ctx.notes == ()
    assert ctx.recall == ()


async def test_build_context_surfaces_structured_social_cues_from_recent_events():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    recent = RecentContextProjection(world)
    recent.subscribe(scenario.actor.bus)
    created_at = datetime.now(UTC)

    await scenario.actor.bus.publish(
        ActorMovedEvent(
            event_id="arrival",
            world_epoch=1,
            created_at=created_at,
            visibility=EventVisibility.ROOM,
            actor_id=str(hazel.id),
            room_id=str(scenario.room_a),
            from_room_id=str(scenario.room_b),
            to_room_id=str(scenario.room_a),
        )
    )
    builder = PromptBuilder(world, recent_context=recent)
    arrived = builder.build(scenario.character)

    assert "Hazel just arrived." in arrived.social_cues
    assert "Hazel is quiet." in arrived.social_cues

    await scenario.actor.bus.publish(
        SpeechSaidEvent(
            event_id="hazel-speaks",
            world_epoch=2,
            created_at=created_at,
            visibility=EventVisibility.ROOM,
            actor_id=str(hazel.id),
            room_id=str(scenario.room_a),
            target_ids=(str(scenario.character),),
            text="The bridge is out.",
            final_interpretation="inform",
        )
    )
    spoke = builder.build(scenario.character)

    assert "Hazel just spoke." in spoke.social_cues
    assert "Hazel is quiet." not in spoke.social_cues

    await scenario.actor.bus.publish(
        SpeechSaidEvent(
            event_id="juniper-speaks",
            world_epoch=3,
            created_at=created_at,
            visibility=EventVisibility.ROOM,
            actor_id=str(scenario.character),
            room_id=str(scenario.room_a),
            target_ids=(str(hazel.id),),
            text="Can you hear me?",
            final_interpretation="question",
        )
    )
    ignored = builder.build(scenario.character)

    assert "Hazel has not answered you." in ignored.social_cues


def test_build_context_surfaces_visible_social_distress():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [
            IdentityComponent(name="Hazel", kind="character"),
            CharacterComponent(),
            AffectComponent(labels=("afraid", "curious")),
        ],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    builder = PromptBuilder(world)

    ctx = builder.build(scenario.character)
    prompt = render_prompt(ctx)

    assert "Hazel seems afraid." in ctx.social_cues
    assert "Social cues:" in prompt


async def test_build_context_surfaces_pointed_silence_from_hostility():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    hazel.add_relationship(SocialBond(resentment=0.5), scenario.character)
    recent = RecentContextProjection(world)
    recent.subscribe(scenario.actor.bus)

    await scenario.actor.bus.publish(
        SpeechSaidEvent(
            event_id="juniper-asks",
            world_epoch=1,
            created_at=datetime.now(UTC),
            visibility=EventVisibility.ROOM,
            actor_id=str(scenario.character),
            room_id=str(scenario.room_a),
            target_ids=(str(hazel.id),),
            text="Will you answer me?",
            final_interpretation="question",
        )
    )

    ctx = PromptBuilder(world, recent_context=recent).build(scenario.character)

    assert "Hazel is pointedly silent after what you said." in ctx.social_cues
    assert "Hazel has not answered you." not in ctx.social_cues


def test_build_context_surfaces_brooding_presence_without_recent_speech():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [
            IdentityComponent(name="Hazel", kind="character"),
            CharacterComponent(),
            AffectComponent(labels=("angry",)),
        ],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )

    ctx = PromptBuilder(world).build(scenario.character)

    assert "Hazel seems angry." in ctx.social_cues
    assert "Hazel is brooding silently." in ctx.social_cues


def test_build_context_surfaces_familiar_quiet_watching():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    hazel.add_relationship(SocialBond(familiarity=0.5), scenario.character)

    ctx = PromptBuilder(world).build(scenario.character)

    assert "Hazel is watching you quietly." in ctx.social_cues


async def test_build_context_surfaces_recent_brooding_and_watching_presence():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [
            IdentityComponent(name="Hazel", kind="character"),
            CharacterComponent(),
            AffectComponent(labels=("tense",)),
        ],
    )
    clover = spawn_entity(
        world,
        [IdentityComponent(name="Clover", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), clover.id
    )
    clover.add_relationship(SocialBond(trust=0.4), scenario.character)
    recent = RecentContextProjection(world)
    recent.subscribe(scenario.actor.bus)

    for actor_id in (hazel.id, clover.id):
        await scenario.actor.bus.publish(
            ActorMovedEvent(
                event_id=f"arrival-{actor_id}",
                world_epoch=1,
                created_at=datetime.now(UTC),
                visibility=EventVisibility.ROOM,
                actor_id=str(actor_id),
                room_id=str(scenario.room_a),
                from_room_id=str(scenario.room_b),
                to_room_id=str(scenario.room_a),
            )
        )

    ctx = PromptBuilder(world, recent_context=recent).build(scenario.character)

    assert "Hazel is quiet." in ctx.social_cues
    assert "Hazel is brooding silently." in ctx.social_cues
    assert "Clover is quiet." in ctx.social_cues
    assert "Clover is watching you quietly." in ctx.social_cues


def test_social_cues_handle_unresolved_visible_character_ids():
    scenario = build_scenario()
    world = scenario.actor.world
    builder = PromptBuilder(world)

    cues = builder._social_cues(
        world.get_entity(scenario.character),
        (SimpleNamespace(is_character=True, id="not-an-id", name="Stranger"),),
        recent=('Juniper said: "Are you there?"',),
    )

    assert cues == ("Stranger has not answered you.",)


def test_component_prompt_context_lazy_room_siblings_and_inventory_helpers():
    scenario = build_scenario()
    world = scenario.actor.world
    character = world.get_entity(scenario.character)
    room = world.get_entity(scenario.room_a)
    nearby = add_item(scenario, scenario.room_a, "nearby berries")
    carried = spawn_entity(
        world,
        [IdentityComponent(name="carried berries", kind="item"), PortableComponent()],
    )
    character.add_relationship(Contains(mode=ContainmentMode.INVENTORY), carried.id)
    ctx = ComponentPromptContext.for_entity(world, character)
    target_ctx = ComponentPromptContext.for_entity(
        world,
        nearby,
        perspective=ctx.perspective,
        target=character,
    )
    observer_ctx = ComponentPromptContext.for_entity(
        world,
        nearby,
        perspective=PromptPerspective(viewer=room),
        target=character,
    )

    assert ctx.room == room
    assert ctx.can_view_private_state is True
    assert target_ctx.can_view_private_state is True
    assert observer_ctx.can_view_private_state is False
    assert ctx.room_siblings(PortableComponent) == (nearby,)
    assert ctx.inventory_items(PortableComponent) == (carried,)

    later_nearby = add_item(scenario, scenario.room_a, "later berries")
    later_carried = spawn_entity(
        world,
        [IdentityComponent(name="later carried berries", kind="item"), PortableComponent()],
    )
    character.add_relationship(Contains(mode=ContainmentMode.INVENTORY), later_carried.id)

    assert ctx.room_siblings(PortableComponent) == (nearby,)
    assert ctx.inventory_items(PortableComponent) == (carried,)
    fresh_ctx = ComponentPromptContext.for_entity(world, character)
    assert fresh_ctx.room_siblings(PortableComponent) == (nearby, later_nearby)
    assert fresh_ctx.inventory_items(PortableComponent) == (carried, later_carried)


def test_prompt_perspective_choose_selects_by_person():
    assert (
        PromptPerspective(perspective="first-person").choose(first="I", second="you", third="they")
        == "I"
    )
    assert (
        PromptPerspective(perspective="second-person").choose(first="I", second="you", third="they")
        == "you"
    )
    assert (
        PromptPerspective(perspective="third-person").choose(first="I", second="you", third="they")
        == "they"
    )


def test_component_prompt_context_handles_missing_world_and_room():
    scenario = build_scenario()
    world = scenario.actor.world
    character = world.get_entity(scenario.character)

    # No world: sibling/inventory lookups return empty and are cached.
    no_world = ComponentPromptContext(
        perspective=PromptPerspective(viewer=character),
        entity=character,
        room=world.get_entity(scenario.room_a),
    )
    assert no_world.room_siblings() == ()
    assert no_world.inventory_items() == ()

    # An explicitly supplied room skips the container lookup entirely (87->91).
    explicit_room = world.get_entity(scenario.room_a)
    with_room = ComponentPromptContext.for_entity(world, character, room=explicit_room)
    assert with_room.room == explicit_room

    # World present but the entity is in no room: the container lookup finds nothing and
    # room stays None, so room_siblings short-circuits to empty.
    roomless = spawn_entity(world, [IdentityComponent(name="floating mote", kind="item")])
    no_room = ComponentPromptContext.for_entity(world, roomless)
    assert no_room.room is None
    assert no_room.room_siblings() == ()


def test_component_prompt_context_skips_non_matching_siblings_and_inventory():
    scenario = build_scenario()
    world = scenario.actor.world
    character = world.get_entity(scenario.character)
    # A room sibling without the requested component is filtered out.
    add_item(scenario, scenario.room_a, "plain berries")
    plain_carried = spawn_entity(world, [IdentityComponent(name="plain note", kind="item")])
    character.add_relationship(Contains(mode=ContainmentMode.INVENTORY), plain_carried.id)
    portable_carried = spawn_entity(
        world, [IdentityComponent(name="carried berries", kind="item"), PortableComponent()]
    )
    character.add_relationship(Contains(mode=ContainmentMode.INVENTORY), portable_carried.id)

    # A room sibling that lacks the requested component (a non-portable fixture).
    fixture = add_item(scenario, scenario.room_a, "stone fixture")
    fixture.remove_component(PortableComponent)

    ctx = ComponentPromptContext.for_entity(world, character)

    # The non-portable note is excluded by the component filter (143->139 branch).
    assert ctx.inventory_items(PortableComponent) == (portable_carried,)
    # The character itself is skipped as a room sibling (self-skip continue).
    assert character not in ctx.room_siblings()
    # The non-portable fixture is excluded by the sibling component filter (126->122).
    portable_siblings = ctx.room_siblings(PortableComponent)
    assert fixture not in portable_siblings
    assert all(sibling.has_component(PortableComponent) for sibling in portable_siblings)


def test_perspective_phrase_supports_static_and_templated_lines():
    phrase = PerspectivePhrase(
        "I have {count} berry.",
        "You have {count} berry.",
        "They have {count} berry.",
    )

    assert phrase.render("first-person", count=1) == "I have 1 berry."
    assert phrase.render("second-person", count=1) == "You have 1 berry."
    assert PerspectivePhrase("Ready.", "Ready.", "Ready.").render("third-person") == "Ready."


def test_migrated_component_prompt_fragments_cover_cross_pack_branches():
    scenario = build_scenario()
    world = scenario.actor.world
    character = world.get_entity(scenario.character)

    def entity(name, *components):
        return spawn_entity(world, [IdentityComponent(name=name, kind="thing"), *components])

    def target_ctx(item):
        return ComponentPromptContext.for_entity(
            world, item, perspective=self_ctx.perspective, target=character
        )

    def observer_target_ctx(item):
        return ComponentPromptContext.for_entity(
            world, item, perspective=other_ctx.perspective, target=character
        )

    self_ctx = ComponentPromptContext.for_entity(world, character)
    other = entity("viewer")
    other_ctx = ComponentPromptContext.for_entity(
        world, character, perspective=PromptPerspective(viewer=other)
    )

    discovered = entity("ruin", PointOfInterestComponent(discovered=True))
    marker = entity("camp", MapMarkerComponent(label="Camp", marked_by=(str(character.id),)))
    inactive_zone = entity("zone", EncounterZoneComponent(active=False))
    active_quest = entity(
        "quest",
        QuestComponent(
            quest_id="q1",
            title="Find the relic",
        ),
        QuestStateComponent(status="active"),
    )
    declined_quest = entity(
        "declined",
        QuestComponent(quest_id="q2", title="Bad idea"),
        QuestStateComponent(status="declined"),
    )
    offered_quest = entity(
        "offered",
        QuestComponent(quest_id="q3", title="Unaccepted"),
        QuestStateComponent(),
    )
    generated_quest = entity(
        "generated",
        QuestComponent(quest_id="q4", title="Generated"),
        QuestStateComponent(),
        QuestProvenanceComponent(generator="bunnyland.dragonsim"),
    )
    hidden_generated_quest = entity(
        "hidden generated",
        QuestComponent(quest_id="q5", title="Hidden"),
        QuestStateComponent(status="active"),
        QuestProvenanceComponent(generator="bunnyland.dragonsim"),
    )
    hidden_generated_quest.add_relationship(QuestAcceptedBy(), other.id)
    active_quest.add_relationship(QuestAcceptedBy(), character.id)
    character.add_relationship(TracksQuest(), active_quest.id)
    replace_component(active_quest, QuestStateComponent(status="active", stage=2, branch="a"))
    faction = entity("Companions", FactionComponent(name="Companions"))
    known_spell = entity("spark", SpellComponent(name="Spark"))
    character.add_relationship(KnowsSpell(), known_spell.id)

    assert (
        discovered.get_component(PointOfInterestComponent).prompt_fragments(target_ctx(discovered))
        == ()
    )
    assert marker.get_component(MapMarkerComponent).prompt_fragments(target_ctx(marker)) == (
        "Map marker: Camp (landmark).",
    )
    assert (
        marker.get_component(MapMarkerComponent).prompt_fragments(observer_target_ctx(marker)) == ()
    )
    assert MapMarkerComponent(label="Hidden").prompt_fragments(self_ctx) == ()
    assert (
        inactive_zone.get_component(EncounterZoneComponent).prompt_fragments(
            target_ctx(inactive_zone)
        )
        == ()
    )
    assert EncounterZoneComponent(zone_type="crypt", danger_rating=3).prompt_fragments(
        self_ctx
    ) == ("Encounter zone nearby: crypt (danger 3).",)
    assert active_quest.get_component(QuestComponent).prompt_fragments(
        target_ctx(active_quest)
    ) == ("Active quest: Find the relic.",)
    assert (
        active_quest.get_component(QuestComponent).prompt_fragments(
            observer_target_ctx(active_quest)
        )
        == ()
    )
    assert declined_quest.get_component(QuestComponent).prompt_fragments(
        target_ctx(declined_quest)
    ) == ("Declined quest: Bad idea.",)
    assert (
        offered_quest.get_component(QuestComponent).prompt_fragments(target_ctx(offered_quest))
        == ()
    )
    assert generated_quest.get_component(QuestComponent).prompt_fragments(
        target_ctx(generated_quest)
    ) == ("Generated quest: Generated (offered).",)
    assert (
        hidden_generated_quest.get_component(QuestComponent).prompt_fragments(
            target_ctx(hidden_generated_quest)
        )
        == ()
    )
    assert QuestComponent(quest_id="q3", title="Ignored").prompt_fragments(self_ctx) == ()
    assert active_quest.get_component(QuestStateComponent).prompt_fragments(
        target_ctx(active_quest)
    ) == ("Tracked quest stage 2, branch a.",)
    assert (
        active_quest.get_component(QuestStateComponent).prompt_fragments(
            observer_target_ctx(active_quest)
        )
        == ()
    )
    assert JailedByFaction(release_epoch=10).prompt_fragments(other_ctx) == ()
    assert JailedByFaction(release_epoch=10).prompt_fragments(self_ctx) == ()
    assert JailedByFaction(release_epoch=10).prompt_fragments(
        ComponentPromptContext.for_entity(world, character, target=faction)
    ) == ("Serving jail time for Companions until 10.",)
    assert JailedByFaction(release_epoch=10).prompt_fragments(
        ComponentPromptContext.for_entity(world, character, target=entity("holding cell"))
    ) == ("Serving jail time for holding cell until 10.",)
    assert MemberOfFaction(rank="thane").prompt_fragments(self_ctx) == ()
    assert MemberOfFaction(rank="thane").prompt_fragments(
        ComponentPromptContext.for_entity(world, character, target=faction)
    ) == ("You are a thane of Companions.",)
    assert (
        MemberOfFaction(rank="thane").prompt_fragments(
            ComponentPromptContext.for_entity(
                world, character, perspective=other_ctx.perspective, target=faction
            )
        )
        == ()
    )
    assert AncientBeastComponent(name="Alduin", soul_absorbed=True).prompt_fragments(self_ctx) == (
        "Ancient beast nearby: Alduin (soul absorbed).",
    )
    assert GreatSoulComponent(souls=0).prompt_fragments(self_ctx) == ()
    assert WantedByFaction(amount=40).prompt_fragments(other_ctx) == ()
    assert WantedByFaction(amount=40).prompt_fragments(
        ComponentPromptContext.for_entity(world, character, target=faction)
    ) == ("Bounty of 40 with Companions.",)
    assert LockDifficultyComponent(locked=False).prompt_fragments(self_ctx) == ()
    assert LockDifficultyComponent(difficulty=5).prompt_fragments(
        ComponentPromptContext.for_entity(world, entity("chest"))
    ) == ("Locked target nearby: chest (difficulty 5).",)
    assert (
        LoreBookComponent(title="Read", read_by=(str(character.id),)).prompt_fragments(
            target_ctx(known_spell)
        )
        == ()
    )
    assert LoreBookComponent(title="Herbs", skill_name="alchemy").prompt_fragments(self_ctx) == (
        "Unread skill book nearby: Herbs (alchemy).",
    )
    assert MagicComponent().prompt_fragments(other_ctx) == ()
    assert SpellCooldownComponent().prompt_fragments(self_ctx) == ()
    assert SpellCooldownComponent(ready_at_epoch=7).prompt_fragments(self_ctx) == (
        "Spell cooldown nearby: ready at epoch 7.",
    )
    assert SurrenderComponent().prompt_fragments(other_ctx) == ()
    assert known_spell.get_component(SpellComponent).prompt_fragments(target_ctx(known_spell)) == (
        "Spell learned: Spark.",
    )
    assert (
        known_spell.get_component(SpellComponent).prompt_fragments(observer_target_ctx(known_spell))
        == ()
    )
    unknown_spell = entity("bolt", SpellComponent(name="Bolt"))
    assert unknown_spell.get_component(SpellComponent).prompt_fragments(
        target_ctx(unknown_spell)
    ) == ("Learnable spell nearby: Bolt.",)
    assert PotionRecipeComponent(name="Health", potion_name="Health").prompt_fragments(
        self_ctx
    ) == ("Potion recipe nearby: Health.",)
    assert ArtifactComponent(name="Blade", identified_by=(str(character.id),)).prompt_fragments(
        target_ctx(known_spell)
    ) == ("Artifact nearby: Blade (1 charges, identified).",)
    known_spell.add_relationship(StudiedBy(), character.id)
    assert VoiceInscriptionComponent(word_id="w").prompt_fragments(
        target_ctx(known_spell)
    ) == ()
    assert VoiceInscriptionComponent(word_id="w").prompt_fragments(target_ctx(unknown_spell)) == (
        "Voice inscription nearby: bolt.",
    )

    fossil = entity("fossil", FossilFragmentComponent())
    fossil.add_component(SpeciesIdentificationComponent(species_name="raptor"))
    egg = entity(
        "egg",
        EggComponent(species_name="raptor", laid_at_epoch=0),
        IncubationComponent(started_at_epoch=0, ready=True, temperature=37.0),
    )
    enclosure = entity(
        "pen",
        EnclosureComponent(name="Raptor pen"),
        ContainmentPanicComponent(),
        FenceComponent(),
        GateComponent(open=True, locked=True),
        EscapeRiskComponent(risk=2.0),
    )

    assert fossil.get_component(FossilFragmentComponent).prompt_fragments(target_ctx(fossil)) == (
        "Nearby fossil: fossil (raptor).",
    )
    assert FossilSurveyComponent(stabilized=True).prompt_fragments(target_ctx(fossil)) == (
        "Fossil survey fossil: stabilized.",
    )
    assert egg.get_component(EggComponent).prompt_fragments(target_ctx(egg)) == (
        "Nearby egg: egg (raptor, ready to hatch, 37 C).",
    )
    assert (
        LabIncubationComponent(lab_id="lab", active=False).prompt_fragments(target_ctx(egg)) == ()
    )
    assert ContainmentPanicComponent(active=False).prompt_fragments(target_ctx(enclosure)) == ()
    assert TrainingComponent().prompt_fragments(target_ctx(enclosure)) == ()
    assert ImprintComponent(imprinted_by="other").prompt_fragments(target_ctx(egg)) == ()
    assert enclosure.get_component(ContainmentPanicComponent).prompt_fragments(
        target_ctx(enclosure)
    ) == ("Raptor pen containment panic: severity 1.",)
    assert enclosure.get_component(FenceComponent).prompt_fragments(target_ctx(enclosure)) == (
        "Raptor pen fence: 10/10.",
    )
    assert enclosure.get_component(GateComponent).prompt_fragments(target_ctx(enclosure)) == (
        "Raptor pen gate: open, locked.",
    )
    assert FenceComponent().prompt_fragments(self_ctx) == ()
    assert GateComponent().prompt_fragments(self_ctx) == ()
    assert enclosure.get_component(EscapeRiskComponent).prompt_fragments(target_ctx(enclosure)) == (
        "Raptor pen escape risk: 2.",
    )
    assert SettlementDamageComponent(repaired=True).prompt_fragments(target_ctx(enclosure)) == ()
    assert WeakPointComponent(exposed=False).prompt_fragments(target_ctx(enclosure)) == ()
    assert ApexPredatorComponent(threat_level=0).prompt_fragments(target_ctx(enclosure)) == ()
    assert ArmyResponseComponent(called=False).prompt_fragments(target_ctx(enclosure)) == ()
    assert (
        CreatureProductComponent(product_type="egg", quantity=0).prompt_fragments(
            target_ctx(enclosure)
        )
        == ()
    )
    assert HideComponent(harvested=True).prompt_fragments(target_ctx(enclosure)) == ()
    assert BoneComponent(harvested=True).prompt_fragments(target_ctx(enclosure)) == ()
    assert ToxinComponent(quantity=0).prompt_fragments(target_ctx(enclosure)) == ()
    assert CreatureMilkComponent(volume=0).prompt_fragments(target_ctx(enclosure)) == ()
    assert RanchLaborComponent(active=False).prompt_fragments(target_ctx(enclosure)) == ()
    assert GuardAnimalComponent(active=False).prompt_fragments(target_ctx(enclosure)) == ()


def test_render_prompt_matches_foundation_layout():
    scenario = build_scenario()
    builder = PromptBuilder(scenario.actor.world)
    ctx = builder.build(scenario.character, epoch=scenario.actor.epoch)
    text = render_prompt(ctx)

    assert "You are Juniper, a character." in text
    assert "Location:" in text
    assert "Points:" in text
    assert "Action: 5.0/5.0" in text
    assert "Available commands:" in text


def test_render_compact_prompt_preserves_scoped_facts_with_less_text():
    scenario = build_scenario()
    context = PromptBuilder(scenario.actor.world).build(scenario.character)

    compact = render_compact_prompt(context)

    assert "character: Juniper" in compact
    assert "location:" in compact
    assert "exits:" in compact
    assert "available_actions:" in compact
    assert len(compact) < len(render_prompt(context))


def test_recent_context_appears_in_prompt():
    scenario = build_scenario()
    recent = RecentContextProjection(scenario.actor.world)
    # seed a recent entry directly
    recent._log[str(scenario.room_a)].append("Hazel warned the water tasted strange.")
    builder = PromptBuilder(
        scenario.actor.world,
        room_summary=RoomSummaryProjection(scenario.actor.world),
        recent_context=recent,
    )
    ctx = builder.build(scenario.character, epoch=scenario.actor.epoch)
    assert "Hazel warned the water tasted strange." in ctx.recent
    assert "Recent context:" in render_prompt(ctx)


def test_perceived_event_stream_and_overflow_are_distinct_prompt_sections():
    scenario = build_scenario()
    context = PromptBuilder(scenario.actor.world).build(scenario.character)
    context = replace(
        context,
        perceived_events=(
            PerceivedPromptEvent(
                event_id="speech-1",
                event_type="SpeechSaidEvent",
                world_epoch=30,
                summary='Hazel said, "Hello."',
                salience=80,
            ),
        ),
        omitted_perceived_events=2,
        omitted_event_epoch_range=(10, 20),
    )

    prompt = render_prompt(context)

    assert "Observed since your last prompt:" in prompt
    assert '- Hazel said, "Hello."' in prompt
    assert "SpeechSaidEvent" not in prompt
    assert "speech-1" not in prompt
    assert "2 additional visible event(s) during epochs 10-20 were omitted" in prompt


def test_unnamed_inventory_item_falls_back_to_something():
    scenario = build_scenario()
    world = scenario.actor.world
    # An inventory item with no IdentityComponent is labelled "something".
    bare = spawn_entity(world)
    world.get_entity(scenario.character).add_relationship(
        Contains(mode=ContainmentMode.INVENTORY), bare.id
    )

    ctx = PromptBuilder(world).build(scenario.character)

    assert "something" in ctx.inventory


def test_build_context_for_roomless_character_reports_nowhere():
    scenario = build_scenario()
    world = scenario.actor.world
    # Detach the character from every room: the room block is skipped entirely.
    world.get_entity(scenario.room_a).remove_relationship(Contains, scenario.character)

    ctx = PromptBuilder(world).build(scenario.character)

    assert ctx.location_title == "nowhere"
    assert ctx.room_summary == ""
    assert ctx.exits == ()


def test_status_line_reports_human_for_discord_controller():
    from bunnyland.core.controllers import DiscordControllerComponent

    scenario = build_scenario()
    world = scenario.actor.world
    discord = spawn_entity(
        world,
        [DiscordControllerComponent(discord_user_id=1, default_channel_id=2)],
    )
    scenario.actor.assign_controller(scenario.character, discord.id)

    ctx = PromptBuilder(world).build(scenario.character)

    assert "controlled by a human" in ctx.status


def test_status_line_covers_all_controller_kinds():
    from bunnyland.core.controllers import (
        BehaviorControllerComponent,
        MCPControllerComponent,
        ScriptedControllerComponent,
    )

    cases = [
        (MCPControllerComponent(client_id="a"), "controlled by an MCP client"),
        (BehaviorControllerComponent(behavior_name="wander"), "controlled by a behavior routine"),
        (ScriptedControllerComponent(), "controlled by a scripted routine"),
        # An unrecognized controller kind falls through to the suspended label.
        (IdentityComponent(name="mystery", kind="controller"), "suspended"),
    ]
    for component, expected in cases:
        scenario = build_scenario()
        world = scenario.actor.world
        controller = spawn_entity(world, [component])
        scenario.actor.assign_controller(scenario.character, controller.id)

        ctx = PromptBuilder(world).build(scenario.character)

        assert expected in ctx.status


def test_social_cue_reports_a_visible_character_just_left():
    scenario = build_scenario()
    world = scenario.actor.world
    hazel = spawn_entity(
        world,
        [IdentityComponent(name="Hazel", kind="character"), CharacterComponent()],
    )
    world.get_entity(scenario.room_a).add_relationship(
        Contains(mode=ContainmentMode.ROOM_CONTENT), hazel.id
    )
    recent = RecentContextProjection(world)
    # Hazel is still physically present but a recent "left." line exists for the room.
    recent._log[str(scenario.room_a)].append("hazel left.")

    ctx = PromptBuilder(world, recent_context=recent).build(scenario.character)

    assert "Hazel just left." in ctx.social_cues


def test_prompt_builder_leaves_recall_empty_even_when_character_has_memory():
    scenario = build_scenario()
    world = scenario.actor.world
    # A room with an empty title and nothing visible produces an empty recall query.
    blank_room = spawn_entity(world, [RoomComponent(title="")])
    world.get_entity(scenario.room_a).remove_relationship(Contains, scenario.character)
    blank_room.add_relationship(Contains(mode=ContainmentMode.ROOM_CONTENT), scenario.character)
    char = world.get_entity(scenario.character)
    char.add_component(MemoryProfileComponent(vector_collection="juniper"))
    ctx = PromptBuilder(world).build(scenario.character)

    assert ctx.location_title == ""
    assert ctx.recall == ()


def test_prompt_builder_bounds_and_normalizes_long_structured_memory():
    scenario = build_scenario()
    character = scenario.actor.world.get_entity(scenario.character)
    character.add_component(MemoryProfileComponent(vector_collection="juniper"))
    store = InMemoryStore()
    store.add(
        "juniper",
        text="Mosslit\nBurrow " + ("important detail " * 40),
        source="manual",
    )
    builder = PromptBuilder(scenario.actor.world)

    context = builder.with_memory(
        builder.build(scenario.character),
        scenario.character,
        store=store,
        limit=1,
        min_score=0.0,
    )

    recalled_text = context.recall[0].split('"', maxsplit=2)[1]
    assert len(recalled_text) == 240
    assert recalled_text.endswith("...")
    assert "\n" not in recalled_text
