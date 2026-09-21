"""Authored scene behavior, information boundaries, and restart/handoff contracts."""

import asyncio
from dataclasses import replace

import pytest

from bunnyland.core.components import DeadComponent, IdentityComponent
from bunnyland.core.controllers import (
    BehaviorControllerComponent,
    LLMControllerComponent,
    WebControllerComponent,
)
from bunnyland.core.ecs import container_of, parse_entity_id, replace_component, spawn_entity
from bunnyland.core.edges import Contains, ControlledBy
from bunnyland.core.events import ActorMovedEvent, event_base, serialized_event_visible_to
from bunnyland.core.handlers.base import HandlerContext
from bunnyland.core.mutations import execute_mutation_plan
from bunnyland.core.world_actor import WorldActor
from bunnyland.foundation.persona.mechanics import GoalComponent
from bunnyland.foundation.social.mechanics import GossipClaimComponent, SocialBond, known_gossip
from bunnyland.llm_agents import ControllerDispatch, ScriptedAgent, ToolCall
from bunnyland.llm_agents.tools import command_from_tool_call
from bunnyland.persistence import WorldMeta, load_world, save_world
from bunnyland.plugins import PluginRegistry, apply_plugins, bunnyland_plugins
from bunnyland.plugins.loader import collect_content_items
from bunnyland.prompts.builder import PromptBuilder
from bunnyland.prompts.facts import collect_prompt_facts
from bunnyland.worldgen import GenOptions
from bunnyland.worldgen.wild_boar_forest import (
    NAMES,
    SECRETS,
    STAGE_TEXT,
    ForestMovementReactor,
    ForestSceneComponent,
    ForestSceneEvent,
    ForestSceneHandler,
    forest_decision,
    forest_facts,
    wild_boar_forest_generator,
)


@pytest.fixture
async def scene():
    actor = WorldActor()
    apply_plugins(bunnyland_plugins(), actor)
    result = await wild_boar_forest_generator(actor, "野猪林", GenOptions())
    return actor, result


def builder(actor):
    plugins = bunnyland_plugins()
    return PromptBuilder(
        actor.world,
        fragment_providers=actor.prompt_fragment_providers,
        persona_providers=tuple(collect_content_items(plugins, "persona_fragments")),
    )


def command(actor, cid, choice=None, *, tool="forest_scene", **arguments):
    edge, controller = actor.world.get_entity(cid).get_relationships(ControlledBy)[0]
    return command_from_tool_call(
        ToolCall(tool, {"choice": choice} if choice is not None else arguments),
        character_id=str(cid),
        controller_id=str(controller),
        controller_generation=edge.generation,
        submitted_at_epoch=actor.epoch,
        definitions=actor.action_definitions(),
    )


def direct(actor, result, role, choice):
    return ForestSceneHandler().execute(
        HandlerContext(actor.world, actor.epoch),
        command(actor, result.characters[role], choice),
    )


def apply(actor, result, role, choice):
    outcome = direct(actor, result, role, choice)
    assert outcome.ok, outcome.reason
    execute_mutation_plan(actor.world, outcome.plan)
    return outcome


async def act(actor, result, role, choice=None, *, tool="forest_scene", **arguments):
    cmd = command(actor, result.characters[role], choice, tool=tool, **arguments)
    outcome = await actor.submit(cmd)
    assert outcome.accepted
    await actor.tick(1)
    return actor.receipt_for(cmd.character_id, cmd.command_id)


def stage(actor, result):
    return (
        actor.world.get_entity(result.rooms["clearing"]).get_component(ForestSceneComponent).stage
    )


def knowledge(actor, cid):
    return tuple(
        e.get_component(GossipClaimComponent).text for e, _ in known_gossip(actor.world, cid)
    )


async def test_authored_scene_and_private_knowledge(scene):
    actor, result = scene
    assert len(result.rooms) == 3
    assert len(result.characters) == 4
    for role, cid in result.characters.items():
        entity = actor.world.get_entity(cid)
        assert entity.get_component(IdentityComponent).name == NAMES[role]
        assert entity.get_component(GoalComponent).active_goals
        assert knowledge(actor, cid) == (SECRETS[role],)
    prompt = builder(actor)
    lin = prompt.build(result.characters["lin"])
    lu = prompt.build(result.characters["lu"])
    assert "鲁智深" not in lin.visible_characters
    assert lu.visible_characters == ()
    assert "forest.stage" not in {f.key for f in lu.facts}
    assert SECRETS["dong"] not in repr(lin)
    assert SECRETS["lu"] not in repr(lin)
    assert SECRETS["dong"] in repr(prompt.build(result.characters["dong"]))
    # Public inspection cannot turn a target's self-only provider into an oracle.
    public = collect_prompt_facts(
        actor.world,
        actor.world.get_entity(result.characters["dong"]),
        actor.prompt_fragment_providers,
        cutoff=30,
        viewer=actor.world.get_entity(result.characters["lin"]),
    )
    assert not any(f.key.startswith("forest.") for f in public)


