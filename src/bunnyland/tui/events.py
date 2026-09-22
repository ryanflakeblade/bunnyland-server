"""Shared event narration for Textual clients."""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text

from ..core.actions import action_icon_for
from ..core.events import serialized_event_visible_to

# Events that would drown out narration rather than describe activity: command lifecycle,
# continuous point/need/affect telemetry, and perception bookkeeping.
_UNNARRATED_EVENT_TYPES = frozenset(
    {
        "CommandSubmittedEvent",
        "CommandAcceptedEvent",
        "CommandQueuedEvent",
        "CommandExecutedEvent",
        "CommandExpiredEvent",
        "ActionPointsChangedEvent",
        "FocusPointsChangedEvent",
        "EncumbranceChangedEvent",
        "PainChangedEvent",
        "BleedingChangedEvent",
        "AttentionShiftedEvent",
        "AffectChangedEvent",
        "EntitySeenEvent",
        "RoomQualityUpdatedEvent",
        "HungerChangedEvent",
        "ThirstChangedEvent",
        "DailyNeedChangedEvent",
        "SkillXPChangedEvent",
    }
)

_SYSTEM_EVENT_TYPES = frozenset(
    {
        "ControllerChangedEvent",
        "WorldPauseStatusChangedEvent",
    }
)

EVENT_ICON_BY_TYPE: dict[str, str] = {
    "ActorMovedEvent": "➡️",
    "RoomLookedEvent": "👁️",
    "CommandRejectedEvent": "⚠️",
    "ControllerChangedEvent": "🎮",
    "WorldPauseStatusChangedEvent": "⏸️",
    "CharacterClaimedEvent": "🎮",
}

# Fields on every ``DomainEvent``; the rest of a serialized event is its specific payload.
_EVENT_BASE_KEYS = frozenset(
    {
        "event_id",
        "world_epoch",
        "created_at",
        "visibility",
        "actor_id",
        "room_id",
        "target_ids",
        "causation_id",
        "correlation_id",
        "arrival_summary",
    }
)


def _humanize_event_type(event_type: str) -> str:
    """``ResourceGatheredEvent`` -> ``Resource gathered`` (splits on CamelCase)."""
    name = event_type.removesuffix("Event")
    words: list[str] = []
    current = ""
    for char in name:
        if char.isupper() and current:
            words.append(current)
            current = char
        else:
            current += char
    if current:
        words.append(current)
    return " ".join(words).capitalize()


class EventNarrator:
    """Render the not-yet-seen events a player can perceive."""

    def __init__(self) -> None:
        self._seen_event_ids: set[str] = set()

    def drain_events(
        self,
        messages: list[dict],
        *,
        player_id: str,
        room_of: Callable[[str | None], str | None],
        name_for: Callable[[str], str | None],
        show_icons: bool = True,
    ) -> list[Text]:
        rendered: list[Text] = []
        current: set[str] = set()
        for message in messages:
            data = message.get("data", message)
            event = data.get("event", {})
            event_id = event.get("event_id")
            if event_id is None:
                continue
            current.add(event_id)
            if event_id in self._seen_event_ids:
                continue
            event_type = data.get("event_type")
            if event_type in _UNNARRATED_EVENT_TYPES:
                continue
            own = bool(player_id) and event.get("actor_id") == player_id
            if own or self._perceives(event, player_id=player_id, room_of=room_of):
                rendered.append(
                    self._render_event(
                        data,
                        player_id=player_id,
                        name_for=name_for,
                        show_icons=show_icons,
                    )
                )
        self._seen_event_ids = current
        return rendered

    def _perceives(
        self,
        event: dict,
        *,
        player_id: str,
        room_of: Callable[[str | None], str | None],
    ) -> bool:
        return serialized_event_visible_to(
            event,
            character_id=player_id,
            room_of=lambda character_id: room_of(character_id),
        )

    def _render_event(
        self,
        data: dict,
        *,
        player_id: str,
        name_for: Callable[[str], str | None],
        show_icons: bool = True,
    ) -> Text:
        event = data.get("event", {})
        event_type = str(data.get("event_type", "Event"))
        if (
            event_type == "ActorMovedEvent"
            and bool(player_id)
            and event.get("actor_id") == player_id
            and event.get("arrival_summary")
        ):
            prefix = "➡️ " if show_icons else ""
            return Text(f"{prefix}{event['arrival_summary']}")
        if event_type == "RoomLookedEvent" and event.get("summary"):
            prefix = "👁️ " if show_icons else ""
            return Text(f"{prefix}{event['summary']}")
        if event_type in {"SpeechSaidEvent", "SpeechToldEvent"}:
            speaker = name_for(event.get("actor_id") or "") or "Someone"
            approach = event.get("approach")
            if approach:
                speaker += f"（{approach}）"
            if event_type == "SpeechToldEvent":
                recipients = [name_for(str(target)) for target in event.get("target_ids", ())]
                names = [name for name in recipients if name]
                if names:
                    speaker += " → " + "、".join(names)
            prefix = "💬 " if show_icons else ""
            line = Text(f"{prefix}{speaker}：{event.get('text', '')}")
            intents = [
                f"{label}：{event[key]}"
                for key, label in (
                    ("author_intent", "表达意图"),
                    ("inferred_intent", "系统推断"),
                    ("final_interpretation", "最终解读"),
                )
                if event.get(key)
            ]
            if intents:
                line.append("\n    [" + "；".join(intents) + "]", style="dim")
            return line
        label = _humanize_event_type(event_type)
        icon = _event_icon(event_type, event) if show_icons else ""
        actor = name_for(event.get("actor_id") or "") if event.get("actor_id") else None
        details: list[str] = []
        for key, value in event.items():
            if key in _EVENT_BASE_KEYS or value in (None, "", (), []):
                continue
            if key.endswith("_ids"):
                names = [name_for(str(item)) for item in value]
                names = [name for name in names if name]
                if names:
                    details.append(", ".join(names))
            elif key.endswith("_id"):
                name = name_for(str(value))
                if name is not None:
                    details.append(name)
            elif key == "facts" and isinstance(value, list | tuple):
                details.extend(
                    str(fact.get("text", "")).strip()
                    for fact in value
                    if isinstance(fact, dict) and str(fact.get("text", "")).strip()
                )
            else:
                details.append(f"{key.replace('_', ' ')} {value}")
        line = f"{actor}: {label}" if actor else label
        if icon:
            line = f"{icon} {line}"
        if details:
            line += f" — {'; '.join(details)}"
        if event_type == "CommandRejectedEvent":
            return Text(line, style="dark_orange")
        if event_type in _SYSTEM_EVENT_TYPES:
            return Text(line, style="dim")
        return Text(line)


def _event_icon(event_type: str, event: dict) -> str:
    if event_type == "CommandRejectedEvent" and event.get("command_type"):
        return action_icon_for(str(event["command_type"]))
    return EVENT_ICON_BY_TYPE.get(event_type, "•")
