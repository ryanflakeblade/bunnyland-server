"""Tests for the REPL client: parsing, name resolution, completion, command rendering, and
the Textual app (RichLog scrollback, clickable target links, Tab completion, history)."""

from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace

import pytest
from rich.style import Style

from bunnyland.core.actions import ActionArgument, ActionDefinition, definitions_by_tool_name
from bunnyland.plugins import PluginRegistry, bunnyland_plugins
from bunnyland.repl import app as repl_app
from bunnyland.repl.app import BunnylandReplApp, ReplInput
from bunnyland.repl.client import (
    BunnylandRepl,
    LoginIntent,
    ParsedCommand,
    _humanize_event_type,
    available_generators,
    format_generator_lines,
    link,
    parse_line,
    resolve_name,
)
from bunnyland.repl.completion import complete_line, reference_candidates, value_candidates
from bunnyland.tui.backend import Backend, ControlClaim, LocalBackend, RemoteBackend, SubmitResult
from bunnyland.tui.generator_selector import GeneratorSelection, WorldGeneratorSelector
from bunnyland.tui.model import World, entity_name
from bunnyland.tui.screens import SignInCredentials, SignInScreen
from bunnyland.tui.splash import IntroSplash

PLAYER = "character:1"
MARLOW = "character:2"
APPLE = "item:1"
KEY = "item:2"
PARLOR = "room:1"
HALL = "room:2"

ALL_ACTION_DEFINITIONS = tuple(
    definition for _owner, definition in PluginRegistry(bunnyland_plugins()).actions.values()
)
DEFS = definitions_by_tool_name(ALL_ACTION_DEFINITIONS)


def _action_views() -> list[dict]:
    return [
        {
            "command_type": definition.command_type,
            "tool_name": definition.name,
            "title": definition.title,
            "description": definition.description,
            "icon": definition.icon,
            "arguments": [
                {
                    "key": key,
                    "title": argument.title,
                    "kind": argument.kind,
                    "required": argument.required,
                }
                for key, argument in (definition.arguments or {}).items()
            ],
            "natural_patterns": [
                {
                    "text": pattern.text,
                    "fixed_arguments": pattern.fixed_arguments,
                    "argument_aliases": pattern.argument_aliases,
                }
                for pattern in definition.natural_patterns
            ],
        }
        for definition in ALL_ACTION_DEFINITIONS
    ]