async def test_deterministic_rescue_through_dispatch(scene):
    actor, result = scene
    events = []
    actor.bus.subscribe(ForestSceneEvent, events.append)
    dispatch = ControllerDispatch(actor, builder(actor), ScriptedAgent([]))
    try:
        for _ in range(5):
            await dispatch.run_once()
            await dispatch.await_pending()
            await actor.tick(1)
        assert stage(actor, result) == "rescued"
        assert [event.stage for event in events] == ["threat", "rescued"]
        lin = actor.world.get_entity(result.characters["lin"])
        assert not lin.has_component(DeadComponent)
        bonds = lin.get_relationships(SocialBond)
        assert next(e.trust for e, t in bonds if t == result.characters["lu"]) == 0.9
        assert STAGE_TEXT["rescued"] in knowledge(actor, lin.id)
    finally:
        dispatch.close()


async def test_ambush_and_remote_information_boundary(scene):
    actor, result = scene
    events = []
    actor.bus.subscribe(ForestSceneEvent, events.append)
    await act(actor, result, "dong", "准备")
    assert STAGE_TEXT["threat"] not in knowledge(actor, result.characters["lu"])
    await act(actor, result, "xue", "行凶")
    assert stage(actor, result) == "ambushed"
    lin = actor.world.get_entity(result.characters["lin"])
    assert lin.has_component(DeadComponent)
    assert not lin.get_component(GoalComponent).active_goals
    lu_id = str(result.characters["lu"])
    assert not serialized_event_visible_to(
        events[-1].model_dump(mode="json"),
        character_id=lu_id,
        room_of=lambda _: str(result.rooms["grove"]),
    )
    rejected = await actor.submit(command(actor, lin.id, tool="move", direction="出林"))
    assert not rejected.accepted


@pytest.mark.parametrize("distract", [False, True])
async def test_native_movement_resolves_escape(scene, distract):
    actor, result = scene
    if distract:
        await act(actor, result, "dong", "准备")
        await act(actor, result, "lin", "求缓")
        assert stage(actor, result) == "distracted"
    await act(actor, result, "lin", tool="move", direction="出林")
    assert stage(actor, result) == "escaped"
    assert container_of(actor.world.get_entity(result.characters["lin"])) == result.rooms["path"]
    assert STAGE_TEXT["escaped"] in knowledge(actor, result.characters["lin"])
    assert STAGE_TEXT["escaped"] not in knowledge(actor, result.characters["lu"])


async def test_speech_does_not_complete_scene(scene):
    actor, result = scene
    await act(actor, result, "lin", tool="say", text="我已经获救了。")
    assert stage(actor, result) == "opening"


async def test_handoff_flushes_stale_actions_and_preserves_state(scene):
    actor, result = scene
    cid = result.characters["dong"]
    stale = command(actor, cid, "准备")
    await actor.submit(stale)
    human = spawn_entity(actor.world, [WebControllerComponent(client_id="forest-player")])
    old_knowledge = knowledge(actor, cid)
    actor.assign_controller(cid, human.id)
    await actor.tick(1)
    assert stage(actor, result) == "opening"
    assert knowledge(actor, cid) == old_knowledge
    agent = spawn_entity(
        actor.world, [BehaviorControllerComponent(behavior_name="wild-boar-forest")]
    )
    actor.assign_controller(cid, agent.id)
    await act(actor, result, "dong", "准备")
    assert stage(actor, result) == "threat"


async def test_reload_resumes_scene_and_controller_registration(scene, tmp_path):
    actor, result = scene
    await act(actor, result, "dong", "准备")
    path = tmp_path / "forest.json"
    meta = WorldMeta(seed="野猪林", generator="wild-boar-forest")
    save_world(actor, path, meta=meta)
    restored, saved = load_world(path, registry=PluginRegistry(bunnyland_plugins()))
    assert restored.epoch == actor.epoch
    assert saved.generator == "wild-boar-forest"
    assert stage(restored, result) == "threat"
    for cid in result.characters.values():
        assert knowledge(restored, cid) == knowledge(actor, cid)
    await act(restored, result, "lu", tool="move", direction="现身")
    await act(restored, result, "lu", "救援")
    assert stage(restored, result) == "rescued"
    await act(restored, result, "lin", tool="move", direction="出林")
    assert stage(restored, result) == "rescued"


