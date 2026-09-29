"""One coalesced LLM decision per perceived player action, with no AI reply chains."""

from relics import EntityId

from ..core.components import CharacterComponent, IdentityComponent
from ..core.ecs import container_of, parse_entity_id
from ..core.edges import ControlledBy
from ..core.events import (
    CharacterReactionEvent,
    CommandExecutedEvent,
    CommandRejectedEvent,
    EventVisibility,
    event_base,
    serialized_event_visible_to,
)
from ..core.world_actor import WorldActor
from ..prompts.builder import PromptBuilder
from .agent import CharacterAgent
from .dispatch import ControllerDispatch


class PlayerTurnDispatch(ControllerDispatch):
    """Local authored-scene policy; pending triggers are runtime state, not saved actions."""

    def __init__(self, actor: WorldActor, builder: PromptBuilder, agent: CharacterAgent):
        super().__init__(actor, builder, agent)
        self._ready: dict[EntityId, tuple[EntityId, int]] = {}
        self._pending_reactions: dict[EntityId, str] = {}
        for character in actor.world.query().with_all([CharacterComponent]).execute_entities():
            self._state_for(str(character.id))
        actor.bus.subscribe(CommandExecutedEvent, self._player_action)
        actor.bus.subscribe(CommandRejectedEvent, self._agent_rejection)

    def _room_of(self, raw_id: str) -> str | None:
        entity_id = parse_entity_id(raw_id)
        if entity_id is None or not self.actor.world.has_entity(entity_id):
            return None
        room_id = container_of(self.actor.world.get_entity(entity_id))
        return str(room_id) if room_id is not None else None

    async def _player_action(self, event: CommandExecutedEvent) -> None:
        actor_id = parse_entity_id(event.actor_id or "")
        if actor_id is None or not self.actor.world.has_entity(actor_id):
            return
        player_id = self._pending_reactions.pop(actor_id, None)
        if player_id is not None:
            visible_result = any(
                serialized_event_visible_to(
                    result,
                    character_id=player_id,
                    room_of=self._room_of,
                )
                for result in event.result_events
            )
            if not visible_result:
                await self._publish_reaction(actor_id, player_id, event.command_type)
            return
        actor = self.actor.world.get_entity(actor_id)
        controls = actor.get_relationships(ControlledBy)
        if not controls or self.actor._controller_kind(controls[0][1]) not in {
            "web",
            "discord",
            "mcp",
        }:
            return
        if event.command_type in {"look", "inspect", "wait"}:
            return
        visible = []
        addressed_ids: set[EntityId] = set()
        text = event.payload.get("text", "")
        for character in self.actor.world.query().with_all([CharacterComponent]).execute_entities():
            if (
                event.command_type == "say"
                and isinstance(text, str)
                and character.has_component(IdentityComponent)
                and text.lstrip().startswith(character.get_component(IdentityComponent).name)
            ):
                addressed_ids.add(character.id)
            controller = self._autonomous_controller(character.id)
            if controller is None:
                continue
            if any(
                serialized_event_visible_to(
                    result, character_id=str(character.id), room_of=self._room_of
                )
                or (
                    result.get("event_type") == "ActorMovedEvent"
                    and result.get("to_room_id") == self._room_of(str(character.id))
                )
                for result in event.result_events
            ):
                visible.append((character, controller))
        # Only an explicit leading name counts as addressing someone; mentioning a
        # name later in a sentence must not redirect the audience.
        for character, controller in visible:
            if addressed_ids and character.id not in addressed_ids:
                continue
            self._ready[character.id] = (controller[0], controller[1])
            if character.id in addressed_ids:
                self._pending_reactions[character.id] = str(actor_id)

    async def _agent_rejection(self, event: CommandRejectedEvent) -> None:
        actor_id = parse_entity_id(event.actor_id or "")
        if actor_id is None:
            return
        player_id = self._pending_reactions.pop(actor_id, None)
        if player_id is not None:
            await self._publish_reaction(
                actor_id,
                player_id,
                event.command_type,
                f"动作被拒绝：{event.reason}",
            )

    async def _publish_reaction(
        self,
        actor_id: EntityId,
        player_id: str,
        command_type: str,
        summary: str | None = None,
    ) -> None:
        if summary is None:
            summary = {
                "wait": "暂时没有行动。",
                "look": "观察了周围环境。",
                "inspect": "进行了观察。",
                "move": "采取了移动行动。",
                "say": "进行了公开发言。",
                "tell": "发送了私下消息。",
                "forest-scene": "推进了剧情。",
            }.get(command_type, "采取了行动。")
        await self.actor.bus.publish(
            CharacterReactionEvent(
                **event_base(
                    self.actor.epoch,
                    default_visibility=EventVisibility.DIRECTED,
                    actor_id=str(actor_id),
                    target_ids=(player_id,),
                ),
                command_type=command_type,
                summary=summary,
            )
        )

    def _actable_characters(self) -> list[EntityId]:
        eligible = []
        for character_id in super()._actable_characters():
            expected = self._ready.get(character_id)
            controller = self._autonomous_controller(character_id)
            if expected is None:
                continue
            if controller is None or expected != (controller[0], controller[1]):
                self._ready.pop(character_id)
                self._pending_reactions.pop(character_id, None)
                continue
            if self._has_pending(str(character_id)):
                continue
            self._ready.pop(character_id)
            eligible.append(character_id)
        return eligible

    def close(self) -> None:
        self.actor.bus.unsubscribe(CommandExecutedEvent, self._player_action)
        self.actor.bus.unsubscribe(CommandRejectedEvent, self._agent_rejection)
        self._ready.clear()
        self._pending_reactions.clear()
        super().close()