@pytest.fixture(autouse=True)
def _isolate_history(monkeypatch, tmp_path):
    """Sandbox the REPL history file so app tests never read or clobber the real one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def _snapshot() -> dict:
    """A parlor with the player, Marlow and an apple; the player holds a brass key and
    there is a north exit to a hallway. The player is driven by controller gen 2."""
    return {
        "world_epoch": 42,
        "entities": [
            {
                "id": PARLOR,
                "components": {"RoomComponent": {"title": "Parlor"}},
                "relationships": {
                    "Contains": [
                        {"target_id": PLAYER, "edge": {}},
                        {"target_id": MARLOW, "edge": {}},
                        {"target_id": APPLE, "edge": {}},
                    ],
                    "ExitTo": [{"target_id": HALL, "edge": {"direction": "north"}}],
                },
            },
            {
                "id": HALL,
                "components": {"RoomComponent": {"title": "Hallway"}},
                "relationships": {},
            },
            {
                "id": PLAYER,
                "components": {
                    "CharacterComponent": {},
                    "IdentityComponent": {"name": "Pib", "kind": "character"},
                    "ActionPointsComponent": {"current": 5, "maximum": 5},
                    "FocusPointsComponent": {"current": 3, "maximum": 3},
                },
                "relationships": {
                    "Holding": [{"target_id": KEY, "edge": {}}],
                    "ControlledBy": [{"target_id": "controller:1", "edge": {"generation": 2}}],
                },
            },
            {
                "id": MARLOW,
                "components": {
                    "CharacterComponent": {},
                    "IdentityComponent": {"name": "Marlow", "kind": "character"},
                },
                "relationships": {},
            },
            {
                "id": APPLE,
                "components": {
                    "PortableComponent": {},
                    "IdentityComponent": {"name": "an apple", "kind": "food"},
                },
                "relationships": {},
            },
            {
                "id": KEY,
                "components": {
                    "PortableComponent": {},
                    "IdentityComponent": {"name": "a brass key", "kind": "item"},
                },
                "relationships": {},
            },
        ],
    }


def _character_list_from_snapshot(snapshot: dict) -> list:
    """The claim-lobby records, derived from a snapshot fixture."""
    from bunnyland.server.models import CharacterSummaryView

    world = World.parse(snapshot)
    return [
        CharacterSummaryView(
            character_id=character["id"],
            name=entity_name(character),
            kind=character["components"].get("IdentityComponent", {}).get("kind", "character"),
            suspended="SuspendedComponent" in character["components"],
        )
        for character in world.characters()
    ]


def _client_view_from_snapshot(snapshot: dict, character_id: str = PLAYER) -> dict | None:
    """Synthesize a character projection (own-room view) from a snapshot fixture, so the
    REPL — which now reads projections, not the full snapshot — sees the same world."""
    entities = {entity["id"]: entity for entity in snapshot["entities"]}
    character = entities.get(character_id)
    if character is None:
        return None
    room = next(
        (
            entity
            for entity in snapshot["entities"]
            if "RoomComponent" in entity["components"]
            and any(
                link["target_id"] == character_id
                for link in entity["relationships"].get("Contains", [])
            )
        ),
        None,
    )
    room_entities = []
    exits = []
    if room is not None:
        for link in room["relationships"].get("Contains", []):
            target = entities.get(link["target_id"])
            if target is None or link["target_id"] == character_id:
                continue
            identity = target["components"].get("IdentityComponent", {})
            room_entities.append(
                {
                    "id": link["target_id"],
                    "name": identity.get("name", link["target_id"]),
                    "kind": identity.get("kind", "other"),
                    "is_character": "CharacterComponent" in target["components"],
                    "contents": [],
                }
            )
        for link in room["relationships"].get("ExitTo", []):
            direction = link["edge"].get("direction", "")
            exits.append(
                {
                    "id": link["target_id"],
                    "direction": direction,
                    "label": f"{direction}: {link['target_id']}"
                    if direction
                    else link["target_id"],
                    "locked": link["edge"].get("locked", False),
                }
            )
    inventory = []
    for relationship in ("Holding", "Wearing", "Contains"):
        for link in character["relationships"].get(relationship, []):
            target = entities.get(link["target_id"])
            if target is None:
                continue
            identity = target["components"].get("IdentityComponent", {})
            inventory.append(
                {
                    "id": link["target_id"],
                    "label": identity.get("name", link["target_id"]),
                    "kind": identity.get("kind", "item"),
                }
            )
    ap = character["components"].get("ActionPointsComponent", {})
    fp = character["components"].get("FocusPointsComponent", {})
    controlled_by = character["relationships"].get("ControlledBy", [])
    controller = (
        {
            "controller_id": controlled_by[0]["target_id"],
            "generation": controlled_by[0]["edge"].get("generation", 0),
        }
        if controlled_by
        else None
    )
    identity = character["components"].get("IdentityComponent", {})
    return {
        "world_epoch": snapshot.get("world_epoch", 0),
        "character_id": character_id,
        "character_name": identity.get("name", character_id),
        "room": {
            "id": room["id"] if room else None,
            "title": room["components"]["RoomComponent"].get("title") if room else None,
            "entities": room_entities,
            "exits": exits,
        },
        "inventory": inventory,
        "points": {
            "action": ap.get("current", 0),
            "action_max": ap.get("maximum", 0),
            "focus": fp.get("current", 0),
            "focus_max": fp.get("maximum", 0),
        },
        "controller": controller,
        "target_groups": {},
        "actions": _action_views(),
    }


class RecordingBackend(Backend):
    """A projection backend recording submitted commands, like the TUI test double.

    The REPL now reads the claim lobby and the player's own room projection (never the full
    snapshot), so this backend synthesizes both from its snapshot fixture. Subclasses that
    override ``fetch_snapshot`` to drive the world keep working, since the list and
    projection are derived from it."""

    def __init__(self, snapshot: dict | None = None) -> None:
        self.snapshot = snapshot or _snapshot()
        self.commands: list[dict] = []
        self.label = "test"
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def fetch_snapshot(self) -> dict:
        return self.snapshot

    async def fetch_character_list(self) -> list:
        return _character_list_from_snapshot(await self.fetch_snapshot())

    async def fetch_character_projection(self, character_id: str) -> dict | None:
        return _client_view_from_snapshot(await self.fetch_snapshot(), character_id)

    async def submit(self, command: dict) -> SubmitResult:
        self.commands.append(command)
        return SubmitResult(accepted=True)

    async def claim(self, player_id, world):
        return ControlClaim(
            *(World.parse(self.snapshot).control(player_id) or ("controller:new", 0))
        )


def _repl(snapshot: dict | None = None, *, player: bool = True) -> BunnylandRepl:
    repl = BunnylandRepl(RecordingBackend(snapshot), definitions=ALL_ACTION_DEFINITIONS)
    repl.world = World.parse(snapshot or _snapshot())
    repl.character_list = _character_list_from_snapshot(snapshot or _snapshot())
    if player:
        repl.player_id = PLAYER
        repl.control = ControlClaim("controller:1", 2)
    return repl


# ── parsing ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "line, tool, arguments",
    [
        ("wait", "wait", {}),
        ("move direction=north", "move", {"direction": "north"}),
        ("take item_id=a brass key", "take", {"item_id": "a brass key"}),
        ("go north", "move", {"direction": "north"}),
        ("take brass key", "take", {"item_id": "brass key"}),
        ("say hello there", "say", {"text": "hello there"}),
    ],
)
def test_parse_line_named_and_natural(line, tool, arguments):
    assert parse_line(line, DEFS) == ParsedCommand(tool, arguments)


def test_parse_line_unknown_or_empty_returns_none():
    assert parse_line("", DEFS) is None
    assert parse_line("xyzzy zork", DEFS) is None


# ── name resolution ───────────────────────────────────────────────────────────
def test_resolve_name_passthrough_exact_and_prefix():
    world = World.parse(_snapshot())
    candidates = reference_candidates(world, PLAYER)
    assert resolve_name(KEY, world, candidates) == KEY  # already a valid id
    assert resolve_name("a brass key", world, candidates) == KEY  # exact name
    assert resolve_name("an ap", world, candidates) == APPLE  # shortest prefix
    assert resolve_name("", world, candidates) == ""  # empty query
    assert resolve_name("dragon", world, candidates) == "dragon"  # unresolvable


# ── completion ────────────────────────────────────────────────────────────────
def test_complete_command_names():
    matches = complete_line("mo", definitions=DEFS, commands=(*DEFS, "help", "quit"))
    assert "move" in matches


def test_complete_parameter_names():
    assert set(complete_line("move ", definitions=DEFS, commands=tuple(DEFS))) == {
        "move direction=",
        "move exit_id=",
    }


def test_complete_direction_values():
    matches = complete_line("move direction=", definitions=DEFS, commands=tuple(DEFS))
    assert "move direction=north" in matches and "move direction=south" in matches


def test_complete_entity_values_handle_spaces_and_prefix():
    names = ["a brass key", "an apple"]
    assert complete_line(
        "take item_id=", definitions=DEFS, commands=tuple(DEFS), entity_names=names
    ) == ["take item_id=a brass key", "take item_id=an apple"]
    assert complete_line(
        "take item_id=a b", definitions=DEFS, commands=tuple(DEFS), entity_names=names
    ) == ["take item_id=a brass key"]


def test_complete_second_parameter_name_after_a_value():
    matches = complete_line(
        "use target_id=an apple tool_",
        definitions=DEFS,
        commands=tuple(DEFS),
        entity_names=["an apple", "a brass key"],
    )
    assert matches == ["use target_id=an apple tool_id="]


def test_complete_help_and_play_and_unknown():
    assert "help move" in complete_line("help mo", definitions=DEFS, commands=tuple(DEFS))
    assert complete_line(
        "play P", definitions=DEFS, commands=tuple(DEFS), players=["Pib", "Marlow"]
    ) == ["play Pib"]
    assert complete_line("bogus key=", definitions=DEFS, commands=tuple(DEFS)) == []


def test_value_candidates_kinds():
    boolean = ActionDefinition(
        command_type="toggle", arguments={"flag": ActionArgument(kind="boolean")}
    )
    assert value_candidates(boolean, "flag", ()) == ["false", "true"]
    assert value_candidates(boolean, "missing", ()) == []
    assert value_candidates(DEFS["move"], "exit_id", ["Hallway"]) == ["Hallway"]


def test_reference_candidates_empty_and_dangling_exit():
    assert reference_candidates(World.parse(_snapshot()), "") == []

    snapshot = _snapshot()
    parlor = snapshot["entities"][0]
    parlor["relationships"]["ExitTo"].append(
        {"target_id": "room:void", "edge": {"direction": "down"}}
    )
    parlor["relationships"]["ExitTo"].append({"target_id": HALL, "edge": {"direction": "north"}})
    player = next(e for e in snapshot["entities"] if e["id"] == PLAYER)
    player["relationships"]["Holding"].append({"target_id": APPLE, "edge": {}})

    pairs = reference_candidates(World.parse(snapshot), PLAYER)
    candidates = dict(pairs)
    assert candidates["room:void"] == "room:void"
    assert "a brass key" in candidates
    assert [cid for _name, cid in pairs].count(APPLE) == 1  # de-duplicated
    assert [cid for _name, cid in pairs].count(HALL) == 1  # de-duplicated


# ── dispatch (returns Rich Text) ──────────────────────────────────────────────
def _click_metas(text) -> list[str]:
    return [
        span.style.meta["@click"]
        for span in text.spans
        if isinstance(span.style, Style) and "@click" in span.style.meta
    ]


async def test_dispatch_meta_commands_render_text():
    repl = _repl()
    assert (await repl.dispatch("")).plain == ""
    assert "Parlor" in (await repl.dispatch("look")).plain
    assert "Marlow" in (await repl.dispatch("who")).plain
    assert "AP 5/5" in (await repl.dispatch("points")).plain
    assert (await repl.dispatch("refresh")).plain == "Refreshed."
    assert "parameters" in (await repl.dispatch("help move")).plain
    assert "Commands" in (await repl.dispatch("help")).plain
    assert "Usage" in (await repl.dispatch("play")).plain
    assert "You are now Pib" in (await repl.dispatch("play Pib")).plain


async def test_dispatch_login_returns_secure_ui_intent():
    repl = _repl()

    result = await repl.dispatch("/login")

    assert isinstance(result, LoginIntent)
    assert result.plain == "Sign in"
    assert "/login" in repl.meta_commands()
    assert repl.complete("/l") == ["/login"]


async def test_drain_events_surfaces_scene_image_and_failure():
    repl = _repl()

    def completed(epoch, url):
        return {
            "event_type": "ImageGenerationCompletedEvent",
            "event": {"world_epoch": epoch, "purpose": "event", "url": url},
        }

    lines = repl.drain_events([completed(5, "/public/media/events/a.png")])
    assert any("scene image ready" in line.plain and "a.png" in line.plain for line in lines)
    # The same image does not repeat.
    repeat = repl.drain_events([completed(5, "/public/media/events/a.png")])
    assert all("scene image ready" not in line.plain for line in repeat)

    def failed(epoch, reason=None):
        event = {"world_epoch": epoch, "purpose": "event"}
        if reason is not None:
            event["reason"] = reason
        return {"event_type": "ImageGenerationFailedEvent", "event": event}

    flines = repl.drain_events([failed(9, "boom")])
    assert any("image request failed: boom" in line.plain for line in flines)
    # A failure with no reason falls back to a default message.
    dlines = repl.drain_events([failed(11)])
    assert any("image generation failed" in line.plain for line in dlines)

    video = {
        "event_type": "VideoGenerationCompletedEvent",
        "event": {"world_epoch": 12, "purpose": "event", "url": "/videos/scene.mp4"},
    }
    vlines = repl.drain_events([video])
    assert any("scene video ready" in line.plain for line in vlines)
    assert not repl.drain_events([video])
    video_failure = {
        "event_type": "VideoGenerationFailedEvent",
        "event": {"world_epoch": 13, "purpose": "event"},
    }
    assert any(
        "video generation failed" in line.plain for line in repl.drain_events([video_failure])
    )


async def test_release_drops_the_current_player():
    repl = _repl()
    released = await repl.dispatch("release")
    assert "Released" in released.plain
    assert repl.player_id == ""
    assert repl.control is None
    # Nothing to release the second time.
    assert "aren't playing" in (await repl.dispatch("release")).plain


async def test_release_failure_keeps_character_and_blocks_switch():
    class RejectReleaseBackend(RecordingBackend):
        async def release_controller(self, player_id, control):
            return None

    repl = _repl()
    repl.backend = RejectReleaseBackend()
    control = repl.control
    assert "Could not release" in (await repl.dispatch("release")).plain
    assert repl.player_id == PLAYER and repl.control == control
    assert "Could not release" in await repl.select_player("Marlow")
    assert repl.player_id == PLAYER and repl.control == control


async def test_queued_lists_commands_and_cancel_removes_them():
    class QueuedBackend(RecordingBackend):
        def __init__(self):
            super().__init__()
            self.cancelled: list[tuple] = []
            self.cancel_result = True

        async def fetch_queued_commands(self, character_id):
            return {
                "character_id": character_id,
                "commands": [
                    {"command_id": "cmd-1", "command_type": "wait", "lane": "world"},
                    {"command_id": "cmd-2", "command_type": "reflect"},
                ],
            }

        async def cancel_command(self, character_id, command_id, controller_id, generation):
            self.cancelled.append((character_id, command_id, controller_id, generation))
            return self.cancel_result

    backend = QueuedBackend()
    repl = BunnylandRepl(backend)
    repl.world = World.parse(_snapshot())
    repl.character_list = _character_list_from_snapshot(_snapshot())
    repl.player_id = PLAYER
    repl.control = ControlClaim("controller:1", 2)

    listed = await repl.dispatch("queued")
    assert "cmd-1" in listed.plain and "wait" in listed.plain
    cancelled = await repl.dispatch("cancel cmd-1")
    assert "Cancelled cmd-1" in cancelled.plain
    assert backend.cancelled == [(PLAYER, "cmd-1", "controller:1", 2)]
    # A backend that refuses to cancel reports the failure.
    backend.cancel_result = False
    assert "Could not cancel" in (await repl.dispatch("cancel cmd-1")).plain
    # Cancel needs a command id.
    assert "Usage: cancel" in (await repl.dispatch("cancel")).plain


async def test_queued_and_cancel_require_a_player_and_handle_empty():
    idle = _repl(player=False)
    assert "Pick a player first" in (await idle.dispatch("queued")).plain
    assert "Pick a player first" in (await idle.dispatch("cancel cmd-1")).plain
    # A player with nothing queued (the default backend returns no commands).
    assert "No queued actions" in (await _repl().dispatch("queued")).plain


async def test_look_and_who_render_clickable_target_links():
    repl = _repl()
    room = await repl.dispatch("look")
    # The room, its occupants, and the exit are all clickable, keyed by entity id.
    assert {PARLOR, MARLOW, APPLE, HALL} <= {
        m.split("(")[1].rstrip(")").strip("'") for m in _click_metas(room)
    }
    who = await repl.dispatch("who")
    assert any(MARLOW in meta for meta in _click_metas(who))


async def test_dispatch_action_resolves_reference_and_submits():
    repl = _repl()
    message = await repl.dispatch("take item_id=a brass key")
    assert message.plain.startswith("» 🤲 take")
    command = repl.backend.commands[-1]
    assert command["command_type"] == "take"
    assert command["payload"] == {"item_id": KEY}
    assert command["controller_generation"] == 2
    assert command["cost"] == {"action": 1, "focus": 0}
    assert command["lane"] == "world"

    plain = _repl()
    plain.show_icons = False
    message = await plain.dispatch("take item_id=a brass key")
    assert message.plain.startswith("» take")


async def test_dispatch_action_with_string_argument_passes_through():
    repl = _repl()
    await repl.dispatch("say text=hello there")
    assert repl.backend.commands[-1]["payload"] == {"text": "hello there"}


async def test_dispatch_action_reports_failed_lazy_claim():
    # A player is chosen but not yet controlling (control is None), so _act lazily
    # claims before acting. When that claim fails, dispatch reports it and submits
    # nothing. This pins the "Could not claim" branch (client.py:236), which was only
    # covered incidentally by other suites under some test orderings.
    class NoClaimBackend(RecordingBackend):
        async def claim(self, player_id, world):
            return None

    repl = BunnylandRepl(NoClaimBackend(_snapshot()), definitions=ALL_ACTION_DEFINITIONS)
    repl.world = World.parse(_snapshot())
    repl.character_list = _character_list_from_snapshot(_snapshot())
    repl.player_id = PLAYER
    repl.control = None

    message = await repl.dispatch("wait")

    assert "Could not claim" in message.plain
    assert repl.backend.commands == []


async def test_dispatch_action_surfaces_submit_rejection_reason():
    class RejectingBackend(RecordingBackend):
        async def submit(self, command: dict) -> SubmitResult:
            self.commands.append(command)
            return SubmitResult(accepted=False, reason="character is asleep")

    repl = BunnylandRepl(RejectingBackend(_snapshot()), definitions=ALL_ACTION_DEFINITIONS)
    repl.world = World.parse(_snapshot())
    repl.character_list = _character_list_from_snapshot(_snapshot())
    repl.player_id = PLAYER
    repl.control = ControlClaim("controller:1", 2)

    message = await repl.dispatch("wait")
    assert message.plain.startswith("✗ ⏳ wait")
    assert "character is asleep" in message.plain


def test_render_help_orders_available_first_and_dims_unavailable():
    repl = _repl()
    repl.world.actions = [
        {"command_type": "wait", "available": True, "unavailable_reason": ""},
        {
            "command_type": "pick-lock",
            "available": False,
            "unavailable_reason": "missing a required skill or item",
        },
    ]

    listing = repl.render_help("").plain
    lines = listing.splitlines()
    # The gated verb is listed only on the dimmed "unavailable:" line; available verbs
    # (everything else, e.g. "wait") stay on the main command line.
    unavailable_line = next(line for line in lines if "unavailable:" in line)
    available_line = next(line for line in lines if "wait" in line and "unavailable:" not in line)
    assert "pick_lock" in unavailable_line
    assert "pick_lock" not in available_line


def test_render_help_topic_shows_unavailable_reason():
    repl = _repl()
    repl.world.actions = [
        {
            "command_type": "pick-lock",
            "available": False,
            "unavailable_reason": "missing a required skill or item",
        },
    ]

    topic = repl.render_help("pick_lock").plain
    assert "unavailable: missing a required skill or item" in topic


async def test_dispatch_unresolved_reference_does_not_submit():
    repl = _repl()
    message = await repl.dispatch("take item_id=dragon")
    assert "don't see 'dragon'" in message.plain and "Did you mean" not in message.plain
    assert repl.backend.commands == []


async def test_dispatch_unresolved_reference_suggests_names():
    repl = _repl()
    message = await repl.dispatch("take item_id=appl")
    assert "Did you mean" in message.plain and "an apple" in message.plain
    assert repl.backend.commands == []


async def test_dispatch_action_requires_player_and_known_command():
    repl = _repl(player=False)
    assert "Pick a player first" in (await repl.dispatch("wait")).plain
    assert "don't understand" in (await repl.dispatch("flibber")).plain


# ── rendering fallbacks ───────────────────────────────────────────────────────
def test_render_fallbacks_without_player_or_room():
    repl = BunnylandRepl(RecordingBackend())  # empty world, no player
    assert repl.render_room().plain == "No room."
    assert repl.render_players().plain == "No players."
    assert "Pick a player first" in repl.render_points()


async def test_inventory_command_groups_and_tags_items():
    snapshot = _snapshot()
    player = next(e for e in snapshot["entities"] if e["id"] == PLAYER)
    player["relationships"]["Wearing"] = [{"target_id": APPLE, "edge": {}}]  # held KEY + worn APPLE
    player["relationships"]["Contains"] = [{"target_id": "item:3", "edge": {}}]  # kind-less item
    snapshot["entities"].append(
        {
            "id": "item:3",
            "components": {"IdentityComponent": {"name": "a plain rock"}},
            "relationships": {},
        }
    )
    repl = BunnylandRepl(RecordingBackend(snapshot))
    repl.world = World.parse(snapshot)
    repl.player_id = PLAYER

    for line in ("inventory", "inv"):  # both the full name and the alias work
        text = await repl.dispatch(line)
        assert "worn:" in text.plain and "held:" in text.plain and "carrying:" in text.plain
        assert "a brass key (item)" in text.plain and "an apple (food)" in text.plain
        assert "a plain rock" in text.plain and "a plain rock (" not in text.plain  # no kind tag
        assert {KEY, APPLE} <= {m.split("(")[1].rstrip(")").strip("'") for m in _click_metas(text)}


async def test_inventory_command_empty_and_without_player():
    repl = _repl()
    repl.player_id = MARLOW  # Marlow carries nothing
    assert (await repl.dispatch("inventory")).plain == "You aren't carrying anything."

    idle = BunnylandRepl(RecordingBackend())
    assert "Pick a player first" in (await idle.dispatch("inventory")).plain


def test_inventory_is_completable():
    assert "inventory" in _repl().complete("inv")


def _event(event_id, *, event_type="PingEvent", **fields):
    return {
        "type": "event",
        "data": {
            "event_type": event_type,
            "event": {"event_id": event_id, "note": event_id, **fields},
        },
    }


def test_drain_events_filters_by_perception():
    repl = _repl()  # the player is in the Parlor
    messages = [
        _event("a", visibility="room", room_id=PARLOR, actor_id=MARLOW),
        _event("b", visibility="room", room_id=HALL, actor_id=MARLOW),
        _event("c", visibility="public"),
        _event("d", visibility="directed", actor_id=MARLOW, target_ids=[PLAYER]),
        _event("e", visibility="directed", actor_id=MARLOW, target_ids=[MARLOW]),
        _event("f", visibility="private", actor_id=PLAYER),
        _event("g", visibility="private", actor_id=MARLOW),
        _event("h", visibility="system"),
        _event("u", visibility="mystery", actor_id=MARLOW),  # unknown visibility: not perceived
        _event(None, visibility="public"),  # no id: skipped, not repeated
    ]
    shown = " | ".join(text.plain for text in repl.drain_events(messages))
    assert "note a" in shown and "note c" in shown and "note d" in shown and "note f" in shown
    for hidden in ("note b", "note e", "note g", "note h", "note u"):
        assert hidden not in shown


def test_drain_events_skips_telemetry_noise():
    repl = _repl()
    messages = [
        _event("p", event_type="ActionPointsChangedEvent", visibility="private", actor_id=PLAYER),
        _event("s", event_type="EntitySeenEvent", visibility="private", actor_id=PLAYER),
        _event("k", event_type="SpokeEvent", visibility="room", room_id=PARLOR, actor_id=MARLOW),
    ]
    shown = " | ".join(text.plain for text in repl.drain_events(messages))
    assert "note k" in shown  # real activity is narrated
    assert "note p" not in shown and "note s" not in shown  # telemetry is suppressed


def test_drain_events_narrates_own_system_actions_uniformly():
    repl = _repl()  # player is in the Parlor
    messages = [
        _event(
            "m1",
            event_type="ActorMovedEvent",
            visibility="system",
            actor_id=PLAYER,
            from_room_id=PARLOR,
            to_room_id=HALL,
            arrival_summary="Hallway\nHere: Pib.\nExits: south.",
        ),
        _event(
            "t1", event_type="ItemTakenEvent", visibility="system", actor_id=PLAYER, item_id=APPLE
        ),
        _event("x1", event_type="CommandExecutedEvent", visibility="system", actor_id=PLAYER),
        _event(
            "m2",
            event_type="ActorMovedEvent",
            visibility="system",
            actor_id=MARLOW,
            from_room_id=PARLOR,
            to_room_id=HALL,
        ),
    ]
    shown = " | ".join(text.plain for text in repl.drain_events(messages))
    assert "Hallway\nHere: Pib.\nExits: south." in shown  # your own move shows arrival room
    assert "Pib: Actor moved" not in shown
    assert "Pib: Item taken" in shown and "an apple" in shown  # ...and so is your own take
    assert "Command executed" not in shown  # command lifecycle stays suppressed
    assert "Marlow" not in shown  # someone else's system-only action is not perceived


def test_drain_events_surfaces_own_command_rejections():
    repl = _repl()
    messages = [
        _event(
            "r1",
            event_type="CommandRejectedEvent",
            visibility="system",
            actor_id=PLAYER,
            command_type="take",
            reason="that item is not portable",
        ),
        _event(
            "r2",
            event_type="CommandRejectedEvent",
            visibility="system",
            actor_id=MARLOW,
            command_type="take",
            reason="secret",
        ),
    ]
    rendered = repl.drain_events(messages)
    shown = " | ".join(text.plain for text in rendered)
    assert "Command rejected" in shown and "not portable" in shown  # your failure explains itself
    assert "secret" not in shown  # another character's rejection is private to them
    assert str(rendered[0].style) == "dark_orange"  # rejections stand out in orange


def test_drain_events_dedupes_already_seen():
    repl = _repl()
    first = repl.drain_events([_event("x", visibility="public")])
    assert len(first) == 1
    assert repl.drain_events([_event("x", visibility="public")]) == []  # same event, not repeated
    again = repl.drain_events([_event("x", visibility="public"), _event("y", visibility="public")])
    assert len(again) == 1 and "note y" in again[0].plain


def test_humanize_event_type_splits_camelcase_and_handles_bare():
    assert _humanize_event_type("ResourceGatheredEvent") == "Resource gathered"
    assert _humanize_event_type("Event") == ""


def test_render_event_humanizes_and_resolves_names():
    repl = _repl()
    [text] = repl.drain_events(
        [
            _event(
                "g1",
                event_type="GaveEvent",
                visibility="room",
                room_id=PARLOR,
                actor_id=MARLOW,
                item_id=APPLE,
                tool_id="ghost",
                recipient_ids=[PLAYER],
                witness_ids=["nobody1", "nobody2"],
            ),
        ]
    )
    assert text.plain.startswith("• Marlow: Gave")
    assert "an apple" in text.plain and "Pib" in text.plain  # item_id and _ids resolved to names
    assert "ghost" not in text.plain  # unresolvable id dropped
    assert "nobody" not in text.plain  # an _ids field with no resolvable names is dropped


def test_render_event_without_details_is_just_a_label():
    repl = _repl()
    bare = {
        "type": "event",
        "data": {"event_type": "WaitedEvent", "event": {"event_id": "w1", "visibility": "public"}},
    }
    [text] = repl.drain_events([bare])
    assert text.plain == "• Waited"

    repl = _repl()
    repl.show_icons = False
    [plain] = repl.drain_events([bare])
    assert plain.plain == "Waited"


def test_status_text_with_and_without_player():
    repl = _repl()
    status = repl.status_text()
    assert "Pib" in status and "test" in status and "AP 5/5" in status and "epoch 42s" in status

    idle = BunnylandRepl(RecordingBackend())
    assert idle.status_text().startswith("no player")


def test_render_room_includes_clickable_inventory():
    snapshot = _snapshot()
    player = next(e for e in snapshot["entities"] if e["id"] == PLAYER)
    player["relationships"]["Holding"].append({"target_id": APPLE, "edge": {}})  # a second item
    repl = BunnylandRepl(RecordingBackend(snapshot))
    repl.world = World.parse(snapshot)
    repl.player_id = PLAYER
    room = repl.render_room()
    assert "carrying: a brass key, an apple" in room.plain
    clicked = {meta.split("(")[1].rstrip(")").strip("'") for meta in _click_metas(room)}
    assert {KEY, APPLE} <= clicked


async def test_refresh_clears_claim_when_projection_missing():
    # A missing claim projection means the persisted remote claim has expired.
    class NoProjectionBackend(RecordingBackend):
        async def fetch_character_projection(self, character_id: str) -> dict | None:
            return None

    repl = BunnylandRepl(NoProjectionBackend())
    repl.player_id = PLAYER
    repl.control = ControlClaim("controller:1", 2)
    await repl.refresh()
    assert repl.player_id == PLAYER
    assert repl.control is None
    assert repl.world.get(PLAYER) is None
    assert repl.world.get(PARLOR) is None


async def test_refresh_coalesces_overlapping_projection_requests():
    class BlockingProjectionBackend(RecordingBackend):
        def __init__(self):
            super().__init__(_snapshot())
            self.projection_calls = 0
            self.projection_started = asyncio.Event()
            self.release_projection = asyncio.Event()

        async def fetch_character_projection(self, character_id: str) -> dict | None:
            self.projection_calls += 1
            self.projection_started.set()
            await self.release_projection.wait()
            return await super().fetch_character_projection(character_id)

    backend = BlockingProjectionBackend()
    repl = BunnylandRepl(backend)
    repl.player_id = PLAYER
    first = asyncio.create_task(repl.refresh())
    await backend.projection_started.wait()
    second = asyncio.create_task(repl.refresh())
    await asyncio.sleep(0)

    assert backend.projection_calls == 1

    backend.release_projection.set()
    await asyncio.gather(first, second)
    assert backend.projection_calls == 1


async def test_refresh_resets_world_when_projection_id_mismatches():
    # projection returns a view for a different character -> empty World (152)
    class MismatchedBackend(RecordingBackend):
        async def fetch_character_projection(self, character_id: str) -> dict | None:
            return _client_view_from_snapshot(await self.fetch_snapshot(), MARLOW)

    repl = BunnylandRepl(MismatchedBackend())
    repl.player_id = PLAYER
    await repl.refresh()
    assert repl.world.get(PLAYER) is None


async def test_refresh_accepts_projection_without_enabled_actions():
    class NoActionsBackend(RecordingBackend):
        async def fetch_character_projection(self, character_id: str) -> dict | None:
            projection = _client_view_from_snapshot(await self.fetch_snapshot(), character_id)
            projection["actions"] = []
            return projection

    repl = BunnylandRepl(NoActionsBackend())
    repl.player_id = PLAYER
    await repl.refresh()
    assert repl.world.actions == []


def test_render_room_omits_carrying_when_empty():
    # player holds nothing -> the "carrying:" section is skipped (284->290 false branch)
    snapshot = _snapshot()
    player = next(e for e in snapshot["entities"] if e["id"] == PLAYER)
    player["relationships"]["Holding"] = []
    repl = BunnylandRepl(RecordingBackend(snapshot))
    repl.world = World.parse(snapshot)
    repl.player_id = PLAYER
    room = repl.render_room().plain
    assert "Pib (you)" in room
    assert "carrying:" not in room


def test_render_room_skips_doors_and_omits_empty_exits():
    snapshot = _snapshot()
    parlor = snapshot["entities"][0]
    parlor["relationships"]["Contains"].append({"target_id": "door:1", "edge": {}})
    snapshot["entities"].append(
        {"id": "door:1", "components": {"DoorComponent": {}}, "relationships": {}}
    )
    parlor["relationships"]["ExitTo"] = []
    repl = BunnylandRepl(RecordingBackend(snapshot))
    repl.world = World.parse(snapshot)
    repl.player_id = PLAYER
    room = repl.render_room().plain
    assert "Pib (you)" in room
    assert "door" not in room.lower()
    assert "exits" not in room


def test_link_carries_clickable_meta_keyed_by_id():
    text = link("a brass key", KEY)
    assert text.plain == "a brass key"
    assert _click_metas(text) == [f"app.insert({KEY!r})"]


def test_render_room_multiple_exits_with_and_without_direction():
    snapshot = _snapshot()
    snapshot["entities"][0]["relationships"]["ExitTo"] = [
        {"target_id": HALL, "edge": {}},  # no direction -> bare name
        {"target_id": PARLOR, "edge": {"direction": "south"}},  # second exit -> comma joined
    ]
    repl = BunnylandRepl(RecordingBackend(snapshot))
    repl.world = World.parse(snapshot)
    repl.player_id = PLAYER
    room = repl.render_room().plain
    assert "exits: Hallway, south → Parlor" in room


def test_replinput_remember_dedupes_consecutive_and_ignores_blank():
    command = ReplInput()
    command.remember("look")
    command.remember("look")  # consecutive duplicate is not stored twice
    command.remember("")  # blank is ignored
    assert command.history == ["look"]


# ── select player ─────────────────────────────────────────────────────────────
async def test_select_player_claims_and_sets_control():
    repl = _repl(player=False)
    assert "You are now Pib" in await repl.select_player("Pib")
    assert repl.player_id == PLAYER and repl.control == ("controller:1", 2)


async def test_select_player_rejects_unknown_and_failed_claim():
    repl = _repl(player=False)
    assert "No such player" in await repl.select_player("Nobody")

    class NoClaimBackend(RecordingBackend):
        async def claim(self, player_id, world):
            return None

    repl = BunnylandRepl(NoClaimBackend())
    repl.character_list = _character_list_from_snapshot(_snapshot())
    assert "Could not claim" in await repl.select_player("Pib")


def test_name_for_resolves_and_misses():
    repl = _repl()
    assert repl.name_for(APPLE) == "an apple"
    assert repl.name_for("missing") is None


async def test_refresh_drops_missing_player():
    snapshot = _snapshot()
    snapshot["entities"] = [e for e in snapshot["entities"] if e["id"] != PLAYER]
    repl = BunnylandRepl(RecordingBackend(snapshot))
    repl.player_id = PLAYER
    repl.control = ControlClaim("controller:1", 2)
    await repl.refresh()
    assert repl.player_id == "" and repl.control is None


async def test_refresh_clears_stale_control_and_play_reclaims():
    class ToggleControllerBackend(RecordingBackend):
        def __init__(self):
            super().__init__(_snapshot())
            self.include_controller = False
            self.claims: list[str] = []

        async def fetch_character_projection(self, character_id: str) -> dict | None:
            projection = _client_view_from_snapshot(await self.fetch_snapshot(), character_id)
            if projection and not self.include_controller:
                projection["controller"] = None
            return projection

        async def claim(self, player_id, world):
            self.claims.append(player_id)
            return await super().claim(player_id, world)

    backend = ToggleControllerBackend()
    repl = BunnylandRepl(backend)
    repl.character_list = _character_list_from_snapshot(_snapshot())
    repl.player_id = PLAYER
    repl.control = ControlClaim("controller:1", 2)

    await repl.refresh()
    assert repl.player_id == PLAYER
    assert repl.control is None

    message = await repl.dispatch("wait")
    assert message.plain.startswith("» ⏳ wait")
    assert backend.claims == [PLAYER]
    assert repl.control == ("controller:1", 2)
    assert backend.commands[-1]["controller_generation"] == 2


# ── the Textual app ───────────────────────────────────────────────────────────
def _log_text(app: BunnylandReplApp) -> str:
    from textual.widgets import RichLog

    log = app.query_one(RichLog)
    return "\n".join("".join(seg.text for seg in strip._segments) for strip in log.lines)


async def _submit(app, pilot, text: str) -> None:
    command = app.query_one(ReplInput)
    command.value = text
    command.cursor_position = len(text)
    await pilot.press("enter")
    await pilot.pause()


async def test_app_runs_meta_and_action_commands():
    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test() as pilot:
        assert app.repl.backend.started
        await _submit(app, pilot, "")  # blank line is ignored
        await _submit(app, pilot, "play Pib")
        await _submit(app, pilot, "look")
        await _submit(app, pilot, "take item_id=a brass key")
        text = _log_text(app)
        assert "You are now Pib" in text
        assert "Parlor" in text
        assert app.repl.backend.commands[-1]["command_type"] == "take"


async def test_app_login_uses_shared_modal_and_refreshes_remote_session(monkeypatch):
    class LoginBackend(RemoteBackend):
        def __init__(self) -> None:
            super().__init__("https://play.example/v1", username="saved-player")
            self.signed_in: list[tuple[str, str]] = []

        async def sign_in(self, username: str, password: str) -> None:
            self.username = username
            self.signed_in.append((username, password))

    backend = LoginBackend()
    app = BunnylandReplApp(backend)
    pushed = []
    workers = []
    refreshed = []
    logged = []

    def fake_push_screen(screen, callback=None):
        pushed.append((screen, callback))

    async def fake_refresh(prime=False):
        refreshed.append(prime)

    monkeypatch.setattr(app, "push_screen", fake_push_screen)
    monkeypatch.setattr(app, "run_worker", lambda coroutine, **_kwargs: workers.append(coroutine))
    monkeypatch.setattr(app, "_safe_refresh", fake_refresh)
    monkeypatch.setattr(app, "write_log", lambda renderable: logged.append(renderable.plain))
    monkeypatch.setattr(app.command, "focus", lambda: None)

    app._show_sign_in()
    screen, callback = pushed.pop()
    assert isinstance(screen, SignInScreen)
    assert screen.username == "saved-player"

    callback(SignInCredentials(username="player", password="secret"))
    await workers.pop()

    assert backend.signed_in == [("player", "secret")]
    assert refreshed == [False]
    assert logged == ["Signed in as player."]


async def test_app_failed_login_reopens_shared_modal_with_error(monkeypatch):
    class LoginBackend(RemoteBackend):
        async def sign_in(self, username: str, _password: str) -> None:
            self.username = username
            raise RuntimeError(f"Sign in failed for {username}")

    backend = LoginBackend("https://play.example/v1")
    app = BunnylandReplApp(backend)
    pushed = []
    refreshed = []

    def fake_push_screen(screen, callback=None):
        pushed.append((screen, callback))

    async def fake_refresh(prime=False):
        refreshed.append(prime)

    monkeypatch.setattr(app, "push_screen", fake_push_screen)
    monkeypatch.setattr(app, "_safe_refresh", fake_refresh)

    await app._sign_in(
        backend,
        SignInCredentials(username="player", password="wrong"),
    )

    screen, _callback = pushed.pop()
    assert isinstance(screen, SignInScreen)
    assert screen.username == "player"
    assert screen.error == "Sign in failed for player"
    assert refreshed == []


async def test_app_login_never_records_inline_credentials():
    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test() as pilot:
        await _submit(app, pilot, "/login player visible-secret")

        assert app.command.history[-1] == "/login"
        assert "visible-secret" not in _log_text(app)
        assert "Sign in requires a remote server URL" in _log_text(app)


async def test_app_auth_required_prompts_once_until_manual_login(monkeypatch):
    class Unauthorized(RuntimeError):
        response = SimpleNamespace(status_code=401)

    backend = RemoteBackend("https://play.example/v1")
    app = BunnylandReplApp(backend)
    pushed = []

    async def unauthorized_refresh():
        raise Unauthorized("authentication required")

    def fake_push_screen(screen, callback=None):
        pushed.append((screen, callback))

    monkeypatch.setattr(app.repl, "refresh", unauthorized_refresh)
    monkeypatch.setattr(app, "push_screen", fake_push_screen)
    monkeypatch.setattr(app, "write_log", lambda _renderable: None)
    monkeypatch.setattr(app.command, "focus", lambda: None)

    await app._safe_refresh_once()
    screen, callback = pushed[-1]
    assert isinstance(screen, SignInScreen)
    assert screen.error == "Sign in is required to join this server."

    callback(None)
    await app._safe_refresh_once()
    assert len(pushed) == 1

    app._show_sign_in()
    assert len(pushed) == 2


async def test_intro_splash_fades_and_dismisses():
    app = BunnylandReplApp(RecordingBackend(), show_intro=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert any(isinstance(screen, IntroSplash) for screen in app.screen_stack)

        splash = next(screen for screen in app.screen_stack if isinstance(screen, IntroSplash))
        panel = splash.query_one("#splash")
        timers = []

        def capture_timer(delay, callback):
            timers.append((delay, callback))

        splash.set_timer = capture_timer
        splash._start_fade()

        fade_delay, fade_callback = timers[0]
        assert fade_delay > 0
        fade_callback()
        assert 0 < panel.styles.opacity <= 1

        finish_delay, finish_callback = timers[-1]
        assert finish_delay > fade_delay
        finish_callback()
        await pilot.pause()
        assert not any(isinstance(screen, IntroSplash) for screen in app.screen_stack)


async def test_intro_splash_does_not_use_widget_animation_api(monkeypatch):
    def fail_if_animated(*_args, **_kwargs) -> None:
        raise AssertionError("IntroSplash should not call animate() on this Textual version")

    monkeypatch.setattr(IntroSplash, "animate", fail_if_animated)
    app = BunnylandReplApp(RecordingBackend(), show_intro=True)
    async with app.run_test() as _pilot:
        pass


async def test_app_status_line_tracks_player():
    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test() as pilot:
        assert app.sub_title.startswith("no player")
        await _submit(app, pilot, "play Pib")
        assert "Pib" in app.sub_title and "AP 5/5" in app.sub_title


async def test_app_status_line_updates_as_the_world_advances():
    import re

    class TickingBackend(RecordingBackend):
        """Each snapshot reports a later epoch and more action points."""

        def __init__(self) -> None:
            super().__init__()
            self.tick = 0

        async def fetch_snapshot(self) -> dict:
            self.tick += 1
            snapshot = _snapshot()
            snapshot["world_epoch"] = 100 * self.tick
            for entity in snapshot["entities"]:
                if entity["id"] == PLAYER:
                    entity["components"]["ActionPointsComponent"]["current"] = self.tick
            return snapshot

    def epoch(status: str) -> int:
        return int(re.search(r"epoch (\d+)s", status).group(1))

    def ap(status: str) -> int:
        return int(re.search(r"AP (\d+)/", status).group(1))

    app = BunnylandReplApp(TickingBackend())
    async with app.run_test():
        app.repl.player_id = PLAYER
        app.repl.control = ControlClaim("controller:1", 2)
        await app._safe_refresh()
        before = app.sub_title
        await app._safe_refresh()
        after = app.sub_title
        assert epoch(after) > epoch(before)  # the clock advances in the status line
        assert ap(after) > ap(before)  # AP/FP track the latest snapshot


class NarratingBackend(RecordingBackend):
    """A backend whose event feed the test controls."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    async def recent_events(self, character_id: str = "") -> list[dict]:
        return self.events


