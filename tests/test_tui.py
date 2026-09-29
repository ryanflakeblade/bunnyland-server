"""Tests for the Textual terminal client: world model, backends, and the app itself."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import sys
from types import SimpleNamespace

import pytest

from bunnyland.core import (
    CharacterComponent,
    CommandCost,
    IdentityComponent,
    Lane,
    LLMControllerComponent,
    OnInsufficientPoints,
    SuspendedComponent,
    SuspendedControllerComponent,
    WebControllerComponent,
    build_submitted_command,
    parse_entity_id,
    spawn_entity,
)
from bunnyland.core.controllers import ClaimTimeoutComponent
from bunnyland.core.world_actor import WorldActor
from bunnyland.persistence import type_registries
from bunnyland.plugins import PluginRegistry, bunnyland_plugins
from bunnyland.tui import app as tui_app
from bunnyland.tui import backend as tui_backend
from bunnyland.tui.app import ActionForm, BunnylandTUI, FormField, HelpScreen
from bunnyland.tui.backend import (
    Backend,
    ControlClaim,
    ImageRequestResult,
    LocalBackend,
    RemoteBackend,
    SubmitResult,
    clear_claim_control,
    is_authentication_required,
    load_claim_control,
    persistent_client_id,
    save_claim_control,
)
from bunnyland.tui.events import EventNarrator
from bunnyland.tui.generator_selector import (
    PRESET_SEEDS,
    GeneratorSelection,
    WorldGeneratorSelector,
    random_preset_seed,
)
from bunnyland.tui.model import Target, World, entity_icon, entity_name, entity_type
from bunnyland.tui.screens import SignInCredentials, SignInScreen
from bunnyland.tui.splash import IntroSplash
from bunnyland.worldgen import GenOptions, InstantiatedWorld, WorldGenerator

PLAYER = "character:1"
MARLOW = "character:2"
APPLE = "item:1"
KEY = "item:2"
PARLOR = "room:1"
HALL = "room:2"


def _snapshot() -> dict:
    """A serialize_world-shaped snapshot: a parlor (player, Marlow, an apple) with a
    held key, a north exit to a hallway, and the player driven by controller gen 2."""
    return {
        "schema_version": 1,
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
                    "SpriteLayerComponent": {"layer": 30},
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
                    "SpriteLayerComponent": {"layer": 20},
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


def _client_view() -> dict:
    return {
        "schema_version": 1,
        "world_epoch": 43,
        "character_id": PLAYER,
        "character_name": "Pib",
        "can_perceive": True,
        "room": {
            "id": PARLOR,
            "title": "Parlor",
            "entities": [
                {
                    "id": MARLOW,
                    "name": "Marlow",
                    "kind": "character",
                    "is_character": True,
                    "contents": [],
                },
                {
                    "id": APPLE,
                    "name": "an apple",
                    "kind": "item",
                    "is_character": False,
                    "contents": [],
                },
            ],
            "exits": [
                {
                    "id": HALL,
                    "direction": "north",
                    "label": "north: Hallway",
                    "locked": False,
                }
            ],
        },
        "inventory": [{"id": KEY, "label": "a brass key", "kind": "item"}],
        "points": {"action": 4, "action_max": 5, "focus": 2, "focus_max": 3},
        "controller": {"controller_id": "controller:1", "generation": 3},
        "target_groups": {
            "exits": [{"id": HALL, "label": "north: Hallway", "kind": "exit"}],
            "roomItems": [{"id": APPLE, "label": "an apple", "kind": "item"}],
            "inventory": [{"id": KEY, "label": "a brass key", "kind": "item"}],
            "characters": [{"id": MARLOW, "label": "Marlow", "kind": "character"}],
            "reachableItems": [
                {"id": APPLE, "label": "an apple", "kind": "item"},
                {"id": KEY, "label": "a brass key", "kind": "item"},
            ],
        },
        "actions": [_projected_action()],
    }


def _projected_action(**overrides) -> dict:
    action = {
        "command_type": "inspect",
        "tool_name": "inspect",
        "title": "Inspect",
        "description": "Inspect something nearby.",
        "lane": "world",
        "cost": {"action": 1, "focus": 0},
        "arguments": [
            {
                "key": "target_id",
                "title": "target",
                "kind": "entity",
                "required": True,
                "target_group": "reachableItems",
            }
        ],
        "available": True,
        "enough_action_points": True,
        "enough_focus_points": True,
        "has_required_target": True,
        "meets_requirements": True,
        "unavailable_reason": "",
    }
    action.update(overrides)
    return action


def _action(command_type: str) -> dict:
    definitions = {
        "move": ("Move", "world", 1, 0, "exit_id", "exits"),
        "say": ("Say", "world", 1, 1, "text", None),
        "tell": ("Tell", "world", 1, 1, "target_id", "characters"),
        "wait": ("Wait", "world", 0, 0, None, None),
    }
    title, lane, action_cost, focus_cost, key, target_group = definitions[command_type]
    arguments = []
    if key:
        arguments.append(
            {
                "key": key,
                "title": key.replace("_", " "),
                "kind": "entity" if target_group else "string",
                "required": True,
                "target_group": target_group,
            }
        )
    if command_type == "tell":
        arguments.append(
            {
                "key": "text",
                "title": "text",
                "kind": "string",
                "required": True,
                "target_group": None,
            }
        )
    return _projected_action(
        command_type=command_type,
        tool_name=command_type,
        title=title,
        lane=lane,
        cost={"action": action_cost, "focus": focus_cost},
        arguments=arguments,
    )


def _queued_response(*commands: dict) -> dict:
    return {
        "ok": True,
        "schema_version": 1,
        "world_epoch": 42,
        "character_id": PLAYER,
        "generated_at_unix": 100.0,
        "next_tick_at_unix": 101.0,
        "tick_seconds": 1.0,
        "time_scale": 3600.0,
        "game_seconds_per_tick": 3600.0,
        "commands": list(commands),
    }


def _queued_command(**overrides) -> dict:
    command = {
        "command_id": "cmd-1",
        "character_id": PLAYER,
        "command_type": "say",
        "payload": {"text": "next"},
        "cost": {"action": 1, "focus": 1},
        "lane": "focus",
        "submitted_at_epoch": 41,
        "expires_at_epoch": None,
    }
    command.update(overrides)
    return command


def _event(event_id: str | None, event_type="CustomEvent", **fields) -> dict:
    return {
        "type": "event",
        "data": {
            "event_type": event_type,
            "event": {
                "event_id": event_id,
                "note": event_id,
                **fields,
            },
        },
    }


def test_event_narrator_renders_arrival_room_for_own_move():
    world = World.parse(_snapshot())
    narrator = EventNarrator()
    shown = narrator.drain_events(
        [
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
                "m2",
                event_type="ActorMovedEvent",
                visibility="system",
                actor_id=MARLOW,
                from_room_id=PARLOR,
                to_room_id=HALL,
                arrival_summary="Hallway\nHere: Marlow.\nExits: south.",
            ),
        ],
        player_id=PLAYER,
        room_of=world.room_of,
        name_for=lambda entity_id: entity_name(world.get(entity_id)),
    )

    assert [item.plain for item in shown] == ["➡️ Hallway\nHere: Pib.\nExits: south."]

    narrator = EventNarrator()
    plain = narrator.drain_events(
        [
            _event(
                "m1",
                event_type="ActorMovedEvent",
                visibility="system",
                actor_id=PLAYER,
                from_room_id=PARLOR,
                to_room_id=HALL,
                arrival_summary="Hallway\nHere: Pib.\nExits: south.",
            )
        ],
        player_id=PLAYER,
        room_of=world.room_of,
        name_for=lambda entity_id: entity_name(world.get(entity_id)),
        show_icons=False,
    )
    assert [item.plain for item in plain] == ["Hallway\nHere: Pib.\nExits: south."]


def test_event_narrator_uses_normal_style_for_activity_and_dim_for_system():
    world = World.parse(_snapshot())
    narrator = EventNarrator()
    shown = narrator.drain_events(
        [
            _event(
                "claim",
                event_type="CharacterClaimedEvent",
                visibility="public",
                actor_id=PLAYER,
                note="",
                character_id=PLAYER,
                controller_id="controller:1",
                generation=3,
            ),
            _event(
                "look",
                event_type="RoomLookedEvent",
                visibility="private",
                actor_id=PLAYER,
                note="",
                room_id=PARLOR,
                room_title="Parlor",
                summary="Parlor: Marlow, an apple",
            ),
            _event(
                "controller",
                event_type="ControllerChangedEvent",
                visibility="public",
                actor_id=PLAYER,
                note="",
                generation=4,
                controller_kind="web",
            ),
        ],
        player_id=PLAYER,
        room_of=world.room_of,
        name_for=lambda entity_id: (
            entity_name(world.get(entity_id)) if world.get(entity_id) else None
        ),
    )

    assert [item.plain for item in shown] == [
        "🎮 Pib: Character claimed — Pib; generation 3",
        "👁️ Parlor: Marlow, an apple",
        "🎮 Pib: Controller changed — generation 4; controller kind web",
    ]
    assert shown[0].style == ""
    assert shown[1].style == ""
    assert shown[2].style == "dim"


def test_event_narrator_renders_character_reaction_summary():
    world = World.parse(_snapshot())
    narrator = EventNarrator()
    [shown] = narrator.drain_events(
        [
            _event(
                "reaction",
                event_type="CharacterReactionEvent",
                visibility="directed",
                actor_id=MARLOW,
                target_ids=(PLAYER,),
                command_type="wait",
                summary="暂时没有行动。",
            )
        ],
        player_id=PLAYER,
        room_of=world.room_of,
        name_for=lambda entity_id: entity_name(world.get(entity_id)),
    )

    assert shown.plain == "Marlow：暂时没有行动。"
    assert shown.style == "dim"


def test_event_narrator_renders_inspection_fact_text_without_raw_records():
    world = World.parse(_snapshot())
    narrator = EventNarrator()
    [shown] = narrator.drain_events(
        [
            _event(
                "inspect",
                event_type="EntityInspectedEvent",
                visibility="private",
                actor_id=PLAYER,
                note="",
                entity_id=PLAYER,
                name="Pib",
                facts=[{"key": "needs.hunger", "text": "You are not hungry.", "detail": 30}],
            )
        ],
        player_id=PLAYER,
        room_of=world.room_of,
        name_for=lambda entity_id: (
            entity_name(world.get(entity_id)) if world.get(entity_id) else None
        ),
    )

    assert "You are not hungry." in shown.plain
    assert "needs.hunger" not in shown.plain
    assert "{'key'" not in shown.plain


# ── lazy package exports ──────────────────────────────────────────────────────
def test_tui_package_lazily_exports_app_symbols():
    import bunnyland.tui as tui

    # The Textual app is imported lazily; the package exposes it on access so the REPL can
    # reuse the textual-free backend/model without importing Textual.
    assert tui.main is tui_app.main
    assert tui.BunnylandTUI is BunnylandTUI
    unknown = "does_not_exist"
    with pytest.raises(AttributeError):
        getattr(tui, unknown)


def test_action_list_height_leaves_queue_visible():
    assert "#members, #doors, #activity {" in BunnylandTUI.CSS
    assert "#verbs { height: auto; max-height: 12; }" in BunnylandTUI.CSS
    assert "#queued { height: 1fr; min-height: 4;" in BunnylandTUI.CSS
    assert "#action-filter-row { height: 3; }" in BunnylandTUI.CSS
    assert "#action-filter-clear { width: 9; min-width: 9; }" in BunnylandTUI.CSS
    assert "#doors-title, #activity-title, #queued-title { border-top:" in BunnylandTUI.CSS
    assert "#doors { border-top:" not in BunnylandTUI.CSS


# ── world model ───────────────────────────────────────────────────────────────
def test_parse_normalizes_relationships_and_epoch():
    snapshot = _snapshot()
    snapshot["queued_commands"] = [
        _queued_command(),
        _queued_command(command_id="cmd-2", character_id=MARLOW),
    ]
    world = World.parse(snapshot)
    assert world.epoch == 42
    assert set(world.entities) == {PARLOR, HALL, PLAYER, MARLOW, APPLE, KEY}
    # target_id is normalized to target on every edge.
    assert world.get(PARLOR)["relationships"]["Contains"][0]["target"] == PLAYER
    assert [command["command_id"] for command in world.queued_for(PLAYER)] == ["cmd-1"]
    assert world.queued_for("") == []


def test_rooms_characters_and_containment():
    world = World.parse(_snapshot())
    assert {r["id"] for r in world.rooms()} == {PARLOR, HALL}
    assert [entity_name(c) for c in world.characters()] == ["Marlow", "Pib"]  # sorted
    assert world.room_of(PLAYER) == PARLOR
    assert {m["id"] for m in world.room_members(PARLOR)} == {PLAYER, MARLOW, APPLE}


def test_doors_and_carried():
    world = World.parse(_snapshot())
    doors = world.doors(PARLOR)
    assert doors == [(HALL, "north", world.get(HALL))]
    assert [e["id"] for e in world.carried(PLAYER)] == [KEY]


def test_control_and_points():
    world = World.parse(_snapshot())
    assert world.control(PLAYER) == ("controller:1", 2)
    pts = world.points(PLAYER)
    assert (pts["ap"], pts["ap_max"], pts["fp"], pts["fp_max"]) == (5, 5, 3, 3)
    assert world.control(MARLOW) is None  # no ControlledBy edge


def test_character_sheet_url_derives_frontend_from_api_base():
    from bunnyland.tui.backend import character_sheet_url, frontend_base_for_api

    assert frontend_base_for_api("https://play.test/api") == "https://play.test"
    assert frontend_base_for_api("https://play.test/prefix/api/") == "https://play.test/prefix"
    assert frontend_base_for_api("https://play.test/prefix/api/v1/") == "https://play.test/prefix"
    assert frontend_base_for_api("https://play.test/raw") == "https://play.test/raw"
    assert character_sheet_url("https://play.test/api", PLAYER) == (
        "https://play.test/character.html?server=https%3A%2F%2Fplay.test%2Fapi#character:1"
    )


async def test_remote_backend_opens_character_sheet(monkeypatch):
    from bunnyland.tui.backend import RemoteBackend

    opened: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "bunnyland.tui.backend.webbrowser.open",
        lambda url, new=0: opened.append((url, new)) or True,
    )

    backend = RemoteBackend("https://play.test/api/")
    result = await backend.open_character_sheet(PLAYER)

    assert result.ok is True
    assert result.url.endswith("/character.html?server=https%3A%2F%2Fplay.test%2Fapi#character:1")
    assert opened == [(result.url, 2)]


async def test_remote_backend_reports_browser_open_failure(monkeypatch):
    from bunnyland.tui.backend import RemoteBackend

    monkeypatch.setattr("bunnyland.tui.backend.webbrowser.open", lambda url, new=0: False)

    backend = RemoteBackend("https://play.test/api")
    result = await backend.open_character_sheet(PLAYER)

    assert result.ok is False
    assert result.reason == "could not open browser"
    assert "character.html" in result.url


async def test_backend_base_open_character_sheet_default():
    from bunnyland.tui.backend import Backend

    class _Stub(Backend):
        async def start(self): ...
        async def close(self): ...
        async def fetch_snapshot(self):
            return {}

        async def submit(self, command):
            raise NotImplementedError

        async def claim(self, player_id, world):
            return None

    result = await _Stub().open_character_sheet(PLAYER)
    assert result.ok is False
    assert "remote server URL" in result.reason
    control = ControlClaim("controller:1", 2, "claim-1", "secret-1")
    assert await _Stub().release_controller(PLAYER, control) == control
    assert await _Stub().release_claim(PLAYER, control) is False
    image = await _Stub().request_image(PLAYER)
    assert image.ok is False
    assert image.status == "unavailable"
    video = await _Stub().request_video(PLAYER)
    assert video.ok is False
    assert video.status == "unavailable"
    assert await _Stub().allows_sleeping_character_chat() is False


def test_local_backend_image_capability_reflects_service():
    from bunnyland.tui.backend import LocalBackend

    backend = LocalBackend(autorun=False)
    assert backend.supports_image_requests is False
    backend.imagegen = SimpleNamespace()
    assert backend.supports_image_requests is True
    assert backend.supports_video_requests is False
    backend.videogen = SimpleNamespace()
    assert backend.supports_video_requests is True


async def test_local_backend_sleeping_chat_capability_reflects_service():
    from bunnyland.tui.backend import LocalBackend

    backend = LocalBackend(autorun=False)
    assert await backend.allows_sleeping_character_chat() is False
    backend.character_chat = SimpleNamespace(allow_sleeping_character_chat=True)
    assert await backend.allows_sleeping_character_chat() is True


async def test_local_backend_request_image_reports_unconfigured_service():
    backend = LocalBackend(generator="apartment-demo", autorun=False, client_id="local-client")
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        result = await backend.request_image(player)
    finally:
        await backend.close()

    assert result.ok is False
    assert result.status == "unavailable"


async def test_local_backend_request_image_reports_no_room_and_success(monkeypatch):
    calls = []

    async def request_scene_image(actor, imagegen, *, character_id, event_id=""):
        del event_id
        calls.append((actor, imagegen, character_id))
        if len(calls) == 1:
            return None
        return SimpleNamespace(status="queued", url="http://image")

    monkeypatch.setattr(
        "bunnyland.imagegen.scene.request_scene_image",
        request_scene_image,
    )
    backend = LocalBackend(generator="apartment-demo", autorun=False, client_id="local-client")
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        backend.imagegen = object()
        no_room = await backend.request_image(player)
        queued = await backend.request_image(player)
    finally:
        await backend.close()

    assert no_room.ok is False
    assert no_room.status == "no-room"
    assert queued.ok is True
    assert queued.status == "queued"
    assert queued.url == "http://image"


async def test_local_backend_request_video_reports_unavailable_no_room_and_success(monkeypatch):
    calls = []

    async def request_scene_video(actor, imagegen, *, character_id, event_id=""):
        del event_id
        calls.append((actor, imagegen, character_id))
        if len(calls) == 1:
            return None
        return SimpleNamespace(status="queued", url="http://video")

    monkeypatch.setattr("bunnyland.imagegen.scene.request_scene_video", request_scene_video)
    backend = LocalBackend(generator="apartment-demo", autorun=False, client_id="local-client")
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        unavailable = await backend.request_video(player)
        backend.videogen = SimpleNamespace()
        no_room = await backend.request_video(player)
        queued = await backend.request_video(player)
    finally:
        await backend.close()

    assert unavailable.status == "unavailable"
    assert no_room.status == "no-room"
    assert queued == ImageRequestResult(ok=True, status="queued", url="http://video")


def test_parse_client_view_supports_filtered_structured_surface():
    world = World.parse(_client_view())

    assert world.epoch == 43
    assert world.room_of(PLAYER) == PARLOR
    assert entity_name(world.get(PARLOR)) == "Parlor"
    assert world.control(PLAYER) == ("controller:1", 3)
    assert world.points(PLAYER) == {
        "has": True,
        "ap": 4,
        "ap_max": 5,
        "fp": 2,
        "fp_max": 3,
    }
    assert {target.value for target in world.target_candidates(PLAYER, "exits")} == {HALL}
    assert {target.value for target in world.target_candidates(PLAYER, "roomItems")} == {APPLE}
    assert {target.value for target in world.target_candidates(PLAYER, "inventory")} == {KEY}
    assert [action["command_type"] for action in world.actions] == ["inspect"]


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("exits", {HALL}),
        ("roomItems", {APPLE}),
        ("inventory", {KEY}),
        ("characters", {MARLOW}),
        ("reachableItems", {APPLE, KEY}),
    ],
)
def test_target_candidates(kind, expected):
    world = World.parse(_snapshot())
    assert {t.value for t in world.target_candidates(PLAYER, kind)} == expected


def test_entity_presentation():
    world = World.parse(_snapshot())
    assert entity_type(world.get(PARLOR)) == "room"
    assert entity_type(world.get(PLAYER)) == "character"
    assert entity_type(world.get(APPLE)) == "item"
    assert entity_name(world.get(PARLOR)) == "Parlor"
    assert entity_icon(world.get(APPLE)) == "🍎"


def test_world_model_empty_and_missing_lookup_fallbacks():
    world = World.parse(None)

    assert world.epoch == 0
    assert world.first_room_id() is None
    assert world.room_of(None) is None
    assert world.room_members("missing") == []
    assert world.doors("missing") == []
    assert world.carried("missing") == []
    assert world.target_candidates("missing", "unknown") == []


def test_parse_client_view_without_room_keeps_character_only():
    # A client view whose room is empty (no id) routes through _parse_client_view but skips
    # all room/containment synthesis: only the character entity is materialized.
    view = {
        "world_epoch": 7,
        "character_id": PLAYER,
        "character_name": "Pib",
        "room": {},
        "points": {"action": 1, "action_max": 2, "focus": 0, "focus_max": 1},
    }
    world = World.parse(view)
    assert set(world.entities) == {PLAYER}
    assert world.get(PLAYER) is not None
    assert world.rooms() == []
    assert world.room_of(PLAYER) is None


def test_carried_skips_relationship_targets_missing_from_snapshot():
    # A Holding edge whose target is absent from the snapshot (a dangling reference) is
    # skipped rather than surfacing a None entry in the carried list.
    snapshot = {
        "world_epoch": 0,
        "entities": [
            {
                "id": PLAYER,
                "components": {"CharacterComponent": {}},
                "relationships": {
                    "Holding": [
                        {"target_id": KEY},
                        {"target_id": "item:ghost"},
                    ]
                },
            },
            {
                "id": KEY,
                "components": {"PortableComponent": {}},
                "relationships": {},
            },
        ],
    }
    world = World.parse(snapshot)
    carried = world.carried(PLAYER)
    assert [e["id"] for e in carried] == [KEY]
    assert world.get("item:ghost") is None


def test_entity_presentation_fallbacks():
    room = {"id": "room:missing-title", "components": {"RoomComponent": {}}, "relationships": {}}
    door = {"id": "door:1", "components": {"DoorComponent": {}}, "relationships": {}}
    container = {
        "id": "container:1",
        "components": {"ContainerComponent": {}},
        "relationships": {},
    }
    custom = {
        "id": "custom:1",
        "components": {
            "IdentityComponent": {"name": "", "kind": "mystery"},
            "EditorDisplayComponent": {"emoji": "?"},
        },
        "relationships": {},
    }
    nameless = {"id": "nameless-entity-123456789", "components": {}, "relationships": {}}

    assert entity_type(door) == "door"
    assert entity_type(container) == "container"
    assert entity_type(nameless) == "other"
    assert entity_icon(custom) == "?"
    assert entity_icon(nameless) == "⬡"
    assert entity_name(None) == "?"
    assert entity_name(room) == "room:missing-title"
    assert entity_name(nameless) == "nameless-entity-"


def test_missing_projected_action_metadata_produces_empty_action_state():
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    app.action_views = []
    assert app._available_actions() == []


def test_queued_command_label_formats_unknown_free_command():
    label = tui_app._queued_command_label(
        {
            "command_type": "custom-command",
            "payload": {"empty": "", "target_id": APPLE},
            "cost": {},
        }
    )

    assert label == "custom command — free · target_id: item:1"


def test_queued_command_label_prefers_projected_action_title():
    # When a projected action matches the queued command's type, its title wins over the
    # verb-catalogue fallback.
    label = tui_app._queued_command_label(
        {"command_type": "inspect", "payload": {}, "cost": {"action": 1, "focus": 0}},
        [{"command_type": "inspect", "title": "Inspect Closely"}],
    )

    assert label.startswith("Inspect Closely")


# ── web controller ────────────────────────────────────────────────────────────
def test_web_controller_registered_for_persistence():
    components, _edges = type_registries(PluginRegistry(bunnyland_plugins()))
    assert components.get("WebControllerComponent") is WebControllerComponent


def test_assign_web_controller_reports_web_kind():
    actor = WorldActor()
    character = spawn_entity(
        actor.world, [CharacterComponent(), IdentityComponent(name="Pib", kind="character")]
    )
    controller = spawn_entity(actor.world, [WebControllerComponent()])
    generation = actor.assign_controller(character.id, controller.id)
    assert generation == 0
    assert actor._controller_kind(controller.id) == "web"


# ── local backend (host a world in-process) ───────────────────────────────────
async def test_local_backend_hosts_claims_and_submits(monkeypatch, tmp_path):
    monkeypatch.setattr(tui_backend, "CONFIG_DIR", tmp_path / "config")
    backend = LocalBackend(
        generator="apartment-demo",
        autorun=False,
        client_id="local-client",
        fallback_controller="llm",
        timeout_seconds=900,
    )
    await backend.start()
    try:
        world = World.parse(await backend.fetch_snapshot())
        assert world.rooms() and world.characters()

        player = world.characters()[0]["id"]
        save_claim_control(
            "local-client",
            player,
            ControlClaim("controller:old", 1, "client-chosen-claim", "stale-secret"),
        )
        control = await backend.claim(player, world)
        assert control is not None
        assert control.claim_id != "client-chosen-claim"
        controller_id, _generation = control
        # The claim attaches our reusable web controller.
        assert backend.actor._controller_kind(backend._controller.id) == "web"
        assert backend._controller.get_component(WebControllerComponent).client_id == "local-client"
        claim = backend._controller.get_component(ClaimTimeoutComponent)
        assert claim.fallback_controller == "llm"
        assert claim.timeout_seconds == 900

        epoch_before = backend.actor.epoch
        ok = await backend.submit(
            {
                "character_id": player,
                "controller_id": controller_id,
                "controller_generation": control[1],
                "command_type": "wait",
                "payload": {},
                "cost": {"action": 0, "focus": 0},
                "lane": "world",
                "on_insufficient_points": "queue",
            }
        )
        assert ok
        await backend.actor.tick(60.0)  # process the queued command
        assert backend.actor.epoch > epoch_before  # the world advanced
        # The claimed controller is what the fresh snapshot reports.
        refreshed = World.parse(await backend.fetch_snapshot())
        assert refreshed.control(player) == (controller_id, control[1])

        await backend.actor.submit(
            build_submitted_command(
                character_id=player,
                controller_id=controller_id,
                controller_generation=control[1],
                command_type="say",
                payload={"text": "later"},
                cost=CommandCost(action=1, focus=1),
                lane=Lane.FOCUS,
                on_insufficient_points=OnInsufficientPoints.QUEUE,
                submitted_at_epoch=backend.actor.epoch,
            )
        )
        queued = await backend.fetch_queued_commands(player)
        assert queued["character_id"] == player
        assert queued["commands"][-1]["command_type"] == "say"

        character_projection = await backend.fetch_character_projection(player)
        assert character_projection["character_id"] == player
        assert character_projection["actions"]
        room_projection = await backend.fetch_room_projection(refreshed.room_of(player), player)
        assert room_projection["room"]["id"] == refreshed.room_of(player)
        hidden_room = next(
            room["id"] for room in refreshed.rooms() if room["id"] != refreshed.room_of(player)
        )
        with pytest.raises(PermissionError, match="not currently visible"):
            await backend.fetch_room_projection(hidden_room, player)
    finally:
        await backend.close()


async def test_local_backend_claim_unsuspends_player():
    backend = LocalBackend(generator="apartment-demo", autorun=False, client_id="local-client")
    await backend.start()
    try:
        world = World.parse(await backend.fetch_snapshot())
        player = world.characters()[0]["id"]
        character = backend.actor.world.get_entity(parse_entity_id(player))
        assert character.has_component(SuspendedComponent)

        control = await backend.claim(player, world)

        assert control is not None
        assert not character.has_component(SuspendedComponent)
    finally:
        await backend.close()


async def test_local_backend_autorun_starts_and_close_awaits_loop_task():
    # A short tick keeps the live loop cheap; start() schedules it as a task and close()
    # stops the loop and awaits the task to completion (covering the autorun + teardown path).
    import asyncio

    backend = LocalBackend(
        generator="apartment-demo", autorun=True, tick_seconds=0.01, client_id="local-client"
    )
    await backend.start()
    assert backend._task is not None
    # Let the loop actually start running (it sets its own running flag on first schedule)
    # before we ask it to stop, so close() exercises a genuinely live loop teardown.
    for _ in range(5):
        await asyncio.sleep(0.01)
        if getattr(backend._loop, "running", False):
            break
    await backend.close()
    assert backend._task.done()


async def test_local_backend_projects_next_tick_when_loop_is_running():
    backend = LocalBackend(generator="apartment-demo", autorun=False, client_id="local-client")
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        # No live loop yet: the queued projection has no forward-looking tick.
        assert (await backend.fetch_queued_commands(player))["next_tick_at_unix"] is None

        # Mark the loop running (without starting the real cadence) so the projection computes
        # the next tick from now + tick_seconds.
        backend._loop._running = True
        backend._loop._paused = False
        queued = await backend.fetch_queued_commands(player)
        assert queued["next_tick_at_unix"] is not None
        assert queued["tick_seconds"] == backend.tick_seconds
    finally:
        backend._loop._running = False
        await backend.close()


async def test_local_backend_cancel_command_validates_ids_and_generation():
    backend = LocalBackend(generator="apartment-demo", autorun=False, client_id="local-client")
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        controller_id, generation = await backend.claim(
            player, World.parse(await backend.fetch_snapshot())
        )

        # A live generation but an unknown command id resolves to no cancellation.
        assert (
            await backend.cancel_command(player, "no-such-cmd", controller_id, generation) is False
        )
        # A mismatched generation short-circuits before reaching the actor.
        assert (
            await backend.cancel_command(player, "no-such-cmd", controller_id, generation + 99)
            is False
        )
        # An unparseable controller id also returns False.
        assert await backend.cancel_command(player, "no-such-cmd", "bogus", generation) is False
    finally:
        await backend.close()


async def test_local_backend_reclaim_reuses_controller_and_skips_unsuspend():
    backend = LocalBackend(generator="apartment-demo", autorun=False, client_id="local-client")
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        controller_id, _generation = await backend.claim(
            player, World.parse(await backend.fetch_snapshot())
        )
        first_controller = backend._controller

        # A second claim reuses the existing controller (no second spawn) and is a no-op for
        # the now-unsuspended character.
        controller_id_again, _generation = await backend.claim(
            player, World.parse(await backend.fetch_snapshot())
        )
        assert backend._controller is first_controller
        assert controller_id_again == controller_id
    finally:
        await backend.close()


async def test_local_backend_release_controller_and_claim_lifecycle():
    backend = LocalBackend(
        generator="apartment-demo",
        autorun=False,
        client_id="local-client",
        fallback_controller="llm",
    )
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        control = await backend.claim(player, World.parse(await backend.fetch_snapshot()))
        assert control is not None
        backend.actor.world.get_entity(parse_entity_id(player)).add_component(
            SuspendedComponent(reason="manual")
        )

        released = await backend.release_controller(player, control)
        assert released is not None
        assert released.active is False
        controller = backend.actor.world.get_entity(parse_entity_id(released.controller_id))
        assert controller.has_component(LLMControllerComponent)
        assert not backend.actor.world.get_entity(parse_entity_id(player)).has_component(
            SuspendedComponent
        )

        assert await backend.release_claim(player, released) is True
        assert await backend.release_claim(player, released) is True
        assert (
            await backend.release_claim(
                player,
                ControlClaim(controller_id="not-an-entity", generation=0),
            )
            is False
        )
        assert await backend.release_controller("not-an-entity", released) is None
        assert (
            await backend.release_controller(
                player,
                ControlClaim(controller_id="not-an-entity", generation=0),
            )
            is None
        )
    finally:
        await backend.close()


async def test_local_backend_release_controller_to_existing_and_suspended():
    backend = LocalBackend(
        generator="apartment-demo",
        autorun=False,
        client_id="local-client",
    )
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        control = await backend.claim(player, World.parse(await backend.fetch_snapshot()))
        assert control is not None

        suspended = spawn_entity(
            backend.actor.world,
            [SuspendedControllerComponent(reason="idle")],
        )
        backend.fallback_controller = str(suspended.id)
        released = await backend.release_controller(player, control)
        assert released is not None
        assert released.controller_id == str(suspended.id)
        assert backend.actor.world.get_entity(parse_entity_id(player)).has_component(
            SuspendedComponent
        )

        backend.fallback_controller = str(spawn_entity(backend.actor.world).id)
        assert await backend.release_controller(player, released) is None
    finally:
        await backend.close()


async def test_local_backend_release_controller_to_existing_llm_without_suspended_marker():
    backend = LocalBackend(
        generator="apartment-demo",
        autorun=False,
        client_id="local-client",
    )
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        control = await backend.claim(player, World.parse(await backend.fetch_snapshot()))
        assert control is not None
        llm = spawn_entity(
            backend.actor.world,
            [LLMControllerComponent(profile_name="idle", model="claim-model")],
        )
        backend.fallback_controller = str(llm.id)

        released = await backend.release_controller(player, control)

        assert released is not None
        assert released.controller_id == str(llm.id)
        assert not backend.actor.world.get_entity(parse_entity_id(player)).has_component(
            SuspendedComponent
        )
    finally:
        await backend.close()


async def test_local_backend_release_controller_creates_suspended_fallback():
    backend = LocalBackend(
        generator="apartment-demo",
        autorun=False,
        client_id="local-client",
    )
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        control = await backend.claim(player, World.parse(await backend.fetch_snapshot()))
        assert control is not None
        old_controller_id = parse_entity_id(control.controller_id)
        backend._controller = None

        released = await backend.release_controller(player, control)

        assert released is not None
        assert not backend.actor.world.has_entity(old_controller_id)
        controller = backend.actor.world.get_entity(parse_entity_id(released.controller_id))
        assert controller.has_component(SuspendedControllerComponent)
        assert backend.actor.world.get_entity(parse_entity_id(player)).has_component(
            SuspendedComponent
        )
    finally:
        await backend.close()


async def test_local_backend_close_before_start_is_noop():
    # Closing a backend that was never started has no loop or task to tear down.
    backend = LocalBackend(generator="apartment-demo", autorun=False)
    await backend.close()
    assert backend._loop is None
    assert backend._task is None


async def test_local_backend_character_chat_controller_choices_and_assignment_guards():
    pending = LocalBackend(generator="apartment-demo", autorun=False)
    assert await pending.assignable_character_chat_controllers() == ()
    with pytest.raises(RuntimeError, match="has not started"):
        await pending.assign_character_chat_controller("character:1", "controller:1")

    backend = LocalBackend(generator="apartment-demo", autorun=False, client_id="chat-admin")
    await backend.start()
    try:
        player = (await backend.fetch_character_list())[0].character_id
        empty_detail = spawn_entity(
            backend.actor.world,
            [LLMControllerComponent(profile_name="zeta", model="", provider="")],
        )
        selected = spawn_entity(
            backend.actor.world,
            [LLMControllerComponent(profile_name="alpha", model="qwen", provider="ollama")],
        )
        not_llm = spawn_entity(
            backend.actor.world,
            [IdentityComponent(name="not LLM", kind="controller")],
        )

        choices = await backend.assignable_character_chat_controllers()
        assert tuple(choice.label for choice in choices) == tuple(
            sorted((choice.label for choice in choices), key=str.lower)
        )
        assert any(
            choice.controller_id == str(empty_detail.id) and choice.label == "zeta"
            for choice in choices
        )
        assert any(
            choice.controller_id == str(selected.id) and choice.label == "alpha (ollama/qwen)"
            for choice in choices
        )

        with pytest.raises(ValueError, match="character does not exist"):
            await backend.assign_character_chat_controller("not-an-id", str(selected.id))
        with pytest.raises(ValueError, match="controller does not exist"):
            await backend.assign_character_chat_controller(player, "not-an-id")
        with pytest.raises(ValueError, match="not an LLM"):
            await backend.assign_character_chat_controller(player, str(not_llm.id))

        character = backend.actor.world.get_entity(parse_entity_id(player))
        if not character.has_component(SuspendedComponent):
            character.add_component(SuspendedComponent(reason="offline"))
        access = await backend.assign_character_chat_controller(player, str(selected.id))
        assert access.writable is False
        assert "not available" in access.reason
        assert backend.actor._controller_kind(selected.id) == "llm"
        assert backend.actor.current_generation(character.id, selected.id) is not None
        assert not character.has_component(SuspendedComponent)
        await backend.assign_character_chat_controller(player, str(selected.id))
        assert not character.has_component(SuspendedComponent)
    finally:
        await backend.close()


async def test_remote_backend_cancel_command_returns_false_on_error():
    class Response:
        is_success = False

    class Client:
        async def delete(self, url, **kwargs):
            return Response()

    backend = RemoteBackend("https://server.example")
    backend._client = Client()
    backend._claims[PLAYER] = ControlClaim("controller:1", 3, "claim-1", "secret-1")

    assert await backend.cancel_command(PLAYER, "cmd-1", "controller:1", 3) is False


def test_persistent_client_id_reuses_config_file(tmp_path):
    path = tmp_path / "bunnyland" / "client-id"

    first = persistent_client_id(path)
    second = persistent_client_id(path)

    assert first == second
    assert path.read_text(encoding="utf-8").strip() == first
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_persistent_client_id_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("00000000-0000-0000-0000-000000000000\n")
    path = tmp_path / "client-id"
    path.symlink_to(target)

    with pytest.raises(PermissionError, match="non-symlink"):
        persistent_client_id(path)


def test_persistent_client_id_propagates_secure_write_rejection(monkeypatch, tmp_path):
    def reject_write(_path, _contents):
        raise PermissionError("unsafe destination")

    monkeypatch.setattr(tui_backend, "secure_write_text", reject_write)
    with pytest.raises(PermissionError, match="unsafe destination"):
        persistent_client_id(tmp_path / "client-id")


def test_claim_files_are_atomic_private_and_reject_symlinks(monkeypatch, tmp_path):
    import bunnyland.tui.backend as backend_module

    monkeypatch.setattr(backend_module, "CONFIG_DIR", tmp_path / "bunnyland")
    control = ControlClaim("controller:1", 4, "claim:1", "secret:1")
    backend_module.save_claim_control("client:1", "character:1", control)
    path = backend_module._claim_path("client:1", "character:1")

    assert backend_module.load_claim_control("client:1", "character:1") == control
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))

    path.unlink()
    target = tmp_path / "unsafe-target"
    target.write_text("{}\n")
    path.symlink_to(target)
    with pytest.raises(PermissionError, match="non-symlink"):
        backend_module.load_claim_control("client:1", "character:1")
    with pytest.raises(PermissionError, match="non-symlink"):
        backend_module.save_claim_control("client:1", "character:1", control)


def test_terminal_backends_share_default_persistent_client_id(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    local = LocalBackend(generator="apartment-demo", autorun=False)
    remote = RemoteBackend("https://server.example")

    assert local.client_id == remote.client_id
    assert (tmp_path / "bunnyland" / "client-id").read_text(encoding="utf-8").strip() == (
        local.client_id
    )


def test_persistent_client_id_recovers_from_invalid_or_unreadable_config(tmp_path):
    invalid = tmp_path / "invalid-client-id"
    invalid.write_text("not-a-uuid\n", encoding="utf-8")
    recovered = persistent_client_id(invalid)
    assert recovered != "not-a-uuid"

    class UnreadablePath:
        parent = tmp_path

        def exists(self):
            return True

        def read_text(self, *, encoding):
            raise OSError("cannot read")

        def write_text(self, text, *, encoding):
            self.text = text

    assert persistent_client_id(UnreadablePath())


def test_persistent_client_id_tolerates_unwritable_config(tmp_path):
    class UnwritablePath:
        parent = tmp_path

        def exists(self):
            return False

        def write_text(self, text, *, encoding):
            raise OSError("cannot write")

    assert persistent_client_id(UnwritablePath())


def test_claim_control_persistence_round_trip_and_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(tui_backend, "CONFIG_DIR", tmp_path / "config")
    control = ControlClaim(
        controller_id="controller:1",
        generation=7,
        claim_id="claim-1",
        claim_secret="secret-1",
        active=False,
    )

    assert load_claim_control("client", PLAYER) is None
    save_claim_control("client", PLAYER, ControlClaim("controller:2", 1))
    assert load_claim_control("client", PLAYER) is None

    save_claim_control("client/one", PLAYER, control)
    assert load_claim_control("client/one", PLAYER) == control

    path = tui_backend._claim_path("client/one", PLAYER)
    path.write_text("{not json", encoding="utf-8")
    assert load_claim_control("client/one", PLAYER) is None
    path.write_text('{"claim_id": "claim-only"}', encoding="utf-8")
    assert load_claim_control("client/one", PLAYER) is None

    save_claim_control("client/one", PLAYER, control)
    clear_claim_control("client/one", PLAYER)
    assert load_claim_control("client/one", PLAYER) is None
    clear_claim_control("client/one", PLAYER)


def test_claim_control_persistence_logs_write_and_unlink_errors(monkeypatch, tmp_path):
    class BadPath:
        parent = tmp_path

        def write_text(self, text, *, encoding):
            raise OSError("cannot write")

        def unlink(self):
            raise OSError("cannot unlink")

    monkeypatch.setattr(tui_backend, "_claim_path", lambda *_args: BadPath())
    control = ControlClaim("controller:1", 2, "claim-1", "secret-1")

    save_claim_control("client", PLAYER, control)
    clear_claim_control("client", PLAYER)


def test_control_claim_tuple_compatibility_and_mismatch():
    control = ControlClaim("controller:1", 2, "claim-1", "secret-1", active=False)

    assert list(control) == ["controller:1", 2]
    assert control[0] == "controller:1"
    assert control[1] == 2
    assert control == ("controller:1", 2)
    assert control != ("controller:1", 3)
    assert control != "controller:1"
    assert tui_app._control_claim(("controller:1",)) is None


async def test_remote_backend_claims_web_controller(monkeypatch, tmp_path):
    monkeypatch.setattr(tui_backend, "CONFIG_DIR", tmp_path / "config")

    class Response:
        is_success = True
        status_code = 200
        text = ""
        headers = {"X-Bunnyland-Claim-Secret": "secret-1"}

        def json(self):
            return {
                "id": "claim-1",
                "controller_id": "controller:web",
                "controller_generation": 4,
                "control": "active",
            }

    class Client:
        def __init__(self) -> None:
            self.requests: list[tuple[str, dict]] = []

        async def post(self, url: str, **kwargs):
            self.requests.append((url, kwargs))
            return Response()

    backend = RemoteBackend(
        "https://server.example",
        client_id="remote-client",
        fallback_controller="llm",
        timeout_seconds=1200,
    )
    backend._client = Client()

    control = await backend.claim(PLAYER, World.parse(_snapshot()))

    assert control == ("controller:web", 4)
    assert backend._client.requests == [
        (
            "https://server.example/play/claims",
            {
                "json": {
                    "character_id": PLAYER,
                    "label": "tui",
                    "fallback_controller": "llm",
                    "timeout_seconds": 1200,
                },
            },
        )
    ]


async def test_remote_backend_failed_claim_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(tui_backend, "CONFIG_DIR", tmp_path / "config")

    class Response:
        is_success = False
        status_code = 503
        text = "nope"

    class Client:
        async def post(self, url: str, **_kwargs):
            return Response()

    backend = RemoteBackend("https://server.example", client_id="remote-client")
    backend._client = Client()

    assert await backend.claim(PLAYER, World.parse(_snapshot())) is None


async def test_remote_backend_reclaim_uses_stored_claim_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(tui_backend, "CONFIG_DIR", tmp_path / "config")
    stored = ControlClaim("controller:old", 2, "claim-1", "secret-1")
    save_claim_control("remote-client", PLAYER, stored)

    class Response:
        is_success = True
        status_code = 200
        text = ""
        headers = {"X-Bunnyland-Claim-Secret": "secret-1"}

        def json(self):
            return {
                "controller_id": "controller:new",
                "controller_generation": 3,
                "id": "claim-1",
                "control": "active",
            }

    class Client:
        def __init__(self) -> None:
            self.requests: list[tuple[str, dict]] = []

        async def put(self, url: str, **kwargs):
            self.requests.append((url, kwargs))
            return Response()

    backend = RemoteBackend("https://server.example", client_id="remote-client")
    backend._client = Client()

    control = await backend.claim(PLAYER, World.parse(_snapshot()))

    assert control == ControlClaim("controller:new", 3, "claim-1", "secret-1")
    assert backend._client.requests == [
        (
            "https://server.example/play/claims/claim-1",
            {
                "headers": {"X-Bunnyland-Claim-Secret": "secret-1"},
            },
        )
    ]


async def test_remote_backend_reclaim_replaces_expired_stored_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(tui_backend, "CONFIG_DIR", tmp_path / "config")
    save_claim_control(
        "remote-client",
        PLAYER,
        ControlClaim("controller:old", 2, "claim-expired", "secret-expired"),
    )

    class Response:
        text = ""

        def __init__(self, status_code, payload=None, headers=None):
            self.status_code = status_code
            self.is_success = status_code < 400
            self.payload = payload or {}
            self.headers = headers or {}

        def json(self):
            return self.payload

    class Client:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, dict]] = []

        async def put(self, url: str, **kwargs):
            self.requests.append(("PUT", url, kwargs))
            return Response(404)

        async def post(self, url: str, **kwargs):
            self.requests.append(("POST", url, kwargs))
            return Response(
                200,
                {
                    "controller_id": "controller:new",
                    "controller_generation": 1,
                    "id": "claim-new",
                    "control": "active",
                },
                {"X-Bunnyland-Claim-Secret": "secret-new"},
            )

    backend = RemoteBackend("https://server.example", client_id="remote-client")
    backend._client = Client()

    control = await backend.claim(PLAYER, World.parse(_snapshot()))

    assert control == ControlClaim("controller:new", 1, "claim-new", "secret-new")
    assert load_claim_control("remote-client", PLAYER) == control
    assert backend._client.requests == [
        (
            "PUT",
            "https://server.example/play/claims/claim-expired",
            {"headers": {"X-Bunnyland-Claim-Secret": "secret-expired"}},
        ),
        (
            "POST",
            "https://server.example/play/claims",
            {
                "json": {
                    "character_id": PLAYER,
                    "label": "tui",
                    "fallback_controller": None,
                    "timeout_seconds": None,
                }
            },
        ),
    ]


async def test_remote_backend_projection_expiry_clears_saved_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(tui_backend, "CONFIG_DIR", tmp_path / "config")
    control = ControlClaim("controller:old", 2, "claim-expired", "secret-expired")
    save_claim_control("remote-client", PLAYER, control)

    class Response:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("expired claims should be handled before raise_for_status")

    class Client:
        async def get(self, _url: str, **_kwargs):
            return Response()

    backend = RemoteBackend("https://server.example", client_id="remote-client")
    backend._client = Client()
    backend._claims[PLAYER] = control

    assert await backend.fetch_character_projection(PLAYER) is None
    assert PLAYER not in backend._claims
    assert load_claim_control("remote-client", PLAYER) is None


async def test_remote_backend_uses_claim_headers(monkeypatch, tmp_path):
    monkeypatch.setattr(tui_backend, "CONFIG_DIR", tmp_path / "config")

    class Response:
        is_success = True
        status_code = 200

        def __init__(self, payload=None):
            self.payload = payload or {"ok": True}

        def raise_for_status(self) -> None: ...

        def json(self):
            return self.payload

    class Client:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, dict]] = []

        async def get(self, url: str, **kwargs):
            self.requests.append(("GET", url, kwargs))
            return Response({"world_epoch": 7, "character": {}, "commands": [], "scene": {}})

        async def post(self, url: str, **kwargs):
            self.requests.append(("POST", url, kwargs))
            if url.endswith("/commands"):
                return Response({"status": "queued", "reason": ""})
            if url.endswith("/jobs"):
                return Response({"status": "queued", "result": {"url": "http://image"}})
            return Response()

        async def delete(self, url: str, **kwargs):
            self.requests.append(("DELETE", url, kwargs))
            return Response({"status": "cancelled"})

    backend = RemoteBackend("https://server.example", client_id="remote-client")
    backend._client = Client()
    backend._claims[PLAYER] = ControlClaim(
        "controller:1",
        3,
        claim_id="claim-1",
        claim_secret="secret-1",
    )

    await backend.fetch_character_projection(PLAYER)
    await backend.fetch_queued_commands(PLAYER)
    await backend.cancel_command(PLAYER, "cmd-1", "controller:1", 3)
    submitted = await backend.submit(
        {"character_id": PLAYER, "command_id": "cmd-2", "command_type": "wait"}
    )
    image = await backend.request_image(PLAYER)
    video = await backend.request_video(PLAYER)

    assert submitted.accepted is True
    assert image.ok is True
    assert video.ok is True
    assert backend._client.requests == [
        (
            "GET",
            "https://server.example/play/claims/claim-1/projection",
            {"headers": {"X-Bunnyland-Claim-Secret": "secret-1"}},
        ),
        (
            "GET",
            "https://server.example/play/claims/claim-1/projection",
            {"headers": {"X-Bunnyland-Claim-Secret": "secret-1"}},
        ),
        (
            "DELETE",
            "https://server.example/play/claims/claim-1/commands/cmd-1",
            {"headers": {"X-Bunnyland-Claim-Secret": "secret-1"}},
        ),
        (
            "POST",
            "https://server.example/play/claims/claim-1/commands",
            {
                "headers": {"X-Bunnyland-Claim-Secret": "secret-1"},
                "json": {"id": "cmd-2", "command_type": "wait"},
            },
        ),
        (
            "POST",
            "https://server.example/play/claims/claim-1/jobs",
            {
                "headers": {"X-Bunnyland-Claim-Secret": "secret-1"},
                "json": {"kind": "scene_image"},
            },
        ),
        (
            "POST",
            "https://server.example/play/claims/claim-1/jobs",
            {
                "headers": {"X-Bunnyland-Claim-Secret": "secret-1"},
                "json": {"kind": "scene_video"},
            },
        ),
    ]


async def test_remote_backend_request_image_reports_unavailable_and_error():
    class Response:
        def __init__(self, *, status_code, is_success=False):
            self.status_code = status_code
            self.is_success = is_success

    class Client:
        def __init__(self, responses):
            self.responses = list(responses)

        async def post(self, url: str, **kwargs):
            return self.responses.pop(0)

    backend = RemoteBackend("https://server.example", client_id="remote-client")
    backend._client = Client(
        [
            Response(status_code=409),
            Response(status_code=500),
            Response(status_code=409),
            Response(status_code=500),
        ]
    )
    backend._claims[PLAYER] = ControlClaim("controller:1", 1, "claim-1", "secret-1")

    unavailable = await backend.request_image(PLAYER)
    errored = await backend.request_image(PLAYER)
    video_unavailable = await backend.request_video(PLAYER)
    video_errored = await backend.request_video(PLAYER)

    assert unavailable.ok is False
    assert unavailable.status == "unavailable"
    assert errored.ok is False
    assert errored.status == "error"
    assert video_unavailable.status == "unavailable"
    assert video_errored.status == "error"


async def test_remote_backend_claim_scoped_operations_require_a_claim():
    backend = RemoteBackend("https://server.example", client_id="remote-client")
    states: list[str] = []

    assert backend._claim_request_kwargs(PLAYER) == {}
    assert await backend.fetch_character_projection(PLAYER) is None
    assert await backend.fetch_room_projection("room:1", PLAYER) is None
    assert await backend.fetch_queued_commands(PLAYER) == {
        "character_id": PLAYER,
        "commands": [],
    }
    assert await backend.cancel_command(PLAYER, "cmd-1", "controller:1", 1) is False
    assert (await backend.submit({"character_id": PLAYER, "command_type": "wait"})) == (
        SubmitResult(accepted=False, reason="a character claim is required")
    )
    assert await backend.recent_events(PLAYER) == []
    assert (await backend.request_image(PLAYER)) == ImageRequestResult(
        ok=False,
        status="error",
        reason="a claim is required",
    )
    assert (await backend.request_video(PLAYER)) == ImageRequestResult(
        ok=False,
        status="error",
        reason="a claim is required",
    )
    await backend.watch_updates(PLAYER, None, lambda _frame: None, states.append)
    assert states == ["fallback"]


async def test_remote_backend_release_controller_and_claim_requests():
    class Response:
        status_code = 200
        text = ""

        def __init__(self, *, is_success=True, payload=None):
            self.is_success = is_success
            self.payload = payload or {}

        def json(self):
            return self.payload

    class Client:
        def __init__(self, responses):
            self.responses = list(responses)
            self.requests: list[tuple[str, dict]] = []

        async def patch(self, url: str, **kwargs):
            self.requests.append(("PATCH", url, kwargs))
            return self.responses.pop(0)

        async def delete(self, url: str, **kwargs):
            self.requests.append(("DELETE", url, kwargs))
            return self.responses.pop(0)

        async def post(self, url: str, **kwargs):
            self.requests.append((url, kwargs))
            return self.responses.pop(0)

    control = ControlClaim("controller:1", 3, "claim-1", "secret-1")
    backend = RemoteBackend(
        "https://server.example",
        client_id="remote-client",
        fallback_controller="llm",
        timeout_seconds=900,
    )
    backend._client = Client(
        [
            Response(
                payload={
                    "controller_id": "controller:2",
                    "controller_generation": 4,
                    "id": "claim-1",
                }
            ),
            Response(payload={"ok": True}),
        ]
    )

    released = await backend.release_controller(PLAYER, control)
    assert released == ControlClaim("controller:2", 4, "claim-1", "secret-1", active=False)
    assert await backend.release_claim(PLAYER, released) is True
    assert backend._client.requests == [
        (
            "PATCH",
            "https://server.example/play/claims/claim-1",
            {
                "headers": {"X-Bunnyland-Claim-Secret": "secret-1"},
                "json": {"kind": "control", "desired": "fallback"},
            },
        ),
        (
            "DELETE",
            "https://server.example/play/claims/claim-1",
            {"headers": {"X-Bunnyland-Claim-Secret": "secret-1"}},
        ),
    ]

    failed = RemoteBackend("https://server.example", client_id="remote-client")
    failed._client = Client([Response(is_success=False), Response(is_success=False)])
    assert await failed.release_controller(PLAYER, control) is None
    assert await failed.release_claim(PLAYER, control) is False


async def test_remote_backend_recent_events_reads_endpoint():
    class Response:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"events": [{"type": "event", "data": {"event_type": "PingEvent"}}]}

    class Client:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get(self, url: str, **_kwargs):
            self.urls.append(url)
            return Response()

    backend = RemoteBackend("https://server.example")
    backend._client = Client()
    backend._claims["character:1"] = ControlClaim("controller:1", 1, "claim-1", "secret-1")

    events = await backend.recent_events("character:1")

    assert events == [{"type": "event", "data": {"event_type": "PingEvent"}}]
    assert backend._client.urls == ["https://server.example/play/claims/claim-1/events"]


async def test_remote_backend_watches_authenticated_player_updates(monkeypatch):
    sockets = []
    states = []
    frames = []

    class Socket:
        def __init__(self, url):
            self.url = url
            self.sent = []
            self.messages = [
                "not json",
                json.dumps({"type": "mystery", "data": {}}),
                json.dumps(
                    {
                        "type": "ready",
                        "data": {"character_id": "character:1", "world_epoch": 1},
                    }
                ),
                json.dumps(
                    {
                        "type": "event",
                        "data": {"event_type": "MovedEvent", "event": {}},
                    }
                ),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

        async def send(self, value):
            self.sent.append(value)

        async def recv(self):
            return self.messages.pop(0)

    def connect(url, **_kwargs):
        socket = Socket(url)
        sockets.append(socket)
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    backend = RemoteBackend("https://player:password@server.example/api")
    control = ControlClaim(
        controller_id="controller:1",
        generation=2,
        claim_id="claim-1",
        claim_secret="top-secret",
    )

    async def on_message(frame):
        frames.append(frame)
        if frame["type"] == "event":
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await backend.watch_updates("character:1", control, on_message, states.append)

    assert backend.supports_live_updates() is True
    assert sockets[0].url == ("wss://player:password@server.example/api/play/claims/claim-1/stream")
    assert "top-secret" not in sockets[0].url
    assert json.loads(sockets[0].sent[0]) == {
        "type": "authenticate",
        "data": {
            "token": None,
            "client_id": backend.client_id,
            "claim_secret": "top-secret",
        },
    }
    assert states == ["connecting", "live"]
    assert [frame["type"] for frame in frames] == ["ready", "event"]


async def test_live_updates_are_optional_for_local_and_missing_dependency(monkeypatch):
    local = RecordingBackend(_snapshot())
    assert local.supports_live_updates() is False
    assert (
        await local.watch_updates("character:1", None, lambda _frame: None, lambda _state: None)
        is None
    )
    monkeypatch.setitem(sys.modules, "websockets", None)
    backend = RemoteBackend("https://server.example")
    states = []

    assert backend.supports_live_updates() is False
    await backend.watch_updates("character:1", None, lambda _frame: None, states.append)
    assert states == ["fallback"]
    assert await RemoteBackend("https://server.example").recent_events() == []


async def test_remote_backend_login_persistence_refresh_rotation_and_close(tmp_path, monkeypatch):
    class Response:
        def __init__(self, body, status_code=200):
            self._body = body
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

        def json(self):
            return self._body

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.headers = {}
            self.closed = False
            self.rotate_conflict = False

        async def post(self, url, **_kwargs):
            if url.endswith("/auth/session"):
                return Response(
                    {
                        "token": "login-token",
                        "subject": "player",
                        "scopes": ["world:play", "world:admin"],
                        "expires_at": 999,
                        "rotate_after": 123,
                        "rotation_eligible": True,
                    }
                )
            raise AssertionError(url)

        async def patch(self, url, **_kwargs):
            assert url.endswith("/auth/session")
            if self.rotate_conflict:
                return Response({}, 409)
            return Response(
                {
                    "token": "rotated-token",
                    "subject": "player",
                    "scopes": ["world:play", "world:admin"],
                    "expires_at": 999,
                    "rotate_after": 456,
                    "rotation_eligible": True,
                }
            )

        async def get(self, _url):
            return Response(
                {
                    "subject": "player",
                    "scopes": ["world:play", "world:admin"],
                    "expires_at": 999,
                    "rotate_after": 321,
                    "rotation_eligible": True,
                }
            )

        async def aclose(self):
            self.closed = True

    client = Client()
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=lambda **_kw: client))
    token_file = tmp_path / "credentials" / "token"
    backend = RemoteBackend(
        "https://server/api",
        username="player",
        password="password",
        token_file=token_file,
    )
    await backend.start()
    assert backend._access_token == "login-token"
    assert backend._password == ""
    assert token_file.read_text().strip() == "login-token"
    assert token_file.stat().st_mode & 0o777 == 0o600
    await backend._refresh_auth_metadata()
    assert backend._rotate_after == 321
    client.rotate_conflict = True
    await backend._rotate_token()
    assert backend._rotate_after == 321
    client.rotate_conflict = False
    await backend._rotate_token()
    assert backend._access_token == "rotated-token"
    await backend.close()
    assert client.closed is True

    existing = RemoteBackend("https://server/api", token_file=token_file)
    second_client = Client()
    monkeypatch.setitem(
        sys.modules, "httpx", SimpleNamespace(AsyncClient=lambda **_kw: second_client)
    )
    await existing.start()
    assert second_client.headers["Authorization"] == "Bearer rotated-token"
    await existing.close()


async def test_remote_backend_starts_with_in_memory_access_token(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self): ...

        def json(self):
            return {
                "subject": "player",
                "scopes": ["world:play"],
                "expires_at": 999,
                "rotate_after": 321,
                "rotation_eligible": True,
            }

    class Client:
        def __init__(self):
            self.headers = {}

        async def get(self, url):
            if url.endswith("/public/features"):
                return SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {},
                )
            assert url.endswith("/auth/session")
            return Response()

        async def aclose(self): ...

    client = Client()
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=lambda **_kw: client))
    backend = RemoteBackend("https://server/api", access_token="memory-token")

    await backend.start()

    assert backend._access_token == "memory-token"
    assert client.headers["Authorization"] == "Bearer memory-token"
    await backend.close()


async def test_remote_backend_runtime_sign_in_and_auth_required_detection(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self): ...

        def json(self):
            return {
                "token": "runtime-token",
                "subject": "player",
                "scopes": ["world:play"],
                "expires_at": 999,
                "rotate_after": None,
                "rotation_eligible": True,
            }

    class Client:
        def __init__(self, **_kwargs):
            self.headers = {}
            self.login = None
            self.post_error = None

        async def post(self, _url, *, json):
            self.login = json
            if self.post_error is not None:
                raise self.post_error
            return Response()

        async def get(self, url):
            assert url.endswith("/public/features")

            class FeatureResponse:
                def raise_for_status(self): ...

                def json(self):
                    return {
                        "character_sheets": True,
                        "character_chat": True,
                        "image_generation": False,
                        "video_generation": True,
                    }

            return FeatureResponse()

        async def aclose(self): ...

    client = Client()
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=lambda **_kw: client))
    backend = RemoteBackend("https://server/api")
    with pytest.raises(RuntimeError, match="must be started"):
        await backend.sign_in("player", "password")
    await backend.start()
    assert backend.supports_image_requests is False
    assert backend.supports_video_requests is True
    await backend.sign_in(" player ", "password")
    assert client.login == {
        "username": "player",
        "password": "password",
        "delivery": "body",
    }
    assert client.headers["Authorization"] == "Bearer runtime-token"
    assert backend._password == ""

    client.post_error = RuntimeError("login unavailable")
    with pytest.raises(RuntimeError, match="login unavailable"):
        await backend.sign_in("player", "another password")
    assert backend._password == ""
    client.post_error = None

    completed_task = asyncio.create_task(asyncio.sleep(0))
    await completed_task
    active_rotation = backend._rotation_task
    active_rotation.cancel()
    await asyncio.gather(active_rotation, return_exceptions=True)
    backend._rotation_task = completed_task
    backend._start_rotation_loop()
    assert backend._rotation_task is not completed_task
    await backend.close()

    auth_error = RuntimeError("unauthorized")
    auth_error.response = SimpleNamespace(status_code=401)
    other_error = RuntimeError("forbidden")
    other_error.response = SimpleNamespace(status_code=403)
    assert is_authentication_required(auth_error) is True
    assert is_authentication_required(other_error) is False
    assert is_authentication_required(RuntimeError("offline")) is False


def test_remote_backend_rejects_insecure_servers_and_token_files(tmp_path) -> None:
    from bunnyland.tui.backend import RemoteBackend

    with pytest.raises(ValueError, match="absolute HTTP"):
        RemoteBackend("server.example")
    with pytest.raises(ValueError, match="require HTTPS"):
        RemoteBackend("http://server.example")
    assert RemoteBackend("http://localhost:8765").base == "http://localhost:8765"

    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    token_file.chmod(0o640)
    RemoteBackend("https://server.example", token_file=token_file)
    assert token_file.stat().st_mode & 0o777 == 0o600

    symlink = tmp_path / "token-link"
    symlink.symlink_to(token_file)
    with pytest.raises(PermissionError, match="non-symlink"):
        RemoteBackend("https://server.example", token_file=symlink)


async def test_remote_backend_rotation_loop_recovers_from_failure(monkeypatch, caplog):
    backend = RemoteBackend("https://server/api")
    backend._rotate_after = 0

    async def fail_rotation():
        raise RuntimeError("offline")

    sleeps = 0

    async def controlled_sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(backend, "_rotate_token", fail_rotation)
    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
    with pytest.raises(asyncio.CancelledError):
        await backend._rotation_loop()
    assert "Could not rotate Bunnyland access token" in caplog.text


async def test_remote_backend_in_memory_token_and_idle_rotation_loop(monkeypatch):
    backend = RemoteBackend("https://server/api")
    backend._set_access_token("memory-only")
    backend._persist_access_token()
    assert backend._access_token == "memory-only"

    sleeps = 0

    async def controlled_sleep(delay):
        nonlocal sleeps
        assert delay == 60.0
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
    with pytest.raises(asyncio.CancelledError):
        await backend._rotation_loop()


async def test_remote_backend_live_updates_reconnect_after_transport_failure(monkeypatch):
    calls = []
    states = []

    class FailingSocket:
        async def __aenter__(self):
            raise OSError("offline")

        async def __aexit__(self, *_exc_info):
            return None

    def connect(url, **_kwargs):
        calls.append(url)
        return FailingSocket()

    async def stop_after_delay(delay):
        calls.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    monkeypatch.setattr(asyncio, "sleep", stop_after_delay)
    monkeypatch.setattr("bunnyland.tui.backend.random.uniform", lambda _low, _high: 1.0)
    backend = RemoteBackend("https://server.example")
    control = ControlClaim("controller:1", 1, "claim-1", "secret-1")

    with pytest.raises(asyncio.CancelledError):
        await backend.watch_updates("character:1", control, lambda _frame: None, states.append)

    assert calls == [
        "wss://server.example/play/claims/claim-1/stream",
        1.0,
    ]
    assert states == ["connecting", "fallback"]


async def test_backend_recent_events_defaults_to_empty():
    assert await RecordingBackend(_snapshot()).recent_events() == []


async def test_local_backend_records_recent_events():
    backend = LocalBackend(generator="apartment-demo", autorun=False, client_id="local-client")
    await backend.start()
    try:
        events = await backend.recent_events()
        assert isinstance(events, list)  # world generation may have recorded events
    finally:
        await backend.close()


async def test_local_backend_rejects_unknown_generator():
    backend = LocalBackend(generator="missing-generator", autorun=False)

    with pytest.raises(SystemExit, match="unknown generator 'missing-generator'"):
        await backend.start()


async def test_remote_backend_http_methods_use_async_client(monkeypatch):
    class Response:
        def __init__(self, *, is_success=True, payload=None):
            self.is_success = is_success
            self.payload = payload or {}
            self.raised = False

        def raise_for_status(self):
            self.raised = True

        def json(self):
            return self.payload

    class Client:
        def __init__(self, *, timeout):
            self.timeout = timeout
            self.headers = {}
            self.closed = False
            self.requests: list[tuple[str, str, dict | None]] = []

        async def get(self, url: str, **kwargs):
            self.requests.append(("GET", url, kwargs or None))
            if url.endswith("/play/characters"):
                return Response(payload={"world_epoch": 7, "characters": []})
            if url.endswith("/projection"):
                return Response(
                    payload={"world_epoch": 7, "character": {}, "scene": {}, "commands": []}
                )
            return Response(payload={"world_epoch": 7})

        async def post(self, url: str, **kwargs):
            self.requests.append(("POST", url, kwargs))
            return Response(is_success=False)

        async def delete(self, url: str, **kwargs):
            self.requests.append(("DELETE", url, kwargs))
            return Response(payload={"status": "cancelled"})

        async def aclose(self):
            self.closed = True

    clients = []

    def async_client(*, timeout):
        client = Client(timeout=timeout)
        clients.append(client)
        return client

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=async_client))
    backend = RemoteBackend("https://server.example/")

    await backend.start()
    backend._claims[PLAYER] = ControlClaim("controller:1", 3, "claim-1", "secret-1")
    snapshot = await backend.fetch_snapshot()
    character_list = await backend.fetch_character_list()
    character = await backend.fetch_character_projection(PLAYER)
    room = await backend.fetch_room_projection(PARLOR, PLAYER)
    queued = await backend.fetch_queued_commands(PLAYER)
    submitted = await backend.submit({"character_id": PLAYER, "command_type": "wait"})
    cancelled = await backend.cancel_command(PLAYER, "cmd-1", "controller:1", 3)
    await backend.close()

    assert snapshot == {"world_epoch": 7}
    assert character_list == []  # validated CharacterListResponse with no characters
    assert character == {
        "world_epoch": 7,
        "sheet": {},
        "actions": [],
        "queued_commands": [],
    }
    assert room == {}
    assert queued == {"character_id": PLAYER, "world_epoch": 7, "commands": []}
    assert submitted.accepted is False
    assert cancelled is True
    assert clients[0].closed is True
    assert clients[0].requests == [
        ("GET", "https://server.example/public/features", None),
        ("GET", "https://server.example/admin/world/snapshot", None),
        ("GET", "https://server.example/play/characters", None),
        (
            "GET",
            "https://server.example/play/claims/claim-1/projection",
            {"headers": {"X-Bunnyland-Claim-Secret": "secret-1"}},
        ),
        (
            "GET",
            "https://server.example/play/claims/claim-1/projection",
            {"headers": {"X-Bunnyland-Claim-Secret": "secret-1"}},
        ),
        (
            "GET",
            "https://server.example/play/claims/claim-1/projection",
            {"headers": {"X-Bunnyland-Claim-Secret": "secret-1"}},
        ),
        (
            "POST",
            "https://server.example/play/claims/claim-1/commands",
            {
                "headers": {"X-Bunnyland-Claim-Secret": "secret-1"},
                "json": {"command_type": "wait"},
            },
        ),
        (
            "DELETE",
            "https://server.example/play/claims/claim-1/commands/cmd-1",
            {"headers": {"X-Bunnyland-Claim-Secret": "secret-1"}},
        ),
    ]


async def test_remote_backend_close_without_client_is_noop():
    backend = RemoteBackend("https://server.example")

    await backend.close()

    assert backend._client is None


async def test_remote_backend_coalesces_claim_resource_reads():
    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self): ...

        def json(self):
            return self.payload

    class Client:
        def __init__(self):
            self.calls = {"projection": 0, "events": 0}
            self.started = {"projection": asyncio.Event(), "events": asyncio.Event()}
            self.release = {"projection": asyncio.Event(), "events": asyncio.Event()}

        async def get(self, url: str, **_kwargs):
            resource = url.rsplit("/", 1)[-1]
            self.calls[resource] += 1
            self.started[resource].set()
            await self.release[resource].wait()
            if resource == "events":
                return Response({"events": [{"event_type": "MovedEvent"}]})
            return Response(
                {
                    "world_epoch": 7,
                    "character": {"character_id": PLAYER},
                    "scene": {"room": {"id": PARLOR}},
                    "commands": [{"command_id": "cmd-1"}],
                }
            )

    backend = RemoteBackend("https://server.example", client_id="remote-client")
    backend._client = Client()
    backend._claims[PLAYER] = ControlClaim("controller:1", 3, "claim-1", "secret-1")

    projection_reads = [
        asyncio.create_task(backend.fetch_character_projection(PLAYER)),
        asyncio.create_task(backend.fetch_room_projection(PARLOR, PLAYER)),
        asyncio.create_task(backend.fetch_queued_commands(PLAYER)),
    ]
    await backend._client.started["projection"].wait()
    await asyncio.sleep(0)
    assert backend._client.calls["projection"] == 1
    backend._client.release["projection"].set()
    character, room, queue = await asyncio.gather(*projection_reads)
    assert character["character_id"] == PLAYER
    assert room == {"room": {"id": PARLOR}}
    assert queue["commands"] == [{"command_id": "cmd-1"}]

    event_reads = [
        asyncio.create_task(backend.recent_events(PLAYER)),
        asyncio.create_task(backend.recent_events(PLAYER)),
    ]
    await backend._client.started["events"].wait()
    await asyncio.sleep(0)
    assert backend._client.calls["events"] == 1
    backend._client.release["events"].set()
    assert await asyncio.gather(*event_reads) == [
        [{"event_type": "MovedEvent"}],
        [{"event_type": "MovedEvent"}],
    ]


async def test_remote_backend_does_not_coalesce_across_claim_identity():
    class Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self.payload = payload or {}

        def raise_for_status(self): ...

        def json(self):
            return self.payload

    class Client:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []
            self.started = {"claim-old": asyncio.Event(), "claim-new": asyncio.Event()}
            self.release = {"claim-old": asyncio.Event(), "claim-new": asyncio.Event()}

        async def get(self, url: str, **kwargs):
            claim_id = url.split("/claims/", 1)[1].split("/", 1)[0]
            self.requests.append((url, kwargs))
            self.started[claim_id].set()
            await self.release[claim_id].wait()
            if claim_id == "claim-old":
                return Response(404)
            return Response(200, {"character": {"character_id": PLAYER}})

    backend = RemoteBackend("https://server.example", client_id="remote-client")
    backend._client = Client()
    old_claim = ControlClaim("controller:old", 1, "claim-old", "secret-old")
    new_claim = ControlClaim("controller:new", 1, "claim-new", "secret-new")
    backend._claims[PLAYER] = old_claim

    old_read = asyncio.create_task(backend.fetch_character_projection(PLAYER))
    await backend._client.started["claim-old"].wait()
    backend._claims[PLAYER] = new_claim
    new_read = asyncio.create_task(backend.fetch_character_projection(PLAYER))
    await backend._client.started["claim-new"].wait()

    assert len(backend._client.requests) == 2
    assert backend._client.requests[0][1] == {"headers": {"X-Bunnyland-Claim-Secret": "secret-old"}}
    assert backend._client.requests[1][1] == {"headers": {"X-Bunnyland-Claim-Secret": "secret-new"}}

    backend._client.release["claim-old"].set()
    assert await old_read is None
    assert backend._claims[PLAYER] == new_claim
    backend._client.release["claim-new"].set()
    assert (await new_read)["character_id"] == PLAYER


async def test_remote_backend_close_cancels_pending_claim_reads():
    class Client:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = False
            self.closed_after_cancel = False

        async def get(self, _url: str, **_kwargs):
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def aclose(self):
            self.closed_after_cancel = self.cancelled

    backend = RemoteBackend("https://server.example", client_id="remote-client")
    backend._client = Client()
    backend._claims[PLAYER] = ControlClaim("controller:1", 1, "claim-1", "secret-1")
    read = asyncio.create_task(backend.fetch_character_projection(PLAYER))
    await backend._client.started.wait()

    await backend.close()
    await asyncio.gather(read, return_exceptions=True)

    assert backend._client.cancelled is True
    assert backend._client.closed_after_cancel is True
    assert backend._claim_get_tasks == {}


async def test_remote_backend_claim_read_omits_empty_secret_header():
    class Response:
        status_code = 200

        def raise_for_status(self): ...

        def json(self):
            return {"character": {"character_id": PLAYER}}

    class Client:
        def __init__(self):
            self.kwargs = None

        async def get(self, _url: str, **kwargs):
            self.kwargs = kwargs
            return Response()

    backend = RemoteBackend("https://server.example", client_id="remote-client")
    backend._client = Client()
    backend._claims[PLAYER] = ControlClaim("controller:1", 1, "claim-1", "")

    assert (await backend.fetch_character_projection(PLAYER))["character_id"] == PLAYER
    assert backend._client.kwargs == {}


async def test_remote_backend_submit_reports_rejection_reason():
    class Response:
        def __init__(self, *, is_success=True, payload=None, raises=False):
            self.is_success = is_success
            self.status_code = 200 if is_success else 422
            self._payload = payload or {}
            self._raises = raises

        def json(self):
            if self._raises:
                raise ValueError("no body")
            return self._payload

    class Client:
        def __init__(self, response):
            self.response = response

        async def post(self, url, **_kwargs):
            return self.response

    accepted = RemoteBackend("https://server.example")
    accepted._client = Client(Response(payload={"status": "queued", "reason": ""}))
    accepted._claims[PLAYER] = ControlClaim("controller:1", 1, "claim-1", "secret-1")
    result = await accepted.submit({"character_id": PLAYER, "command_type": "wait"})
    assert result.accepted is True and result.reason == ""

    rejected = RemoteBackend("https://server.example")
    rejected._client = Client(
        Response(payload={"status": "rejected", "reason": "missing required argument: text"})
    )
    rejected._claims[PLAYER] = ControlClaim("controller:1", 1, "claim-1", "secret-1")
    result = await rejected.submit({"character_id": PLAYER, "command_type": "say"})
    assert result.accepted is False
    assert result.reason == "missing required argument: text"

    # A non-2xx response with an unparseable body still yields a usable reason.
    errored = RemoteBackend("https://server.example")
    errored._client = Client(Response(is_success=False, raises=True))
    errored._claims[PLAYER] = ControlClaim("controller:1", 1, "claim-1", "secret-1")
    result = await errored.submit({"character_id": PLAYER, "command_type": "say"})
    assert result.accepted is False
    assert "422" in result.reason


async def test_backend_default_queued_commands_response():
    class MinimalBackend(Backend):
        label = "minimal"

        async def start(self) -> None: ...
        async def close(self) -> None: ...
        async def fetch_snapshot(self) -> dict:
            return {}

        async def submit(self, command: dict) -> SubmitResult:
            return SubmitResult(accepted=True)

        async def claim(self, player_id: str, world: World):
            return None

    queued = await MinimalBackend().fetch_queued_commands(PLAYER)
    assert queued == {
        "ok": True,
        "schema_version": 1,
        "world_epoch": 0,
        "character_id": PLAYER,
        "generated_at_unix": queued["generated_at_unix"],
        "next_tick_at_unix": None,
        "tick_seconds": None,
        "time_scale": None,
        "game_seconds_per_tick": None,
        "commands": [],
    }
    assert isinstance(queued["generated_at_unix"], float)


async def test_backend_base_defaults_are_empty():
    """A backend that only implements the abstract surface falls back to the empty
    defaults the base class provides for the optional projection/event/cancel hooks."""

    class MinimalBackend(Backend):
        async def start(self) -> None: ...
        async def close(self) -> None: ...
        async def fetch_snapshot(self) -> dict:
            return {}

        async def submit(self, command: dict) -> SubmitResult:
            return SubmitResult(accepted=True)

        async def claim(self, player_id: str, world: World):
            return None

    backend = MinimalBackend()
    assert await backend.fetch_character_list() == []
    assert await backend.fetch_character_projection(PLAYER) is None
    assert await backend.fetch_room_projection(PARLOR, PLAYER) is None
    assert await backend.cancel_command(PLAYER, "cmd-1", "controller:1", 0) is False
    assert await backend.recent_events() == []


# ── the app (Textual pilot) ───────────────────────────────────────────────────
def _character_list_from_snapshot(snapshot: dict) -> list:
    """The claim-lobby records the app's picker needs, derived from a snapshot fixture."""
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


