"""Developer-owned dialogue journal of committed speech, outside player projections."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from .core.components import IdentityComponent
from .core.ecs import parse_entity_id
from .core.events import SpeechSaidEvent, SpeechToldEvent
from .core.world_actor import WorldActor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DialogueEntry:
    session_id: str
    world_id: str
    event_id: str
    recorded_at: str
    game_epoch_seconds: int
    kind: str
    speaker_id: str | None
    speaker: str
    recipient_ids: tuple[str, ...]
    recipients: tuple[str, ...]
    room_id: str | None
    text: str
    approach: str | None


class DialogueRecorder:
    def __init__(self, actor: WorldActor, path: Path, *, world_id: str) -> None:
        self.actor = actor
        self.path = path
        self.text_path = path.with_suffix(".txt")
        self.world_id = world_id
        self.session_id = uuid4().hex
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        with self.text_path.open("a", encoding="utf-8") as output:
            output.write(f"\n=== 世界 {world_id} / 本次运行 {self.session_id} ===\n")
        actor.bus.subscribe(SpeechSaidEvent, self.record)
        actor.bus.subscribe(SpeechToldEvent, self.record)

    def _name(self, raw_id: str | None) -> str:
        entity_id = parse_entity_id(raw_id or "")
        if entity_id is not None and self.actor.world.has_entity(entity_id):
            entity = self.actor.world.get_entity(entity_id)
            if entity.has_component(IdentityComponent):
                return entity.get_component(IdentityComponent).name
        return raw_id or "未知人物"

    def record(self, event: SpeechSaidEvent | SpeechToldEvent) -> None:
        directed = isinstance(event, SpeechToldEvent)
        entry = DialogueEntry(
            session_id=self.session_id,
            world_id=self.world_id,
            event_id=event.event_id,
            recorded_at=event.created_at.isoformat(),
            game_epoch_seconds=event.world_epoch,
            kind="directed" if directed else "public",
            speaker_id=event.actor_id,
            speaker=self._name(event.actor_id),
            recipient_ids=event.target_ids if directed else (),
            recipients=tuple(self._name(target) for target in event.target_ids) if directed else (),
            room_id=event.room_id,
            text=event.text,
            approach=event.approach,
        )
        audience = "、".join(entry.recipients) if directed else "在场众人"
        delivery = f"（{entry.approach}）" if entry.approach else ""
        # Indent every line so multiline dialogue stays distinct from record headings.
        spoken = "\n".join("    " + line for line in entry.text.splitlines())
        try:
            with self.path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            with self.text_path.open("a", encoding="utf-8") as output:
                output.write(
                    f"[游戏时间 {entry.game_epoch_seconds} 秒] "
                    f"{entry.speaker} → {audience}{delivery}\n{spoken}\n\n"
                )
        except OSError:
            logger.exception("Could not append dialogue to %s", self.path)

    def close(self) -> None:
        self.actor.bus.unsubscribe(SpeechSaidEvent, self.record)
        self.actor.bus.unsubscribe(SpeechToldEvent, self.record)