async def test_app_narrates_new_perceived_events():
    app = BunnylandReplApp(NarratingBackend())
    async with app.run_test():
        app.repl.player_id = PLAYER  # in the Parlor
        app.repl.backend.events = [_event("e1", visibility="room", room_id=PARLOR, actor_id=MARLOW)]
        await app._safe_refresh()
        assert "note e1" in _log_text(app)


async def test_app_primes_event_history_without_dumping_backlog():
    backend = NarratingBackend()
    backend.events = [_event("old", visibility="public")]  # already in the feed at startup
    app = BunnylandReplApp(backend)
    async with app.run_test():
        assert "note old" not in _log_text(app)  # the backlog is seeded, not printed


async def test_app_throttles_repeated_refresh_errors_then_reports_recovery():
    class FlakyBackend(RecordingBackend):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        async def fetch_snapshot(self) -> dict:
            if self.fail:
                raise RuntimeError("down")
            return self.snapshot

    app = BunnylandReplApp(FlakyBackend())
    async with app.run_test():
        await app._safe_refresh()
        await app._safe_refresh()
        assert _log_text(app).count("down") == 1  # reported once, not every tick
        app.repl.backend.fail = False
        await app._safe_refresh()
        assert "reconnected" in _log_text(app)


async def test_app_coalesces_overlapping_event_refreshes():
    class BlockingEventBackend(RecordingBackend):
        def __init__(self):
            super().__init__()
            self.block_events = False
            self.event_calls = 0
            self.event_started = asyncio.Event()
            self.release_events = asyncio.Event()

        async def recent_events(self, character_id: str = "") -> list[dict]:
            if not self.block_events:
                return []
            self.event_calls += 1
            self.event_started.set()
            await self.release_events.wait()
            return []

    backend = BlockingEventBackend()
    app = BunnylandReplApp(backend)
    async with app.run_test():
        app.repl.player_id = PLAYER
        backend.block_events = True
        first = asyncio.create_task(app._safe_refresh())
        await backend.event_started.wait()
        second = asyncio.create_task(app._safe_refresh())
        await asyncio.sleep(0)

        assert backend.event_calls == 1

        backend.release_events.set()
        await asyncio.gather(first, second)
        assert backend.event_calls == 1