class RecordingBackend(Backend):
    """A static projection backend that records submitted commands, for app tests.

    The app now reads the claim lobby (``fetch_character_list``) for the picker and the
    player's own room (``fetch_character_projection``) for the world, never the full
    snapshot, so this backend defaults the projection to the parlor client-view and derives
    the lobby from the snapshot fixture."""

    def __init__(
        self,
        snapshot: dict,
        queued_response: dict | None = None,
        events: list[dict] | None = None,
        character_projection: dict | None = None,
        character_list: list | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.queued_response = queued_response or _queued_response()
        self.events = events or []
        self.character_projection = (
            character_projection if character_projection is not None else _client_view()
        )
        self.character_list = (
            character_list
            if character_list is not None
            else _character_list_from_snapshot(snapshot)
        )
        self.queued_requests: list[str] = []
        self.commands: list[dict] = []
        self.cancelled_commands: list[tuple[str, str, str, int]] = []
        self.claims: list[str] = []
        self.label = "test"

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def fetch_snapshot(self) -> dict:
        return copy.deepcopy(self.snapshot)

    async def fetch_character_list(self) -> list:
        return list(self.character_list)

    async def fetch_queued_commands(self, character_id: str) -> dict:
        self.queued_requests.append(character_id)
        return copy.deepcopy(self.queued_response)

    async def fetch_character_projection(self, character_id: str) -> dict | None:
        if self.character_projection is None:
            return None
        return copy.deepcopy(self.character_projection)

    async def submit(self, command: dict) -> SubmitResult:
        self.commands.append(command)
        return SubmitResult(accepted=True)

    async def cancel_command(
        self,
        character_id: str,
        command_id: str,
        controller_id: str,
        controller_generation: int,
    ) -> bool:
        self.cancelled_commands.append(
            (character_id, command_id, controller_id, controller_generation)
        )
        return True

    async def recent_events(self, character_id: str = "") -> list[dict]:
        return copy.deepcopy(self.events)

    async def claim(self, player_id, world):
        self.claims.append(player_id)
        return World.parse(self.snapshot).control(player_id)


class FailingQueueBackend(RecordingBackend):
    async def fetch_queued_commands(self, character_id: str) -> dict:
        self.queued_requests.append(character_id)
        raise RuntimeError("queue failed")


class FlakyBackend(RecordingBackend):
    def __init__(self, snapshot: dict) -> None:
        super().__init__(snapshot)
        self.fail = True

    async def fetch_character_list(self) -> list:
        if self.fail:
            raise RuntimeError("lobby failed")
        return await super().fetch_character_list()


async def _select_player(app, pilot):
    from textual.widgets import Select

    select = await _wait_for_widget(app, pilot, "#player", Select)
    # Finish the mount-time refresh before changing the picker. Otherwise that refresh
    # can rebuild the choices and replace the queued selection with Select.BLANK.
    await app.refresh_world()
    select.value = PLAYER
    # Await the state transition directly instead of relying on Textual's message queue
    # becoming idle before the test continues. The queued duplicate is then a no-op.
    await app._player_changed(Select.Changed(select, PLAYER))


async def _wait_for_action_view(app, pilot, command_type: str) -> dict:
    del pilot
    await app.refresh_world()
    action = next(
        (item for item in app.action_views if item["command_type"] == command_type),
        None,
    )
    if action is None:
        raise AssertionError(f"action view did not appear: {command_type}")
    return action


async def _wait_for_widget(root, pilot, selector: str, expect_type=None):
    del pilot
    await asyncio.wait_for(root._mounted_event.wait(), timeout=2)
    return root.query_one(selector, expect_type)


async def _wait_for_tui_ready(app, pilot) -> None:
    await _wait_for_widget(app, pilot, "#player")
    await _wait_for_widget(app, pilot, "#character-release")
    await _wait_for_widget(app, pilot, "#activity")


async def _push_action_form(app, pilot, screen: ActionForm, *, callback=None) -> ActionForm:
    await _wait_for_tui_ready(app, pilot)
    await app.push_screen(screen, callback=callback)
    await _wait_for_widget(screen, pilot, "#form-submit")
    return screen


async def test_sign_in_screen_masks_password_validates_and_submits():
    from textual.widgets import Input, Label

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    results = []
    async with app.run_test() as pilot:
        await _wait_for_tui_ready(app, pilot)

        blank = SignInScreen()
        await app.push_screen(blank, callback=results.append)
        await pilot.pause()
        assert blank.query_one("#sign-in-username", Input).has_focus
        blank.query_one("#sign-in-submit").press()
        await pilot.pause()
        assert "Username is required" in str(blank.query_one("#sign-in-error", Label).render())
        blank.query_one("#sign-in-cancel").press()
        await pilot.pause()
        assert results == [None]

        screen = SignInScreen(username="player")
        await app.push_screen(screen, callback=results.append)
        await pilot.pause()

        username = screen.query_one("#sign-in-username", Input)
        password = screen.query_one("#sign-in-password", Input)
        assert username.value == "player"
        assert password.password is True
        assert password.has_focus

        screen.query_one("#sign-in-submit").press()
        await pilot.pause()
        assert "Password is required" in str(screen.query_one("#sign-in-error", Label).render())
        assert results == [None]

        password.value = "secret"
        await pilot.press("enter")
        await pilot.pause()
        assert results == [None, SignInCredentials(username="player", password="secret")]


async def test_tui_prompts_for_remote_auth_and_retries_after_sign_in(monkeypatch):
    backend = RemoteBackend("https://server.example/v1")
    app = BunnylandTUI(backend)
    pushed = []
    workers = []
    fetches = []
    continued = []

    class Unauthorized(RuntimeError):
        response = SimpleNamespace(status_code=401)

    async def start(): ...

    async def fetch_character_list():
        fetches.append(True)
        if len(fetches) == 1:
            raise Unauthorized("authentication required")

    async def continue_start():
        continued.append("connected")

    signed_in = []

    async def sign_in(username, password):
        signed_in.append((username, password))

    def push_screen(screen, callback=None):
        pushed.append((screen, callback))

    monkeypatch.setattr(backend, "start", start)
    monkeypatch.setattr(backend, "sign_in", sign_in)
    monkeypatch.setattr(backend, "fetch_character_list", fetch_character_list)
    monkeypatch.setattr(app, "_continue_backend_start", continue_start)
    monkeypatch.setattr(app, "push_screen", push_screen)
    monkeypatch.setattr(app, "run_worker", lambda coroutine, **_kwargs: workers.append(coroutine))

    await app._start_backend()
    screen, callback = pushed[-1]
    assert isinstance(screen, SignInScreen)
    assert screen.error == "Sign in is required to join this server."

    callback(SignInCredentials(username="player", password="secret"))
    await workers.pop()
    assert signed_in == [("player", "secret")]
    assert fetches == [True, True]
    assert continued == ["connected"]


async def test_tui_sign_in_handles_cancel_rejection_connection_error_and_local_backend(
    monkeypatch,
):
    backend = RemoteBackend("https://server.example/v1")
    app = BunnylandTUI(backend)
    pushed = []
    exits = []

    def push_screen(screen, callback=None):
        pushed.append((screen, callback))

    monkeypatch.setattr(app, "push_screen", push_screen)
    monkeypatch.setattr(app, "exit", lambda: exits.append(True))
    app._show_sign_in()
    pushed[-1][1](None)
    assert exits == [True]

    class Unauthorized(RuntimeError):
        response = SimpleNamespace(status_code=401)

    failures = [Unauthorized("no"), RuntimeError("offline")]

    async def sign_in(_username, _password):
        raise failures.pop(0)

    monkeypatch.setattr(backend, "sign_in", sign_in)
    credentials = SignInCredentials(username="player", password="secret")
    await app._sign_in_and_retry(credentials)
    assert pushed[-1][0].error == "The username or password was not accepted."
    await app._sign_in_and_retry(credentials)
    assert pushed[-1][0].error == "Could not sign in: offline"

    local_app = BunnylandTUI(RecordingBackend(_snapshot()))
    await local_app._sign_in_and_retry(credentials)
    local_screens = []
    monkeypatch.setattr(
        local_app, "push_screen", lambda screen, callback=None: local_screens.append(screen)
    )
    local_app._show_sign_in()
    assert local_screens[-1].username == ""


async def test_tui_backend_start_reraises_non_auth_and_preserves_content_warning(monkeypatch):
    backend = RecordingBackend(_snapshot())
    app = BunnylandTUI(backend)

    async def broken_start():
        raise RuntimeError("offline")

    monkeypatch.setattr(backend, "start", broken_start)
    with pytest.raises(RuntimeError, match="offline"):
        await app._start_backend()

    pushed = []

    async def fetch_content_flags():
        return ("violence",)

    monkeypatch.setattr(backend, "fetch_content_flags", fetch_content_flags)
    monkeypatch.setattr(app, "push_screen", lambda screen, callback=None: pushed.append(screen))
    await app._continue_backend_start()
    assert pushed and pushed[-1].content_flags == ("violence",)

    finished = []

    async def no_content_flags():
        return ()

    async def finish_backend_start():
        finished.append(True)

    monkeypatch.setattr(backend, "fetch_content_flags", no_content_flags)
    monkeypatch.setattr(app, "_finish_backend_start", finish_backend_start)
    await app._continue_backend_start()
    assert finished == [True]


async def test_action_form_dropdown_selects_and_cancels():
    from textual.widgets import Select

    candidates = World.parse(_snapshot()).target_candidates(PLAYER, "exits")
    field = FormField(
        key="exit_id",
        label="exit",
        kind="entity",
        required=True,
        candidates=tuple(candidates),
    )
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    results = []

    async with app.run_test() as pilot:
        screen = ActionForm("Move", [field])
        await _push_action_form(app, pilot, screen, callback=results.append)
        screen.query_one("#field-exit_id", Select).value = HALL
        screen.query_one("#form-submit").press()
        await pilot.pause()
        assert results[-1] == {"exit_id": HALL}

        cancelled = ActionForm("Move", [field])
        await _push_action_form(app, pilot, cancelled, callback=results.append)
        await pilot.press("escape")
        await pilot.pause()
        assert results[-1] is None


async def test_image_activity_surfaces_scene_images_and_failures():
    app = BunnylandTUI(RecordingBackend(_snapshot()))

    def completed(epoch, url):
        return {
            "event_type": "ImageGenerationCompletedEvent",
            "event": {"world_epoch": epoch, "purpose": "event", "url": url},
        }

    # Priming records the current image without emitting a line.
    assert app._image_activity([completed(5, "/public/media/events/a.png")], prime=True) == []
    assert app._event_image_url == "/public/media/events/a.png"
    # A newer image emits a "scene image ready" line; the same url does not repeat.
    lines = app._image_activity([completed(7, "/public/media/events/b.png")], prime=False)
    assert any("scene image ready" in line.plain and "b.png" in line.plain for line in lines)
    assert app._image_activity([completed(7, "/public/media/events/b.png")], prime=False) == []

    def failed(epoch, reason=None):
        event = {"world_epoch": epoch, "purpose": "event"}
        if reason is not None:
            event["reason"] = reason
        return {"event_type": "ImageGenerationFailedEvent", "event": event}

    flines = app._image_activity([failed(9, "boom")], prime=False)
    assert any("image request failed: boom" in line.plain for line in flines)
    # A failure with no reason falls back to a default message.
    dlines = app._image_activity([failed(11)], prime=False)
    assert any("image generation failed" in line.plain for line in dlines)
    # Priming a newer failure records its epoch without emitting a line.
    assert app._image_activity([failed(12, "later")], prime=True) == []
    assert app._event_image_failure_epoch == 12


async def test_video_activity_surfaces_scene_clips_and_failures():
    app = BunnylandTUI(RecordingBackend(_snapshot()))

    def completed(epoch, url):
        return {
            "event_type": "VideoGenerationCompletedEvent",
            "event": {"world_epoch": epoch, "purpose": "event", "url": url},
        }

    assert app._video_activity([completed(5, "/videos/a.mp4")], prime=True) == []
    lines = app._video_activity([completed(7, "/videos/b.webm")], prime=False)
    assert any("scene video ready" in line.plain and "b.webm" in line.plain for line in lines)
    assert app._video_activity([completed(7, "/videos/b.webm")], prime=False) == []

    def failed(epoch, reason=None):
        event = {"world_epoch": epoch, "purpose": "event"}
        if reason is not None:
            event["reason"] = reason
        return {"event_type": "VideoGenerationFailedEvent", "event": event}

    assert any(
        "video request failed: boom" in line.plain
        for line in app._video_activity([failed(9, "boom")], prime=False)
    )
    assert any(
        "video generation failed" in line.plain
        for line in app._video_activity([failed(11)], prime=False)
    )
    assert app._video_activity([failed(12, "later")], prime=True) == []


async def test_refresh_appends_scene_image_activity():
    backend = RecordingBackend(_snapshot())
    app = BunnylandTUI(backend)
    async with app.run_test() as pilot:
        await _wait_for_tui_ready(app, pilot)
        await _select_player(app, pilot)
        backend.events = [
            {
                "event_type": "ImageGenerationCompletedEvent",
                "event": {
                    "world_epoch": 99,
                    "purpose": "event",
                    "url": "/public/media/events/z.png",
                },
            },
            {
                "event_type": "VideoGenerationCompletedEvent",
                "event": {
                    "world_epoch": 100,
                    "purpose": "event",
                    "url": "/public/media/videos/z.mp4",
                },
            },
        ]
        await app.refresh_world()
        await pilot.pause()
        assert any("scene image ready" in line.plain for line in app.activity_lines)
        assert any("scene video ready" in line.plain for line in app.activity_lines)


async def _open_help_with_key(app, pilot):
    from textual.widgets import Button, OptionList

    # The "?" binding lives on the app, so focus a main-screen widget that does not itself
    # consume the key (the activity list) before pressing it.
    app.query_one("#activity", OptionList).focus()
    await pilot.pause()
    await pilot.press("question_mark")
    assert isinstance(app.screen, HelpScreen)
    return await _wait_for_widget(app.screen, pilot, "#help-close", Button)


async def test_help_screen_opens_and_closes():
    from textual.widgets import Button

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _wait_for_tui_ready(app, pilot)
        # Open the help cheat-sheet via the "?" binding, close it with Escape (action_close).
        await _open_help_with_key(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
        # Reopen and close via the Close button (_close_pressed).
        button = await _open_help_with_key(app, pilot)
        assert isinstance(button, Button)
        button.press()
        await pilot.pause()


async def test_action_form_dropdown_uses_initial_value():
    from textual.widgets import Select

    field = FormField(
        key="target_id",
        label="target",
        kind="entity",
        required=True,
        candidates=(Target(APPLE, "an apple", "✦"),),
        initial_value=APPLE,
    )
    app = BunnylandTUI(RecordingBackend(_snapshot()))

    async with app.run_test() as pilot:
        screen = ActionForm("Inspect", [field])
        await _push_action_form(app, pilot, screen)
        assert screen.query_one("#field-target_id", Select).value == APPLE


async def test_action_form_text_field_submits_and_blocks_when_required():
    from textual.widgets import Input, Label

    field = FormField(key="text", label="text", kind="string", required=True)
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    results = []

    async with app.run_test() as pilot:
        screen = ActionForm("Say", [field])
        await _push_action_form(app, pilot, screen, callback=results.append)
        screen.query_one("#field-text", Input).value = "hello"
        await pilot.press("enter")
        await pilot.pause()
        assert results[-1] == {"text": "hello"}

        # A blank required field reports an error and does not submit (no new result).
        blocked = ActionForm("Say", [field])
        await _push_action_form(app, pilot, blocked, callback=results.append)
        blocked.query_one("#form-submit").press()
        await pilot.pause()
        assert "required" in str(blocked.query_one("#form-error", Label).render())
        assert len(results) == 1

        await pilot.press("escape")
        await pilot.pause()
        assert results[-1] is None


async def test_action_form_boolean_field_initial_value_and_cancel_button():
    from textual.widgets import Input, Select

    # A boolean field renders a yes/no dropdown; a text field with an initial value is
    # pre-filled; on mount the first field is focused and later fields are not reassigned.
    fields = [
        FormField(key="confirm", label="confirm", kind="boolean", required=False),
        FormField(key="note", label="note", kind="string", required=False, initial_value="hi"),
    ]
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    results = []

    async with app.run_test() as pilot:
        screen = ActionForm("Confirm", fields)
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        confirm = screen.query_one("#field-confirm", Select)
        # The boolean dropdown offers yes/no options (plus the blank prompt placeholder).
        assert {"true", "false"} <= {value for _label, value in confirm._options}
        # The text field was pre-filled from its initial value.
        assert screen.query_one("#field-note", Input).value == "hi"

        # The Cancel button dismisses with None (distinct from the escape binding).
        screen.query_one("#form-cancel").press()
        await pilot.pause()
        assert results[-1] is None


@pytest.mark.parametrize(("selected", "expected"), [("true", True), ("false", False)])
async def test_action_form_boolean_field_submits_boolean(selected, expected):
    from textual.widgets import Select

    field = FormField(key="acknowledged", label="Acknowledged", kind="boolean", required=True)
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    results = []

    async with app.run_test() as pilot:
        screen = ActionForm("Accept After Dark Warning", [field])
        await _push_action_form(app, pilot, screen, callback=results.append)
        screen.query_one("#field-acknowledged", Select).value = selected
        screen.query_one("#form-submit").press()
        await pilot.pause()

        assert results[-1] == {"acknowledged": expected}
        assert results[-1]["acknowledged"] is expected


async def test_action_form_omits_blank_optional_boolean():
    field = FormField(key="confirmed", label="Confirmed", kind="boolean", required=False)
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    results = []

    async with app.run_test() as pilot:
        screen = ActionForm("Optional Confirmation", [field])
        await _push_action_form(app, pilot, screen, callback=results.append)
        screen.query_one("#form-submit").press()
        await pilot.pause()

        assert results[-1] == {}


async def test_action_form_with_no_fields_submits_empty_payload():
    # A form with no fields has nothing to focus on mount and submits an empty payload.
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    results = []

    async with app.run_test() as pilot:
        screen = ActionForm("Wait", [])
        app.push_screen(screen, callback=results.append)
        await pilot.pause()
        screen.query_one("#form-submit").press()
        await pilot.pause()
        assert results[-1] == {}


async def test_action_form_omits_blank_optional_fields_from_payload():
    from textual.widgets import Input

    # An optional field left blank is omitted from the payload rather than sent as None.
    fields = [
        FormField(key="text", label="text", kind="string", required=True),
        FormField(key="aside", label="aside", kind="string", required=False),
    ]
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    results = []

    async with app.run_test() as pilot:
        screen = ActionForm("Say", fields)
        app.push_screen(screen, callback=results.append)
        await pilot.pause()
        screen.query_one("#field-text", Input).value = "hello"
        # Leave #field-aside blank.
        screen.query_one("#form-submit").press()
        await pilot.pause()
        assert results[-1] == {"text": "hello"}


async def test_action_form_renders_numeric_input_for_number_fields():
    from textual.widgets import Input

    field = FormField(key="amount", label="amount", kind="number", required=True)
    app = BunnylandTUI(RecordingBackend(_snapshot()))

    async with app.run_test() as pilot:
        screen = ActionForm("Trade", [field])
        await _push_action_form(app, pilot, screen)
        assert screen.query_one("#field-amount", Input).type == "number"


async def test_intro_splash_fades_and_dismisses():
    app = BunnylandTUI(RecordingBackend(_snapshot()), show_intro=True)
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


def test_tui_main_module_exposes_app_main():
    # Importing the ``python -m bunnyland.tui`` entrypoint binds the app's main(); the
    # ``if __name__ == "__main__"`` guard itself is excluded from coverage by config.
    import bunnyland.tui.__main__ as tui_main

    assert tui_main.main is tui_app.main


async def test_intro_splash_dismisses_when_panel_is_missing(monkeypatch):
    from textual.css.query import NoMatches

    app = BunnylandTUI(RecordingBackend(_snapshot()), show_intro=True)
    async with app.run_test() as pilot:
        splash = next(s for s in app.screen_stack if isinstance(s, IntroSplash))
        await asyncio.wait_for(splash._mounted_event.wait(), timeout=2)

        # If the panel has vanished by the time the fade starts, the splash dismisses itself
        # rather than animating a missing widget.
        def raise_no_matches(*_args, **_kwargs):
            raise NoMatches("#splash")

        monkeypatch.setattr(splash, "query_one", raise_no_matches)
        splash._start_fade()
        await pilot.pause()
        assert not any(isinstance(s, IntroSplash) for s in app.screen_stack)


async def test_intro_splash_does_not_use_widget_animation_api(monkeypatch):
    def fail_if_animated(*_args, **_kwargs) -> None:
        raise AssertionError("IntroSplash should not call animate() on this Textual version")

    monkeypatch.setattr(IntroSplash, "animate", fail_if_animated)
    app = BunnylandTUI(RecordingBackend(_snapshot()), show_intro=True)
    async with app.run_test() as _pilot:
        # If the old animate-based implementation is used, this would raise before splash
        # dismissal or app startup completes.
        pass


async def test_app_reports_refresh_errors():
    from textual.widgets import OptionList, Static

    backend = FlakyBackend(_snapshot())
    app = BunnylandTUI(backend)
    async with app.run_test():
        assert "lobby failed" in str(app.query_one("#status", Static).render())
        activity = app.query_one("#activity", OptionList)
        assert activity.option_count == 1
        assert "lobby failed" in str(activity.get_option_at_index(0).prompt)

        await app.refresh_world()
        assert activity.option_count == 1

        backend.fail = False
        await app.refresh_world()
        assert activity.option_count == 2
        assert "reconnected" in str(activity.get_option_at_index(1).prompt)


async def test_app_returns_expired_claim_to_reclaimable_state():
    from textual.widgets import Button, OptionList, Select

    backend = RecordingBackend(_snapshot())
    app = BunnylandTUI(backend)
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        assert app.control is not None

        backend.character_projection = None
        await app.refresh_world()

        assert app.player_id == PLAYER
        assert app.control is None
        assert app.query_one("#player", Select).value == PLAYER
        assert str(app.query_one("#character-release", Button).label) == "Claim"
        activity = app.query_one("#activity", OptionList)
        assert any(
            "Claim expired. Claim this character again." in prompt
            for prompt in _activity_prompts(activity)
        )


async def test_app_coalesces_overlapping_projection_refreshes():
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
    app = BunnylandTUI(backend)
    async with app.run_test():
        app.player_id = PLAYER
        first = asyncio.create_task(app.refresh_world())
        await backend.projection_started.wait()
        second = asyncio.create_task(app.refresh_world())
        await asyncio.sleep(0)

        assert backend.projection_calls == 1

        backend.release_projection.set()
        await asyncio.gather(first, second)
        assert backend.projection_calls == 1


async def test_app_unmount_cancels_refresh_before_closing_backend():
    class BlockingProjectionBackend(RecordingBackend):
        def __init__(self):
            super().__init__(_snapshot())
            self.projection_started = asyncio.Event()
            self.projection_cancelled = False
            self.closed_after_cancel = False

        async def fetch_character_projection(self, character_id: str) -> dict | None:
            self.projection_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.projection_cancelled = True
                raise

        async def close(self) -> None:
            self.closed_after_cancel = self.projection_cancelled

    backend = BlockingProjectionBackend()
    app = BunnylandTUI(backend)
    async with app.run_test():
        app.player_id = PLAYER
        refresh = asyncio.create_task(app.refresh_world())
        await backend.projection_started.wait()

    await asyncio.gather(refresh, return_exceptions=True)
    assert backend.projection_cancelled is True
    assert backend.closed_after_cancel is True


async def test_app_renders_room_and_actions():
    from textual.widgets import OptionList, Static

    text = lambda wid: str(app.query_one(wid, Static).render())  # noqa: E731
    backend = RecordingBackend(_snapshot(), _queued_response(_queued_command()))
    app = BunnylandTUI(backend)
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        assert "Parlor" in text("#room-title")
        members = app.query_one("#members", OptionList)
        # The held key is inventory, not a room member, so only the room's contents show.
        assert {o.id for o in members.options} == {PLAYER, MARLOW, APPLE}
        doors = app.query_one("#doors", OptionList)
        assert [o.id for o in doors.options] == [f"door:{HALL}"]
        assert "4/5 AP" in text("#points")
        queued = app.query_one("#queued", OptionList)
        assert backend.queued_requests[-1] == PLAYER
        assert queued.option_count == 1
        assert "say [focus]" in str(queued.get_option_at_index(0).prompt)
        assert "1 AP + 1 FP" in str(queued.get_option_at_index(0).prompt)
        assert "text: next" in str(queued.get_option_at_index(0).prompt)


async def test_app_uses_projected_actions_and_target_groups(monkeypatch):
    from textual.widgets import OptionList

    projection = _client_view()
    projection["actions"] = [
        _projected_action(
            command_type="custom-inspect",
            tool_name="custom_inspect",
            title="Custom Inspect",
            lane="focus",
            cost={"action": 0, "focus": 1},
        )
    ]
    app = BunnylandTUI(RecordingBackend(_snapshot(), character_projection=projection))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        verbs = app.query_one("#verbs", OptionList)
        assert verbs.option_count == 1
        assert "Custom Inspect" in str(verbs.get_option_at_index(0).prompt)
        assert "1 FP" in str(verbs.get_option_at_index(0).prompt)

        async def fake_form(screen):
            assert isinstance(screen, ActionForm)
            assert [field.key for field in screen.fields] == ["target_id"]
            return {"target_id": APPLE}

        monkeypatch.setattr(app, "push_screen_wait", fake_form)
        app._verb_selected(
            SimpleNamespace(option=SimpleNamespace(id=verbs.get_option_at_index(0).id))
        )
        await pilot.pause()

        command = app.backend.commands[-1]
        assert command["command_type"] == "custom-inspect"
        assert command["payload"] == {"target_id": APPLE}
        assert command["cost"] == {"action": 0, "focus": 1}
        assert command["lane"] == "focus"


async def test_app_preselects_selected_entity_in_action_form(monkeypatch):
    from textual.widgets import OptionList

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        app.selected_id = APPLE

        async def fake_form(screen):
            assert isinstance(screen, ActionForm)
            (target_field,) = screen.fields
            assert target_field.key == "target_id"
            assert target_field.initial_value == APPLE
            return {"target_id": APPLE}

        monkeypatch.setattr(app, "push_screen_wait", fake_form)
        verbs = app.query_one("#verbs", OptionList)
        app._verb_selected(
            SimpleNamespace(option=SimpleNamespace(id=verbs.get_option_at_index(0).id))
        )
        await pilot.pause()
        assert app.backend.commands[-1]["payload"] == {"target_id": APPLE}


async def test_app_ignores_second_action_form_while_one_is_open():
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        field = FormField(key="text", label="text", kind="string", required=True)
        await _push_action_form(app, pilot, ActionForm("Say", [field]))
        assert sum(isinstance(s, ActionForm) for s in app.screen_stack) == 1

        # Selecting another action while a form is open is a no-op: no second form, no submit.
        await app._do_action(_action("say"))
        await pilot.pause()
        assert sum(isinstance(s, ActionForm) for s in app.screen_stack) == 1
        assert app.backend.commands == []


async def test_app_refreshes_main_ui_while_action_form_is_open():
    from textual.widgets import Static

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        field = FormField(key="text", label="text", kind="string", required=True)
        await _push_action_form(app, pilot, ActionForm("Say", [field]))

        await app.refresh_world()

        assert sum(isinstance(s, ActionForm) for s in app.screen_stack) == 1
        assert "Parlor" in str(app._main_query_one("#room-title", Static).render())


async def test_app_main_query_falls_back_to_mounted_screen_stack(monkeypatch):
    from textual.css.query import NoMatches
    from textual.widgets import Static

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _wait_for_tui_ready(app, pilot)
        original_query_one = app.query_one

        def miss_app_query(selector, expect_type=None):
            if selector == "#room-title":
                raise NoMatches("forced app-level miss")
            return original_query_one(selector, expect_type)

        monkeypatch.setattr(app, "query_one", miss_app_query)

        assert app._main_query_one("#room-title", Static).id == "room-title"
        with pytest.raises(NoMatches):
            app._main_query_one("#not-mounted")


async def test_app_refresh_preserves_stable_action_list_position():
    from textual.widgets import OptionList

    projection = _client_view()
    projection["actions"] = [
        _projected_action(
            command_type=f"custom-{index}",
            tool_name=f"custom_{index}",
            title=f"Custom {index}",
        )
        for index in range(18)
    ]
    app = BunnylandTUI(RecordingBackend(_snapshot(), character_projection=projection))

    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        verbs = app.query_one("#verbs", OptionList)
        verbs.highlighted = 17
        updated_projection = copy.deepcopy(projection)
        updated_projection["actions"][0] = {
            **updated_projection["actions"][0],
            "title": "Renamed action",
        }
        app.backend.character_projection = updated_projection

        await app.refresh_world()

        assert verbs.option_count == 18
        assert verbs.highlighted == 17
        assert "Custom 17" in str(verbs.get_option_at_index(17).prompt)


async def test_app_filters_and_clears_action_list():
    from textual.widgets import Button, Input, OptionList

    projection = _client_view()
    projection["actions"] = [
        _projected_action(
            command_type="inspect",
            tool_name="inspect",
            title="Inspect",
        ),
        _projected_action(
            command_type="negotiate",
            tool_name="negotiate",
            title="Negotiate",
        ),
        _projected_action(
            command_type="bribe",
            tool_name="bribe",
            title="Bribe",
        ),
    ]
    app = BunnylandTUI(
        RecordingBackend(
            _snapshot(),
            _queued_response(_queued_command(command_type="say")),
            character_projection=projection,
        )
    )

    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        verbs = app.query_one("#verbs", OptionList)
        queued = app.query_one("#queued", OptionList)
        assert verbs.option_count == 3
        assert queued.option_count == 1

        filter_input = app.query_one("#action-filter", Input)
        filter_input.value = "neg"
        await pilot.pause()

        assert verbs.option_count == 1
        assert "Negotiate" in str(verbs.get_option_at_index(0).prompt)
        assert queued.option_count == 1

        app._action_filter_clear_pressed(
            SimpleNamespace(button=app.query_one("#action-filter-clear", Button))
        )
        await pilot.pause()

        assert filter_input.value == ""
        assert verbs.option_count == 3


async def test_app_clear_filter_is_a_noop_when_already_empty():
    from textual.widgets import Button, Input

    # Pressing Clear with no active filter clears the input but skips the re-render
    # (the filter string is already empty).
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        assert app.action_filter == ""

        app._action_filter_clear_pressed(
            SimpleNamespace(button=app.query_one("#action-filter-clear", Button))
        )
        await pilot.pause()

        assert app.query_one("#action-filter", Input).value == ""
        assert app.action_filter == ""


async def test_app_renders_perceived_activity_after_initial_prime():
    from textual.widgets import OptionList

    backend = RecordingBackend(_snapshot())
    app = BunnylandTUI(backend)
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        activity = app.query_one("#activity", OptionList)
        assert "No recent activity." in str(activity.get_option_at_index(0).prompt)

        backend.events = [
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
        await app.refresh_world()

        assert activity.option_count == 1
        shown = str(activity.get_option_at_index(0).prompt)
        assert "Command rejected" in shown and "not portable" in shown
        assert "secret" not in shown


async def test_app_points_line_colors_values_and_dims_zero_pips():
    from textual.widgets import Static

    projection = _client_view()
    projection["points"] = {
        "action": 0,
        "action_max": 5,
        "focus": 2,
        "focus_max": 3,
    }
    app = BunnylandTUI(RecordingBackend(_snapshot(), character_projection=projection))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        line = app.query_one("#points", Static).render()

    assert line.plain == "⚡ 0/5 AP   🔹 2/3 FP"
    spans = {line.plain[span.start : span.end]: str(span.style) for span in line.spans}
    assert "dim" in spans["⚡"]
    assert "255,135,0" in spans[" 0/5 AP"]
    assert "cyan" in spans["🔹"]
    assert "cyan" in spans[" 2/3 FP"]


async def test_app_covers_defensive_selection_and_action_branches(monkeypatch):
    from textual.widgets import OptionList

    projection = _client_view()
    projection["actions"] = [
        _projected_action(command_type="wait", tool_name="wait", title="Wait"),
        _projected_action(command_type="rest", tool_name="wait", title="Rest"),
    ]
    app = BunnylandTUI(RecordingBackend(_snapshot(), character_projection=projection))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        verbs = app.query_one("#verbs", OptionList)
        assert {option.id for option in verbs.options} == {"wait", "wait:1"}

        run_calls = []
        monkeypatch.setattr(app, "run_worker", lambda *args, **kwargs: run_calls.append(args))
        app._door_selected(SimpleNamespace(option=SimpleNamespace(id="not-a-door")))
        app._queued_selected(SimpleNamespace(option=SimpleNamespace(id="not-queued")))
        app._queued_selected(SimpleNamespace(option=SimpleNamespace(id="queued:99")))
        assert run_calls == []

        fields = app._action_fields(
            {
                "arguments": [
                    {"title": "missing key", "required": True},
                    {"key": "optional", "required": False},
                    {"key": "target_id", "required": False, "target_group": "characters"},
                ],
            }
        )
        assert [field.key for field in fields] == ["target_id"]

        app.action_views = []
        await app._move_through_exit("missing-exit")

        app.action_views = [
            {
                "command_type": "move",
                "tool_name": "move",
                "arguments": [{"key": "exit_id", "target_group": "elsewhere", "required": True}],
            }
        ]
        await app._move_through_exit("missing-exit")

        app.player_id = ""
        app.control = None
        await app._do_action({"command_type": "wait", "tool_name": "wait", "arguments": []})
        await app._submit_action({"command_type": "wait", "tool_name": "wait"}, {})
        await app._cancel_queued_command({"command_id": "cmd-1"})


async def test_app_clears_stale_control_when_projection_omits_controller():
    from textual.widgets import Button, Select

    projection = _client_view()
    del projection["controller"]
    app = BunnylandTUI(RecordingBackend(_snapshot(), character_projection=projection))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        assert app.player_id == PLAYER
        assert app.control is None
        assert app.query_one("#player", Select).value == PLAYER
        assert str(app.query_one("#character-release", Button).label) == "Claim"


async def test_app_clears_replaced_controller_and_can_reclaim():
    from types import SimpleNamespace

    projection = _client_view()
    projection["controller"] = {"controller_id": "controller:other", "generation": 9}
    backend = RecordingBackend(_snapshot(), character_projection=projection)
    app = BunnylandTUI(backend)
    async with app.run_test():
        app.player_id = PLAYER
        app.control = ("controller:1", 2)
        await app.refresh_world()

        assert app.player_id == PLAYER
        assert app.control is None

        backend.character_projection = _client_view()
        await app._character_release_pressed(SimpleNamespace())

        assert backend.claims == [PLAYER]
        assert app.player_id == PLAYER
        assert app.control == ("controller:1", 3)


async def test_refresh_world_returns_quietly_when_status_widget_is_absent():
    from textual.css.query import NoMatches

    # refresh_world tolerates the status widget being gone (e.g. mid-teardown): both the
    # error path and the success path swallow NoMatches and return instead of crashing.
    def hide_status(app):
        # _main_query_one falls back to the screen stack, so #status must miss on the app
        # *and* on every mounted screen for the refresh guards to fire.
        def patch(obj):
            real = obj.query_one

            def query_one(selector, *args, **kwargs):
                if selector == "#status":
                    raise NoMatches("#status")
                return real(selector, *args, **kwargs)

            obj.query_one = query_one  # type: ignore[method-assign]

        patch(app)
        for screen in app.get_screen_stack():
            patch(screen)

    # Error path: the backend fails and the status update finds no widget.
    failing = FlakyBackend(_snapshot())
    app = BunnylandTUI(failing)
    async with app.run_test():
        hide_status(app)
        await app.refresh_world()  # error branch hits the NoMatches guard

    # Success path: a healthy refresh whose status update finds no widget also returns.
    healthy = BunnylandTUI(RecordingBackend(_snapshot()))
    async with healthy.run_test() as pilot:
        await _select_player(healthy, pilot)
        hide_status(healthy)
        await healthy.refresh_world()  # success branch hits the NoMatches guard


async def test_refresh_world_returns_quietly_when_release_widget_is_absent():
    from textual.css.query import NoMatches

    def hide_release(app):
        def patch(obj):
            real = obj.query_one

            def query_one(selector, *args, **kwargs):
                if selector == "#character-release":
                    raise NoMatches("#character-release")
                return real(selector, *args, **kwargs)

            obj.query_one = query_one  # type: ignore[method-assign]

        patch(app)
        for screen in app.get_screen_stack():
            patch(screen)

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        hide_release(app)
        await app.refresh_world()


async def test_app_failed_queue_cancel_reports_activity():
    class CancelFailBackend(RecordingBackend):
        async def cancel_command(
            self,
            character_id: str,
            command_id: str,
            controller_id: str,
            controller_generation: int,
        ) -> bool:
            await super().cancel_command(
                character_id, command_id, controller_id, controller_generation
            )
            return False

    app = BunnylandTUI(CancelFailBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        await app._cancel_queued_command({"command_id": "cmd-1"})

    assert any("Could not cancel queued command" in line.plain for line in app.activity_lines)


async def test_app_renders_empty_and_keeps_last_queue_on_fetch_failure():
    from textual.widgets import OptionList

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        queued = app.query_one("#queued", OptionList)
        assert queued.option_count == 1
        assert "No queued actions." in str(queued.get_option_at_index(0).prompt)

    # A transient queue-fetch failure keeps the last-known queue rather than blanking it.
    fallback_app = BunnylandTUI(FailingQueueBackend(_snapshot()))
    fallback_app.player_id = PLAYER
    fallback_app.queued_commands = [_queued_command(command_id="cmd-last")]
    async with fallback_app.run_test():
        assert await fallback_app._fetch_queued_commands() == [
            _queued_command(command_id="cmd-last")
        ]


async def test_app_reuses_commands_from_character_projection():
    backend = RecordingBackend(_snapshot())
    app = BunnylandTUI(backend)
    app.player_id = PLAYER
    projection = {
        **_client_view(),
        "queued_commands": [_queued_command()],
    }

    assert await app._fetch_queued_commands(projection) == [_queued_command()]
    assert backend.queued_requests == []


async def test_app_selecting_queued_command_cancels_it():
    from textual.widgets import OptionList

    backend = RecordingBackend(_snapshot(), _queued_response(_queued_command()))
    app = BunnylandTUI(backend)
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        queued = app.query_one("#queued", OptionList)

        app._queued_selected(
            SimpleNamespace(option=SimpleNamespace(id=queued.get_option_at_index(0).id))
        )
        await pilot.pause()

        assert backend.cancelled_commands == [(PLAYER, "cmd-1", "controller:1", 3)]


async def test_app_discards_mismatched_queued_command_response():
    app = BunnylandTUI(
        RecordingBackend(
            _snapshot(),
            {
                **_queued_response(_queued_command()),
                "character_id": MARLOW,
            },
        )
    )
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        assert app.queued_commands == []


async def test_app_rendering_restores_highlight_and_handles_missing_room():
    from textual.widgets import OptionList, Static

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        app.selected_id = MARLOW
        app._render_room()
        members = app.query_one("#members", OptionList)
        assert members.get_option_at_index(members.highlighted).id == MARLOW

        app.selected_id = "missing"
        app._render_room()
        assert members.option_count == 3

        app.view_room_id = "room:missing"
        app.player_id = ""
        app._render_room()
        assert str(app.query_one("#room-title", Static).render()) == "No room"


async def test_app_renders_only_the_players_own_room():
    from textual.widgets import OptionList, Static

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        # The view is always the player's own perceived room; there is no door-spectating.
        assert app.view_room_id == PARLOR
        assert "Parlor" in str(app.query_one("#room-title", Static).render())
        assert "spectating" not in str(app.query_one("#room-title", Static).render())
        # Exits are labelled by direction, not the neighbouring room's name (no map leak).
        doors = app.query_one("#doors", OptionList)
        assert [o.id for o in doors.options] == [f"door:{HALL}"]
        assert "north" in str(doors.get_option_at_index(0).prompt)
        assert "Hallway" not in str(doors.get_option_at_index(0).prompt)


async def test_app_selecting_door_queues_move():
    from textual.widgets import OptionList

    projection = _client_view()
    projection["actions"] = [
        _projected_action(
            command_type="move",
            tool_name="move",
            title="Move",
            cost={"action": 1, "focus": 0},
            arguments=[
                {
                    "key": "exit_id",
                    "title": "exit",
                    "kind": "entity",
                    "required": True,
                    "target_group": "exits",
                }
            ],
        )
    ]
    app = BunnylandTUI(RecordingBackend(_snapshot(), character_projection=projection))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        doors = app.query_one("#doors", OptionList)

        app._door_selected(
            SimpleNamespace(option=SimpleNamespace(id=doors.get_option_at_index(0).id))
        )
        await pilot.pause()

        assert app.backend.commands[-1]["command_type"] == "move"
        assert app.backend.commands[-1]["payload"] == {"exit_id": HALL}


async def test_app_member_and_verb_selection_handlers():
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)

        app._member_selected(SimpleNamespace(option=SimpleNamespace(id=APPLE)))
        assert app.selected_id == APPLE

        app.action_views = [_action("wait")]
        app._render_actions()
        app._verb_selected(SimpleNamespace(option=SimpleNamespace(id="wait")))
        await pilot.pause()
        assert app.backend.commands[-1]["command_type"] == "wait"

        app._verb_selected(SimpleNamespace(option=SimpleNamespace(id="missing")))
        await pilot.pause()
        assert app.backend.commands[-1]["command_type"] == "wait"


async def test_app_target_label_and_clear_target_controls():
    from textual.widgets import Button, OptionList, Static

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        target_label = app.query_one("#target-label", Static)
        clear_target = app.query_one("#target-clear", Button)

        assert str(target_label.render()) == "Target: none"
        assert clear_target.disabled

        app.action_clear_target()
        assert app.selected_id is None
        assert str(target_label.render()) == "Target: none"
        assert clear_target.disabled

        app._member_selected(SimpleNamespace(option=SimpleNamespace(id=APPLE)))
        assert str(target_label.render()) == "Target: an apple"
        assert not clear_target.disabled

        app.action_clear_target()
        assert app.selected_id is None
        assert str(target_label.render()) == "Target: none"
        assert clear_target.disabled

        inventory = app.query_one("#inventory", OptionList)
        app._inventory_selected(
            SimpleNamespace(option=SimpleNamespace(id=inventory.get_option_at_index(0).id))
        )
        assert app.selected_id == KEY
        assert str(target_label.render()) == "Target: a brass key"
        assert not clear_target.disabled

        app._target_clear_pressed(SimpleNamespace(button=clear_target))
        await pilot.pause()
        assert app.selected_id is None
        assert str(target_label.render()) == "Target: none"
        assert clear_target.disabled

        app.selected_id = "missing"
        app._sync_target_controls()
        assert str(target_label.render()) == "Target: missing"


async def test_app_action_form_selection_runs_in_worker(monkeypatch):
    from textual.worker import get_current_worker

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)

        async def fake_form(screen):
            assert isinstance(screen, ActionForm)
            get_current_worker()
            return {"exit_id": HALL}

        monkeypatch.setattr(app, "push_screen_wait", fake_form)

        app.action_views = [_action("move")]
        app._render_actions()
        app._verb_selected(SimpleNamespace(option=SimpleNamespace(id="move")))
        await pilot.pause()

        assert app.backend.commands[-1]["command_type"] == "move"
        assert app.backend.commands[-1]["payload"] == {"exit_id": HALL}


async def test_app_wait_submits_a_command():
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        await app._do_action(_action("wait"))
        assert app.backend.commands[-1]["command_type"] == "wait"
        # Generation comes from the character projection's controller (gen 3 in _client_view).
        assert app.backend.commands[-1]["controller_generation"] == 3


async def test_app_refresh_action_resyncs_existing_player():
    from textual.widgets import Select

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        await app.action_refresh()
        assert app.query_one("#player", Select).value == PLAYER


async def test_app_syncs_picker_and_noops_reselecting_same_player():
    from textual.widgets import Select

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test():
        app.character_list = _character_list_from_snapshot(_snapshot())
        app.player_id = PLAYER
        app._player_choice_ids = [MARLOW]
        app._sync_players()
        assert app.query_one("#player", Select).value == PLAYER

        # The view always tracks the player's own room after a refresh.
        await app.refresh_world()
        assert app.view_room_id == PARLOR

        # Re-selecting the player you already control is a no-op (selection preserved).
        app.selected_id = APPLE
        await app._player_changed(SimpleNamespace(value=PLAYER))
        assert app.selected_id == APPLE


async def test_app_move_uses_action_form_dropdown(monkeypatch):
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)

        async def fake_form(screen):
            assert isinstance(screen, ActionForm)
            # the exit argument is offered as a dropdown of nearby candidates
            (exit_field,) = screen.fields
            assert exit_field.candidates is not None
            return {"exit_id": HALL}  # choose the north exit

        monkeypatch.setattr(app, "push_screen_wait", fake_form)
        await app._do_action(_action("move"))
        cmd = app.backend.commands[-1]
        assert cmd["command_type"] == "move"
        assert cmd["payload"] == {"exit_id": HALL}


async def test_app_action_form_cancellation_submits_nothing(monkeypatch):
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)

        async def fake_form(screen):
            # tell collects both its target dropdown and message in one form
            assert {field.key for field in screen.fields} == {"target_id", "text"}
            return None  # user cancelled the form

        monkeypatch.setattr(app, "push_screen_wait", fake_form)
        await app._do_action(_action("tell"))
        assert app.backend.commands == []


