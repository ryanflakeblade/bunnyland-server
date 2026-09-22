"""Two ways to drive the TUI: host a world in this process, or talk to a running server.

Both expose the same tiny surface — fetch a snapshot, submit a command, claim a player —
so the app never needs to know which one it is using.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
import urllib.parse
import webbrowser
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, JsonValue

from ..claims import (
    CLIENT_KIND_WEB,
    ClaimOwner,
    ClaimSecretRegistry,
    add_claim,
    remove_claim,
    retire_transient_controller,
    spawn_transient_controller,
    transfer_claim,
)
from ..content_warnings import world_content_flags
from ..core import (
    CommandCost,
    Lane,
    LLMControllerComponent,
    OnInsufficientPoints,
    SuspendedComponent,
    SuspendedControllerComponent,
    WebControllerComponent,
    build_submitted_command,
    container_of,
)
from ..core.claim_timeout import apply_claim_timeout_settings
from ..core.ecs import parse_entity_id
from ..secure_files import secure_read_text, secure_write_text
from ..server.auth import WORLD_ADMIN_SCOPE, AuthMeResponse, TokenResponse
from ..server.models import (
    CharacterChatActionResult,
    CharacterChatRequest,
    CharacterListResponse,
    CharacterSummaryView,
    FeatureStatusResponse,
)
from ..server.serialization import (
    serialize_character_list,
    serialize_character_projection,
    serialize_character_queued_commands,
    serialize_room_projection,
    serialize_world,
)
from ..server.v1_models import (
    CharacterProfileResource,
    ChatJobResult,
    ControllerAssignment,
    ImageJobResult,
    JobResource,
    PublicWorldResource,
)
from ..terminal_config import ResolvedTerminalChatConfig, build_terminal_chat_agent
from .model import World

logger = logging.getLogger("bunnyland.tui")

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "bunnyland"
CLIENT_ID_PATH = CONFIG_DIR / "client-id"


@runtime_checkable
class _StatusResponse(Protocol):
    status_code: int


def is_authentication_required(error: BaseException) -> bool:
    """Return whether a terminal-client request failed for missing player auth."""

    response: object = getattr(error, "response", None)
    return isinstance(response, _StatusResponse) and response.status_code == 401


def _validate_remote_server_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("remote server must be an absolute HTTP(S) URL")
    if parsed.scheme == "http":
        try:
            import ipaddress

            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if not loopback:
            raise ValueError("remote non-loopback servers require HTTPS")
    return value.rstrip("/")


def _validate_token_file_mode(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    secure_read_text(path)


async def _call_update_callback(callback: Callable, value) -> None:
    result = callback(value)
    if hasattr(result, "__await__"):
        await result


@dataclass(frozen=True)
class ControlClaim:
    controller_id: str
    generation: int
    claim_id: str = ""
    claim_secret: str = ""
    active: bool = True

    def __iter__(self):
        yield self.controller_id
        yield self.generation

    def __getitem__(self, index: int):
        return (self.controller_id, self.generation)[index]

    def __eq__(self, other) -> bool:
        if isinstance(other, ControlClaim):
            return (
                self.controller_id == other.controller_id
                and self.generation == other.generation
                and self.claim_id == other.claim_id
                and self.claim_secret == other.claim_secret
                and self.active == other.active
            )
        if isinstance(other, (tuple, list)) and len(other) == 2:
            return (self.controller_id, self.generation) == tuple(other)
        return False


def _client_id_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "bunnyland" / "client-id"


def persistent_client_id(path: Path | None = None) -> str:
    path = path or _client_id_path()
    if path.exists():
        try:
            value = secure_read_text(path).strip()
            return str(UUID(value))
        except ValueError:
            logger.warning("Ignoring invalid TUI client id in %s", path, exc_info=True)
        except PermissionError:
            raise
        except (AttributeError, OSError):
            logger.warning("Could not read TUI client id from %s", path, exc_info=True)

    client_id = ""
    # Releases before terminal.yml gave standalone chat a separate identity. Adopt it
    # when possible so saved conversations continue under the shared terminal client id.
    legacy_path = path.parent / "chat-client-id"
    if path == _client_id_path() and legacy_path.exists():
        try:
            client_id = str(UUID(secure_read_text(legacy_path).strip()))
        except (OSError, ValueError):
            client_id = ""
    if not client_id:
        client_id = str(uuid4())
    try:
        secure_write_text(path, f"{client_id}\n")
    except PermissionError:
        raise
    except (AttributeError, OSError):
        logger.warning("Could not persist TUI client id to %s", path, exc_info=True)
    return client_id


def _claim_path(client_id: str, character_id: str) -> Path:
    safe_client = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in client_id)
    safe_character = "".join(ch if ch.isalnum() or ch in "-_:" else "_" for ch in character_id)
    return CONFIG_DIR / "claims" / safe_client / f"{safe_character}.json"


def load_claim_control(client_id: str, character_id: str) -> ControlClaim | None:
    try:
        data = json.loads(secure_read_text(_claim_path(client_id, character_id)))
    except PermissionError:
        raise
    except (AttributeError, OSError, json.JSONDecodeError):
        return None
    if not data.get("claim_id") or not data.get("claim_secret"):
        return None
    return ControlClaim(
        controller_id=str(data.get("controller_id") or ""),
        generation=int(data.get("generation") or 0),
        claim_id=str(data["claim_id"]),
        claim_secret=str(data["claim_secret"]),
        active=bool(data.get("active", True)),
    )


def save_claim_control(client_id: str, character_id: str, control: ControlClaim) -> None:
    if not control.claim_id or not control.claim_secret:
        return
    path = _claim_path(client_id, character_id)
    try:
        secure_write_text(
            path,
            json.dumps(
                {
                    "controller_id": control.controller_id,
                    "generation": control.generation,
                    "claim_id": control.claim_id,
                    "claim_secret": control.claim_secret,
                    "active": control.active,
                },
                sort_keys=True,
            )
            + "\n",
        )
    except PermissionError:
        raise
    except (AttributeError, OSError):
        logger.warning("Could not persist TUI claim data to %s", path, exc_info=True)


def clear_claim_control(client_id: str, character_id: str) -> None:
    try:
        _claim_path(client_id, character_id).unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Could not remove TUI claim data for %s", character_id, exc_info=True)


@dataclass(frozen=True)
class ImageRequestResult:
    """Outcome of a camera (image) request: whether it was accepted, and a status/url."""

    ok: bool
    status: str = ""
    url: str = ""
    reason: str = ""


@dataclass(frozen=True)
class SheetOpenResult:
    """Outcome of opening a browser character sheet from a terminal client."""

    ok: bool
    url: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CharacterChatJob:
    """Backend-neutral state for an asynchronous character-chat exchange."""

    id: str
    status: str
    character_id: str
    reply: str = ""
    action: CharacterChatActionResult = field(default_factory=CharacterChatActionResult)
    failure: str = ""

    @property
    def pending(self) -> bool:
        return self.status in {"queued", "running"}

    @property
    def complete(self) -> bool:
        return self.status in {"succeeded", "failed"}


@dataclass(frozen=True)
class CharacterChatMediaJob:
    """Backend-neutral state for one private chat illustration."""

    id: str
    status: str
    character_id: str
    kind: str
    focus: str = ""
    url: str = ""
    failure: str = ""

    @property
    def pending(self) -> bool:
        return self.status in {"queued", "running"}


@dataclass(frozen=True)
class CharacterChatController:
    """An existing LLM controller that an administrator may assign to a character."""

    controller_id: str
    label: str
    is_default: bool = False


@dataclass(frozen=True)
class CharacterChatAccess:
    """Current send eligibility and optional administrator handoff choices."""

    writable: bool
    reason: str = ""
    controllers: tuple[CharacterChatController, ...] = ()
    activation_controller_id: str = ""

    @property
    def can_assign(self) -> bool:
        return bool(self.controllers)

    @property
    def can_activate(self) -> bool:
        return bool(self.activation_controller_id)


class _AdminSnapshotEntity(BaseModel):
    """Narrow view of the existing admin snapshot used to find LLM controllers."""

    id: str
    components: dict[str, JsonValue] = Field(default_factory=dict)


class _AdminSnapshot(BaseModel):
    entities: list[_AdminSnapshotEntity] = Field(default_factory=list)


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of submitting a command: accepted for queuing, or rejected at submit.

    Mirrors the server's ``SubmissionOutcome`` / ``CommandResponse`` so both the local and
    remote backends report the synchronous rejection ``reason`` to the client UI.
    """

    accepted: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.accepted