async def test_app_unmount_cancels_refresh_before_closing_backend():
    class BlockingEventBackend(RecordingBackend):
        def __init__(self):
            super().__init__()
            self.block_events = False
            self.event_started = asyncio.Event()
            self.event_cancelled = False
            self.closed_after_cancel = False

        async def recent_events(self, character_id: str = "") -> list[dict]:
            if not self.block_events:
                return []
            self.event_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.event_cancelled = True
                raise

        async def close(self) -> None:
            self.closed_after_cancel = self.event_cancelled

    backend = BlockingEventBackend()
    app = BunnylandReplApp(backend)
    async with app.run_test():
        app.repl.player_id = PLAYER
        backend.block_events = True
        refresh = asyncio.create_task(app._safe_refresh())
        await backend.event_started.wait()

    await asyncio.gather(refresh, return_exceptions=True)
    assert backend.event_cancelled is True
    assert backend.closed_after_cancel is True


async def test_app_quit_exits():
    app = BunnylandReplApp(RecordingBackend())
    exits: list[bool] = []
    async with app.run_test() as pilot:
        app.exit = lambda *a, **k: exits.append(True)  # type: ignore[method-assign]
        await _submit(app, pilot, "quit")
    assert exits == [True]


async def test_app_dispatch_errors_are_caught():
    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test() as pilot:

        async def boom(_line):
            raise RuntimeError("kaboom")

        app.repl.dispatch = boom  # type: ignore[method-assign]
        await _submit(app, pilot, "anything")
        assert "kaboom" in _log_text(app)