async def test_app_verb_without_player_or_control_submits_nothing():
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test():
        wait = _action("wait")
        await app._do_action(wait)
        assert app.backend.commands == []

        app.player_id = MARLOW
        app.control = None
        await app._do_action(wait)
        assert app.backend.commands == []


async def test_app_say_collects_text(monkeypatch):
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)

        async def fake_form(screen):
            assert isinstance(screen, ActionForm)
            assert [field.key for field in screen.fields] == ["text"]
            return {"text": "hello terminal"}

        monkeypatch.setattr(app, "push_screen_wait", fake_form)
        await app._do_action(_action("say"))
        assert app.backend.commands[-1]["payload"] == {"text": "hello terminal"}


async def test_app_cancelled_form_submits_nothing(monkeypatch):
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)

        async def fake_form(screen):
            return None  # user pressed escape

        monkeypatch.setattr(app, "push_screen_wait", fake_form)
        await app._do_action(_action("move"))
        assert app.backend.commands == []


async def test_app_deemphasizes_unavailable_verbs_but_keeps_them_selectable():
    from textual.widgets import OptionList

    # An available "wait", and two unavailable actions the projection flagged. They are not
    # removed or disabled -- only de-emphasized and sorted after the available ones.
    projection = _client_view()
    projection["actions"] = [
        _projected_action(
            command_type="move",
            tool_name="move",
            title="Move",
            cost={"action": 1, "focus": 0},
            arguments=[],
            available=False,
            enough_action_points=False,
            unavailable_reason="not enough action points",
        ),
        _projected_action(
            command_type="wait",
            tool_name="wait",
            title="Wait",
            cost={"action": 0, "focus": 0},
            arguments=[],
        ),
        _projected_action(
            command_type="pick-lock",
            tool_name="pick_lock",
            title="Pick Lock",
            cost={"action": 1, "focus": 0},
            arguments=[],
            available=False,
            meets_requirements=False,
            unavailable_reason="missing a required skill or item",
        ),
    ]

    app = BunnylandTUI(RecordingBackend(_snapshot(), character_projection=projection))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        options = list(app.query_one("#verbs", OptionList).options)
        by_id = {option.id: option for option in options}

        # Nothing is disabled: an unavailable action can still be selected/queued.
        assert all(not option.disabled for option in options)

        # The available action sorts first; unavailable ones follow.
        assert options[0].id == "wait"
        assert {options[1].id, options[2].id} == {"move", "pick_lock"}

        # Unavailable actions are dimmed and show their reason; available ones are not.
        move_label = by_id["move"].prompt
        assert move_label.style == "dim"
        assert "not enough action points" in move_label.plain
        assert "missing a required skill or item" in by_id["pick_lock"].prompt.plain
        assert by_id["wait"].prompt.style != "dim"