class Backend(ABC):
    """A source of world snapshots that also accepts player commands."""

    label: str = ""
    supports_character_sheets: bool = False
    supports_character_chat: bool = False
    supports_image_requests: bool = False
    supports_video_requests: bool = False
    supports_chat_image_requests: bool = False
    supports_chat_video_requests: bool = False
    supports_character_chat_media_tools: bool = False

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def fetch_snapshot(self) -> dict: ...

    async def fetch_character_list(self) -> list[CharacterSummaryView]:
        """The claim lobby — one typed :class:`CharacterSummaryView` per character — used to
        populate the player picker without the admin-gated full snapshot. Remote backends
        re-validate the server JSON back into the shared model, so the client owns
        validation of its server interaction layer."""
        return []

    async def fetch_content_flags(self) -> tuple[str, ...]:
        return ()

    async def fetch_public_world(self) -> PublicWorldResource:
        return PublicWorldResource(
            world_id="",
            world_epoch=0,
            title="",
            description="",
            content_flags=list(await self.fetch_content_flags()),
        )

    @property
    def world_introduction_server(self) -> str:
        return self.label or "unknown"

    async def fetch_character_projection(self, character_id: str) -> dict | None:
        return None

    async def fetch_room_projection(self, room_id: str, character_id: str) -> dict | None:
        return None

    async def fetch_queued_commands(self, character_id: str) -> dict:
        return {
            "ok": True,
            "schema_version": 1,
            "world_epoch": 0,
            "character_id": character_id,
            "generated_at_unix": time.time(),
            "next_tick_at_unix": None,
            "tick_seconds": None,
            "time_scale": None,
            "game_seconds_per_tick": None,
            "commands": [],
        }

    async def cancel_command(
        self,
        character_id: str,
        command_id: str,
        controller_id: str,
        controller_generation: int,
    ) -> bool:
        return False

    @abstractmethod
    async def submit(self, command: dict) -> SubmitResult: ...

    @abstractmethod
    async def claim(self, player_id: str, world: World) -> ControlClaim | None:
        """Return the controller (id, generation) the player should submit commands as."""

    async def release_controller(
        self,
        player_id: str,
        control: ControlClaim,
    ) -> ControlClaim | None:
        return control

    async def release_claim(self, player_id: str, control: ControlClaim) -> bool:
        return False

    async def recent_events(self, character_id: str = "") -> list[dict]:
        """Recent domain-event messages (``{"type": "event", "data": {...}}``) for clients
        that narrate perceived activity. Backends without an event feed return nothing."""
        return []

    def supports_live_updates(self) -> bool:
        return False

    async def watch_updates(
        self,
        character_id: str,
        control: ControlClaim | None,
        on_message: Callable[[dict], object],
        on_state: Callable[[str], object],
    ) -> None:
        """Watch player updates until cancelled. Local backends remain polling-only."""
        return None

    async def request_image(self, character_id: str, event_id: str = "") -> ImageRequestResult:
        """Request an image of the character's current scene (the 📷 camera affordance)."""
        return ImageRequestResult(ok=False, status="unavailable", reason="not available")

    async def request_video(self, character_id: str, event_id: str = "") -> ImageRequestResult:
        """Request a short clip of the character's recent scene events."""
        return ImageRequestResult(ok=False, status="unavailable", reason="not available")

    async def open_character_sheet(self, character_id: str) -> SheetOpenResult:
        """Open the web character-sheet deep link for a character."""
        return SheetOpenResult(ok=False, reason="Character sheets require a remote server URL")

    async def fetch_character_profile(self, character_id: str) -> CharacterProfileResource:
        raise RuntimeError("Character sheets are not available for this session")

    async def character_chat_availability(self) -> tuple[bool, str]:
        if self.supports_character_chat:
            return True, ""
        return False, "Character chat is not available for this session"

    async def allows_sleeping_character_chat(self) -> bool:
        return False

    async def character_chat_access(self, character_id: str) -> CharacterChatAccess:
        """Return whether this character can receive chat and any safe handoff choices."""

        available, reason = await self.character_chat_availability()
        if not available:
            return CharacterChatAccess(writable=False, reason=reason)
        profile = await self.fetch_character_profile(character_id)
        statuses = {status.split(" (", 1)[0].strip().lower() for status in profile.sheet.status}
        if "dead" in statuses:
            return CharacterChatAccess(
                writable=False,
                reason=f"{profile.character_name} is dead and is not available to chat.",
            )
        if "downed" in statuses:
            return CharacterChatAccess(
                writable=False,
                reason=f"{profile.character_name} is unconscious and is not available to chat.",
            )
        if "sleeping" in statuses and not await self.allows_sleeping_character_chat():
            return CharacterChatAccess(
                writable=False,
                reason=f"{profile.character_name} is sleeping and cannot be interrupted by chat.",
            )
        controller = profile.controller
        if controller is not None and controller.kind == "llm":
            return CharacterChatAccess(writable=True)
        kind = controller.kind if controller is not None and controller.kind else "no controller"
        controllers = await self.assignable_character_chat_controllers()
        default = next((choice for choice in controllers if choice.is_default), None)
        return CharacterChatAccess(
            writable=False,
            reason=(f"{profile.character_name} has {kind}; existing chat history is read-only."),
            controllers=controllers,
            activation_controller_id=(
                default.controller_id if "suspended" in statuses and default is not None else ""
            ),
        )

    async def assignable_character_chat_controllers(
        self,
    ) -> tuple[CharacterChatController, ...]:
        """Return existing LLM controllers this session may assign, if any."""

        return ()

    async def assign_character_chat_controller(
        self,
        character_id: str,
        controller_id: str,
    ) -> CharacterChatAccess:
        del character_id, controller_id
        raise PermissionError("This session cannot assign character controllers")

    async def submit_character_chat(
        self,
        character_id: str,
        message: str,
        *,
        history_summary: str = "",
        history: list[dict] | None = None,
        allow_character_media: bool = False,
    ) -> CharacterChatJob:
        del character_id, message, history_summary, history, allow_character_media
        raise RuntimeError("Character chat is not available for this session")

    async def poll_character_chat(self, job: CharacterChatJob) -> CharacterChatJob:
        return job

    async def cancel_character_chat(self, job: CharacterChatJob) -> None:
        del job

    async def request_character_chat_media(
        self,
        character_id: str,
        kind: str,
        *,
        focus: str = "",
        history_summary: str = "",
        history: list[dict] | None = None,
    ) -> CharacterChatMediaJob:
        del character_id, kind, focus, history_summary, history
        raise RuntimeError("Chat media is not available for this session")

    async def poll_character_chat_media(
        self, job: CharacterChatMediaJob
    ) -> CharacterChatMediaJob:
        return job