async def test_app_refresh_errors_are_reported():
    class FailingBackend(RecordingBackend):
        async def fetch_snapshot(self) -> dict:
            raise RuntimeError("snapshot failed")

    app = BunnylandReplApp(FailingBackend())
    async with app.run_test():
        assert "snapshot failed" in _log_text(app)


async def test_app_click_inserts_target_name():
    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test():
        # Target links resolve against the player's own room, so claim one first.
        app.repl.player_id = PLAYER
        await app._safe_refresh()
        app.action_insert(APPLE)
        assert app.query_one(ReplInput).value == "an apple"
        app.action_insert("missing")  # unknown id falls back to the raw reference
        assert "missing" in app.query_one(ReplInput).value


async def test_app_tab_completion_single_multi_and_none():
    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test() as pilot:
        await _submit(app, pilot, "play Pib")
        command = app.query_one(ReplInput)

        command.value = "wai"
        command.cursor_position = 3
        await pilot.press("tab")
        assert command.value == "wait"  # single match completes fully

        command.value = "invent"
        command.cursor_position = 6
        await pilot.press("tab")
        assert command.value == "inventory"  # meta command completes from an unambiguous prefix

        command.value = "move "
        command.cursor_position = 5
        await pilot.press("tab")
        assert command.value.startswith("move ")  # common prefix; options listed in the log

        command.value = "zzz"
        command.cursor_position = 3
        await pilot.press("tab")
        assert command.value == "zzz"  # no match: unchanged

        # Several matches sharing a longer prefix complete up to that common prefix.
        app.repl.complete = lambda line: ["movement", "mover"]  # type: ignore[method-assign]
        command.value = "mo"
        command.cursor_position = 2
        await pilot.press("tab")
        assert command.value == "move"