async def test_app_surfaces_submit_rejection_reason():

    projection = _client_view()
    projection["actions"] = [
        _projected_action(
            command_type="wait",
            tool_name="wait",
            title="Wait",
            cost={"action": 0, "focus": 0},
            arguments=[],
        ),
    ]

    class RejectingBackend(RecordingBackend):
        async def submit(self, command: dict) -> SubmitResult:
            self.commands.append(command)
            return SubmitResult(accepted=False, reason="character is asleep")

    app = BunnylandTUI(RejectingBackend(_snapshot(), character_projection=projection))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        await app._do_action(await _wait_for_action_view(app, pilot, "wait"))
        assert any("character is asleep" in line.plain for line in app.activity_lines)


async def test_app_clears_missing_player_after_refresh():
    snapshot = _snapshot()
    snapshot["entities"] = [entity for entity in snapshot["entities"] if entity["id"] != PLAYER]
    app = BunnylandTUI(RecordingBackend(snapshot))

    async with app.run_test():
        app.player_id = PLAYER
        app.control = ("controller:1", 2)
        await app.refresh_world()
        assert app.player_id == ""
        assert app.control is None


async def test_app_release_clears_character_selection():
    from textual.widgets import Button, Select

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await app._character_release_pressed(SimpleNamespace())
        assert app.player_id == ""
        assert app.control is None

        await _select_player(app, pilot)
        assert app.player_id == PLAYER
        assert not app.query_one("#character-release", Button).disabled

        await app._character_release_pressed(SimpleNamespace())
        await pilot.pause()

        assert app.player_id == ""
        assert app.control is None
        assert app.query_one("#player", Select).value == Select.NULL
        assert app.query_one("#character-release", Button).disabled


