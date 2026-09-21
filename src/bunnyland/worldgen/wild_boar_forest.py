"""Authored Water Margin scene; decisions use only character-scoped prompt facts."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from pydantic.dataclasses import dataclass
from relics import Component, Edge, Entity, World

from ..core.actions import ActionDefinition, ActionRequirement, define_action
from ..core.commands import SubmittedCommand
from ..core.components import DeadComponent
from ..core.ecs import container_of, parse_entity_id, reachable_ids, spawn_entity
from ..core.events import ActorMovedEvent, CharacterDiedEvent, DomainEvent, EventVisibility
from ..core.handlers.base import HandlerContext, HandlerResult, planned, rejected
from ..core.mutations import (
    AddEdge,
    AddEntity,
    EntityReference,
    MutationOperation,
    MutationPlan,
    SetComponent,
    execute_mutation_plan,
)
from ..core.world_actor import WorldActor
from ..foundation.persona.mechanics import GoalComponent
from ..foundation.social.mechanics import GossipClaimComponent, KnowsGossip, SocialBond
from ..llm_agents.behavior_tree import Action, BehaviorTree, register_behavior_tree
from ..llm_agents.tools import ToolCall
from ..plugins.ids import CORE_VERBS, PERSONA, SOCIAL
from ..prompts.builder import PromptContext
from ..prompts.facts import PromptFact
from .generators import GenOptions
from .instantiate import InstantiatedWorld, instantiate
from .proposal import CharacterSpec, ExitSpec, RoomSpec, WorldProposal

Role = Literal["lin", "lu", "dong", "xue"]
Stage = Literal["opening", "threat", "distracted", "rescued", "escaped", "ambushed"]
TERMINAL = frozenset({"rescued", "escaped", "ambushed"})
FOREST_DESCRIPTION = "野猪林：林冲、鲁智深与两名差役的可分支场景。"
STAGE_TEXT: dict[Stage, str] = {
    "opening": "押解队伍在古树下暂歇，林冲尚未被捆绑。",
    "threat": "董超拦住退路，薛霸举起水火棍，林冲面临危险。",
    "distracted": "林冲请求暂缓动手，差役一时迟疑，出现脱身机会。",
    "rescued": "鲁智深出手制止差役，林冲获救。",
    "escaped": "林冲离开危险空地，成功脱险。",
    "ambushed": "无人及时阻止，薛霸杀害了林冲。",
}
NAMES: dict[Role, str] = {"lin": "林冲", "lu": "鲁智深", "dong": "董超", "xue": "薛霸"}
SECRETS: dict[Role, str] = {
    "lin": "差役一路刁难，我心有戒备，但不清楚他们的完整计划。",
    "lu": "我正在暗中跟随押解队伍，打算保护林冲。",
    "dong": "我与薛霸约定在野猪林加害林冲，再伪称意外。",
    "xue": "我与董超约定在野猪林加害林冲，由我动手。",
}
GOALS: dict[Role, str] = {
    "lin": "保住性命，设法脱险。",
    "lu": "保护林冲，阻止差役加害。",
    "dong": "配合薛霸加害林冲，避免承担后果。",
    "xue": "执行与董超商议的加害计划。",
}


@dataclass(frozen=True)
class ForestSceneComponent(Component):
    """Singleton scene progress on its clearing; contains no live entity references."""

    stage: Stage = "opening"


@dataclass(frozen=True)
class ForestResident(Edge):
    """Character -> clearing, one role per scene, many residents per clearing.

    Relics removes links when either endpoint is deleted. Roles are private prompt
    facts; other residents and remote scene state are never disclosed by this edge.
    This new edge needs no migration of existing worlds.
    """

    role: Role


class ForestSceneEvent(DomainEvent):
    stage: Stage
    text: str


FOREST_ACTIONS: tuple[ActionDefinition, ...] = (
    define_action(
        "forest-scene",
        args=("choice",),
        required=("choice",),
        description="野猪林场景行动，可选：准备、求缓、救援、行凶。由角色与现场状态决定能否执行。",
        examples=("forest-scene choice=救援",),
        requirement=ActionRequirement(character_edges=("ForestResident",)),
    ),
)


def forest_facts(world: World, character: Entity) -> tuple[PromptFact, ...]:
    """Self-only provider: inspection excludes providers without a viewer argument."""
    links = character.get_relationships(ForestResident)
    if not links:
        return ()
    role, scene_id = links[0]
    facts = [PromptFact("forest.role", NAMES[role.role])]
    if container_of(character) == scene_id:
        scene = world.get_entity(scene_id)
        if scene.has_component(ForestSceneComponent):
            stage = scene.get_component(ForestSceneComponent).stage
            facts.append(PromptFact("forest.stage", STAGE_TEXT[stage]))
    return tuple(facts)


def forest_decision(context: PromptContext) -> ToolCall | None:
    facts = {fact.key: fact.text for fact in context.facts}
    role = next((key for key, name in NAMES.items() if name == facts.get("forest.role")), None)
    stage = next(
        (key for key, text in STAGE_TEXT.items() if text == facts.get("forest.stage")), None
    )
    if stage in TERMINAL:
        return None
    if role == "lu" and context.location_title == "隐蔽树林":
        return ToolCall("move", {"direction": "现身"})
    choice = None
    if role == "dong" and stage in {"opening", "distracted"}:
        choice = "准备"
    elif role == "lu" and stage in {"threat", "distracted"}:
        choice = "救援"
    elif role == "xue" and stage == "threat":
        if not any(name.startswith("鲁智深") for name in context.visible_characters):
            choice = "行凶"
    return ToolCall("forest_scene", {"choice": choice}) if choice else None


def _residents(scene: Entity, world: World) -> dict[Role, Entity]:
    return {
        edge.role: world.get_entity(source)
        for source, edge in scene.get_incoming_relationships(ForestResident)
    }


def _transition(
    scene: Entity,
    stage: Stage,
    witnesses: tuple[Entity, ...],
    epoch: int,
) -> MutationPlan:
    """Persist one observation entity shared only by the actual witnesses."""
    observation = EntityReference()
    operations: list[MutationOperation] = [
        SetComponent(scene.id, replace(scene.get_component(ForestSceneComponent), stage=stage)),
        AddEntity(
            (GossipClaimComponent(text=STAGE_TEXT[stage], created_at_epoch=epoch),),
            reference=observation,
        ),
    ]
    for witness in witnesses:
        operations.append(AddEdge(witness.id, observation, KnowsGossip(learned_at_epoch=epoch)))
    return MutationPlan(tuple(operations))


class ForestSceneHandler:
    command_type = "forest-scene"

    def execute(self, ctx: HandlerContext, command: SubmittedCommand) -> HandlerResult:
        character_id = parse_entity_id(command.character_id)
        if character_id is None:
            return rejected("invalid character id")
        if not ctx.world.has_entity(character_id):
            return rejected("character does not exist")
        character = ctx.entity(character_id)
        room_id = container_of(character)
        if room_id is None or not ctx.world.has_entity(room_id):
            return rejected("character is not in a room")
        scene = ctx.entity(room_id)
        links = character.get_relationships(ForestResident)
        role = next((edge.role for edge, target in links if target == room_id), None)
        if not scene.has_component(ForestSceneComponent) or role is None:
            return rejected("character is not in the forest scene")
        residents = _residents(scene, ctx.world)
        lin = residents.get("lin")
        if lin is None:
            return rejected("Lin Chong does not exist")
        if lin.id not in reachable_ids(ctx.world, character):
            return rejected("Lin Chong is not reachable")
        if character.has_component(DeadComponent) or lin.has_component(DeadComponent):
            return rejected("character is dead")
        stage = scene.get_component(ForestSceneComponent).stage
        if stage in TERMINAL:
            return rejected("forest scene is already resolved")
        choice = command.payload.get("choice")
        choices: dict[str, tuple[Role, tuple[Stage, ...], Stage]] = {
            "准备": ("dong", ("opening", "distracted"), "threat"),
            "求缓": ("lin", ("threat",), "distracted"),
            "救援": ("lu", ("threat", "distracted"), "rescued"),
            "行凶": ("xue", ("threat",), "ambushed"),
        }
        if not isinstance(choice, str) or choice not in choices:
            return rejected("unknown forest choice")
        required_role, stages, next_stage = choices[choice]
        if role != required_role:
            return rejected("choice is not available to this role")
        if stage not in stages:
            return rejected("choice is not available in this stage")
        lu = residents.get("lu")
        if choice == "行凶" and lu is not None and container_of(lu) == room_id:
            if not lu.has_component(DeadComponent):
                return rejected("Lu Zhishen is guarding Lin Chong")
        witnesses = tuple(e for e in residents.values() if container_of(e) == room_id)
        plan = _transition(scene, next_stage, witnesses, ctx.epoch)
        operations = list(plan.operations)
        events: list[DomainEvent] = [
            ForestSceneEvent(
                **ctx.event_base(
                    visibility=EventVisibility.DIRECTED,
                    actor_id=str(character_id),
                    room_id=str(room_id),
                    target_ids=tuple(str(e.id) for e in witnesses),
                ),
                stage=next_stage,
                text=STAGE_TEXT[next_stage],
            )
        ]
        if next_stage == "rescued":
            operations.extend(
                (
                    AddEdge(lin.id, character_id, SocialBond(affinity=0.9, trust=0.9)),
                    SetComponent(lin.id, GoalComponent(active_goals=("与鲁智深安全离开野猪林。",))),
                    SetComponent(
                        character_id, GoalComponent(active_goals=("护送林冲离开野猪林。",))
                    ),
                )
            )
        elif next_stage == "ambushed":
            operations.extend(
                (
                    SetComponent(
                        lin.id, DeadComponent(died_at_epoch=ctx.epoch, cause="野猪林伏击")
                    ),
                    SetComponent(lin.id, GoalComponent()),
                )
            )
            events.append(
                CharacterDiedEvent(
                    **ctx.event_base(
                        visibility=EventVisibility.ROOM, actor_id=str(lin.id), room_id=str(room_id)
                    ),
                    cause="野猪林伏击",
                )
            )
        return planned(MutationPlan(tuple(operations)), *events)


class ForestMovementReactor:
    def __init__(self, actor: WorldActor) -> None:
        self.actor = actor

    async def on_moved(self, event: ActorMovedEvent) -> None:
        world = self.actor.world
        cid, room_id = parse_entity_id(event.actor_id), parse_entity_id(event.from_room_id)
        if (
            cid is None
            or room_id is None
            or not world.has_entity(cid)
            or not world.has_entity(room_id)
        ):
            return
        character, scene = world.get_entity(cid), world.get_entity(room_id)
        if not scene.has_component(ForestSceneComponent) or character.has_component(DeadComponent):
            return
        if not any(
            edge.role == "lin" and target == room_id
            for edge, target in character.get_relationships(ForestResident)
        ):
            return
        if scene.get_component(ForestSceneComponent).stage in TERMINAL:
            return
        witnesses = tuple(
            e
            for e in _residents(scene, world).values()
            if e.id == cid or container_of(e) == room_id
        )
        plan = _transition(scene, "escaped", witnesses, self.actor.epoch)
        execute_mutation_plan(
            world,
            MutationPlan(
                (
                    *plan.operations,
                    SetComponent(cid, GoalComponent(active_goals=("寻找安全去处，躲避追赶。",))),
                )
            ),
        )
        await self.actor.bus.publish(
            ForestSceneEvent(
                **self.actor._event_base(
                    visibility=EventVisibility.DIRECTED,
                    actor_id=str(cid),
                    target_ids=tuple(str(e.id) for e in witnesses),
                ),
                stage="escaped",
                text=STAGE_TEXT["escaped"],
            )
        )


def install_forest(actor: WorldActor) -> None:
    # Register before snapshot loading as well as before fresh generation so the
    # action availability gate can resolve this edge by name after a restart.
    actor.world.register_edge_type(ForestResident)
    actor.world.register_component_type(ForestSceneComponent)
    register_behavior_tree(BehaviorTree("wild-boar-forest", Action(forest_decision)))
    actor.bus.subscribe(ActorMovedEvent, ForestMovementReactor(actor).on_moved)


async def wild_boar_forest_generator(
    actor: WorldActor,
    seed: str,
    options: GenOptions,
) -> InstantiatedWorld:
    # Only this scene requires social/persona mechanics; keep the worldgen plugin
    # usable in existing minimal installations that generate other worlds.
    enabled = actor.plugins.plugins if actor.plugins is not None else {}
    missing = sorted({CORE_VERBS, PERSONA, SOCIAL} - set(enabled))
    if missing:
        raise ValueError(f"wild-boar-forest requires plugins: {', '.join(missing)}")
    proposal = WorldProposal(
        seed=seed,
        rooms=[
            RoomSpec(
                key="path",
                title="林间小路",
                biome="树林",
                light=1.0,
                description="通往林外的小路，脚下落叶沙沙作响。",
            ),
            RoomSpec(
                key="clearing",
                title="古树空地",
                biome="树林",
                light=1.0,
                description="古树遮蔽的空地，押解队伍在此暂歇。",
            ),
            RoomSpec(
                key="grove",
                title="隐蔽树林",
                biome="树林",
                light=1.0,
                description="茂密树丛隔开视线，一条窄道通向空地。",
            ),
        ],
        exits=[
            ExitSpec(from_key="path", direction="入林", to_key="clearing"),
            ExitSpec(from_key="clearing", direction="出林", to_key="path"),
            ExitSpec(from_key="grove", direction="现身", to_key="clearing"),
            ExitSpec(from_key="clearing", direction="入丛", to_key="grove"),
        ],
        characters=[
            CharacterSpec(
                key=role,
                name=name,
                species="人",
                room_key="grove" if role == "lu" else "clearing",
                description=f"{name}，身在野猪林。",
                goals=(GOALS[role],),
                controller="llm" if options.llm else "behavioral",
                llm_model=options.model,
                llm_provider=options.provider,
                behavior_name="wild-boar-forest",
                with_needs=False,
            )
            for role, name in NAMES.items()
        ],
    )
    result = await instantiate(actor, proposal)
    async with actor._lock:
        scene = actor.world.get_entity(result.rooms["clearing"])
        scene.add_component(ForestSceneComponent())
        for role in NAMES:
            character = actor.world.get_entity(result.characters[role])
            character.add_relationship(ForestResident(role=role), scene.id)
            memory = spawn_entity(actor.world, [GossipClaimComponent(text=SECRETS[role])])
            character.add_relationship(KnowsGossip(), memory.id)
        for source, target, bond in (
            ("lin", "lu", SocialBond(affinity=0.7, trust=0.6)),
            ("lu", "lin", SocialBond(affinity=0.8, trust=0.7)),
            ("lin", "dong", SocialBond(fear=0.5, trust=-0.3)),
            ("lin", "xue", SocialBond(fear=0.6, trust=-0.4)),
            ("dong", "xue", SocialBond(trust=0.6, familiarity=0.8)),
            ("xue", "dong", SocialBond(trust=0.6, familiarity=0.8)),
        ):
            actor.world.get_entity(result.characters[source]).add_relationship(
                bond, result.characters[target]
            )
    return result