async def test_qwen_scene_handoff_and_reload(monkeypatch, tmp_path):
    from bunnyland.repl.client import BunnylandRepl
    from bunnyland.terminal_config import ResolvedTerminalChatConfig
    from bunnyland.tui.backend import LocalBackend

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Exercise world/control persistence independently of OS credential-file permissions.
    monkeypatch.setattr("bunnyland.tui.backend.load_claim_control", lambda *args: None)
    monkeypatch.setattr("bunnyland.tui.backend.save_claim_control", lambda *args: None)
    monkeypatch.setattr(
        "bunnyland.tui.backend.build_terminal_chat_agent", lambda config: ScriptedAgent([])
    )
    backend = LocalBackend(
        generator="wild-boar-forest",
        client_id="forest-test",
        autorun=False,
        autonomous_llm=True,
        fallback_controller="llm",
        chat_config=ResolvedTerminalChatConfig(
            enabled=True,
            provider="openrouter",
            model="qwen3.8-omni-flash",
            ollama_host="",
            openrouter_server_url="https://example.invalid/v1",
        ),
    )
    await backend.start()
    try:
        from bunnyland.llm_agents.player_turn_dispatch import PlayerTurnDispatch

        assert isinstance(backend._loop.dispatch, PlayerTurnDispatch)
        repl = BunnylandRepl(backend)
        await repl.refresh()
        characters = await backend.fetch_character_list()
        assert len(characters) == 4
        initial_knowledge = {}
        for character in characters:
            cid = parse_entity_id(character.character_id)
            initial_knowledge[cid] = knowledge(backend.actor, cid)
            controller_id = backend.actor.world.get_entity(cid).get_relationships(ControlledBy)[0][
                1
            ]
            config = backend.actor.world.get_entity(controller_id).get_component(
                LLMControllerComponent
            )
            assert (config.model, config.provider) == ("qwen3.8-omni-flash", "openrouter")
        assert "You are now 林冲" in await repl.select_player("林冲")
        assert repl.control.claim_id and repl.control.claim_secret
        claimed = repl.control
        await repl.refresh()
        assert repl.control == claimed
        lin = next(
            cid
            for cid in initial_knowledge
            if backend.actor.world.get_entity(cid).get_component(IdentityComponent).name == "林冲"
        )
        assert (
            backend.actor._controller_kind(
                backend.actor.world.get_entity(lin).get_relationships(ControlledBy)[0][1]
            )
            == "web"
        )
        assert "You are now 鲁智深" in await repl.select_player("鲁智深")
        lin_controller = backend.actor.world.get_entity(lin).get_relationships(ControlledBy)[0][1]
        assert (
            backend.actor.world.get_entity(lin_controller)
            .get_component(LLMControllerComponent)
            .model
            == "qwen3.8-omni-flash"
        )
        assert "Released" in (await repl.dispatch("release")).plain
        path = tmp_path / "qwen-world.json"
        save_world(backend.actor, path, meta=backend.meta)
        restored, _ = load_world(path, registry=PluginRegistry(bunnyland_plugins()))
        for cid, memories in initial_knowledge.items():
            controller_id = restored.world.get_entity(cid).get_relationships(ControlledBy)[0][1]
            config = restored.world.get_entity(controller_id).get_component(LLMControllerComponent)
            assert (config.model, config.provider) == ("qwen3.8-omni-flash", "openrouter")
            assert knowledge(restored, cid) == memories
    finally:
        await backend.close()


@pytest.fixture
async def player_turn_scene(scene):
    from bunnyland.llm_agents.player_turn_dispatch import PlayerTurnDispatch

    actor, result = scene
    for cid in result.characters.values():
        controller = spawn_entity(
            actor.world, [LLMControllerComponent(profile_name="default", model="test")]
        )
        actor.assign_controller(cid, controller.id)
    human = spawn_entity(actor.world, [WebControllerComponent(client_id="test-player")])
    actor.assign_controller(result.characters["lu"], human.id)

    class RecordingAgent:
        def __init__(self):
            self.calls = []
            self.ready = asyncio.Event()
            self.ready.set()

        async def decide(
            self, prompt, context, *, character_id, model=None, provider=None, tools=None
        ):
            self.calls.append((character_id, context))
            await self.ready.wait()
            return ToolCall("say", {"text": "听见了。"})

    agent = RecordingAgent()
    dispatch = PlayerTurnDispatch(actor, builder(actor), agent)
    yield actor, result, dispatch, agent
    dispatch.close()