async def test_app_idle_and_claim_release_paths():
    from textual.widgets import Button, OptionList, Select

    class ReleaseBackend(RecordingBackend):
        def __init__(self, snapshot: dict) -> None:
            super().__init__(snapshot)
            self.release_controller_result: ControlClaim | None = None
            self.release_claim_result = False

        async def release_controller(
            self,
            character_id: str,
            control: ControlClaim,
        ) -> ControlClaim | None:
            return self.release_controller_result

        async def release_claim(self, character_id: str, control: ControlClaim) -> bool:
            return self.release_claim_result

    backend = ReleaseBackend(_snapshot())
    app = BunnylandTUI(backend)
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        app.control = ControlClaim("controller:1", 2, "claim-1", "secret-1", active=False)
        app._render_play_state()
        assert str(app.query_one("#character-release", Button).label) == "Resume"

        app.control = ControlClaim("controller:1", 2, "claim-1", "secret-1")
        await app._character_release_pressed(SimpleNamespace())
        activity = app.query_one("#activity", OptionList)
        assert any("Could not release controller" in p for p in _activity_prompts(activity))

        inactive = ControlClaim("controller:2", 3, "claim-1", "secret-1", active=False)
        backend.release_controller_result = inactive
        await app._character_release_pressed(SimpleNamespace())
        assert app.control is not None
        assert app.control.active is False

        backend.release_claim_result = False
        await app._claim_release_pressed(SimpleNamespace())
        assert any("Could not release claim" in p for p in _activity_prompts(activity))

        backend.release_claim_result = True
        await app._claim_release_pressed(SimpleNamespace())
        assert app.player_id == ""
        assert app.control is None
        assert app.query_one("#player", Select).value == Select.NULL