async def test_app_history_navigation():
    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test() as pilot:
        await _submit(app, pilot, "who")
        await _submit(app, pilot, "look")
        command = app.query_one(ReplInput)
        command.value = "dr"
        command.cursor_position = 2
        await pilot.press("up")
        assert command.value == "look"
        await pilot.press("up")
        assert command.value == "who"
        await pilot.press("down")
        assert command.value == "look"
        await pilot.press("down")
        assert command.value == "dr"  # back to the in-progress draft


async def test_app_history_navigation_noop_without_history():
    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test() as pilot:
        command = app.query_one(ReplInput)
        await pilot.press("up")
        assert command.value == ""


async def test_app_history_file_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    history = tmp_path / "bunnyland" / "repl-history"
    history.parent.mkdir(parents=True)
    history.write_text("who\nlook\n", encoding="utf-8")

    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test() as pilot:
        assert app.query_one(ReplInput).history == ["who", "look"]
        await _submit(app, pilot, "points")
    assert history.read_text(encoding="utf-8").splitlines() == ["who", "look", "points"]


async def test_app_history_file_missing_and_unwritable(monkeypatch, tmp_path):
    # A missing history file is fine on load; an unwritable path is swallowed on save.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test():
        assert app.query_one(ReplInput).history == []

    monkeypatch.setattr(repl_app, "history_path", lambda: tmp_path)  # a directory: write fails
    app = BunnylandReplApp(RecordingBackend())
    async with app.run_test():
        pass


