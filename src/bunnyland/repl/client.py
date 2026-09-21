"""Command handling for the Bunnyland REPL — the I/O-free core behind the Textual app.

It reuses the TUI's backends (``LocalBackend``/``RemoteBackend``) — reading the claim
lobby for the player picker and the player's own room projection for the world — and the
read-only ``World`` model, and accepts two input styles:

* named: ``move direction=north``, ``take item_id=a brass key`` — Tab completes the
  command, then each parameter name, then its value;
* natural: ``go north``, ``take brass key`` — parsed with the shared
  :class:`NaturalCommandParser`.

Both styles resolve human-readable names to entity ids client-side (the raw command
endpoint expects ids), exactly as the TUI sends the ids it picked. Command output is
returned as Rich :class:`~rich.text.Text`, with characters, items, rooms, containers, and
exits rendered as clickable links (``@click`` meta keyed by entity id) that the app turns
into "insert this name into the input" actions.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from rich.style import Style
from rich.text import Text

from ..core.actions import ActionArgument, ActionDefinition, ActionPattern, action_icon_for
from ..imagegen.affordance import (
    DELIVER_EMOJI,
    FAIL_EMOJI,
    REQUEST_EMOJI,
    VIDEO_DELIVER_EMOJI,
    VIDEO_REQUEST_EMOJI,
)
from ..imagegen.feed import (
    latest_image_completion,
    latest_image_failure,
    latest_media_event_id,
    latest_video_completion,
    latest_video_failure,
)
from ..llm_agents.dispatch import suggest_names
from ..llm_agents.natural_language import NaturalCommandParser
from ..server.models import CharacterSummaryView
from ..terminal_generators import available_generators as available_generators
from ..terminal_generators import format_generator_lines as format_generator_lines
from ..tui import events as tui_events
from ..tui.backend import Backend, ControlClaim
from ..tui.model import KIND_ICON, World, entity_icon, entity_name, fmt_points, has
from .completion import complete_line, reference_candidates

META_COMMANDS = (
    "/login",
    "help",
    "who",
    "look",
    "inventory",
    "points",
    "play",
    "release",
    "queued",
    "cancel",
    "refresh",
    "quit",
    "exit",
)
IMAGE_COMMANDS = ("image", "img")
VIDEO_COMMANDS = ("video",)
SHEET_COMMANDS = ("sheet", "profile")
CHAT_COMMANDS = ("chat",)


def _humanize_event_type(event_type: str) -> str:
    return tui_events._humanize_event_type(event_type)


def history_path() -> Path:
    """The REPL history file, beside the TUI's persistent client id."""
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "bunnyland" / "repl-history"


def link(label: str, entity_id: str) -> Text:
    """A clickable target: clicking inserts the target's name into the input. The action is
    keyed by entity id (which never contains quotes) so names with apostrophes are safe.

    The style is applied as a span (not the Text's base style) so it survives ``append_text``
    when the target is embedded in a larger line."""
    text = Text(label)
    text.stylize(Style(color="cyan", underline=True, meta={"@click": f"app.insert({entity_id!r})"}))
    return text


@dataclass(frozen=True)
class ParsedCommand:
    """A command line resolved to a tool name and its raw (unresolved) arguments."""

    tool: str
    arguments: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OpenSheetIntent:
    character_id: str
    character_name: str

    @property
    def plain(self) -> str:
        return f"Open sheet: {self.character_name}"


@dataclass(frozen=True)
class OpenChatIntent:
    character_id: str
    character_name: str

    @property
    def plain(self) -> str:
        return f"Open chat: {self.character_name}"


@dataclass(frozen=True)
class LoginIntent:
    @property
    def plain(self) -> str:
        return "Sign in"


ReplCommandResult = Text | LoginIntent | OpenSheetIntent | OpenChatIntent


def parse_line(line: str, definitions: dict[str, ActionDefinition]) -> ParsedCommand | None:
    """Parse a command line into a :class:`ParsedCommand`.

    A known command followed by ``key=value`` tokens (or nothing) is the named form;
    value tokens without ``=`` extend the preceding value so multi-word names work. Any
    other input falls back to the natural-language parser.
    """
    tokens = line.split()
    if not tokens:
        return None
    command = tokens[0]
    named = command in definitions and (len(tokens) == 1 or "=" in tokens[1])
    if named:
        arguments: dict[str, str] = {}
        current = ""  # the first argument token always contains '=', so this is set before use
        for token in tokens[1:]:
            if "=" in token:
                key, _, value = token.partition("=")
                arguments[key] = value
                current = key
            else:  # a bare token continues the previous value (multi-word names)
                arguments[current] = f"{arguments[current]} {token}".strip()
        return ParsedCommand(definitions[command].name, arguments)

    call = NaturalCommandParser(list(definitions.values())).parse(line)
    if call is None:
        return None
    return ParsedCommand(call.name, {k: v for k, v in call.arguments.items() if isinstance(v, str)})