async def test_app_submit_action_reports_failed_resume():
    from textual.widgets import OptionList

    class ResumeFailBackend(RecordingBackend):
        async def claim(self, player_id, world):
            self.claims.append(player_id)
            return None

    app = BunnylandTUI(ResumeFailBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        app.control = ControlClaim("controller:1", 2, "claim-1", "secret-1", active=False)
        await app._submit_action({"command_type": "wait", "tool_name": "wait"}, {})

        activity = app.query_one("#activity", OptionList)
        assert any("Could not resume this character" in p for p in _activity_prompts(activity))


async def test_app_release_claim_noops_without_claim():
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test():
        await app._claim_release_pressed(SimpleNamespace())


async def test_app_submit_action_resumes_before_submit():
    class ResumeBackend(RecordingBackend):
        async def claim(self, player_id, world):
            self.claims.append(player_id)
            return ControlClaim("controller:1", 2, "claim-1", "secret-1")

    app = BunnylandTUI(ResumeBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        app.control = ControlClaim("controller:old", 1, "claim-1", "secret-1", active=False)
        await app._submit_action({"command_type": "wait", "tool_name": "wait"}, {})

        assert app.backend.claims[-1] == PLAYER
        assert app.backend.commands[-1]["controller_id"] == "controller:1"


async def test_app_empty_character_roster_prompts_for_playable_character():
    from textual.widgets import Button, Static

    app = BunnylandTUI(RecordingBackend(_snapshot(), character_projection=None, character_list=[]))
    async with app.run_test():
        assert app.query_one("#character-release", Button).disabled
        hint = str(app.query_one("#play-hint", Static).render())
        assert "playable characters" in hint


# ── local world generator selector ───────────────────────────────────────────
async def _selector_generator(actor, seed: str, options: GenOptions) -> InstantiatedWorld:
    del actor, seed, options
    return InstantiatedWorld()


async def test_world_generator_selector_groups_generators_and_returns_seed(monkeypatch):
    from textual.app import App
    from textual.widgets import Button, Input, Label, OptionList, Static

    from bunnyland.tui import generator_selector as selector_module

    generators = [
        WorldGenerator(
            name="fixed-demo",
            generate=_selector_generator,
            description="fixed world",
            uses_seed=False,
            group="tutorials",
        ),
        WorldGenerator(
            name="recursive",
            generate=_selector_generator,
            description="seeded world",
            uses_seed=True,
            group="algorithmic",
        ),
    ]
    results = []
    screen = WorldGeneratorSelector(
        generators,
        initial_generator="recursive",
        initial_seed="initial seed",
    )

    class Host(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(screen, callback=results.append)

    app = Host()
    async with app.run_test() as pilot:
        options = await _wait_for_widget(screen, pilot, "#generator-list", OptionList)
        prompts = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert any("ALGORITHMIC" in prompt for prompt in prompts)
        assert any("TUTORIALS" in prompt for prompt in prompts)
        assert any("recursive" in prompt for prompt in prompts)
        assert not any("uses seed" in prompt for prompt in prompts)
        assert not any("ignores seed" in prompt for prompt in prompts)

        seed = screen.query_one("#seed-input", Input)
        assert seed.value == "initial seed"
        assert not seed.disabled
        assert str(screen.query_one("#seed-label", Label).render()) == "Seed prompt"
        assert screen.query_one("#generator-start", Button).label.plain == "Select"
        picker_region = screen.query_one("#generator-picker").region
        buttons_region = screen.query_one("#generator-buttons").region
        assert buttons_region.y + buttons_region.height <= picker_region.y + picker_region.height

        screen._generator_selected(
            SimpleNamespace(option=SimpleNamespace(id="generator:fixed-demo"))
        )
        assert seed.disabled
        assert screen.query_one("#seed-random", Button).disabled
        description = str(screen.query_one("#generator-description", Static).render())
        assert "fixed world" in description
        assert "ignores" not in description

        screen._generator_highlighted(
            SimpleNamespace(option=SimpleNamespace(id="generator:recursive"))
        )
        assert screen.selected_generator.name == "recursive"

        monkeypatch.setattr(selector_module.secrets, "choice", lambda choices: choices[-1])
        assert random_preset_seed() == PRESET_SEEDS[-1]
        screen._random_seed_pressed(SimpleNamespace())
        assert seed.value == PRESET_SEEDS[-1]

        seed.value = ""
        screen._seed_submitted(SimpleNamespace())
        assert results == []
        assert "Seed is required" in str(screen.query_one("#generator-error", Static).render())

        seed.value = "chosen seed"
        screen._start_pressed(SimpleNamespace())
        await pilot.pause()
        assert results[-1].generator == "recursive"
        assert results[-1].seed == "chosen seed"


async def test_world_generator_selector_select_uses_highlighted_generator():
    from textual.app import App
    from textual.widgets import Input, OptionList

    generators = [
        WorldGenerator(
            name="fixed-demo",
            generate=_selector_generator,
            description="fixed world",
            uses_seed=False,
            group="tutorials",
        ),
        WorldGenerator(
            name="recursive",
            generate=_selector_generator,
            description="seeded world",
            uses_seed=True,
            group="algorithmic",
        ),
    ]
    results = []
    screen = WorldGeneratorSelector(
        generators,
        initial_generator="fixed-demo",
        initial_seed="initial seed",
    )

    class Host(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(screen, callback=results.append)

    app = Host()
    async with app.run_test() as pilot:
        options = await _wait_for_widget(screen, pilot, "#generator-list", OptionList)
        options.highlighted = options.get_option_index("generator:recursive")
        await pilot.pause()
        screen.query_one("#seed-input", Input).value = "recursive seed"

        screen._start_pressed(SimpleNamespace())
        await pilot.pause()

    assert results[-1].generator == "recursive"
    assert results[-1].seed == "recursive seed"


async def test_world_generator_selector_select_uses_selected_generator_without_highlight():
    from textual.app import App
    from textual.widgets import Input, OptionList

    screen = WorldGeneratorSelector(
        [
            WorldGenerator(
                name="recursive",
                generate=_selector_generator,
                description="seeded world",
                uses_seed=True,
                group="algorithmic",
            )
        ],
        initial_generator="recursive",
        initial_seed="initial seed",
    )
    results = []

    class Host(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(screen, callback=results.append)

    app = Host()
    async with app.run_test() as pilot:
        options = await _wait_for_widget(screen, pilot, "#generator-list", OptionList)
        options.highlighted = None
        await pilot.pause()
        assert screen._highlighted_generator() is None

        screen.query_one("#seed-input", Input).value = "selected seed"
        screen._start_pressed(SimpleNamespace())
        await pilot.pause()

    assert results[-1].generator == "recursive"
    assert results[-1].seed == "selected seed"


async def test_world_generator_selector_ignores_invalid_selection_and_cancels():
    from textual.app import App

    results = []
    screen = WorldGeneratorSelector(
        [
            WorldGenerator(
                name="recursive",
                generate=_selector_generator,
                uses_seed=True,
                group="algorithmic",
            )
        ],
        initial_generator="missing",
    )

    class Host(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(screen, callback=results.append)

    app = Host()
    async with app.run_test() as pilot:
        await _wait_for_widget(screen, pilot, "#generator-list")
        assert screen.selected_generator.name == "recursive"
        screen._generator_selected(SimpleNamespace(option=SimpleNamespace(id="group")))
        assert screen.selected_generator.name == "recursive"
        screen._generator_selected(SimpleNamespace(option=SimpleNamespace(id="generator:missing")))
        assert screen.selected_generator.name == "recursive"
        screen._generator_highlighted(SimpleNamespace(option=SimpleNamespace(id="group")))
        assert screen.selected_generator.name == "recursive"
        screen._cancel_pressed(SimpleNamespace())
        await pilot.pause()
        assert results == [None]


def test_world_generator_selector_escape_cancels(monkeypatch):
    screen = WorldGeneratorSelector(
        [
            WorldGenerator(
                name="recursive",
                generate=_selector_generator,
                uses_seed=True,
                group="algorithmic",
            )
        ]
    )
    dismissed = []

    monkeypatch.setattr(screen, "dismiss", dismissed.append)
    screen.action_cancel()

    assert dismissed == [None]


def test_world_generator_selector_rejects_empty_generator_list():
    with pytest.raises(ValueError, match="needs at least one generator"):
        WorldGeneratorSelector([])


class SelectorLocalBackend(LocalBackend):
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


async def test_tui_mount_uses_local_generator_selection(monkeypatch):
    backend = SelectorLocalBackend()
    app = BunnylandTUI(backend)
    app.show_generator_selector = True
    refreshed = []
    intervals = []
    workers = []

    def fake_push_screen(screen, callback=None):
        assert isinstance(screen, WorldGeneratorSelector)
        callback(GeneratorSelection(generator="bell-green", seed="town seed"))

    async def fake_refresh():
        refreshed.append(True)

    monkeypatch.setattr(app, "push_screen", fake_push_screen)
    monkeypatch.setattr(app, "run_worker", lambda coro, **kwargs: workers.append(coro))
    monkeypatch.setattr(app, "refresh_world", fake_refresh)
    monkeypatch.setattr(app, "set_interval", lambda *args: intervals.append(args))

    await app.on_mount()
    assert len(workers) == 1
    await workers[0]

    assert backend.started
    assert backend.generator_name == "bell-green"
    assert backend.seed == "town seed"
    assert backend.label == "local · bell-green"
    assert refreshed == [True]
    assert intervals


async def test_tui_mount_shows_intro_before_local_generator_selector(monkeypatch):
    backend = SelectorLocalBackend()
    app = BunnylandTUI(backend, show_intro=True)
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


async def test_tui_mount_exits_when_local_generator_selection_is_cancelled(monkeypatch):
    backend = SelectorLocalBackend()
    app = BunnylandTUI(backend)
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


# ── TUI CLI wiring ────────────────────────────────────────────────────────────
def test_main_runs_remote_backend(monkeypatch):
    backends = []
    runs = []
    apps = []

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

    monkeypatch.setattr(tui_app, "RemoteBackend", BackendStub)
    monkeypatch.setattr(tui_app, "BunnylandTUI", AppStub)

    assert (
        tui_app.main(
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
    assert backends[0].fallback_controller == "llm"
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

    monkeypatch.setattr(tui_app, "RemoteBackend", BackendStub)
    monkeypatch.setattr(tui_app, "BunnylandTUI", AppStub)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "prompt password")
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin password\n"))
    argv = ["--server", "https://example.test", "--username", "player"]
    if password_stdin:
        argv.append("--password-stdin")
    assert tui_app.main(argv) == 0
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
            self.fallback_controller = fallback_controller
            self.timeout_seconds = timeout_seconds
            backends.append(self)

    class AppStub:
        def __init__(self, backend):
            self.backend = backend
            apps.append(self)

        def run(self): ...

    monkeypatch.setattr(tui_app, "LocalBackend", BackendStub)
    monkeypatch.setattr(tui_app, "BunnylandTUI", AppStub)

    assert (
        tui_app.main(
            [
                "--seed",
                "test seed",
                "--generator",
                "empty",
                "--claim-fallback",
                "suspend",
            ]
        )
        == 0
    )
    assert backends[0].seed == "test seed"
    assert backends[0].generator == "empty"
    assert backends[0].fallback_controller == "suspend"
    assert apps[0].show_generator_selector is False


def test_main_no_icons_disables_tui_icons(monkeypatch):
    apps = []

    class BackendStub:
        def __init__(
            self, *, seed=None, generator=None, fallback_controller=None, timeout_seconds=None
        ):
            del seed, generator, fallback_controller, timeout_seconds

    class AppStub:
        def __init__(self, backend):
            self.backend = backend
            self.show_icons = True
            apps.append(self)

        def run(self): ...

    monkeypatch.setattr(tui_app, "LocalBackend", BackendStub)
    monkeypatch.setattr(tui_app, "BunnylandTUI", AppStub)

    assert tui_app.main(["--no-icons"]) == 0
    assert apps[0].show_icons is False
    assert apps[0].show_generator_selector is True


def test_main_lists_generators_and_exits(monkeypatch, capsys):
    from types import SimpleNamespace

    launched: list[bool] = []
    monkeypatch.setattr(
        tui_app,
        "available_generators",
        lambda: [
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
        ],
    )
    monkeypatch.setattr(tui_app, "BunnylandTUI", lambda backend: launched.append(True))

    assert tui_app.main(["--list-generators"]) == 0
    assert launched == []
    output = capsys.readouterr().out
    assert "Algorithmic:" in output
    assert "  recursive" in output
    assert "Pop Culture:" in output
    assert "  apartment-demo *" in output
    assert "* ignores --seed" in output


def _activity_prompts(activity):
    return [str(activity.get_option_at_index(i).prompt) for i in range(activity.option_count)]


# --- character sheet deep links ------------------------------------------------------


class _SheetBackend(RecordingBackend):
    """Records sheet opens and returns a scripted result."""

    supports_character_sheets = True

    def __init__(self, snapshot, result):
        super().__init__(snapshot)
        self._result = result
        self.sheet_requests: list[str] = []

    async def open_character_sheet(self, character_id):
        self.sheet_requests.append(character_id)
        return self._result


class _ImageBackend(RecordingBackend):
    """Records image requests and returns a scripted result."""

    supports_image_requests = True

    def __init__(self, snapshot, result):
        super().__init__(snapshot)
        self._result = result
        self.image_requests: list[str] = []

    async def request_image(self, character_id, event_id=""):
        del event_id
        self.image_requests.append(character_id)
        return self._result


class _VideoBackend(RecordingBackend):
    supports_video_requests = True

    def __init__(self, snapshot, result):
        super().__init__(snapshot)
        self._result = result
        self.video_requests: list[str] = []

    async def request_video(self, character_id, event_id=""):
        del event_id
        self.video_requests.append(character_id)
        return self._result


async def test_app_hides_terminal_affordance_buttons_when_unsupported():
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test():
        assert not app.query("#request-image")
        assert not app.query("#request-video")
        assert not app.query("#open-sheet")


async def test_app_request_image_without_player():
    from textual.widgets import OptionList

    from bunnyland.tui.backend import ImageRequestResult

    app = BunnylandTUI(_ImageBackend(_snapshot(), ImageRequestResult(ok=True, status="queued")))
    async with app.run_test():
        assert app.query("#request-image")
        await app.action_request_image()
        activity = app.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("Select a character before requesting an image" in p for p in prompts)


async def test_app_request_image_when_supported():
    from textual.widgets import OptionList

    from bunnyland.tui.backend import ImageRequestResult

    backend = _ImageBackend(_snapshot(), ImageRequestResult(ok=True, status="queued"))
    app = BunnylandTUI(backend)
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        await pilot.click("#request-image")
        await pilot.pause()
        activity = app.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("image requested" in p for p in prompts)
        assert backend.image_requests == [PLAYER]


async def test_app_request_image_unavailable_and_failure_paths():
    from textual.widgets import OptionList

    from bunnyland.tui.backend import ImageRequestResult

    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        await app.action_request_image()
        activity = app.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("Image requests are not available" in p for p in prompts)

    backend = _ImageBackend(
        _snapshot(),
        ImageRequestResult(ok=False, status="unavailable", reason="camera offline"),
    )
    app2 = BunnylandTUI(backend)
    async with app2.run_test() as pilot:
        await _select_player(app2, pilot)
        await app2.action_request_image()
        activity = app2.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("camera offline" in p for p in prompts)


async def test_app_request_video_capability_and_result_paths():
    from textual.widgets import OptionList

    backend = _VideoBackend(_snapshot(), ImageRequestResult(ok=True, status="queued"))
    app = BunnylandTUI(backend)
    async with app.run_test() as pilot:
        assert app.query("#request-video")
        await app.action_request_video()
        await _select_player(app, pilot)
        await pilot.click("#request-video")
        await pilot.pause()
        prompts = _activity_prompts(app.query_one("#activity", OptionList))
        assert any("Select a character before requesting a video" in p for p in prompts)
        assert any("video requested" in p for p in prompts)
        assert backend.video_requests == [PLAYER]

    unsupported = BunnylandTUI(RecordingBackend(_snapshot()))
    async with unsupported.run_test() as pilot:
        await _select_player(unsupported, pilot)
        await unsupported.action_request_video()
        prompts = _activity_prompts(unsupported.query_one("#activity", OptionList))
        assert any("Video requests are not available" in p for p in prompts)

    for result, expected in (
        (ImageRequestResult(ok=True, status="skipped"), "video ready"),
        (
            ImageRequestResult(ok=False, status="failed", reason="encoder offline"),
            "encoder offline",
        ),
    ):
        app = BunnylandTUI(_VideoBackend(_snapshot(), result))
        async with app.run_test() as pilot:
            await _select_player(app, pilot)
            await app.action_request_video()
            prompts = _activity_prompts(app.query_one("#activity", OptionList))
            assert any(expected in p for p in prompts)


async def test_app_open_sheet_without_player():
    from textual.widgets import OptionList

    from bunnyland.tui.backend import SheetOpenResult

    app = BunnylandTUI(_SheetBackend(_snapshot(), SheetOpenResult(ok=True, url="http://web.test")))
    async with app.run_test():
        assert app.query("#open-sheet")
        await app.action_open_sheet()
        activity = app.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("Select a character before opening a sheet" in p for p in prompts)


async def test_app_open_sheet_unavailable_for_local_backend():
    from textual.widgets import OptionList

    # RecordingBackend inherits the base default -> unavailable.
    app = BunnylandTUI(RecordingBackend(_snapshot()))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        await app.action_open_sheet()
        activity = app.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("Character sheets require a remote server URL" in p for p in prompts)


async def test_app_open_sheet_current_and_selected_character():
    from textual.widgets import OptionList

    from bunnyland.tui.backend import SheetOpenResult

    opened = SheetOpenResult(ok=True, url="http://web.test/character-sheet.html#character:1")
    app = BunnylandTUI(_SheetBackend(_snapshot(), opened))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        await app.action_open_sheet()
        activity = app.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("Opened sheet" in p for p in prompts)
        assert app.backend.sheet_requests == [PLAYER]

    app2 = BunnylandTUI(_SheetBackend(_snapshot(), opened))
    async with app2.run_test() as pilot:
        await _select_player(app2, pilot)
        app2.selected_id = MARLOW
        await app2.action_open_sheet()
        activity = app2.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("Opened sheet" in p for p in prompts)
        assert app2.backend.sheet_requests == [MARLOW]


async def test_app_open_sheet_surfaces_backend_failure():
    from textual.widgets import OptionList

    from bunnyland.tui.backend import SheetOpenResult

    app = BunnylandTUI(_SheetBackend(_snapshot(), SheetOpenResult(ok=False, reason="no browser")))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        await app.action_open_sheet()
        activity = app.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("no browser" in p for p in prompts)


async def test_app_open_sheet_rejects_non_character_selection():
    from textual.widgets import OptionList

    from bunnyland.tui.backend import SheetOpenResult

    app = BunnylandTUI(_SheetBackend(_snapshot(), SheetOpenResult(ok=True, url="http://web.test")))
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        app.selected_id = APPLE
        await app.action_open_sheet()
        activity = app.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("Select a visible character or clear the target" in p for p in prompts)

        app.action_clear_target()
        await app.action_open_sheet()
        assert app.backend.sheet_requests == [PLAYER]


async def test_app_open_sheet_button_press():
    from textual.widgets import OptionList

    from bunnyland.tui.backend import SheetOpenResult

    backend = _SheetBackend(
        _snapshot(),
        SheetOpenResult(ok=True, url="http://web.test/character-sheet.html#character:1"),
    )
    app = BunnylandTUI(backend)
    async with app.run_test() as pilot:
        await _select_player(app, pilot)
        await pilot.click("#open-sheet")
        await pilot.pause()
        activity = app.query_one("#activity", OptionList)
        prompts = _activity_prompts(activity)
        assert any("Opened sheet" in p for p in prompts)
        assert backend.sheet_requests == [PLAYER]


async def test_tui_live_update_worker_suppresses_polling_and_refreshes_events():
    class LiveBackend(RecordingBackend):
        def supports_live_updates(self) -> bool:
            return True

        async def watch_updates(self, character_id, control, on_message, on_state) -> None:
            assert character_id == PLAYER
            assert control == ControlClaim("controller:1", 3, "claim-1", "secret-1")
            await on_state("live")
            await on_message({"type": "heartbeat", "data": {}})
            await on_message({"type": "event", "data": {}})
            await on_state("fallback")

    app = BunnylandTUI(LiveBackend(_snapshot()))
    app.player_id = PLAYER
    app.control = ControlClaim("controller:1", 3, "claim-1", "secret-1")
    refreshes = []

    async def refresh_world():
        refreshes.append(True)

    app.refresh_world = refresh_world
    app._restart_live_updates()
    await app._live_task
    assert refreshes == [True, True]
    assert app._live_ready is False

    app._live_ready = True
    await app._poll_refresh_world()
    assert refreshes == [True, True]
    app._live_ready = False
    await app._poll_refresh_world()
    assert refreshes == [True, True, True]

    sleeping = asyncio.create_task(asyncio.sleep(60))
    app._live_task = sleeping
    app._stop_live_updates()
    assert sleeping.cancelled() or sleeping.cancelling()
    app.player_id = ""
    app._restart_live_updates()