class SelectorReplLocalBackend(LocalBackend):
    def __init__(self) -> None:
        super().__init__(
            seed="old seed",
            generator="apartment-demo",
            autorun=False,
            client_id="selector-client",
        )
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def fetch_content_flags(self):
        return ()


async def test_repl_mount_uses_local_generator_selection(monkeypatch):
    backend = SelectorReplLocalBackend()
    app = BunnylandReplApp(backend)
    app.show_generator_selector = True
    refreshed = []
    intervals = []
    workers = []

    def fake_push_screen(screen, callback=None):
        assert isinstance(screen, WorldGeneratorSelector)
        callback(GeneratorSelection(generator="clover-city", seed="city seed"))

    async def fake_refresh(prime=False):
        refreshed.append(prime)

    monkeypatch.setattr(app, "push_screen", fake_push_screen)
    monkeypatch.setattr(app, "run_worker", lambda coro, **kwargs: workers.append(coro))
    monkeypatch.setattr(app, "_load_history", lambda: None)
    monkeypatch.setattr(app, "_safe_refresh", fake_refresh)
    monkeypatch.setattr(app, "write_log", lambda renderable: None)
    monkeypatch.setattr(app, "set_interval", lambda *args: intervals.append(args))
    monkeypatch.setattr(app.command, "focus", lambda: None)

    await app.on_mount()
    assert len(workers) == 1
    await workers[0]

    assert backend.started
    assert backend.generator_name == "clover-city"
    assert backend.seed == "city seed"
    assert backend.label == "local · clover-city"
    assert refreshed == [True]
    assert intervals


async def test_repl_mount_shows_intro_before_local_generator_selector(monkeypatch):
    backend = SelectorReplLocalBackend()
    app = BunnylandReplApp(backend, show_intro=True)
    app.show_generator_selector = True
    pushed = []

    def fake_push_screen(screen, callback=None):
        pushed.append((screen, callback))

    monkeypatch.setattr(app, "push_screen", fake_push_screen)

    await app.on_mount()

    assert isinstance(pushed[0][0], IntroSplash)
    assert not backend.started

    pushed[0][1](None)
    assert isinstance(pushed[1][0], WorldGeneratorSelector)
    assert not backend.started


async def test_repl_mount_exits_when_local_generator_selection_is_cancelled(monkeypatch):
    backend = SelectorReplLocalBackend()
    app = BunnylandReplApp(backend)
    app.show_generator_selector = True
    exits = []

    def fake_push_screen(screen, callback=None):
        assert isinstance(screen, WorldGeneratorSelector)
        callback(None)

    monkeypatch.setattr(app, "push_screen", fake_push_screen)
    monkeypatch.setattr(app, "exit", lambda: exits.append(True))

    await app.on_mount()

    assert exits == [True]
    assert not backend.started


# ── lazy package exports + CLI wiring ─────────────────────────────────────────
def test_repl_package_lazily_exports_app_symbols():
    import bunnyland.repl as repl

    assert repl.main is repl_app.main
    assert repl.BunnylandReplApp is BunnylandReplApp
    unknown = "does_not_exist"
    with pytest.raises(AttributeError):
        getattr(repl, unknown)


def test_repl_module_entry_point_is_importable():
    import bunnyland.repl.__main__ as entry

    assert entry.main is repl_app.main


def test_available_generators_includes_the_demo_default():
    names = [generator.name for generator in available_generators()]
    assert "apartment-demo" in names and names == sorted(names)


def test_format_generator_lines_flags_seed_and_description():
    from types import SimpleNamespace

    generators = [
        SimpleNamespace(
            name="apartment-demo",
            uses_seed=False,
            description="a demo",
            group="pop culture",
        ),
        SimpleNamespace(
            name="recursive",
            uses_seed=True,
            description="",
            group="algorithmic",
        ),
    ]
    assert format_generator_lines(generators) == [
        "Algorithmic:",
        "  recursive",
        "",
        "Pop Culture:",
        "  apartment-demo *",
        "      a demo",
        "",
        "* ignores --seed",
    ]


def test_format_generator_lines_omits_seed_footer_when_all_seeded():
    from types import SimpleNamespace

    generators = [
        SimpleNamespace(
            name="recursive",
            uses_seed=True,
            description="",
            group="algorithmic",
        ),
    ]
    # No seedless generator, so no "* ignores --seed" footer is appended.
    assert format_generator_lines(generators) == [
        "Algorithmic:",
        "  recursive",
    ]


def test_main_lists_generators_and_exits(monkeypatch, capsys):
    from types import SimpleNamespace

    launched: list[bool] = []
    monkeypatch.setattr(
        repl_app,
        "available_generators",
        lambda: [SimpleNamespace(name="apartment-demo", uses_seed=False, description="a demo")],
    )
    monkeypatch.setattr(
        repl_app,
        "BunnylandReplApp",
        lambda backend: launched.append(True),  # must not be constructed
    )

    assert repl_app.main(["--list-generators"]) == 0
    assert launched == []
    output = capsys.readouterr().out
    assert "Custom:" in output
    assert "apartment-demo *" in output


def test_main_runs_remote_backend(monkeypatch):
    backends, runs, apps = [], [], []

    class BackendStub:
        def __init__(
            self,
            server,
            *,
            fallback_controller=None,
            timeout_seconds=None,
            username="",
            password="",
            token_file=None,
        ):
            self.server = server
            self.fallback_controller = fallback_controller
            self.timeout_seconds = timeout_seconds
            self.username = username
            self.password = password
            self.token_file = token_file
            backends.append(self)

    class AppStub:
        def __init__(self, backend):
            self.backend = backend
            apps.append(self)

        def run(self):
            runs.append(self)

    monkeypatch.setattr(repl_app, "RemoteBackend", BackendStub)
    monkeypatch.setattr(repl_app, "BunnylandReplApp", AppStub)

    assert (
        repl_app.main(
            [
                "--server",
                "https://example.test",
                "--claim-fallback",
                "llm",
                "--claim-timeout-minutes",
                "10",
            ]
        )
        == 0
    )
    assert [app.backend for app in runs] == backends
    assert backends[0].server == "https://example.test"
    assert backends[0].timeout_seconds == 600
    assert apps[0].show_generator_selector is False


@pytest.mark.parametrize("password_stdin", [False, True])
def test_main_reads_remote_password(monkeypatch, password_stdin):
    captured = {}

    class BackendStub:
        def __init__(self, _server, **kwargs):
            captured.update(kwargs)

    class AppStub:
        def __init__(self, _backend):
            pass

        def run(self):
            pass

    monkeypatch.setattr(repl_app, "RemoteBackend", BackendStub)
    monkeypatch.setattr(repl_app, "BunnylandReplApp", AppStub)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "prompt password")
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin password\n"))
    argv = ["--server", "https://example.test", "--username", "player"]
    if password_stdin:
        argv.append("--password-stdin")
    assert repl_app.main(argv) == 0
    assert captured["password"] == ("stdin password" if password_stdin else "prompt password")


def test_main_runs_local_backend(monkeypatch):
    backends = []
    apps = []

    class BackendStub:
        def __init__(
            self, *, seed=None, generator=None, fallback_controller=None, timeout_seconds=None
        ):
            self.seed = seed
            self.generator = generator
            backends.append(self)

    class AppStub:
        def __init__(self, backend):
            self.backend = backend
            apps.append(self)

        def run(self): ...

    monkeypatch.setattr(repl_app, "LocalBackend", BackendStub)
    monkeypatch.setattr(repl_app, "BunnylandReplApp", AppStub)

    assert repl_app.main(["--seed", "test seed", "--generator", "empty"]) == 0
    assert backends[0].seed == "test seed"
    assert backends[0].generator == "empty"
    assert apps[0].show_generator_selector is False