def resolve_name(value: str, world: World, candidates: list[tuple[str, str]]) -> str:
    """Resolve a human-readable name to an entity id, mirroring the server's dispatch:
    valid ids pass through, then an exact (case-insensitive) name, then the shortest name
    with the query as a prefix. Unresolvable values are returned unchanged so the handler
    rejects them observably."""
    if value in world.entities:
        return value
    query = value.strip().lower()
    if not query:
        return value
    for name, entity_id in candidates:
        if name.lower() == query:
            return entity_id
    matches = sorted(
        (nc for nc in candidates if nc[0].lower().startswith(query)),
        key=lambda nc: (len(nc[0]), nc[0].lower()),
    )
    return matches[0][1] if matches else value


class BunnylandRepl:
    """REPL state and command handling over a snapshot :class:`Backend`."""

    def __init__(
        self,
        backend: Backend,
        *,
        show_icons: bool = True,
        definitions: tuple[ActionDefinition, ...] = (),
    ) -> None:
        self.backend = backend
        self.show_icons = show_icons
        self.world = World()
        self.player_id = ""
        self.control: ControlClaim | None = None
        self.character_list: list[CharacterSummaryView] = []
        self._defs = {definition.name: definition for definition in definitions}
        self._events = tui_events.EventNarrator()
        self._event_image_url = ""
        self._event_image_failure_epoch = -1
        self._event_video_url = ""
        self._event_video_failure_epoch = -1
        self._latest_media_event_id = ""
        self._refresh_task: asyncio.Task[None] | None = None

    # ── data ──────────────────────────────────────────────────────────────────
    async def refresh(self) -> None:
        task = self._refresh_task
        if task is None:
            task = asyncio.create_task(self._refresh_once())
            self._refresh_task = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._refresh_task is task:
                self._refresh_task = None

    async def _refresh_once(self) -> None:
        # The picker comes from the claim lobby; the world is only ever the player's own
        # perceived room (their character projection), never the admin-gated full snapshot.
        self.character_list = await self.backend.fetch_character_list()
        known_ids = {summary.character_id for summary in self.character_list}
        if self.player_id and self.player_id not in known_ids:
            self.player_id = ""
            self.control = None
        if self.player_id:
            projection = await self.backend.fetch_character_projection(self.player_id)
            if projection and projection.get("character_id") == self.player_id:
                self.world = World.parse(projection)
                if self.world.actions:
                    self._sync_action_definitions()
                projected_control = self.world.control(self.player_id)
                if self.control:
                    if projected_control and projected_control[0] == self.control[0]:
                        self.control = replace(self.control, generation=projected_control[1])
                    else:
                        self.control = None
            else:
                self.world = World()
                if projection is None:
                    self.control = None
        else:
            self.world = World()

    def _sync_action_definitions(self) -> None:
        """Build the REPL command surface from the server's serialized registry view."""

        self._defs = {}
        for action in self.world.actions:
            arguments = {
                argument["key"]: ActionArgument(
                    title=argument.get("title", ""),
                    kind=argument.get("kind", "string"),
                    required=argument.get("required", False),
                )
                for argument in action.get("arguments", ())
            }
            definition = ActionDefinition(
                command_type=action["command_type"],
                tool_name=action.get("tool_name"),
                title=action.get("title", ""),
                description=action.get("description", ""),
                icon=action.get("icon", ""),
                arguments=arguments,
                natural_patterns=tuple(
                    ActionPattern(
                        pattern["text"],
                        pattern.get("fixed_arguments"),
                        pattern.get("argument_aliases"),
                    )
                    for pattern in action.get("natural_patterns", ())
                ),
            )
            self._defs[definition.name] = definition

    async def select_player(self, name: str) -> str:
        candidates = [(summary.name, summary.character_id) for summary in self.character_list]
        ids = {summary.character_id for summary in self.character_list}
        chosen = name if name in ids else resolve_name(name, self.world, candidates)
        summary = next((s for s in self.character_list if s.character_id == chosen), None)
        if summary is None:
            return f"No such player: {name!r}. Try 'who'."
        if self.player_id and self.player_id != chosen:
            released = await self._release()
            if self.player_id:
                return released.plain
        self.control = await self.backend.claim(chosen, self.world)
        if self.control is None:
            return f"Could not claim {summary.name}."
        self.player_id = chosen
        await self.refresh()
        return f"You are now {summary.name}."

    async def _release(self) -> Text:
        if not self.player_id:
            return Text("You aren't playing a character.")
        name = entity_name(self.world.get(self.player_id)) or self.player_id
        if self.control is not None:
            released = await self.backend.release_controller(self.player_id, self.control)
            if released is None:
                return Text(f"Could not release {name}; you still control this character.")
        self.player_id = ""
        self.control = None
        self.world = World()
        await self.refresh()
        return Text(f"Released {name}.")

    async def render_queued(self) -> Text:
        if not self.player_id:
            return Text("Pick a player first: play <name>.")
        projection = await self.backend.fetch_queued_commands(self.player_id)
        commands = (projection or {}).get("commands") or []
        if not commands:
            return Text("No queued actions.")
        out = Text("Queued actions (cancel <id>):")
        for command in commands:
            out.append("\n  ")
            out.append(str(command.get("command_id") or "?"), style="bold")
            out.append(f"  {command.get('command_type', '?')}")
            lane = command.get("lane")
            if lane:
                out.append(f" · {lane}")
        return out

    async def _cancel(self, command_id: str) -> Text:
        command_id = command_id.strip()
        if not self.player_id or self.control is None:
            return Text("Pick a player first: play <name>.")
        if not command_id:
            return Text("Usage: cancel <command id>. See 'queued'.")
        cancelled = await self.backend.cancel_command(
            self.player_id, command_id, self.control[0], self.control[1]
        )
        if cancelled:
            return Text(f"Cancelled {command_id}.", style="cyan")
        return Text(f"Could not cancel {command_id}.", style="yellow")

    def name_for(self, entity_id: str) -> str | None:
        """The display name of a reachable entity id, used by the app's click action."""
        entity = self.world.get(entity_id)
        return entity_name(entity) if entity else None

    def status_text(self) -> str:
        """A one-line status: who you are, the connection, your points, and the world clock."""
        who = entity_name(self.world.get(self.player_id)) if self.player_id else "no player"
        parts = [who, self.backend.label]
        if self.player_id:
            parts.append(self.render_points())
        parts.append(f"epoch {self.world.epoch}s")
        return " · ".join(parts)

    # ── perceived-event narration ───────────────────────────────────────────────
    def drain_events(self, messages: list[dict]) -> list[Text]:
        """Render the not-yet-seen events the current player can perceive, then mark the
        whole window seen. ``messages`` are ``recent_events()`` payloads."""
        self._latest_media_event_id = latest_media_event_id(messages)
        lines = self._events.drain_events(
            messages,
            player_id=self.player_id,
            room_of=self.world.room_of,
            name_for=self.name_for,
            show_icons=self.show_icons,
        )
        lines.extend(self._image_lines(messages))
        lines.extend(self._video_lines(messages))
        return lines

    def _image_lines(self, messages: list[dict]) -> list[Text]:
        # Image-generation events ride at SYSTEM visibility (no actor), so the perception
        # filter in EventNarrator drops them; read completions/failures straight from the
        # recent-events feed instead, matching the web and TUI clients. The dedupe state is
        # mutated here so the app's priming pass (which discards the lines) still seeds it.
        out: list[Text] = []
        completion = latest_image_completion(messages, purpose="event")
        if completion is not None and completion["url"] != self._event_image_url:
            self._event_image_url = completion["url"]
            out.append(
                Text(f"{DELIVER_EMOJI} scene image ready: {completion['url']}", style="cyan")
            )
        failure = latest_image_failure(messages, purpose="event")
        if failure is not None and failure["world_epoch"] != self._event_image_failure_epoch:
            self._event_image_failure_epoch = failure["world_epoch"]
            reason = failure.get("reason") or "image generation failed"
            out.append(Text(f"{FAIL_EMOJI} image request failed: {reason}", style="yellow"))
        return out

    def _video_lines(self, messages: list[dict]) -> list[Text]:
        out: list[Text] = []
        completion = latest_video_completion(messages)
        if completion is not None and completion["url"] != self._event_video_url:
            self._event_video_url = str(completion["url"])
            out.append(
                Text(
                    f"{VIDEO_DELIVER_EMOJI} scene video ready: {completion['url']}",
                    style="cyan",
                )
            )
        failure = latest_video_failure(messages)
        if failure is not None and failure["world_epoch"] != self._event_video_failure_epoch:
            self._event_video_failure_epoch = int(failure["world_epoch"])
            reason = failure.get("reason") or "video generation failed"
            out.append(Text(f"{FAIL_EMOJI} video request failed: {reason}", style="yellow"))
        return out

    # ── dispatch ──────────────────────────────────────────────────────────────
    async def dispatch(self, line: str) -> ReplCommandResult:
        line = line.strip()
        if not line:
            return Text("")
        verb, _, rest = line.partition(" ")
        rest = rest.strip()
        if verb == "/login":
            return LoginIntent()
        if verb == "help":
            return self.render_help(rest)
        if verb == "who":
            return self.render_players()
        if verb == "look":
            return self.render_room()
        if verb in {"inventory", "inv"}:
            return self.render_inventory()
        if verb == "points":
            return Text(self.render_points())
        if verb == "refresh":
            await self.refresh()
            return Text("Refreshed.")
        if verb == "play":
            if not rest:
                return Text("Usage: play <player name>")
            return Text(await self.select_player(rest))
        if verb == "release":
            return await self._release()
        if verb == "queued":
            return await self.render_queued()
        if verb == "cancel":
            return await self._cancel(rest)
        if verb in IMAGE_COMMANDS:
            return await self._request_image()
        if verb in VIDEO_COMMANDS:
            return await self._request_video()
        if verb in SHEET_COMMANDS:
            return await self._open_sheet(rest)
        if verb in CHAT_COMMANDS:
            return self._open_chat(rest)
        return await self._act(line, verb)

    async def _request_image(self) -> Text:
        if not self.player_id:
            return Text("Pick a player first: play <name>.")
        if not self.backend.supports_image_requests:
            return Text("Image requests are not available for this session.", style="yellow")
        result = await self.backend.request_image(
            self.player_id, self._latest_media_event_id
        )
        if result.ok:
            note = "image ready" if result.status == "skipped" else "image requested"
            return Text(f"{REQUEST_EMOJI} {note}.", style="cyan")
        return Text(f"{REQUEST_EMOJI} {result.reason}", style="yellow")

    async def _request_video(self) -> Text:
        if not self.player_id:
            return Text("Pick a player first: play <name>.")
        if not self.backend.supports_video_requests:
            return Text("Video requests are not available for this session.", style="yellow")
        result = await self.backend.request_video(
            self.player_id, self._latest_media_event_id
        )
        if result.ok:
            note = "video ready" if result.status == "skipped" else "video requested"
            return Text(f"{VIDEO_REQUEST_EMOJI} {note}.", style="cyan")
        return Text(f"{VIDEO_REQUEST_EMOJI} {result.reason}", style="yellow")

    def _sheet_target(self, name: str) -> str | None:
        query = name.strip()
        if not query:
            return self.player_id or None
        if query.lower() in {"me", "self", "player", "current", "you"}:
            return self.player_id or None
        if any(summary.character_id == query for summary in self.character_list):
            return query
        candidates = [(summary.name, summary.character_id) for summary in self.character_list]
        for entity in self.world.characters():
            candidates.append((entity_name(entity), entity["id"]))
        resolved = resolve_name(query, self.world, candidates)
        ids = {summary.character_id for summary in self.character_list}
        ids.update(entity["id"] for entity in self.world.characters())
        return resolved if resolved in ids else None

    async def _open_sheet(self, name: str = "") -> ReplCommandResult:
        if not self.backend.supports_character_sheets:
            return Text("Character sheets require a remote server URL.", style="yellow")
        character_id = self._sheet_target(name)
        if character_id is None:
            if name.strip():
                return Text(f"No character sheet target: {name!r}. Try 'who'.")
            return Text("Pick a player first: play <name>.")
        if type(self.backend).fetch_character_profile is Backend.fetch_character_profile:
            result = await self.backend.open_character_sheet(character_id)
            if result.ok:
                return Text(f"Opened sheet: {result.url}", style="cyan")
            return Text(result.reason, style="yellow")
        summary = next(
            (item for item in self.character_list if item.character_id == character_id), None
        )
        return OpenSheetIntent(character_id, summary.name if summary else character_id)

    def _open_chat(self, name: str = "") -> ReplCommandResult:
        character_id = self._sheet_target(name)
        if character_id is None:
            if name.strip():
                return Text(f"No character chat target: {name!r}. Try 'who'.")
            return Text("Usage: chat <character name>. Pick a player or name a character.")
        summary = next(
            (item for item in self.character_list if item.character_id == character_id), None
        )
        return OpenChatIntent(character_id, summary.name if summary else character_id)

    async def _act(self, line: str, verb: str) -> Text:
        parsed = parse_line(line, self._defs)
        if parsed is None:
            return Text(f"I don't understand {verb!r}. Type 'help'.")
        if not self.player_id or self.control is None:
            if not self.player_id:
                return Text("Pick a player first: play <name>.")
            self.control = await self.backend.claim(self.player_id, self.world)
            if self.control is None:
                return Text(f"Could not claim {self.player_id}.")
        definition = self._defs[parsed.tool]
        candidates = reference_candidates(self.world, self.player_id)

        payload: dict[str, str] = {}
        for key, value in parsed.arguments.items():
            if key in definition.reference_arg_keys:
                resolved = resolve_name(value, self.world, candidates)
                if resolved not in self.world.entities:
                    hints = suggest_names(value, candidates)
                    suffix = f" Did you mean: {', '.join(hints)}?" if hints else ""
                    label = key.removesuffix("_id").replace("_", " ")
                    return Text(f"I don't see {value!r} ({label}) here.{suffix}")
                payload[key] = resolved
            else:
                payload[key] = value

        result = await self.backend.submit(
            {
                "character_id": self.player_id,
                "controller_id": self.control[0],
                "controller_generation": self.control[1],
                "command_type": definition.command_type,
                "payload": payload,
                "cost": {"action": definition.cost.action, "focus": definition.cost.focus},
                "lane": definition.lane.value,
                "on_insufficient_points": "queue",
            }
        )
        detail = " ".join(f"{k}={v}" for k, v in payload.items())
        icon = (
            f"{definition.icon or action_icon_for(definition.command_type)} "
            if self.show_icons
            else ""
        )
        if not result.accepted:
            reason = result.reason or "command rejected"
            return Text(
                f"✗ {icon}{definition.command_type} — {reason}".rstrip(),
                style="dark_orange",
            )
        return Text(f"» {icon}{definition.command_type} {detail}".rstrip(), style="green")

    # ── rendering ─────────────────────────────────────────────────────────────
    def render_room(self) -> Text:
        room_id = self.world.room_of(self.player_id) or self.world.first_room_id()
        room = self.world.get(room_id)
        if room is None:
            return Text("No room.")
        out = Text()
        out.append_text(link(entity_name(room), room["id"]))
        out.stylize("bold", 0, len(out))
        for member in self.world.room_members(room_id):
            if has(member, "RoomComponent") or has(member, "DoorComponent"):
                continue
            out.append(f"\n  {entity_icon(member)} ")
            out.append_text(link(entity_name(member), member["id"]))
            if member["id"] == self.player_id:
                out.append(" (you)")
        doors = self.world.doors(room_id)
        if doors:
            out.append("\n  exits: ")
            for index, (target_id, direction, dest) in enumerate(doors):
                if index:
                    out.append(", ")
                name = entity_name(dest) if dest else target_id
                out.append_text(link(f"{direction} → {name}" if direction else name, target_id))
        carried = self.world.carried(self.player_id) if self.player_id else []
        if carried:
            out.append("\n  carrying: ")
            for index, item in enumerate(carried):
                if index:
                    out.append(", ")
                out.append_text(link(entity_name(item), item["id"]))
        return out

    def render_inventory(self) -> Text:
        """A detailed inventory grouped into worn, held, and otherwise carried items, each
        clickable and tagged with its kind — fuller than the ``look`` summary line."""
        player = self.world.get(self.player_id) if self.player_id else None
        if player is None:
            return Text("Pick a player first: play <name>.")
        out = Text()
        for relationship, label in (
            ("Wearing", "worn"),
            ("Holding", "held"),
            ("Contains", "carrying"),
        ):
            items = [
                self.world.get(edge["target"])
                for edge in player["relationships"].get(relationship, [])
            ]
            items = [item for item in items if item]
            if not items:
                continue
            if len(out):
                out.append("\n")
            out.append(f"{label}:")
            for item in items:
                out.append(f"\n  {entity_icon(item)} ")
                out.append_text(link(entity_name(item), item["id"]))
                kind = item["components"].get("IdentityComponent", {}).get("kind")
                if kind:
                    out.append(f" ({kind})")
        return out if len(out) else Text("You aren't carrying anything.")

    def render_players(self) -> Text:
        if not self.character_list:
            return Text("No players.")
        out = Text()
        for index, summary in enumerate(self.character_list):
            if index:
                out.append("\n")
            icon = KIND_ICON.get(summary.kind) or KIND_ICON["other"]
            out.append(f"  {icon} ")
            out.append_text(link(summary.name, summary.character_id))
            if summary.character_id == self.player_id:
                out.append(" (you)")
        return out

    def render_points(self) -> str:
        if not self.player_id:
            return "Pick a player first: play <name>."
        pts = self.world.points(self.player_id)
        return (
            f"AP {fmt_points(pts['ap'])}/{fmt_points(pts['ap_max'])}   "
            f"FP {fmt_points(pts['fp'])}/{fmt_points(pts['fp_max'])}"
        )

    def _availability(self) -> dict[str, dict]:
        """Per-command availability from the current projection, keyed by command type."""
        return {
            str(action.get("command_type")): action
            for action in (self.world.actions or [])
            if action.get("command_type")
        }

    def render_help(self, topic: str) -> Text:
        availability = self._availability()
        if topic and topic in self._defs:
            definition = self._defs[topic]
            keys = ", ".join(definition.arg_keys) or "(none)"
            icon = (
                f"{definition.icon or action_icon_for(definition.command_type)} "
                if self.show_icons
                else ""
            )
            title = definition.title or definition.command_type
            out = Text(f"{icon}{topic} — {title}\n  parameters: {keys}")
            info = availability.get(definition.command_type)
            if info is not None and not info.get("available", True):
                reason = info.get("unavailable_reason") or "unavailable"
                out.append(f"\n  unavailable: {reason}", style="dim")
            return out

        def is_available(tool: str) -> bool:
            info = availability.get(self._defs[tool].command_type)
            return True if info is None else bool(info.get("available", True))

        tools = sorted(self._defs)

        # Available commands first and prominent; unavailable ones still listed but dimmed,
        # since a player may still choose to queue them.
        def label(tool: str) -> str:
            definition = self._defs[tool]
            icon = definition.icon or action_icon_for(definition.command_type)
            return f"{icon} {tool}" if self.show_icons else tool

        available = [label(tool) for tool in tools if is_available(tool)]
        unavailable = [label(tool) for tool in tools if not is_available(tool)]
        meta = ", ".join(m for m in self.meta_commands() if m != "exit")
        out = Text("Commands (try 'help <command>' for parameters):\n")
        out.append(f"  {', '.join(available)}\n")
        if unavailable:
            out.append(f"  unavailable: {', '.join(unavailable)}\n", style="dim")
        out.append(f"Meta: {meta}\n")
        out.append(
            "Forms: 'move direction=north' (named) or 'go north' (natural). "
            "Click a highlighted target to drop its name into the input."
        )
        return out

    # ── completion ────────────────────────────────────────────────────────────
    def meta_commands(self) -> tuple[str, ...]:
        commands = list(META_COMMANDS)
        if self.backend.supports_image_requests:
            commands.extend(IMAGE_COMMANDS)
        if self.backend.supports_video_requests:
            commands.extend(VIDEO_COMMANDS)
        if self.backend.supports_character_sheets:
            commands.extend(SHEET_COMMANDS)
        if self.backend.supports_character_chat:
            commands.extend(CHAT_COMMANDS)
        return tuple(commands)

    def complete(self, line: str) -> list[str]:
        return complete_line(
            line,
            definitions=self._defs,
            # ``look``/``inventory`` are both tools and meta commands; list each word once.
            commands=tuple(dict.fromkeys((*self._defs, *self.meta_commands()))),
            entity_names=[name for name, _ in reference_candidates(self.world, self.player_id)],
            players=[summary.name for summary in self.character_list],
        )