async def test_player_turn_idle_named_speech_and_no_ai_chain(player_turn_scene):
    actor, result, dispatch, agent = player_turn_scene
    for _ in range(5):
        await dispatch.run_once()
        await actor.tick(1)
    assert not agent.calls
    # Lu is hidden: nobody in the clearing can hear him.
    await act(actor, result, "lu", tool="say", text="林冲，你好吗？")
    await dispatch.run_once()
    assert not agent.calls
    await act(actor, result, "lu", tool="move", direction="现身")
    await dispatch.run_once()
    await dispatch.await_pending()
    assert len(agent.calls) == 3
    await actor.tick(1)
    agent.calls.clear()
    await act(actor, result, "lu", tool="say", text="林冲，你好吗？")
    await dispatch.run_once()
    await dispatch.await_pending()
    assert [cid for cid, _ in agent.calls] == [str(result.characters["lin"])]
    for _ in range(5):
        await actor.tick(1)
        await dispatch.run_once()
        await dispatch.await_pending()
    assert len(agent.calls) == 1
    # Read-only or rejected commands must not wake the residents.
    await act(actor, result, "lu", tool="look")
    await act(actor, result, "lu", tool="move", direction="不存在")
    await dispatch.run_once()
    assert len(agent.calls) == 1


async def test_player_turn_merges_events_during_inflight_request(player_turn_scene):
    actor, result, dispatch, agent = player_turn_scene
    await act(actor, result, "lu", tool="move", direction="现身")
    await dispatch.run_once()
    await dispatch.await_pending()
    await actor.tick(1)
    agent.calls.clear()
    agent.ready.clear()
    await act(actor, result, "lu", tool="say", text="林冲，你好吗？")
    await dispatch.run_once()
    assert len(agent.calls) == 1
    for text in ("林冲，哪里痛？", "林冲，我来救你。"):
        await act(actor, result, "lu", tool="say", text=text)
        await dispatch.run_once()
    assert len(agent.calls) == 1
    agent.ready.set()
    await dispatch.await_pending()
    await dispatch.run_once()
    await dispatch.await_pending()
    assert len(agent.calls) == 2
    await actor.tick(1)
    await dispatch.run_once()
    assert len(agent.calls) == 2


async def test_player_turn_stale_trigger_does_not_survive_handoff(player_turn_scene):
    actor, result, dispatch, agent = player_turn_scene
    await act(actor, result, "lu", tool="move", direction="现身")
    cid = result.characters["lin"]
    human = spawn_entity(actor.world, [WebControllerComponent(client_id="second-player")])
    actor.assign_controller(cid, human.id)
    controller = spawn_entity(
        actor.world, [LLMControllerComponent(profile_name="default", model="test")]
    )
    actor.assign_controller(cid, controller.id)
    await dispatch.run_once()
    await dispatch.await_pending()
    assert {cid for cid, _ in agent.calls} == {
        str(result.characters["dong"]),
        str(result.characters["xue"]),
    }


@pytest.mark.parametrize(
    ("role", "choice", "reason"),
    [
        ("lin", "无效", "unknown forest choice"),
        ("lin", "救援", "choice is not available to this role"),
        ("xue", "行凶", "choice is not available in this stage"),
        ("lu", "救援", "character is not in the forest scene"),
    ],
)
async def test_rejected_choices(scene, role, choice, reason):
    actor, result = scene
    assert direct(actor, result, role, choice).reason == reason
    assert stage(actor, result) == "opening"


async def test_guard_and_terminal_rejections(scene):
    actor, result = scene
    apply(actor, result, "dong", "准备")
    await act(actor, result, "lu", tool="move", direction="现身")
    assert direct(actor, result, "xue", "行凶").reason == "Lu Zhishen is guarding Lin Chong"
    apply(actor, result, "lu", "救援")
    assert direct(actor, result, "dong", "准备").reason == "forest scene is already resolved"


async def test_missing_and_invalid_entity_guards(scene):
    actor, result = scene
    cmd = command(actor, result.characters["dong"], "准备")
    handler, ctx = ForestSceneHandler(), HandlerContext(actor.world, actor.epoch)
    assert handler.execute(ctx, replace(cmd, character_id="bad")).reason == "invalid character id"
    absent = spawn_entity(actor.world)
    actor.world.remove(absent.id)
    assert (
        handler.execute(ctx, replace(cmd, character_id=str(absent.id))).reason
        == "character does not exist"
    )
    actor.world.remove(result.characters["lin"])
    assert direct(actor, result, "dong", "准备").reason == "Lin Chong does not exist"