def frontend_base_for_api(api_base: str) -> str:
    """Best-effort frontend origin for an API base URL.

    Deployed Bunnyland serves the web bundle at the same origin as ``/api/v1``. Raw
    server URLs ending in ``/v1`` still produce a stable same-origin deep link.
    """
    parsed = urllib.parse.urlparse(api_base.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path in {"/api/v1", "/v1", "/api"}:
        path = ""
    elif path.endswith("/api/v1"):
        path = path[: -len("/api/v1")]
    elif path.endswith("/api"):
        path = path[: -len("/api")]
    stripped = parsed._replace(path=path or "", params="", query="", fragment="")
    return urllib.parse.urlunparse(stripped).rstrip("/")


def character_sheet_url(api_base: str, character_id: str) -> str:
    frontend = frontend_base_for_api(api_base)
    query = urllib.parse.urlencode({"server": api_base.rstrip("/")})
    fragment = urllib.parse.quote(character_id, safe=":")
    return f"{frontend}/character.html?{query}#{fragment}"


class LocalBackend(Backend):
    """Generate an offline world and tick it in-process, the TUI as a real player."""

    supports_character_sheets = True

    def __init__(
        self,
        *,
        seed: str = "a quiet marsh",
        generator: str = "apartment-demo",
        tick_seconds: float = 1.0,
        time_scale: float = 3600.0,
        autorun: bool = True,
        client_id: str | None = None,
        fallback_controller: str | None = None,
        timeout_seconds: int | None = None,
        chat_config: ResolvedTerminalChatConfig | None = None,
        autonomous_llm: bool = False,
    ) -> None:
        self.seed = seed
        self.generator_name = generator
        self.tick_seconds = tick_seconds
        self.time_scale = time_scale
        self.autorun = autorun
        self.label = f"local · {generator}"
        self.actor = None
        self.meta = None
        self._loop = None
        self._task: asyncio.Task | None = None
        self._controller = None
        self._claim_secrets = ClaimSecretRegistry()
        self._events = None
        self.client_id = client_id or persistent_client_id()
        self.fallback_controller = fallback_controller
        self.timeout_seconds = timeout_seconds
        self.imagegen = None
        self.videogen = None
        self.chat_config = chat_config
        self.autonomous_llm = autonomous_llm
        self.character_chat = None
        self._dialogue = None

    @property
    def supports_character_chat(self) -> bool:
        return bool(self.chat_config and self.chat_config.enabled)

    async def allows_sleeping_character_chat(self) -> bool:
        return bool(
            self.character_chat is not None
            and self.character_chat.allow_sleeping_character_chat
        )

    @property
    def supports_image_requests(self) -> bool:
        return self.imagegen is not None

    @property
    def supports_video_requests(self) -> bool:
        return self.videogen is not None

    def configure_world(self, *, seed: str, generator: str) -> None:
        """Update local generation inputs before ``start`` creates the world."""
        self.seed = seed
        self.generator_name = generator
        self.label = f"local · {generator}"

    async def start(self) -> None:
        # Imported here so the optional server/llm wiring is only pulled when hosting.
        from ..core.world_actor import WorldActor
        from ..engine import GameLoop
        from ..llm_agents import ControllerDispatch, ScriptedAgent
        from ..persistence import WorldMeta
        from ..plugins import (
            apply_plugins,
            bunnyland_plugins,
            collect_persona_fragments,
            collect_prompt_fragments,
            select,
        )
        from ..prompts.builder import PromptBuilder
        from ..server.subscriptions import EventStream
        from ..worldgen import GenOptions, collect_generators

        plugins = select(list(bunnyland_plugins()), None)
        self.actor = WorldActor()
        apply_plugins(plugins, self.actor)
        self._events = EventStream(self.actor)  # record events for clients that narrate them

        registry = collect_generators(plugins)
        generator = registry.get(self.generator_name)
        if generator is None:
            names = ", ".join(sorted(registry)) or "(none)"
            raise SystemExit(f"unknown generator {self.generator_name!r}; available: {names}")
        if self.autonomous_llm and not self.supports_character_chat:
            raise RuntimeError("autonomous local LLMs require terminal chat configuration")
        generation_options = GenOptions()
        if self.autonomous_llm:
            assert self.chat_config is not None
            provider = (
                "ollama"
                if self.chat_config.provider in {"ollama-local", "ollama-cloud"}
                else "openrouter"
            )
            generation_options = GenOptions(
                llm=True,
                provider=provider,
                model=self.chat_config.model,
                host=self.chat_config.ollama_host,
                api_key=self.chat_config.api_key or None,
                server_url=self.chat_config.openrouter_server_url,
            )
        await generator.generate(self.actor, self.seed, generation_options)
        self.meta = WorldMeta(seed=self.seed, generator=generator.name)
        if self.generator_name == "wild-boar-forest":
            from ..dialogue import DialogueRecorder

            self._dialogue = DialogueRecorder(
                self.actor, Path("dialogue.jsonl"), world_id=self.meta.world_id
            )

        builder = PromptBuilder(
            self.actor.world,
            fragment_providers=collect_prompt_fragments(plugins),
            persona_providers=collect_persona_fragments(plugins),
        )
        terminal_agent = (
            build_terminal_chat_agent(self.chat_config)
            if self.supports_character_chat
            else None
        )
        from ..llm_agents.player_turn_dispatch import PlayerTurnDispatch

        dispatch_type = (
            PlayerTurnDispatch
            if self.autonomous_llm and self.generator_name == "wild-boar-forest"
            else ControllerDispatch
        )
        dispatch = dispatch_type(
            self.actor,
            builder,
            terminal_agent if self.autonomous_llm else ScriptedAgent([]),
        )
        if self.supports_character_chat:
            from ..server.character_chat import CharacterChatService

            assert terminal_agent is not None
            self.character_chat = CharacterChatService(
                self.actor,
                builder,
                terminal_agent,
            )
        self._loop = GameLoop(
            self.actor, dispatch, tick_seconds=self.tick_seconds, time_scale=self.time_scale
        )
        if self.autorun:
            self._task = asyncio.create_task(self._loop.run())

    async def close(self) -> None:
        if self._loop is not None:
            self._loop.stop()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._dialogue is not None:
            self._dialogue.close()

    async def fetch_snapshot(self) -> dict:
        return serialize_world(self.actor, self.meta)

    async def fetch_character_list(self) -> list[CharacterSummaryView]:
        return list(serialize_character_list(self.actor).characters)

    async def fetch_content_flags(self) -> tuple[str, ...]:
        return world_content_flags(self.actor)

    async def fetch_public_world(self) -> PublicWorldResource:
        if self.actor is None or self.meta is None:
            return await super().fetch_public_world()
        return PublicWorldResource(
            world_id=self.meta.world_id,
            world_epoch=self.actor.epoch,
            title=self.actor.world_info.title,
            description=self.actor.world_info.description,
            content_flags=list(await self.fetch_content_flags()),
        )

    @property
    def world_introduction_server(self) -> str:
        return "local"

    async def fetch_character_projection(self, character_id: str) -> dict | None:
        return serialize_character_projection(self.actor, character_id).model_dump(mode="json")

    async def fetch_character_profile(self, character_id: str) -> CharacterProfileResource:
        projection = serialize_character_projection(self.actor, character_id)
        return CharacterProfileResource(
            world_id=self.meta.world_id,
            world_epoch=self.actor.epoch,
            character_id=projection.character_id,
            character_name=projection.character_name,
            portrait=projection.portrait,
            room=projection.room.model_copy(update={"entities": [], "exits": []}),
            points=projection.points,
            controller=projection.controller,
            sheet=projection.sheet,
        )

    async def assignable_character_chat_controllers(
        self,
    ) -> tuple[CharacterChatController, ...]:
        if self.actor is None:
            return ()
        choices = []
        for controller in (
            self.actor.world.query().with_all([LLMControllerComponent]).execute_entities()
        ):
            llm = controller.get_component(LLMControllerComponent)
            detail = f"{llm.provider}/{llm.model}" if llm.model else llm.provider
            label = f"{llm.profile_name} ({detail})" if detail else llm.profile_name
            choices.append(
                CharacterChatController(
                    str(controller.id),
                    label,
                    is_default=llm.profile_name == "default",
                )
            )
        return tuple(
            sorted(choices, key=lambda choice: (choice.label.lower(), choice.controller_id))
        )

    async def assign_character_chat_controller(
        self,
        character_id: str,
        controller_id: str,
    ) -> CharacterChatAccess:
        if self.actor is None:
            raise RuntimeError("local backend has not started")
        parsed_character = parse_entity_id(character_id)
        parsed_controller = parse_entity_id(controller_id)
        if parsed_character is None or not self.actor.world.has_entity(parsed_character):
            raise ValueError("character does not exist")
        if parsed_controller is None or not self.actor.world.has_entity(parsed_controller):
            raise ValueError("controller does not exist")
        controller = self.actor.world.get_entity(parsed_controller)
        if not controller.has_component(LLMControllerComponent):
            raise ValueError("controller is not an LLM controller")
        async with self.actor._lock:
            self.actor.assign_controller(parsed_character, parsed_controller)
            character = self.actor.world.get_entity(parsed_character)
            if character.has_component(SuspendedComponent):
                character.remove_component(SuspendedComponent)
        return await self.character_chat_access(character_id)

    async def submit_character_chat(
        self,
        character_id: str,
        message: str,
        *,
        history_summary: str = "",
        history: list[dict] | None = None,
        allow_character_media: bool = False,
    ) -> CharacterChatJob:
        if self.character_chat is None:
            raise RuntimeError("Character chat is disabled for this local session")
        response = await self.character_chat.chat(
            character_id,
            CharacterChatRequest(
                client_id=self.client_id,
                message=message,
                history_summary=history_summary,
                history=history or [],
                allow_character_media=allow_character_media,
            ),
        )
        pending = response.action.status == "queued" and bool(response.action.command_id)
        return CharacterChatJob(
            id=response.action.command_id or uuid4().hex,
            status="running" if pending else "succeeded",
            character_id=character_id,
            reply=response.reply,
            action=response.action,
        )

    async def poll_character_chat(self, job: CharacterChatJob) -> CharacterChatJob:
        if not job.pending or not job.action.command_id:
            return job
        if self.character_chat is None:
            return CharacterChatJob(
                id=job.id,
                status="failed",
                character_id=job.character_id,
                failure="Character chat is disabled for this local session",
            )
        response = await self.character_chat.pending_result(
            job.character_id,
            self.client_id,
            job.action.command_id,
        )
        return CharacterChatJob(
            id=job.id,
            status="succeeded" if response.complete else "running",
            character_id=job.character_id,
            reply=response.reply or job.reply,
            action=response.action,
        )

    async def fetch_room_projection(self, room_id: str, character_id: str) -> dict | None:
        character = self.actor.world.get_entity(parse_entity_id(character_id))
        if container_of(character) != parse_entity_id(room_id):
            raise PermissionError("room is not currently visible to character")
        return serialize_room_projection(self.actor, room_id).model_dump(mode="json")

    async def fetch_queued_commands(self, character_id: str) -> dict:
        now = time.time()
        tick_seconds = getattr(self._loop, "tick_seconds", None) if self._loop is not None else None
        time_scale = getattr(self._loop, "time_scale", None) if self._loop is not None else None
        next_tick_at_unix = (
            getattr(self._loop, "next_tick_at_unix", None) if self._loop is not None else None
        )
        if (
            next_tick_at_unix is None
            and tick_seconds is not None
            and self._loop is not None
            and self._loop.running
            and not self._loop.paused
        ):
            next_tick_at_unix = now + float(tick_seconds)
        return serialize_character_queued_commands(
            self.actor,
            character_id,
            generated_at_unix=now,
            next_tick_at_unix=next_tick_at_unix,
            tick_seconds=float(tick_seconds) if tick_seconds is not None else None,
            time_scale=float(time_scale) if time_scale is not None else None,
            game_seconds_per_tick=(
                float(tick_seconds) * float(time_scale)
                if tick_seconds is not None and time_scale is not None
                else None
            ),
        ).model_dump(mode="json")

    async def cancel_command(
        self,
        character_id: str,
        command_id: str,
        controller_id: str,
        controller_generation: int,
    ) -> bool:
        controller = parse_entity_id(controller_id)
        character = parse_entity_id(character_id)
        if (
            character is None
            or controller is None
            or self.actor.current_generation(character, controller) != controller_generation
        ):
            return False
        return await self.actor.cancel_command(character_id, command_id) is not None

    async def submit(self, command: dict) -> SubmitResult:
        cost = command.get("cost") or {}
        outcome = await self.actor.submit(
            build_submitted_command(
                character_id=command["character_id"],
                controller_id=command["controller_id"],
                controller_generation=command["controller_generation"],
                command_type=command["command_type"],
                payload=command.get("payload") or {},
                cost=CommandCost(action=cost.get("action", 0), focus=cost.get("focus", 0)),
                lane=Lane(command.get("lane", "world")),
                on_insufficient_points=OnInsufficientPoints(
                    command.get("on_insufficient_points", "queue")
                ),
                submitted_at_epoch=self.actor.epoch,
            )
        )
        return SubmitResult(accepted=outcome.accepted, reason=outcome.reason)

    async def recent_events(self, character_id: str = "") -> list[dict]:
        return self._events.recent_messages() if self._events is not None else []

    async def request_image(self, character_id: str, event_id: str = "") -> ImageRequestResult:
        if self.imagegen is None:
            return ImageRequestResult(
                ok=False, status="unavailable", reason="image generation is not configured"
            )
        from ..imagegen.scene import request_scene_image

        job = await request_scene_image(
            self.actor,
            self.imagegen,
            character_id=character_id,
            event_id=event_id,
        )
        if job is None:
            return ImageRequestResult(
                ok=False, status="no-room", reason="your character has no room to illustrate"
            )
        return ImageRequestResult(ok=True, status=job.status, url=job.url)

    async def request_video(self, character_id: str, event_id: str = "") -> ImageRequestResult:
        if not self.supports_video_requests:
            return ImageRequestResult(
                ok=False, status="unavailable", reason="video generation is not configured"
            )
        from ..imagegen.scene import request_scene_video

        job = await request_scene_video(
            self.actor,
            self.videogen,
            character_id=character_id,
            event_id=event_id,
        )
        if job is None:
            return ImageRequestResult(
                ok=False, status="no-room", reason="your character has no room to illustrate"
            )
        return ImageRequestResult(ok=True, status=job.status, url=job.url)

    async def claim(self, player_id: str, world: World) -> ControlClaim | None:
        """Hand the character to a single reusable web controller, bumping its generation
        so the offline dispatch stops driving it."""
        async with self.actor._lock:
            stored = load_claim_control(self.client_id, player_id)
            owner = ClaimOwner(CLIENT_KIND_WEB, f"local:{self.client_id}")
            stored_valid = (
                stored
                if stored
                and self._claim_secrets.validate(
                    stored.claim_id,
                    stored.claim_secret,
                    owner,
                )
                else None
            )
            if self._controller is None:
                self._controller = spawn_transient_controller(
                    self.actor.world,
                    WebControllerComponent(client_id=self.client_id, label="tui"),
                )
            claim = add_claim(
                self._controller,
                client_kind=CLIENT_KIND_WEB,
                client_id=self.client_id,
                character_id=player_id,
                label="tui",
                claim_id=stored_valid.claim_id if stored_valid else None,
                now_unix=int(time.time()),
            )
            claim_secret = (
                stored_valid.claim_secret
                if stored_valid is not None
                else self._claim_secrets.issue(claim.claim_id, owner)
            )
            apply_claim_timeout_settings(
                self._controller,
                now_unix=int(time.time()),
                fallback_controller=self.fallback_controller,
                timeout_seconds=self.timeout_seconds,
                reset_activity=True,
            )
            generation = self.actor.assign_controller(
                parse_entity_id(player_id), self._controller.id
            )
            character = self.actor.world.get_entity(parse_entity_id(player_id))
            if character.has_component(SuspendedComponent):
                character.remove_component(SuspendedComponent)
        control = ControlClaim(
            controller_id=str(self._controller.id),
            generation=generation,
            claim_id=claim.claim_id,
            claim_secret=claim_secret,
            active=True,
        )
        save_claim_control(self.client_id, player_id, control)
        return control

    async def release_controller(
        self,
        player_id: str,
        control: ControlClaim,
    ) -> ControlClaim | None:
        async with self.actor._lock:
            character_id = parse_entity_id(player_id)
            if character_id is None or not self.actor.world.has_entity(character_id):
                return None
            old_controller_id = parse_entity_id(control.controller_id)
            if old_controller_id is None or not self.actor.world.has_entity(old_controller_id):
                return None
            old_controller = self.actor.world.get_entity(old_controller_id)
            fallback = (self.fallback_controller or "suspend").strip()
            parsed_fallback = parse_entity_id(fallback)
            if parsed_fallback is not None and self.actor.world.has_entity(parsed_fallback):
                new_controller = self.actor.world.get_entity(parsed_fallback)
                kind = self.actor._controller_kind(parsed_fallback)
                if kind == "unknown":
                    return None
            elif fallback == "llm":
                new_controller = spawn_transient_controller(
                    self.actor.world,
                    LLMControllerComponent(
                        profile_name="default",
                        model=(
                            self.chat_config.model
                            if self.chat_config is not None
                            else os.environ.get("BUNNYLAND_CHARACTER_MODEL", "deepseek-v4-flash")
                        ),
                        provider=(
                            "openrouter"
                            if self.chat_config is not None
                            and self.chat_config.provider == "openrouter"
                            else "ollama"
                        ),
                    ),
                )
                kind = "llm"
            else:
                new_controller = spawn_transient_controller(
                    self.actor.world,
                    SuspendedControllerComponent(reason="released by TUI client"),
                )
                kind = "suspended"
            transfer_claim(old_controller, new_controller)
            character = self.actor.world.get_entity(character_id)
            if kind == "suspended":
                generation = self.actor.suspend(
                    character_id,
                    new_controller.id,
                    reason="released by TUI client",
                )
            else:
                generation = self.actor.assign_controller(character_id, new_controller.id)
                if character.has_component(SuspendedComponent):
                    character.remove_component(SuspendedComponent)
            retire_transient_controller(self.actor, old_controller_id)
            if self._controller is not None and self._controller.id == old_controller_id:
                self._controller = None
        released = ControlClaim(
            controller_id=str(new_controller.id),
            generation=generation,
            claim_id=control.claim_id,
            claim_secret=control.claim_secret,
            active=False,
        )
        save_claim_control(self.client_id, player_id, released)
        return released

    async def release_claim(self, player_id: str, control: ControlClaim) -> bool:
        controller_id = parse_entity_id(control.controller_id)
        if controller_id is None or not self.actor.world.has_entity(controller_id):
            return False
        remove_claim(self.actor.world.get_entity(controller_id), self._claim_secrets)
        clear_claim_control(self.client_id, player_id)
        return True


class RemoteBackend(Backend):
    """Poll a running server over HTTP for snapshots and post commands to it."""

    supports_character_sheets = True
    supports_character_chat = True
    supports_image_requests = True
    supports_video_requests = False

    def __init__(
        self,
        base_url: str,
        *,
        client_id: str | None = None,
        fallback_controller: str | None = None,
        timeout_seconds: int | None = None,
        username: str = "",
        password: str = "",
        token_file: str | Path | None = None,
        access_token: str = "",
    ) -> None:
        self.base = _validate_remote_server_url(base_url)
        self.label = f"remote · {self.base}"
        self._client = None
        self.client_id = client_id or persistent_client_id()
        self.fallback_controller = fallback_controller
        self.timeout_seconds = timeout_seconds
        self.username = username
        self._password = password
        self.token_file = Path(token_file) if token_file else None
        if self.token_file is not None:
            _validate_token_file_mode(self.token_file)
        self._access_token = access_token.strip()
        self._auth_scopes: frozenset[str] = frozenset()
        self._rotate_after: int | None = None
        self._rotation_task = None
        self._features: FeatureStatusResponse | None = None
        self._claims: dict[str, ControlClaim] = {}
        self._claim_get_tasks: dict[tuple[str, str, str, str], asyncio.Task[dict | None]] = {}

    def _claim_for(self, character_id: str) -> ControlClaim | None:
        return self._claims.get(character_id) or load_claim_control(self.client_id, character_id)

    def _forget_claim(self, character_id: str, expected: ControlClaim | None = None) -> None:
        current = self._claim_for(character_id)
        if expected is not None and (
            current is None
            or current.claim_id != expected.claim_id
            or current.claim_secret != expected.claim_secret
        ):
            return
        self._claims.pop(character_id, None)
        clear_claim_control(self.client_id, character_id)

    def _claim_headers(self, character_id: str) -> dict[str, str]:
        claim = self._claim_for(character_id)
        return (
            {"X-Bunnyland-Claim-Secret": claim.claim_secret} if claim and claim.claim_secret else {}
        )

    def _claim_request_kwargs(self, character_id: str) -> dict:
        kwargs = {}
        claim_headers = self._claim_headers(character_id)
        if claim_headers:
            kwargs["headers"] = claim_headers
        return kwargs

    async def _fetch_claim_resource(self, character_id: str, resource: str) -> dict | None:
        claim = self._claim_for(character_id)
        if claim is None or not claim.claim_id:
            return None
        key = (character_id, resource, claim.claim_id, claim.claim_secret)
        task = self._claim_get_tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                self._fetch_claim_resource_once(character_id, resource, claim)
            )
            self._claim_get_tasks[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._claim_get_tasks.get(key) is task:
                self._claim_get_tasks.pop(key, None)

    async def _fetch_claim_resource_once(
        self, character_id: str, resource: str, claim: ControlClaim
    ) -> dict | None:
        kwargs = (
            {"headers": {"X-Bunnyland-Claim-Secret": claim.claim_secret}}
            if claim.claim_secret
            else {}
        )
        res = await self._client.get(
            f"{self.base}/play/claims/{urllib.parse.quote(claim.claim_id, safe='')}/{resource}",
            **kwargs,
        )
        if getattr(res, "status_code", None) == 404:
            self._forget_claim(character_id, claim)
            return None
        res.raise_for_status()
        return res.json()

    async def start(self) -> None:
        import httpx

        self._client = httpx.AsyncClient(timeout=10.0)
        self._client.headers["X-Bunnyland-Client-Id"] = self.client_id
        features = await self._fetch_features()
        self.supports_character_sheets = features.character_sheets
        self.supports_image_requests = features.image_generation
        self.supports_video_requests = features.video_generation
        self.supports_chat_image_requests = features.chat_image_generation
        self.supports_chat_video_requests = features.chat_video_generation
        self.supports_character_chat_media_tools = features.character_chat_media_tools
        if self._access_token:
            self._set_access_token(self._access_token)
            await self._refresh_auth_metadata()
        elif self.token_file is not None and self.token_file.exists():
            self._access_token = secure_read_text(self.token_file).strip()
            self._set_access_token(self._access_token)
            await self._refresh_auth_metadata()
        elif self.username and self._password:
            await self.sign_in(self.username, self._password)
        if self._access_token:
            self._start_rotation_loop()

    async def close(self) -> None:
        if self._rotation_task is not None:
            self._rotation_task.cancel()
            try:
                await self._rotation_task
            except asyncio.CancelledError:
                pass
        pending_claim_gets = tuple(self._claim_get_tasks.values())
        for task in pending_claim_gets:
            task.cancel()
        if pending_claim_gets:
            await asyncio.gather(*pending_claim_gets, return_exceptions=True)
        self._claim_get_tasks.clear()
        if self._client is not None:
            await self._client.aclose()

    def _set_access_token(self, token: str) -> None:
        self._access_token = token
        if self._client is not None:
            self._client.headers["Authorization"] = f"Bearer {token}"

    def _persist_access_token(self) -> None:
        if self.token_file is None:
            return
        secure_write_text(self.token_file, f"{self._access_token}\n")

    def _start_rotation_loop(self) -> None:
        if self._rotation_task is None or self._rotation_task.done():
            self._rotation_task = asyncio.create_task(self._rotation_loop())

    async def sign_in(self, username: str, password: str) -> None:
        """Authenticate an already-started remote session and retain its bearer token."""

        if self._client is None:
            raise RuntimeError("remote backend must be started before signing in")
        self.username = username.strip()
        self._password = password
        await self._login()
        self._start_rotation_loop()

    async def _login(self) -> None:
        try:
            res = await self._client.post(
                f"{self.base}/auth/session",
                json={"username": self.username, "password": self._password, "delivery": "body"},
            )
        finally:
            self._password = ""
        res.raise_for_status()
        body = TokenResponse.model_validate(res.json())
        if body.token is None:
            raise RuntimeError("login did not return a bearer token")
        self._set_access_token(body.token)
        self._auth_scopes = frozenset(body.scopes)
        self._rotate_after = body.rotate_after
        self._persist_access_token()

    async def _refresh_auth_metadata(self) -> None:
        res = await self._client.get(f"{self.base}/auth/session")
        res.raise_for_status()
        body = AuthMeResponse.model_validate(res.json())
        self._auth_scopes = frozenset(body.scopes)
        self._rotate_after = body.rotate_after

    async def _rotate_token(self) -> None:
        res = await self._client.patch(f"{self.base}/auth/session")
        if res.status_code == 409:
            await self._refresh_auth_metadata()
            return
        res.raise_for_status()
        body = TokenResponse.model_validate(res.json())
        if body.token is None:
            raise RuntimeError("token rotation did not return a bearer token")
        self._set_access_token(body.token)
        self._auth_scopes = frozenset(body.scopes)
        self._rotate_after = body.rotate_after
        self._persist_access_token()

    async def _rotation_loop(self) -> None:
        while True:
            delay = 60.0
            if self._rotate_after is not None:
                delay = max(0.0, min(60.0, self._rotate_after - time.time()))
            await asyncio.sleep(delay)
            if self._rotate_after is not None and time.time() >= self._rotate_after:
                try:
                    await self._rotate_token()
                except Exception:
                    logger.warning("Could not rotate Bunnyland access token", exc_info=True)
                    await asyncio.sleep(30.0)

    async def fetch_snapshot(self) -> dict:
        res = await self._client.get(f"{self.base}/admin/world/snapshot")
        res.raise_for_status()
        return res.json()

    async def fetch_character_list(self) -> list[CharacterSummaryView]:
        res = await self._client.get(f"{self.base}/play/characters")
        res.raise_for_status()
        data = res.json()
        return list(
            CharacterListResponse.model_validate(
                {
                    "world_epoch": data.get("world_epoch", 0),
                    "characters": [
                        {**item, "character_id": item.get("id", "")}
                        for item in data.get("characters", [])
                    ],
                }
            ).characters
        )

    async def fetch_content_flags(self) -> tuple[str, ...]:
        return tuple((await self.fetch_public_world()).content_flags)

    async def fetch_public_world(self) -> PublicWorldResource:
        res = await self._client.get(f"{self.base}/public/world")
        res.raise_for_status()
        return PublicWorldResource.model_validate(res.json())

    @property
    def world_introduction_server(self) -> str:
        return self.base

    async def fetch_character_projection(self, character_id: str) -> dict | None:
        data = await self._fetch_claim_resource(character_id, "projection")
        if data is None:
            return None
        return {
            **data.get("character", {}),
            "world_epoch": data.get("world_epoch", 0),
            "sheet": data.get("sheet", {}),
            "actions": data.get("actions", []),
            "queued_commands": data.get("commands", []),
        }

    async def fetch_character_profile(self, character_id: str) -> CharacterProfileResource:
        res = await self._client.get(
            f"{self.base}/profile/characters/{urllib.parse.quote(character_id, safe='')}"
        )
        res.raise_for_status()
        return CharacterProfileResource.model_validate(res.json())

    async def assignable_character_chat_controllers(
        self,
    ) -> tuple[CharacterChatController, ...]:
        if WORLD_ADMIN_SCOPE not in self._auth_scopes:
            return ()
        res = await self._client.get(f"{self.base}/admin/world/snapshot")
        res.raise_for_status()
        snapshot = _AdminSnapshot.model_validate(res.json())
        choices = []
        for entity in snapshot.entities:
            raw_llm = entity.components.get("LLMControllerComponent")
            if not isinstance(raw_llm, dict):
                continue
            profile_name = raw_llm.get("profile_name")
            model = raw_llm.get("model")
            provider = raw_llm.get("provider")
            profile_label = profile_name if isinstance(profile_name, str) else entity.id
            detail_parts = [
                value for value in (provider, model) if isinstance(value, str) and value
            ]
            detail = "/".join(detail_parts)
            label = f"{profile_label} ({detail})" if detail else profile_label
            choices.append(
                CharacterChatController(
                    entity.id,
                    label,
                    is_default=profile_name == "default",
                )
            )
        return tuple(
            sorted(choices, key=lambda choice: (choice.label.lower(), choice.controller_id))
        )

    async def assign_character_chat_controller(
        self,
        character_id: str,
        controller_id: str,
    ) -> CharacterChatAccess:
        if WORLD_ADMIN_SCOPE not in self._auth_scopes:
            raise PermissionError("Administrator scope is required to assign a controller")
        res = await self._client.put(
            f"{self.base}/admin/characters/{urllib.parse.quote(character_id, safe='')}/controller",
            json=ControllerAssignment(controller_id=controller_id).model_dump(mode="json"),
        )
        res.raise_for_status()
        return await self.character_chat_access(character_id)

    async def character_chat_availability(self) -> tuple[bool, str]:
        features = await self._fetch_features()
        if features.character_chat:
            return True, ""
        return False, "Character chat is not enabled on this server"

    async def _fetch_features(self) -> FeatureStatusResponse:
        if self._features is not None:
            return self._features
        res = await self._client.get(f"{self.base}/public/features")
        res.raise_for_status()
        self._features = FeatureStatusResponse.model_validate(res.json())
        return self._features

    async def allows_sleeping_character_chat(self) -> bool:
        return (await self._fetch_features()).allow_sleeping_character_chat

    @staticmethod
    def _character_chat_job(resource: JobResource, character_id: str) -> CharacterChatJob:
        result = resource.result
        if result is None:
            reply = ""
            action = CharacterChatActionResult()
        elif isinstance(result, ChatJobResult):
            reply = result.reply
            action = result.action
        else:
            raise ValueError("chat job returned a non-chat result")
        failure = resource.failure.detail if resource.failure is not None else ""
        return CharacterChatJob(
            id=resource.id,
            status=resource.status,
            character_id=character_id,
            reply=reply,
            action=action,
            failure=failure,
        )

    async def submit_character_chat(
        self,
        character_id: str,
        message: str,
        *,
        history_summary: str = "",
        history: list[dict] | None = None,
        allow_character_media: bool = False,
    ) -> CharacterChatJob:
        res = await self._client.post(
            f"{self.base}/chat/characters/{urllib.parse.quote(character_id, safe='')}/jobs",
            json={
                "kind": "chat",
                "message": message,
                "history_summary": history_summary,
                "history": (history or [])[-24:],
                "allow_character_media": allow_character_media,
            },
        )
        res.raise_for_status()
        return self._character_chat_job(JobResource.model_validate(res.json()), character_id)

    async def poll_character_chat(self, job: CharacterChatJob) -> CharacterChatJob:
        if not job.pending:
            return job
        res = await self._client.get(
            f"{self.base}/chat/characters/"
            f"{urllib.parse.quote(job.character_id, safe='')}/jobs/"
            f"{urllib.parse.quote(job.id, safe='')}"
        )
        res.raise_for_status()
        return self._character_chat_job(
            JobResource.model_validate(res.json()),
            job.character_id,
        )

    @staticmethod
    def _character_chat_media_job(
        resource: JobResource,
        character_id: str,
        focus: str,
    ) -> CharacterChatMediaJob:
        result = resource.result
        url = result.url if isinstance(result, ImageJobResult) else ""
        failure = resource.failure.detail if resource.failure is not None else ""
        return CharacterChatMediaJob(
            id=resource.id,
            status=resource.status,
            character_id=character_id,
            kind=resource.kind,
            focus=focus,
            url=url,
            failure=failure,
        )

    async def request_character_chat_media(
        self,
        character_id: str,
        kind: str,
        *,
        focus: str = "",
        history_summary: str = "",
        history: list[dict] | None = None,
    ) -> CharacterChatMediaJob:
        res = await self._client.post(
            f"{self.base}/chat/characters/"
            f"{urllib.parse.quote(character_id, safe='')}/media-jobs",
            json={
                "kind": kind,
                "focus": focus,
                "history_summary": history_summary,
                "history": (history or [])[-24:],
            },
        )
        res.raise_for_status()
        return self._character_chat_media_job(
            JobResource.model_validate(res.json()), character_id, focus
        )

    async def poll_character_chat_media(
        self, job: CharacterChatMediaJob
    ) -> CharacterChatMediaJob:
        if not job.pending:
            return job
        res = await self._client.get(
            f"{self.base}/chat/characters/"
            f"{urllib.parse.quote(job.character_id, safe='')}/media-jobs/"
            f"{urllib.parse.quote(job.id, safe='')}"
        )
        res.raise_for_status()
        return self._character_chat_media_job(
            JobResource.model_validate(res.json()), job.character_id, job.focus
        )

    async def fetch_room_projection(self, room_id: str, character_id: str) -> dict | None:
        del room_id
        data = await self._fetch_claim_resource(character_id, "projection")
        return data.get("scene") if data is not None else None

    async def fetch_queued_commands(self, character_id: str) -> dict:
        data = await self._fetch_claim_resource(character_id, "projection")
        if data is None:
            return {"character_id": character_id, "commands": []}
        return {
            "character_id": character_id,
            "world_epoch": data.get("world_epoch", 0),
            "commands": data.get("commands", []),
        }

    async def cancel_command(
        self,
        character_id: str,
        command_id: str,
        controller_id: str,
        controller_generation: int,
    ) -> bool:
        del controller_id, controller_generation
        claim = self._claim_for(character_id)
        if claim is None or not claim.claim_id:
            return False
        kwargs = self._claim_request_kwargs(character_id)
        res = await self._client.delete(
            f"{self.base}/play/claims/{urllib.parse.quote(claim.claim_id, safe='')}/commands/"
            f"{urllib.parse.quote(command_id, safe='')}",
            **kwargs,
        )
        if not res.is_success:
            return False
        return res.json().get("status") == "cancelled"

    async def submit(self, command: dict) -> SubmitResult:
        character_id = str(command.get("character_id") or "")
        claim = self._claim_for(character_id)
        if claim is None or not claim.claim_id:
            return SubmitResult(accepted=False, reason="a character claim is required")
        command = {
            key: value
            for key, value in command.items()
            if key
            not in {
                "character_id",
                "controller_id",
                "controller_generation",
                "claim_id",
                "client_id",
            }
        }
        if "command_id" in command:
            command["id"] = command.pop("command_id")
        kwargs = self._claim_request_kwargs(character_id)
        kwargs["json"] = command
        res = await self._client.post(
            f"{self.base}/play/claims/{urllib.parse.quote(claim.claim_id, safe='')}/commands",
            **kwargs,
        )
        try:
            body = res.json()
        except Exception:
            body = {}
        if not res.is_success:
            reason = str(
                body.get("reason") or f"request failed ({getattr(res, 'status_code', '?')})"
            )
            return SubmitResult(accepted=False, reason=reason)
        return SubmitResult(
            accepted=body.get("status") == "queued", reason=str(body.get("reason", ""))
        )

    async def recent_events(self, character_id: str = "") -> list[dict]:
        if not character_id:
            return []
        data = await self._fetch_claim_resource(character_id, "events")
        return data.get("events", []) if data is not None else []

    def supports_live_updates(self) -> bool:
        try:
            import websockets  # noqa: F401
        except ImportError:
            return False
        return True

    async def watch_updates(
        self,
        character_id: str,
        control: ControlClaim | None,
        on_message: Callable[[dict], object],
        on_state: Callable[[str], object],
    ) -> None:
        try:
            import websockets
        except ImportError:
            await _call_update_callback(on_state, "fallback")
            return
        parsed = urllib.parse.urlparse(self.base)
        claim = control or self._claim_for(character_id)
        if claim is None or not claim.claim_id:
            await _call_update_callback(on_state, "fallback")
            return
        encoded_claim = urllib.parse.quote(claim.claim_id, safe="")
        path = f"{parsed.path.rstrip('/')}/play/claims/{encoded_claim}/stream"
        url = urllib.parse.urlunparse(
            parsed._replace(
                scheme="wss" if parsed.scheme == "https" else "ws",
                path=path,
                params="",
                query="",
                fragment="",
            )
        )
        attempt = 0
        while True:
            await _call_update_callback(on_state, "connecting")
            ready = False
            try:
                async with websockets.connect(url, open_timeout=10) as socket:
                    await socket.send(
                        json.dumps(
                            {
                                "type": "authenticate",
                                "data": {
                                    "token": self._access_token or None,
                                    "client_id": self.client_id,
                                    "claim_secret": claim.claim_secret,
                                },
                            }
                        )
                    )
                    while True:
                        raw = await asyncio.wait_for(socket.recv(), timeout=70.0)
                        try:
                            frame = json.loads(raw)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if not isinstance(frame, dict) or frame.get("type") not in {
                            "ready",
                            "event",
                            "invalidate",
                            "resync",
                            "heartbeat",
                        }:
                            continue
                        if frame["type"] == "ready":
                            ready = True
                            attempt = 0
                            await _call_update_callback(on_state, "live")
                        await _call_update_callback(on_message, frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Remote player update stream disconnected", exc_info=True)
            await _call_update_callback(on_state, "fallback")
            delay = (1, 2, 4, 8, 16, 30)[min(attempt, 5)]
            attempt = 0 if ready else attempt + 1
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))

    async def request_image(self, character_id: str, event_id: str = "") -> ImageRequestResult:
        claim = self._claim_for(character_id)
        if claim is None or not claim.claim_id:
            return ImageRequestResult(ok=False, status="error", reason="a claim is required")
        res = await self._client.post(
            f"{self.base}/play/claims/{urllib.parse.quote(claim.claim_id, safe='')}/jobs",
            json={"kind": "scene_image", **({"event_id": event_id} if event_id else {})},
            **self._claim_request_kwargs(character_id),
        )
        if res.status_code == 409:
            return ImageRequestResult(
                ok=False, status="unavailable", reason="image generation is not available"
            )
        if not res.is_success:
            return ImageRequestResult(
                ok=False, status="error", reason=f"request failed ({res.status_code})"
            )
        body = res.json()
        result = body.get("result") or {}
        return ImageRequestResult(
            ok=True,
            status=body.get("status", ""),
            url=result.get("url", ""),
        )

    async def request_video(self, character_id: str, event_id: str = "") -> ImageRequestResult:
        claim = self._claim_for(character_id)
        if claim is None or not claim.claim_id:
            return ImageRequestResult(ok=False, status="error", reason="a claim is required")
        res = await self._client.post(
            f"{self.base}/play/claims/{urllib.parse.quote(claim.claim_id, safe='')}/jobs",
            json={"kind": "scene_video", **({"event_id": event_id} if event_id else {})},
            **self._claim_request_kwargs(character_id),
        )
        if res.status_code == 409:
            return ImageRequestResult(
                ok=False, status="unavailable", reason="video generation is not available"
            )
        if not res.is_success:
            return ImageRequestResult(
                ok=False, status="error", reason=f"request failed ({res.status_code})"
            )
        body = res.json()
        result = body.get("result") or {}
        return ImageRequestResult(
            ok=True,
            status=body.get("status", ""),
            url=result.get("url", ""),
        )

    async def open_character_sheet(self, character_id: str) -> SheetOpenResult:
        url = character_sheet_url(self.base, character_id)
        opened = webbrowser.open(url, new=2)
        if not opened:
            return SheetOpenResult(ok=False, url=url, reason="could not open browser")
        return SheetOpenResult(ok=True, url=url)

    async def claim(self, player_id: str, world: World) -> ControlClaim | None:
        stored = load_claim_control(self.client_id, player_id)
        headers = (
            {"X-Bunnyland-Claim-Secret": stored.claim_secret}
            if stored and stored.claim_secret
            else {}
        )
        kwargs = {}
        if stored is None:
            kwargs["json"] = {
                "character_id": player_id,
                "label": "tui",
                "fallback_controller": self.fallback_controller,
                "timeout_seconds": self.timeout_seconds,
            }
        if headers:
            kwargs["headers"] = headers
        claim_path = (
            f"/play/claims/{urllib.parse.quote(stored.claim_id, safe='')}"
            if stored is not None
            else "/play/claims"
        )
        operation = self._client.put if stored is not None else self._client.post
        res = await operation(f"{self.base}{claim_path}", **kwargs)
        if stored is not None and getattr(res, "status_code", None) == 404:
            self._forget_claim(player_id)
            stored = None
            res = await self._client.post(
                f"{self.base}/play/claims",
                json={
                    "character_id": player_id,
                    "label": "tui",
                    "fallback_controller": self.fallback_controller,
                    "timeout_seconds": self.timeout_seconds,
                },
            )
        if not res.is_success:
            logger.warning(
                "Remote web controller claim failed for %s: HTTP %s %s",
                player_id,
                res.status_code,
                res.text,
            )
            return None
        data = res.json()
        control = ControlClaim(
            controller_id=data["controller_id"],
            generation=int(data["controller_generation"]),
            claim_id=str(data.get("id") or ""),
            claim_secret=str(
                res.headers.get("X-Bunnyland-Claim-Secret")
                or (stored.claim_secret if stored else "")
            ),
            active=data.get("control") != "fallback",
        )
        self._claims[player_id] = control
        save_claim_control(self.client_id, player_id, control)
        return control

    async def release_controller(
        self,
        player_id: str,
        control: ControlClaim,
    ) -> ControlClaim | None:
        res = await self._client.patch(
            f"{self.base}/play/claims/{urllib.parse.quote(control.claim_id, safe='')}",
            headers={"X-Bunnyland-Claim-Secret": control.claim_secret},
            json={"kind": "control", "desired": "fallback"},
        )
        if not res.is_success:
            logger.warning(
                "Remote web controller release failed for %s: HTTP %s %s",
                player_id,
                res.status_code,
                res.text,
            )
            return None
        data = res.json()
        released = ControlClaim(
            controller_id=data["controller_id"],
            generation=int(data["controller_generation"]),
            claim_id=str(data.get("id") or control.claim_id),
            claim_secret=control.claim_secret,
            active=False,
        )
        self._claims[player_id] = released
        save_claim_control(self.client_id, player_id, released)
        return released

    async def release_claim(self, player_id: str, control: ControlClaim) -> bool:
        res = await self._client.delete(
            f"{self.base}/play/claims/{urllib.parse.quote(control.claim_id, safe='')}",
            headers={"X-Bunnyland-Claim-Secret": control.claim_secret},
        )
        if not res.is_success:
            logger.warning(
                "Remote web claim release failed for %s: HTTP %s %s",
                player_id,
                res.status_code,
                res.text,
            )
            return False
        self._claims.pop(player_id, None)
        clear_claim_control(self.client_id, player_id)
        return True