def test_main_no_icons_disables_repl_icons(monkeypatch):
    apps = []

    class BackendStub:
        def __init__(
            self, *, seed=None, generator=None, fallback_controller=None, timeout_seconds=None
        ):
            del seed, generator, fallback_controller, timeout_seconds

    class AppStub:
        def __init__(self, backend):
            self.backend = backend
            self.repl = SimpleNamespace(show_icons=True)
            apps.append(self)

        def run(self): ...

    monkeypatch.setattr(repl_app, "LocalBackend", BackendStub)
    monkeypatch.setattr(repl_app, "BunnylandReplApp", AppStub)

    assert repl_app.main(["--no-icons"]) == 0
    assert apps[0].repl.show_icons is False
    assert apps[0].show_generator_selector is True


# ── character sheet deep links ───────────────────────────────────────────────────────


async def test_dispatch_sheet_requires_player():
    class _SheetBackend(RecordingBackend):
        supports_character_sheets = True

    repl = BunnylandRepl(_SheetBackend(_snapshot()))
    repl.character_list = _character_list_from_snapshot(_snapshot())
    message = await repl.dispatch("sheet")
    assert "Pick a player first" in message.plain


async def test_dispatch_sheet_unavailable_for_local_backend():
    # RecordingBackend inherits the base default -> unavailable.
    repl = _repl()
    message = await repl.dispatch("sheet")
    assert "Character sheets require a remote server URL" in message.plain


async def test_dispatch_sheet_opens_current_and_named_character():
    from bunnyland.tui.backend import SheetOpenResult

    class _SheetBackend(RecordingBackend):
        supports_character_sheets = True

        def __init__(self, snapshot):
            super().__init__(snapshot)
            self.opened: list[str] = []

        async def open_character_sheet(self, character_id):
            self.opened.append(character_id)
            return SheetOpenResult(
                ok=True, url=f"http://web.test/character-sheet.html#{character_id}"
            )

    repl = BunnylandRepl(_SheetBackend(_snapshot()))
    repl.world = World.parse(_snapshot())
    repl.character_list = _character_list_from_snapshot(_snapshot())
    repl.player_id = PLAYER

    current = await repl.dispatch("sheet")
    assert "Opened sheet" in current.plain
    assert repl.backend.opened[-1] == PLAYER

    named = await repl.dispatch("profile Marlow")
    assert "Opened sheet" in named.plain
    assert repl.backend.opened[-1] == MARLOW

    direct_id = await repl.dispatch(f"sheet {MARLOW}")
    assert "Opened sheet" in direct_id.plain
    assert repl.backend.opened[-1] == MARLOW


async def test_dispatch_sheet_handles_me_missing_and_backend_failure():
    from bunnyland.tui.backend import SheetOpenResult

    class _SheetBackend(RecordingBackend):
        supports_character_sheets = True

        async def open_character_sheet(self, character_id):
            return SheetOpenResult(ok=False, reason=f"no browser for {character_id}")

    repl = BunnylandRepl(_SheetBackend(_snapshot()))
    repl.world = World.parse(_snapshot())
    repl.character_list = _character_list_from_snapshot(_snapshot())
    repl.player_id = PLAYER

    me = await repl.dispatch("sheet me")
    assert f"no browser for {PLAYER}" in me.plain

    missing = await repl.dispatch("profile Nobody")
    assert "No character sheet target" in missing.plain


async def test_dispatch_image_when_supported():
    from bunnyland.tui.backend import ImageRequestResult

    class _ImageBackend(RecordingBackend):
        supports_image_requests = True

        def __init__(self, snapshot):
            super().__init__(snapshot)
            self.requested: list[str] = []

        async def request_image(self, character_id, event_id=""):
            del event_id
            self.requested.append(character_id)
            return ImageRequestResult(ok=True, status="queued")

    repl = BunnylandRepl(_ImageBackend(_snapshot()))
    repl.world = World.parse(_snapshot())
    repl.character_list = _character_list_from_snapshot(_snapshot())
    repl.player_id = PLAYER

    message = await repl.dispatch("image")

    assert "image requested" in message.plain
    assert repl.backend.requested == [PLAYER]


async def test_dispatch_image_requires_player_and_surfaces_failure():
    from bunnyland.tui.backend import ImageRequestResult

    class _ImageBackend(RecordingBackend):
        supports_image_requests = True

        async def request_image(self, character_id, event_id=""):
            del event_id
            return ImageRequestResult(
                ok=False,
                status="failed",
                reason=f"camera offline for {character_id}",
            )

    repl = BunnylandRepl(_ImageBackend(_snapshot()))

    no_player = await repl.dispatch("img")
    assert "Pick a player first" in no_player.plain

    repl.player_id = PLAYER
    failed = await repl.dispatch("image")
    assert f"camera offline for {PLAYER}" in failed.plain


async def test_dispatch_image_unavailable_when_not_supported():
    repl = _repl()
    message = await repl.dispatch("image")
    assert "Image requests are not available" in message.plain


async def test_dispatch_video_covers_player_capability_and_result_paths():
    from bunnyland.tui.backend import ImageRequestResult

    class _VideoBackend(RecordingBackend):
        supports_video_requests = True

        def __init__(self, snapshot, result):
            super().__init__(snapshot)
            self.result = result
            self.requested: list[str] = []

        async def request_video(self, character_id, event_id=""):
            del event_id
            self.requested.append(character_id)
            return self.result

    queued_backend = _VideoBackend(
        _snapshot(), ImageRequestResult(ok=True, status="queued")
    )
    repl = BunnylandRepl(queued_backend)
    assert "Pick a player first" in (await repl.dispatch("video")).plain
    repl.player_id = PLAYER
    assert "video requested" in (await repl.dispatch("video")).plain
    assert queued_backend.requested == [PLAYER]

    for result, expected in (
        (ImageRequestResult(ok=True, status="skipped"), "video ready"),
        (
            ImageRequestResult(ok=False, status="failed", reason="encoder offline"),
            "encoder offline",
        ),
    ):
        current = BunnylandRepl(_VideoBackend(_snapshot(), result))
        current.player_id = PLAYER
        assert expected in (await current.dispatch("video")).plain

    unsupported = _repl()
    unsupported.player_id = PLAYER
    assert "Video requests are not available" in (await unsupported.dispatch("video")).plain


async def test_dispatch_open_remains_world_action():
    repl = _repl()
    message = await repl.dispatch("open target_id=an apple")

    assert "Opened sheet" not in message.plain
    assert repl.backend.commands[-1]["command_type"] == "open"


def test_terminal_repl_meta_commands_follow_backend_capabilities():
    class _BothBackend(RecordingBackend):
        supports_character_sheets = True
        supports_image_requests = True
        supports_video_requests = True

    local = _repl()
    remote = BunnylandRepl(_BothBackend(_snapshot()))

    assert "sheet" not in local.meta_commands()
    assert "image" not in local.meta_commands()
    assert "video" not in local.meta_commands()
    assert "sheet" in remote.meta_commands()
    assert "profile" in remote.meta_commands()
    assert "image" in remote.meta_commands()
    assert "img" in remote.meta_commands()
    assert "video" in remote.meta_commands()
    assert "open" not in remote.meta_commands()


async def test_repl_live_update_worker_suppresses_polling_and_tracks_player_changes():
    class LiveBackend(RecordingBackend):
        def supports_live_updates(self) -> bool:
            return True

        async def watch_updates(self, character_id, control, on_message, on_state) -> None:
            assert character_id == PLAYER
            assert control == ("controller:1", 2)
            await on_state("live")
            await on_message({"type": "heartbeat", "data": {}})
            await on_message({"type": "event", "data": {}})
            await on_state("fallback")

    app = BunnylandReplApp(LiveBackend(_snapshot()))
    app.repl.player_id = PLAYER
    app.repl.control = ControlClaim("controller:1", 2)
    refreshes = []

    async def safe_refresh(*_args, **_kwargs):
        refreshes.append(True)

    app._safe_refresh = safe_refresh
    app._sync_live_updates()
    await app._live_task
    assert refreshes == [True, True]
    assert app._live_ready is False

    app._sync_live_updates()
    app._live_ready = True
    await app._poll_refresh()
    assert refreshes == [True, True]
    app._live_ready = False
    await app._poll_refresh()
    assert refreshes == [True, True, True]

    sleeping = asyncio.create_task(asyncio.sleep(60))
    app._live_task = sleeping
    app._stop_live_updates()
    assert sleeping.cancelled() or sleeping.cancelling()
    app.repl.player_id = ""
    app._sync_live_updates()