async def test_unreachable_and_dead_target(scene):
    actor, result = scene
    lin = actor.world.get_entity(result.characters["lin"])
    scene_room = actor.world.get_entity(result.rooms["clearing"])
    scene_room.remove_relationship(Contains, lin.id)
    assert direct(actor, result, "dong", "准备").reason == "Lin Chong is not reachable"
    assert direct(actor, result, "lin", "求缓").reason == "character is not in a room"
    scene_room.add_relationship(Contains(), lin.id)
    replace_component(lin, DeadComponent(died_at_epoch=0, cause="test"))
    assert direct(actor, result, "dong", "准备").reason == "character is dead"


async def test_absent_scene_and_nonparticipant_facts(scene):
    actor, result = scene
    outsider = spawn_entity(actor.world)
    assert forest_facts(actor.world, outsider) == ()
    actor.world.get_entity(result.rooms["clearing"]).remove_component(ForestSceneComponent)
    lin = actor.world.get_entity(result.characters["lin"])
    assert [f.key for f in forest_facts(actor.world, lin)] == ["forest.role"]
    assert direct(actor, result, "lin", "求缓").reason == "character is not in the forest scene"


async def test_xue_decision_uses_visible_guard_and_dead_guard_does_not_protect(scene):
    actor, result = scene
    apply(actor, result, "dong", "准备")
    prompt = builder(actor)
    call = forest_decision(prompt.build(result.characters["xue"]))
    assert call == ToolCall("forest_scene", {"choice": "行凶"})
    await act(actor, result, "lu", tool="move", direction="现身")
    assert forest_decision(prompt.build(result.characters["xue"])) is None
    lu = actor.world.get_entity(result.characters["lu"])
    replace_component(lu, DeadComponent(died_at_epoch=0, cause="test"))
    apply(actor, result, "xue", "行凶")
    assert stage(actor, result) == "ambushed"


async def test_movement_observer_ignores_invalid_or_unrelated_events(scene):
    actor, result = scene
    reactor = ForestMovementReactor(actor)
    for cid, source in (
        ("bad", str(result.rooms["clearing"])),
        (str(result.characters["lin"]), "bad"),
        ("entity_99999999", str(result.rooms["clearing"])),
        (str(result.characters["lin"]), "entity_99999999"),
        (str(result.characters["dong"]), str(result.rooms["clearing"])),
        (str(result.characters["lin"]), str(result.rooms["path"])),
    ):
        await reactor.on_moved(
            ActorMovedEvent(
                **event_base(actor.epoch, actor_id=cid),
                from_room_id=source,
                to_room_id=str(result.rooms["path"]),
                direction="出林",
            )
        )
    assert stage(actor, result) == "opening"


async def test_repeated_distraction_rearms_and_missing_guard_allows_ambush(scene):
    actor, result = scene
    apply(actor, result, "dong", "准备")
    apply(actor, result, "lin", "求缓")
    call = forest_decision(builder(actor).build(result.characters["dong"]))
    assert call == ToolCall("forest_scene", {"choice": "准备"})
    apply(actor, result, "dong", "准备")
    actor.world.remove(result.characters["lu"])
    apply(actor, result, "xue", "行凶")
    assert stage(actor, result) == "ambushed"


async def test_generation_is_repeatable_and_no_model_is_used(scene):
    actor, result = scene
    other = WorldActor()
    apply_plugins(bunnyland_plugins(), other)
    generated = await wild_boar_forest_generator(other, "different seed", GenOptions(llm=True))
    for role in NAMES:
        left = builder(actor).build(result.characters[role])
        right = builder(other).build(generated.characters[role])
        assert left.name == right.name
        assert left.location_title == right.location_title
        assert left.visible_characters == right.visible_characters
        assert left.facts == right.facts
        assert knowledge(actor, result.characters[role]) == knowledge(
            other, generated.characters[role]
        )


@pytest.mark.parametrize("configured", [False, True])
async def test_missing_plugins_rejected_before_generation(configured):
    actor = WorldActor()
    if configured:
        apply_plugins(
            [
                p
                for p in bunnyland_plugins()
                if p.id in {"bunnyland.core_verbs", "bunnyland.worldgen"}
            ],
            actor,
        )
    before = set(actor.world.query().execute_ids())
    with pytest.raises(ValueError, match="wild-boar-forest requires plugins"):
        await wild_boar_forest_generator(actor, "野猪林", GenOptions())
    assert set(actor.world.query().execute_ids()) == before
