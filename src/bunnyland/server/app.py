"""FastAPI app factory for web clients."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter
from relics import EntityId

from bunnyland.simpacks.toonsim.mechanics import SpriteImageComponent

from .. import __version__, telemetry
from ..claims import (
    CLIENT_KIND_WEB,
    ClaimOwner,
    ClaimSecretRegistry,
    add_claim,
    claim_client_matches,
    controller_claim,
    current_controller,
    ensure_character_control_claim_allowed,
    ensure_claim_secret,
    matching_controller,
    normalize_claimed_controllers_without_secrets,
    reconcile_transient_controllers,
    remove_claim,
    retire_transient_controller,
    spawn_transient_controller,
    transfer_claim,
)
from ..content import load_content_library
from ..content_warnings import world_content_flags
from ..core import (
    CharacterComponent,
    DeadComponent,
    DownedComponent,
    IdentityComponent,
    LLMControllerComponent,
    MemoryProfileComponent,
    SleepingComponent,
    SuspendedComponent,
    SuspendedControllerComponent,
    WebControllerComponent,
    container_of,
    parse_entity_id,
    replace_component,
)
from ..core.claim_timeout import apply_claim_timeout_settings, record_claim_activity
from ..core.controllers import ClaimedComponent, ClaimTimeoutComponent
from ..core.events import (
    CharacterChatRequestedEvent,
    CharacterClaimedEvent,
    ControllerChangedEvent,
    DomainEvent,
    EventVisibility,
    event_base,
    serialized_event_visible_to,
)
from ..core.perspective import PerspectiveQueryResult
from ..core.world_actor import CONTROL_COMMANDS, CommandExpectationError, WorldActor
from ..foundation.media.service import (
    DEFAULT_MEDIA_CAPACITY_BYTES,
    MediaError,
    sniff_image_extension,
)
from ..imagegen.components import PortraitImageComponent
from ..imagegen.media import (
    SEGMENT_PORTRAITS,
    SEGMENT_SPRITES,
    MediaStore,
)
from ..imagegen.scene import request_scene_image, request_scene_video
from ..imagegen.spec import ImagePurpose, WorkflowTemplate
from ..imagegen.store import WorkflowTemplateStore
from ..llm_agents import (
    ControllerDefinitionStore,
    action_library_names,
    behavior_tree_names,
    condition_library_names,
    script_names,
)
from ..llm_agents.specs import BehaviorTreeSpec, ScriptSpec
from ..mcp import MCP_MOUNT_PATH, create_bunnyland_mcp_app, mcp_enabled
from ..moderation import (
    IdentityKind,
    ModerationActionKind,
    ModerationAuditEntry,
    ModerationIdentity,
    ModerationRestrictedError,
    ModerationRestriction,
    ModerationService,
    ModerationStore,
)
from ..persistence import WorldMeta
from ..plugins import collect_persona_fragments, collect_prompt_fragments
from ..worldgen import GenOptions, collect_generators
from .addons import AddonMediaFacade, PlayWebSocketAuthenticator
from .admin import idle_generation_status, save_configured_world, start_world_generation
from .auth import (
    AUTH_COOKIE_NAME,
    CHARACTER_CHAT_SCOPE,
    CHARACTER_PROFILE_SCOPE,
    WORLD_ADMIN_SCOPE,
    WORLD_PLAY_SCOPE,
    AuthMeResponse,
    LoginRequest,
    RequestAuthenticator,
    TokenPrincipal,
    TokenResponse,
    TokenStore,
    UserCredentialStore,
    scope_granted,
)
from .body_limit import MaxBodySizeMiddleware
from .character_chat import CharacterChatAccess, CharacterChatService, ChatOnlyTool
from .chat_media import (
    capture_chat_media_scene,
    chat_media_prompt_context,
    chat_media_tool,
)
from .client_ids import (
    ADMIN_CLIENT_IDS_ENV,
    CLIENT_ID_HEADER,
    PLAYER_CLIENT_IDS_ENV,
    configured_client_id_allowlist,
    require_allowed_client_id,
)
from .jobs import JOB_TTL_SECONDS, JobRecord, JobRegistry
from .models import (
    IDENTIFIER_MAX_LENGTH,
    CharacterChatActionResult,
    CharacterChatHistoryMessage,
    CharacterChatMediaJobReference,
    CharacterChatRequest,
    CharacterChatResponse,
    ChatMediaCreativeDirection,
    ClaimReleaseResponse,
    CommandCancelResponse,
    CommandRequest,
    CommandResponse,
    ControllerAssignmentRequest,
    ControllerDefinitionListResponse,
    FeatureStatusResponse,
    MemoryCharactersResponse,
    MemoryCharacterView,
    MemoryDocumentResponse,
    MemoryDocumentsResponse,
    MemoryDocumentUpdateRequest,
    MemoryDocumentView,
    StoredControllerDefinitions,
    WebControllerClaimRequest,
    WebControllerClaimResponse,
    WebControllerFallbackRequest,
    WebControllerFallbackResponse,
    WorldCharacterGenerationRequest,
    WorldCharacterGenerationResponse,
    WorldEventGenerationRequest,
    WorldEventGenerationResponse,
    WorldGenerateRequest,
    WorldGenerateResponse,
    WorldGenerationStatusResponse,
    WorldGeneratorInfo,
    WorldGeneratorListResponse,
    WorldImageGenerationRequest,
    WorldImageGenerationResponse,
    WorldItemGenerationRequest,
    WorldItemGenerationResponse,
    WorldPatchRequest,
    WorldPatchResponse,
    WorldRoomGenerationRequest,
    WorldRoomGenerationResponse,
    WorldRuntimeResponse,
    WorldSaveResponse,
    WorldVideoGenerationRequest,
)
from .onboarding import OnboardingTracker
from .patches import WorldPatchError, apply_world_patch
from .rate_limit import ConcurrencyLimiter, FixedWindowRateLimiter
from .schema import world_schema
from .serialization import (
    serialize_character_list,
    serialize_character_projection,
    serialize_character_queued_commands,
    serialize_dm_projection,
    serialize_entity,
    serialize_room_projection,
    serialize_world,
    serialize_world_overview,
)
from .subscriptions import EventStream
from .v1_models import (
    CatalogResource,
    CharacterChatReplyRequest,
    CharacterCollection,
    CharacterProfileResource,
    CharacterResource,
    ChatJobRequest,
    ChatMediaJobRequest,
    CheckpointRequest,
    ClaimCharacterResource,
    ClaimCommandRequest,
    ClaimCreateRequest,
    ClaimProjectionResource,
    ClaimQueryRequest,
    ClaimResource,
    ClaimSceneResource,
    ClaimUpdateRequest,
    CommandResource,
    ControllerAssignment,
    ControllerDefinitionRequest,
    ControllerDefinitionsResource,
    EventCollection,
    GenerationJobRequest,
    GeneratorCollection,
    ImageJobResult,
    JobResource,
    JobResult,
    ModerationActionCollection,
    ModerationActionRequest,
    ModerationActionResource,
    ModerationClaimResource,
    ModerationIdentityResource,
    ModerationPlayerCollection,
    ModerationPlayerResource,
    ModerationRestrictionResource,
    ProblemDetails,
    PublicWorldResource,
    RuntimePatchRequest,
    SceneMediaJobRequest,
)
from .worldgen import (
    generate_character_patch,
    generate_event_patch,
    generate_item_patch,
    generate_room_patch,
)

WEBSOCKET_HEARTBEAT_SECONDS = 30.0
PLAYER_WEBSOCKET_AUTH_SECONDS = 5.0
# Websockets bypass the HTTP middleware stack entirely (BaseHTTPMiddleware returns early for
# any non-http scope), and a socket is one request that then lives as long as the client
# wants, so neither the request rate limiter here nor `limit_req` at the edge bounds how
# many one caller holds. These do. The unauthenticated cap covers the window between accept
# and the first auth frame, which a client can otherwise use to park sockets for free.
WEBSOCKET_CONNECTIONS_PER_IDENTITY_ENV = "BUNNYLAND_WEBSOCKET_CONNECTIONS_PER_IDENTITY"
WEBSOCKET_CONNECTIONS_PER_HOST_ENV = "BUNNYLAND_WEBSOCKET_UNAUTHENTICATED_PER_HOST"
WEBSOCKET_CONNECTIONS_PER_IDENTITY = 8
WEBSOCKET_UNAUTHENTICATED_PER_HOST = 16
#: Close code for "try again later", used when a cap is reached rather than 1008 (policy
#: violation) so a client can tell overload from rejection.
WEBSOCKET_OVERLOADED_CLOSE_CODE = 1013
#: How often a live player socket revalidates its token and claim. Doing it per frame made
#: every frame re-scan every claimed controller in the world.
WEBSOCKET_REAUTHORIZE_SECONDS = 5.0
UPLOAD_IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
MAX_UPLOAD_IMAGE_BYTES = 10 * 1024 * 1024
JOB_RESULT_ADAPTER = TypeAdapter(JobResult)


def _delete_ephemeral_job_media(media_store: MediaStore, record: JobRecord) -> None:
    """Remove completed chat-only media when its owner-scoped job record expires."""

    result = record.job.result
    if not isinstance(result, ImageJobResult):
        return
    _delete_ephemeral_media_urls(media_store, result.url, result.alpha_url)


def _delete_ephemeral_media_urls(
    media_store: MediaStore, url: str, alpha_url: str = ""
) -> None:
    """Best-effort cleanup for generated chat media that has no durable owner."""

    for media_url in (url, alpha_url):
        if not media_url:
            continue
        path = urlsplit(media_url).path
        for prefix in ("/v1/public/media/", "/public/media/"):
            if not path.startswith(prefix):
                continue
            relative = path.removeprefix(prefix)
            parts = relative.split("/")
            if len(parts) != 2:
                continue
            try:
                media_store.delete(parts[0], parts[1])
            except MediaError:
                logger.warning("refused invalid ephemeral media URL %s", media_url)
            except OSError:
                logger.exception("failed to delete ephemeral media URL %s", media_url)

if TYPE_CHECKING:
    from ..engine import GameLoop
    from ..imagegen.backfill import ImageBackfillScheduler
    from ..imagegen.service import ImageGenJob, ImageGenService
    from ..imagegen.video_service import VideoGenJob, VideoGenService
    from ..plugins.model import Plugin
    from .subscriptions import EventSubscription

# Imported at module scope (not inside ``create_app``) so that FastAPI can resolve the
# ``websocket: WebSocket`` annotation on the route handler. Under ``from __future__ import
# annotations`` the hint is the string "WebSocket", which FastAPI looks up in this module's
# globals; a function-local import would leave it unresolvable and FastAPI would misread the
# parameter as a query field, closing every connection with a 403. Optional dependency, so
# fall back to ``None`` and raise a friendly error from ``create_app`` if it is missing.
try:
    from fastapi import (
        APIRouter,
        FastAPI,
        Header,
        HTTPException,
        Request,
        Response,
        WebSocket,
        WebSocketDisconnect,
    )
    from fastapi.exceptions import RequestValidationError
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.routing import Match
except ImportError:
    APIRouter = FastAPI = Header = HTTPException = Request = Response = None  # type: ignore[assignment, misc]
    WebSocket = WebSocketDisconnect = CORSMiddleware = JSONResponse = None  # type: ignore[assignment, misc]
    RequestValidationError = StarletteHTTPException = None  # type: ignore[assignment, misc]
    Match = None  # type: ignore[assignment, misc]


logger = logging.getLogger("bunnyland.server")


@runtime_checkable
class _PublicMediaConfiguration(Protocol):
    @property
    def public_base_url(self) -> str: ...
RATE_LIMIT_REQUESTS_ENV = "BUNNYLAND_HTTP_RATE_LIMIT_REQUESTS"
RATE_LIMIT_WINDOW_ENV = "BUNNYLAND_HTTP_RATE_LIMIT_WINDOW_SECONDS"
# The general request limiter used to default to 0, which FixedWindowRateLimiter reads as
# "disabled". The hosted deployment sets these through Ansible, so only deployments that did
# not -- self-hosters, and any compose run without a rendered compose.user.yml -- silently ran
# with no app-level limit at all. Defaulting on matches the hosted values; set the env var to
# 0 to deliberately turn it off.
RATE_LIMIT_REQUESTS = 120
RATE_LIMIT_WINDOW_SECONDS = 1.0
# Scene images run a GPU generation per request, so they get their own cap rather than only
# the general one. Character chat already had an equivalent.
SCENE_IMAGE_RATE_LIMIT_REQUESTS_ENV = "BUNNYLAND_SCENE_IMAGE_RATE_LIMIT_REQUESTS"
SCENE_IMAGE_RATE_LIMIT_WINDOW_ENV = "BUNNYLAND_SCENE_IMAGE_RATE_LIMIT_WINDOW_SECONDS"
SCENE_IMAGE_RATE_LIMIT_REQUESTS = 4
SCENE_IMAGE_RATE_LIMIT_WINDOW_SECONDS = 60
CORS_ORIGINS_ENV = "BUNNYLAND_CORS_ORIGINS"
CLAIM_COOKIE_NAME = "bunnyland_claim"
CLAIM_COOKIE_PATH = "/api/v1/play"
LOGIN_RATE_LIMIT_REQUESTS = 5
LOGIN_USERNAME_RATE_LIMIT_REQUESTS = 20
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 60
TOKEN_FAILURE_RATE_LIMIT_REQUESTS = 20
TOKEN_FAILURE_RATE_LIMIT_WINDOW_SECONDS = 60
# Per-principal cap on character-chat job submissions (both LLM-bound jobs and jobs routed
# to a human controller). Chat is the most abuse-prone mutation, so a single authenticated
# caller cannot spam it. The live server tightens this to ~2/60s via the env var below.
CHAT_JOB_RATE_LIMIT_REQUESTS_ENV = "BUNNYLAND_CHAT_JOB_RATE_LIMIT_REQUESTS"
CHAT_JOB_RATE_LIMIT_WINDOW_ENV = "BUNNYLAND_CHAT_JOB_RATE_LIMIT_WINDOW_SECONDS"
CHAT_JOB_RATE_LIMIT_REQUESTS = 10
CHAT_JOB_RATE_LIMIT_WINDOW_SECONDS = 60
# A chat job whose action was queued polls for the result. Both bounds exist so a queued
# action that never completes cannot leave a task polling forever: without a deadline every
# such job pinned a permanent 4 Hz poll for the life of the process.
CHAT_JOB_POLL_SECONDS = 0.25
CHAT_JOB_PENDING_TIMEOUT_SECONDS = 120.0
# In-app request body cap as a backstop to the edge nginx `client_max_body_size`, so the
# limit does not vanish if the app is ever exposed without that reverse proxy in front. It
# sits above the largest legitimate body (a max-size image upload plus multipart framing),
# so per-endpoint limits like the image-upload check remain the specific authority.
MAX_REQUEST_BODY_BYTES_ENV = "BUNNYLAND_MAX_REQUEST_BODY_BYTES"
MAX_REQUEST_BODY_BYTES = MAX_UPLOAD_IMAGE_BYTES + 2 * 1024 * 1024
HSTS_VALUE = "max-age=31536000"
# Generation jobs are admin-only and world-wide, so they share one owner bucket rather
# than being scoped per caller.
GENERATION_JOB_OWNER = "admin"


class AuthorizationSurface(StrEnum):
    PUBLIC = "public"
    AUTH_LOGIN = "auth-login"
    AUTH = "auth"
    PROFILE = "profile"
    CHAT = "chat"
    PLAY = "play"
    ADMIN = "admin"
    MCP = "mcp"


SURFACE_SCOPES = {
    AuthorizationSurface.AUTH: None,
    AuthorizationSurface.PROFILE: CHARACTER_PROFILE_SCOPE,
    AuthorizationSurface.CHAT: CHARACTER_CHAT_SCOPE,
    AuthorizationSurface.PLAY: WORLD_PLAY_SCOPE,
    AuthorizationSurface.ADMIN: WORLD_ADMIN_SCOPE,
    AuthorizationSurface.MCP: WORLD_PLAY_SCOPE,
}


def classify_authorization_surface(path: str) -> AuthorizationSurface | None:
    """Return the single declared authorization surface for an application path."""

    normalized = path.rstrip("/") or "/"
    for prefix, surface in (
        ("/v1/public", AuthorizationSurface.PUBLIC),
        ("/v1/auth", AuthorizationSurface.AUTH),
        ("/v1/profile", AuthorizationSurface.PROFILE),
        ("/v1/chat", AuthorizationSurface.CHAT),
        ("/v1/play", AuthorizationSurface.PLAY),
        ("/v1/admin", AuthorizationSurface.ADMIN),
        ("/v1/mcp", AuthorizationSurface.MCP),
    ):
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            return surface
    return None


def _configured_cors_origins(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    raw = os.environ.get(CORS_ORIGINS_ENV, "") if value is None else value
    entries = raw.split(",") if isinstance(raw, str) else list(raw)
    origins: list[str] = []
    for entry in entries:
        origin = str(entry).strip()
        if not origin:
            continue
        parsed = urlsplit(origin)
        if (
            origin in {"*", "null"}
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"invalid CORS origin: {origin!r}")
        normalized = f"{parsed.scheme}://{parsed.netloc}"
        if normalized not in origins:
            origins.append(normalized)
    return origins


def websocket_origin_is_trusted(websocket: WebSocket) -> bool:
    """Accept terminal clients without Origin and same-origin browsers only."""

    headers = getattr(websocket, "headers", {})
    origin = headers.get("Origin")
    if origin is None:
        return True
    host = headers.get("Host", "").strip()
    if not host or origin in {"null", "*"}:
        return False
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    return origin == f"{scheme}://{host}"


def route_surface_matrix(app) -> list[tuple[str, str, AuthorizationSurface]]:
    """Return and validate the declared HTTP/WebSocket authorization matrix."""

    matrix: list[tuple[str, str, AuthorizationSurface]] = []
    for route in app.router.routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            continue
        surface = classify_authorization_surface(path)
        if surface is None:
            raise ValueError(f"application route is outside an authorization zone: {path!r}")
        protocol = "websocket" if route.__class__.__name__ == "APIWebSocketRoute" else "http"
        if protocol == "websocket" and surface not in {
            AuthorizationSurface.PLAY,
            AuthorizationSurface.ADMIN,
        }:
            raise ValueError(f"websocket route uses invalid authorization zone: {path!r}")
        matrix.append((protocol, path, surface))
    return matrix


async def next_websocket_update(actor: WorldActor, subscription: EventSubscription) -> dict:
    try:
        return await asyncio.wait_for(
            subscription.queue.get(),
            timeout=WEBSOCKET_HEARTBEAT_SECONDS,
        )
    except TimeoutError:
        return {"type": "heartbeat", "data": {"world_epoch": actor.epoch}}


def _character_room_id(actor: WorldActor, character_id: str) -> str | None:
    parsed = parse_entity_id(character_id)
    if parsed is None or not actor.world.has_entity(parsed):
        return None
    room_id = container_of(actor.world.get_entity(parsed))
    return str(room_id) if room_id is not None else None


def player_update_for_message(
    actor: WorldActor,
    character_id: str,
    message: dict,
) -> dict | None:
    """Filter one internal broadcast into a safe player update frame."""
    if message.get("type") != "event":
        return {"type": "invalidate", "data": {"world_epoch": actor.epoch}}
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    event = data.get("event")
    if not isinstance(event, dict):
        return None
    if serialized_event_visible_to(
        event,
        character_id=character_id,
        room_of=lambda candidate: _character_room_id(actor, candidate),
    ):
        return message
    if event.get("visibility") == "system":
        return {
            "type": "invalidate",
            "data": {"world_epoch": int(event.get("world_epoch") or actor.epoch)},
        }
    return None


def recent_player_updates(
    actor: WorldActor,
    character_id: str,
    messages: list[dict],
) -> list[dict]:
    return [
        update
        for message in messages
        if (update := player_update_for_message(actor, character_id, message)) is not None
    ]


async def next_player_update(
    actor: WorldActor,
    subscription: EventSubscription,
    character_id: str,
) -> dict:
    """Return the next safe frame, a resync after overflow, or an idle heartbeat."""
    if subscription.consume_dropped():
        return _overflow_resync(actor)
    deadline = asyncio.get_running_loop().time() + WEBSOCKET_HEARTBEAT_SECONDS
    while True:
        timeout = max(0, deadline - asyncio.get_running_loop().time())
        try:
            message = await asyncio.wait_for(subscription.queue.get(), timeout=timeout)
        except TimeoutError:
            return {"type": "heartbeat", "data": {"world_epoch": actor.epoch}}
        if subscription.consume_dropped():
            return _overflow_resync(actor)
        update = player_update_for_message(actor, character_id, message)
        if update is not None:
            return update


def _overflow_resync(actor: WorldActor) -> dict:
    return {
        "type": "resync",
        "data": {
            "world_epoch": actor.epoch,
            "reason": "queue_overflow",
            "resume_supported": False,
            "required_action": "fetch_character_projection",
        },
    }


def create_app(
    actor: WorldActor,
    meta: WorldMeta | None = None,
    *,
    loop: GameLoop | None = None,
    save_path: str | Path | None = None,
    definitions_path: str | Path | None = None,
    worldgen_options: GenOptions | None = None,
    plugins: list[Plugin] | None = None,
    token_store: TokenStore | None = None,
    user_credentials: UserCredentialStore | None = None,
    moderation_store: ModerationStore | None = None,
    moderation_service: ModerationService | None = None,
    player_client_ids: str | list[str] | None = None,
    admin_client_ids: str | list[str] | None = None,
    imagegen: ImageGenService | None = None,
    videogen: VideoGenService | None = None,
    image_backfill: ImageBackfillScheduler | None = None,
    workflow_templates: WorkflowTemplateStore | None = None,
    character_chat: CharacterChatService | None = None,
    open_character_chat: bool = True,
    character_chat_media_tools: bool = False,
    character_sheets: bool = True,
    claim_secrets: ClaimSecretRegistry | None = None,
    memory_store=None,
    rate_limit_requests: int | None = None,
    rate_limit_window_seconds: float | None = None,
    cors_origins: str | list[str] | tuple[str, ...] | None = None,
    allow_unauthenticated_embedding: bool = False,
    title: str = "bunnyland",
):
    """Create the HTTP/websocket app around a live ``WorldActor``."""

    if FastAPI is None:
        raise RuntimeError(
            "bunnyland server API requires FastAPI; install the server dependencies first"
        )
    if allow_unauthenticated_embedding and (
        token_store is not None or user_credentials is not None
    ):
        raise ValueError(
            "allow_unauthenticated_embedding cannot be combined with authentication stores"
        )
    actor.configure_persistence(
        save_path=save_path,
        meta=meta,
        plugins=tuple(plugins or ()),
        plugin_context=getattr(actor.persistence, "plugin_context", None),
    )

    mcp_session_manager = None
    mcp_event_bridge = None
    chat_tasks: set[asyncio.Task[None]] = set()
    chat_tasks_by_job: dict[str, asyncio.Task[None]] = {}
    chat_media_cleanup_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_app):
        mcp_session_context = None
        actor.bus.subscribe(
            DomainEvent,
            onboarding.record_event,
            reaction_id="bunnyland.server:onboarding",
            external=True,
        )
        try:
            if mcp_session_manager is not None:
                mcp_session_context = mcp_session_manager.run()
                await mcp_session_context.__aenter__()
            if image_backfill is not None:
                image_backfill.start()
            yield
        finally:
            actor.bus.unsubscribe(DomainEvent, onboarding.record_event)
            for task in chat_tasks:
                task.cancel()
            if chat_tasks:
                await asyncio.gather(*chat_tasks, return_exceptions=True)
            for task in chat_media_cleanup_tasks:
                task.cancel()
            if chat_media_cleanup_tasks:
                await asyncio.gather(*chat_media_cleanup_tasks, return_exceptions=True)
            if mcp_session_context is not None:
                await mcp_session_context.__aexit__(None, None, None)
            if mcp_event_bridge is not None:
                mcp_event_bridge.close()
            if image_backfill is not None:
                await image_backfill.aclose()
            if imagegen is not None:
                await imagegen.aclose()
            if videogen is not None:
                await videogen.aclose()
            for job in chat_media_job_registry.list_all():
                chat_media_job_registry.update(_refresh_media_job_v1(job))
            chat_media_job_registry.discard_matching()

    trusted_origins = _configured_cors_origins(cors_origins)
    app = FastAPI(
        title=title,
        version=__version__,
        lifespan=lifespan,
        docs_url="/v1/admin/docs",
        redoc_url="/v1/admin/redoc",
        openapi_url="/v1/admin/openapi.json",
        swagger_ui_oauth2_redirect_url="/v1/admin/docs/oauth2-redirect",
        # Keep named DTO components stable across Pydantic serialization-schema changes.
        separate_input_output_schemas=False,
    )

    def _problem_response(
        request: Request,
        status_code: int,
        detail,
        *,
        headers: dict[str, str] | None = None,
        code: str | None = None,
        restriction: ModerationRestriction | None = None,
    ):
        titles = {
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            409: "Conflict",
            413: "Content Too Large",
            422: "Unprocessable Content",
            429: "Too Many Requests",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }
        codes = {
            400: "invalid_request",
            401: "authentication_required",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            413: "content_too_large",
            422: "validation_error",
            429: "rate_limited",
            500: "internal_error",
            503: "unavailable",
        }
        problem_code = code or codes.get(status_code, "request_failed")
        problem = ProblemDetails(
            type=f"https://bunnyland.dev/problems/{problem_code}",
            title=titles.get(status_code, "Request Failed"),
            status=status_code,
            detail=detail if isinstance(detail, str) else str(detail),
            instance=request.url.path,
            code=problem_code,
            restriction_kind=restriction.kind if restriction is not None else None,
            restriction_expires_at=restriction.expires_at if restriction is not None else None,
            restriction_reason=restriction.reason if restriction is not None else None,
        )
        return JSONResponse(
            status_code=status_code,
            content=problem.model_dump(mode="json", exclude_none=True),
            media_type="application/problem+json",
            headers=headers or {},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_problem(request: Request, exc: StarletteHTTPException):
        headers = dict(getattr(exc, "headers", None) or {})
        code = headers.pop("X-Bunnyland-Problem-Code", None)
        return _problem_response(
            request,
            exc.status_code,
            exc.detail,
            headers=headers,
            code=code,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_problem(request: Request, exc: RequestValidationError):
        safe_errors = [
            {
                "loc": list(error["loc"]),
                "msg": str(error["msg"]),
                "type": str(error["type"]),
            }
            for error in exc.errors()
        ]
        return _problem_response(request, 422, safe_errors, code="validation_error")

    @app.exception_handler(ModerationRestrictedError)
    async def _moderation_problem(request: Request, exc: ModerationRestrictedError):
        return _problem_response(
            request,
            403,
            f"account is {exc.restriction.kind.value}",
            code=f"account_{exc.restriction.kind.value}",
            restriction=exc.restriction,
        )

    request_limit = (
        int(os.environ.get(RATE_LIMIT_REQUESTS_ENV, RATE_LIMIT_REQUESTS))
        if rate_limit_requests is None
        else rate_limit_requests
    )
    request_window = (
        float(os.environ.get(RATE_LIMIT_WINDOW_ENV, RATE_LIMIT_WINDOW_SECONDS))
        if rate_limit_window_seconds is None
        else rate_limit_window_seconds
    )
    rate_limiter = FixedWindowRateLimiter(request_limit, request_window)
    login_ip_rate_limiter = FixedWindowRateLimiter(
        LOGIN_RATE_LIMIT_REQUESTS, LOGIN_RATE_LIMIT_WINDOW_SECONDS
    )
    login_username_rate_limiter = FixedWindowRateLimiter(
        LOGIN_USERNAME_RATE_LIMIT_REQUESTS, LOGIN_RATE_LIMIT_WINDOW_SECONDS
    )
    token_failure_rate_limiter = FixedWindowRateLimiter(
        TOKEN_FAILURE_RATE_LIMIT_REQUESTS, TOKEN_FAILURE_RATE_LIMIT_WINDOW_SECONDS
    )
    chat_job_rate_limiter = FixedWindowRateLimiter(
        int(os.environ.get(CHAT_JOB_RATE_LIMIT_REQUESTS_ENV, CHAT_JOB_RATE_LIMIT_REQUESTS)),
        float(os.environ.get(CHAT_JOB_RATE_LIMIT_WINDOW_ENV, CHAT_JOB_RATE_LIMIT_WINDOW_SECONDS)),
    )
    scene_image_rate_limiter = FixedWindowRateLimiter(
        int(
            os.environ.get(SCENE_IMAGE_RATE_LIMIT_REQUESTS_ENV, SCENE_IMAGE_RATE_LIMIT_REQUESTS)
        ),
        float(
            os.environ.get(
                SCENE_IMAGE_RATE_LIMIT_WINDOW_ENV, SCENE_IMAGE_RATE_LIMIT_WINDOW_SECONDS
            )
        ),
    )
    max_request_body_bytes = int(
        os.environ.get(MAX_REQUEST_BODY_BYTES_ENV, MAX_REQUEST_BODY_BYTES)
    )
    websocket_identity_limiter = ConcurrencyLimiter(
        int(
            os.environ.get(
                WEBSOCKET_CONNECTIONS_PER_IDENTITY_ENV, WEBSOCKET_CONNECTIONS_PER_IDENTITY
            )
        )
    )
    websocket_host_limiter = ConcurrencyLimiter(
        int(
            os.environ.get(
                WEBSOCKET_CONNECTIONS_PER_HOST_ENV, WEBSOCKET_UNAUTHENTICATED_PER_HOST
            )
        )
    )
    authenticator = (
        RequestAuthenticator(token_store, user_credentials) if token_store is not None else None
    )
    meta = meta or WorldMeta()
    actor.world_id = meta.world_id
    stream = EventStream(actor)
    actor.event_stream = stream
    claim_secrets = claim_secrets or ClaimSecretRegistry()
    normalize_claimed_controllers_without_secrets(actor, claim_secrets)
    reconcile_transient_controllers(actor)
    moderation_store = moderation_store or ModerationStore(
        token_store.path if token_store is not None else ":memory:"
    )
    moderation_service = moderation_service or ModerationService(
        actor, moderation_store, claim_secrets, token_store=token_store
    )
    allowed_player_client_ids = configured_client_id_allowlist(
        player_client_ids, PLAYER_CLIENT_IDS_ENV
    )
    allowed_admin_client_ids = configured_client_id_allowlist(
        admin_client_ids, ADMIN_CLIENT_IDS_ENV
    )
    addon_media = AddonMediaFacade(
        actor,
        image_service=imagegen,
        video_service=videogen,
        public_base_url=(
            imagegen.public_base_url
            if isinstance(imagegen, _PublicMediaConfiguration)
            else videogen.public_base_url
            if isinstance(videogen, _PublicMediaConfiguration)
            else ""
        ),
    )
    play_websocket_auth = PlayWebSocketAuthenticator(
        authenticator,
        moderation_service,
        allowed_client_ids=allowed_player_client_ids,
    )
    plugin_context = actor.persistence.plugin_context
    if plugin_context is not None:
        plugin_context.addon_media = addon_media
        plugin_context.play_websocket_auth = play_websocket_auth
    generator_registry = collect_generators(plugins or ())
    generation_job = None
    # One bounded registry per job family, replacing plain dicts that nothing evicted. Chat
    # jobs additionally carried three parallel side maps (client id, character id, subject);
    # those now ride along on the record so they cannot drift out of sync with the job.
    player_job_registry = JobRegistry()
    # Chat records carry the authenticated subject that created them, so a reader cannot
    # fetch another account's job by guessing its id and spoofing the client-id header.
    character_chat_job_registry = JobRegistry()
    generation_job_registry = JobRegistry()

    async def _cancel_moderated_identity(identity: ModerationIdentity) -> None:
        if identity.kind is IdentityKind.WEB:
            removed = character_chat_job_registry.discard_matching(
                attributes={"subject": identity.id}
            )
            chat_media_job_registry.discard_matching(attributes={"subject": identity.id})
        elif identity.kind is IdentityKind.CLIENT:
            removed = character_chat_job_registry.discard_matching(owner=identity.id)
            chat_media_job_registry.discard_matching(owner=identity.id)
        else:
            removed = set()
        for job_id in removed:
            task = chat_tasks_by_job.pop(job_id, None)
            if task is not None:
                task.cancel()

    moderation_service.configure_runtime(
        token_store=token_store,
        cancel_identity=_cancel_moderated_identity,
    )
    onboarding = OnboardingTracker(actor)
    memory_store = memory_store or getattr(actor, "memory_store", None)
    media_store = (
        (getattr(imagegen, "media", None) if imagegen is not None else None)
        or (getattr(videogen, "media", None) if videogen is not None else None)
        or getattr(actor, "media_service", None)
        or MediaStore(
            os.environ.get("BUNNYLAND_MEDIA_DIR", "media").strip() or "media",
            capacity_bytes=int(
                os.environ.get(
                    "BUNNYLAND_MEDIA_CAPACITY_BYTES",
                    str(DEFAULT_MEDIA_CAPACITY_BYTES),
                )
            ),
        )
    )
    actor.media_service = media_store

    chat_media_job_registry = JobRegistry(
        on_evict=partial(_delete_ephemeral_job_media, media_store)
    )

    async def _expire_chat_media_job(job_id: str) -> None:
        await asyncio.sleep(JOB_TTL_SECONDS)
        chat_media_job_registry.discard(job_id)

    def _chat_media_job_completed(generated: ImageGenJob | VideoGenJob) -> None:
        stored = chat_media_job_registry.get(generated.job_id)
        if stored is None:
            _delete_ephemeral_media_urls(media_store, generated.url, generated.alpha_url)
            return
        chat_media_job_registry.update(_refresh_media_job_v1(stored))
        task = asyncio.create_task(
            _expire_chat_media_job(generated.job_id),
            name=f"chat-media-expiry-{generated.job_id}",
        )
        chat_media_cleanup_tasks.add(task)
        task.add_done_callback(chat_media_cleanup_tasks.discard)
    # Editor-loaded scripted/behavioral controller definitions: register any already on disk
    # so a restarted server keeps the scripts and behavior trees the editor previously saved.
    definition_store = ControllerDefinitionStore(
        definitions_path, action_definitions=actor.action_definitions()
    )
    definition_store.load()

    def _require_imagegen() -> ImageGenService:
        if imagegen is None:
            raise HTTPException(status_code=409, detail="image generation is not configured")
        return imagegen

    def _require_videogen() -> VideoGenService:
        if videogen is None:
            raise HTTPException(status_code=409, detail="video generation is not configured")
        return videogen

    def _require_workflow_templates() -> WorkflowTemplateStore:
        if workflow_templates is None:
            raise HTTPException(status_code=409, detail="ComfyUI workflows are not configured")
        return workflow_templates

    def _require_memory_store():
        if memory_store is None:
            raise HTTPException(status_code=409, detail="memory is not configured")
        return memory_store

    def _parse_purpose(value: str) -> ImagePurpose:
        try:
            return ImagePurpose(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid image purpose {value!r}") from exc

    def _require_allowed_player_client_id(client_id: str | None) -> str | None:
        try:
            return require_allowed_client_id(client_id, allowed_player_client_ids, "player")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def _require_allowed_admin_client_id(client_id: str | None) -> str | None:
        try:
            return require_allowed_client_id(client_id, allowed_admin_client_ids, "admin")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def _image_response(job: ImageGenJob | VideoGenJob) -> WorldImageGenerationResponse:
        return WorldImageGenerationResponse(
            world_epoch=actor.epoch,
            job_id=job.job_id,
            status=job.status,
            entity_id=job.entity_id,
            purpose=job.purpose.value,
            generator=job.generator,
            url=job.url,
            alpha_url=job.alpha_url,
            source_event_id=job.source_event_id,
            snapshot_epoch=job.snapshot_epoch,
            enhanced_prompt=job.enhanced_prompt,
            prompt_style=job.prompt_style,
            enhancer=job.enhancer,
            prompt_fallback=job.prompt_fallback,
            error=job.error,
        )

    def _client_host(request: Request) -> str:
        # Per-IP limiting is only as accurate as request.client.host. Behind the tunnel that
        # resolves to the real client IP only because the edge nginx translates Cloudflare's
        # CF-Connecting-IP into X-Forwarded-For and uvicorn trusts it (proxy_headers +
        # forwarded_allow_ips). If that reverse proxy is ever removed, every caller collapses
        # into one bucket; the app does not itself parse CF-Connecting-IP.
        return getattr(getattr(request, "client", None), "host", "") or "unknown"

    def _authenticate_websocket_frame(
        websocket: WebSocket,
        data: dict,
        surface: AuthorizationSurface,
    ) -> tuple[str | None, TokenPrincipal | None]:
        if authenticator is None:
            return None, None
        active_authenticator = cast(RequestAuthenticator, authenticator)
        frame_token = data.get("token")
        if frame_token is not None and not isinstance(frame_token, str):
            raise HTTPException(status_code=401, detail="invalid bearer token")
        header_auth = websocket.headers.get("Authorization")
        if frame_token:
            if header_auth:
                scheme, separator, value = header_auth.partition(" ")
                if not separator or scheme.lower() != "bearer" or value.strip() != frame_token:
                    raise HTTPException(status_code=401, detail="conflicting bearer credentials")
            header_auth = f"Bearer {frame_token}"
        principal = active_authenticator.authenticate_values(
            authorization=header_auth,
            cookie_token=websocket.cookies.get(AUTH_COOKIE_NAME),
            required_scopes=(cast(str, SURFACE_SCOPES[surface]),),
        )
        if surface is AuthorizationSurface.ADMIN:
            _require_allowed_admin_client_id(
                data.get("client_id") or websocket.headers.get(CLIENT_ID_HEADER)
            )
        if frame_token:
            return frame_token, principal
        if websocket.cookies.get(AUTH_COOKIE_NAME):
            return websocket.cookies[AUTH_COOKIE_NAME], principal
        return (header_auth.partition(" ")[2].strip() if header_auth else None), principal

    def _auth_response(principal: TokenPrincipal, token: str | None = None) -> TokenResponse:
        return TokenResponse(
            token=token,
            subject=principal.subject,
            scopes=sorted(principal.scopes),
            expires_at=principal.expires_at,
            rotate_after=principal.rotate_after,
            rotation_eligible=principal.can_rotate(),
        )

    def _set_auth_cookie(response: Response, token: str, expires_at: int) -> None:
        response.set_cookie(
            AUTH_COOKIE_NAME,
            token,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=max(0, expires_at - int(time.time())),
        )

    def _current_token(request: Request) -> tuple[str, bool]:
        authorization = request.headers.get("Authorization")
        if authorization:
            # The security dependency has already validated the Bearer wire format.
            return authorization.partition(" ")[2].strip(), False
        # The security dependency has already required and validated one of these sources.
        return request.cookies[AUTH_COOKIE_NAME], True

    async def auth_login(
        login: LoginRequest,
        request: Request,
        response: Response,
    ) -> TokenResponse:
        if token_store is None or user_credentials is None:
            raise HTTPException(status_code=401, detail="invalid username or password")
        normalized_username = login.username.strip()
        ip_allowed, ip_retry_after = login_ip_rate_limiter.check(_client_host(request))
        if not ip_allowed:
            raise HTTPException(
                status_code=429,
                detail="login rate limit exceeded",
                headers={"Retry-After": str(ip_retry_after)},
            )
        user = await user_credentials.authenticate(login.username, login.password)
        if user is None:
            username_allowed, username_retry_after = login_username_rate_limiter.check(
                normalized_username
            )
            if not username_allowed:
                raise HTTPException(
                    status_code=429,
                    detail="login rate limit exceeded",
                    headers={"Retry-After": str(username_retry_after)},
                )
            raise HTTPException(
                status_code=401,
                detail="invalid username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        moderation_service.require_allowed(ModerationIdentity(IdentityKind.WEB, user.username))
        login_username_rate_limiter.reset(normalized_username)
        token, principal = token_store.issue(
            user.username,
            user.scopes,
            automatic_rotation=True,
        )
        if login.delivery == "cookie":
            _set_auth_cookie(response, token, principal.expires_at)
            return _auth_response(principal)
        return _auth_response(principal, token)

    async def auth_rotate(
        request: Request,
        response: Response,
        principal: TokenPrincipal,
    ) -> TokenResponse:
        del principal
        token, cookie_delivery = _current_token(request)
        try:
            replacement, replacement_principal = token_store.rotate(token)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if cookie_delivery:
            _set_auth_cookie(response, replacement, replacement_principal.expires_at)
            return _auth_response(replacement_principal)
        return _auth_response(replacement_principal, replacement)

    async def auth_logout(
        request: Request,
        response: Response,
        principal: TokenPrincipal,
    ) -> dict[str, bool]:
        del principal
        token, _cookie_delivery = _current_token(request)
        token_store.revoke_token(token)
        response.delete_cookie(
            AUTH_COOKIE_NAME,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {"ok": True}

    def _feature_status() -> FeatureStatusResponse:
        return FeatureStatusResponse(
            mcp=mcp_enabled(plugins),
            character_chat=character_chat is not None,
            allow_sleeping_character_chat=(
                character_chat.allow_sleeping_character_chat
                if character_chat is not None
                else False
            ),
            character_sheets=character_sheets,
            image_generation=imagegen is not None,
            video_generation=videogen is not None,
            chat_image_generation=character_chat is not None and imagegen is not None,
            chat_video_generation=character_chat is not None and videogen is not None,
            character_chat_media_tools=(
                character_chat is not None
                and character_chat_media_tools
                and (imagegen is not None or videogen is not None)
            ),
        )

    @app.middleware("http")
    async def _enforce_request_rate_limit(request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path == "/v1/public/health":
            return await call_next(request)
        allowed, retry_after = rate_limiter.check(_client_host(request))
        if not allowed:
            return _problem_response(
                request,
                429,
                "request rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    def _request_matches_route(request: Request) -> bool:
        for route in app.router.routes:
            match, _child_scope = route.matches(request.scope)
            if match in {Match.FULL, Match.PARTIAL}:
                return True
        return False

    @app.middleware("http")
    async def _enforce_authentication(request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"
        if request.method == "OPTIONS" or not _request_matches_route(request):
            return await call_next(request)
        surface = classify_authorization_surface(path)
        if surface is AuthorizationSurface.PUBLIC or (
            path == "/v1/auth/session" and request.method == "POST"
        ):
            return await call_next(request)
        surface = cast(AuthorizationSurface, surface)
        if authenticator is None:
            if allow_unauthenticated_embedding:
                pass
            else:
                return _problem_response(
                    request,
                    401,
                    "bearer token required",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        else:
            required_scope = SURFACE_SCOPES[surface]
            try:
                authenticator.authenticate_request(
                    request,
                    required_scopes=(required_scope,) if required_scope else (),
                )
            except HTTPException as exc:
                raw_authorization = request.headers.get("Authorization", "")
                raw_token = (
                    raw_authorization.partition(" ")[2].strip()
                    if raw_authorization.lower().startswith("bearer ")
                    else request.cookies.get(AUTH_COOKIE_NAME, "")
                )
                restricted_subject = (
                    token_store.subject_for_credential(raw_token)
                    if token_store is not None and raw_token
                    else None
                )
                if restricted_subject is not None:
                    restriction = moderation_store.restriction(
                        ModerationIdentity(IdentityKind.WEB, restricted_subject)
                    )
                    if restriction is not None:
                        return _problem_response(
                            request,
                            403,
                            f"account is {restriction.kind.value}",
                            code=f"account_{restriction.kind.value}",
                            restriction=restriction,
                        )
                allowed, retry_after = token_failure_rate_limiter.check(_client_host(request))
                if not allowed:
                    return _problem_response(
                        request,
                        429,
                        "token verification rate limit exceeded",
                        headers={"Retry-After": str(retry_after)},
                    )
                return _problem_response(
                    request,
                    exc.status_code,
                    exc.detail,
                    headers=exc.headers or {},
                )
        client_id = _client_id_header_value(request.headers.get(CLIENT_ID_HEADER.lower()))
        if client_id is None:
            return _problem_response(
                request,
                403,
                f"{CLIENT_ID_HEADER} header is required",
                code="client_id_required",
            )
        if len(client_id) > IDENTIFIER_MAX_LENGTH:
            return _problem_response(
                request,
                422,
                f"{CLIENT_ID_HEADER} header is too long",
                code="validation_error",
            )
        try:
            if surface is AuthorizationSurface.ADMIN:
                _require_allowed_admin_client_id(client_id)
            else:
                _require_allowed_player_client_id(client_id)
        except HTTPException as exc:
            return _problem_response(
                request,
                exc.status_code,
                exc.detail,
                headers=exc.headers or {},
            )
        principal = getattr(request.state, "auth_principal", None)
        identity = (
            ModerationIdentity(IdentityKind.WEB, principal.subject)
            if isinstance(principal, TokenPrincipal)
            else ModerationIdentity(IdentityKind.CLIENT, client_id)
        )
        try:
            moderation_service.require_allowed(identity)
        except ModerationRestrictedError as exc:
            return _problem_response(
                request,
                403,
                f"account is {exc.restriction.kind.value}",
                code=f"account_{exc.restriction.kind.value}",
                restriction=exc.restriction,
            )
        return await call_next(request)

    @app.middleware("http")
    async def _set_hsts(request: Request, call_next):
        response = await call_next(request)
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = HSTS_VALUE
        return response

    # Starlette prepends middleware. CORS is registered at the end of app construction so
    # it wraps this body cap as well as HSTS, authentication, and both request limiters.
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=max_request_body_bytes)

    from ..foundation.media.plugin import plugin as media_plugin

    router_plugins = [
        media_plugin(),
        *(plugin for plugin in (plugins or ()) if plugin.id != "bunnyland.media"),
    ]
    for plugin in router_plugins:
        for contribution in plugin.runtime.http:
            prefix = (
                "/v1/public"
                if plugin.id == "bunnyland.media"
                else f"/v1/{contribution.zone.value}/extensions/{plugin.id}"
            )
            router = APIRouter(prefix=prefix)
            for registrar in contribution.registrars:
                registrar(
                    router,
                    actor,
                    meta=meta,
                    loop=loop,
                    save_path=save_path,
                    definitions_path=definitions_path,
                    worldgen_options=worldgen_options,
                    plugins=plugins or (),
                    media_store=media_store,
                    addon_media=addon_media,
                    play_websocket_auth=play_websocket_auth,
                )
            for route in router.routes:
                local_path = route.path.removeprefix(prefix)
                if classify_authorization_surface(local_path) is not None:
                    raise ValueError(
                        f"addon {plugin.id!r} attempted absolute or cross-zone route {local_path!r}"
                    )
            app.router.routes.extend(router.routes)

    def _character_entity(character_id: str):
        parsed = parse_entity_id(character_id)
        if parsed is None or not actor.world.has_entity(parsed):
            raise HTTPException(status_code=404, detail="character does not exist")
        character = actor.world.get_entity(parsed)
        if not character.has_component(CharacterComponent):
            raise HTTPException(status_code=400, detail="entity is not a character")
        return character

    def _resume_web_claim_for_command(command, claim_context):
        character, controller, claim = claim_context
        if controller.has_component(WebControllerComponent):
            web = controller.get_component(WebControllerComponent)
            if web.client_id != claim.client_id:
                raise HTTPException(
                    status_code=409,
                    detail="claim is not active for this web client",
                )
            return command
        web_controller = _web_controller_for_client(claim.client_id)
        if web_controller is None:
            web_controller = spawn_transient_controller(
                actor.world,
                WebControllerComponent(client_id=claim.client_id, label=claim.label or "web"),
            )
        transfer_claim(controller, web_controller)
        add_claim(
            web_controller,
            client_kind=CLIENT_KIND_WEB,
            client_id=claim.client_id,
            character_id=claim.character_id,
            label=claim.label,
            claim_id=claim.claim_id,
            now_unix=claim.claimed_at_unix,
        )
        generation = actor.assign_controller(character.id, web_controller.id)
        record_claim_activity(web_controller, now_unix=int(time.time()))
        if character.has_component(SuspendedComponent):
            character.remove_component(SuspendedComponent)
        retire_transient_controller(actor, controller.id)
        return replace(
            command,
            controller_id=str(web_controller.id),
            controller_generation=generation,
        )

    def _claim_secret(request) -> str | None:
        return getattr(request, "_claim_secret", None)

    def _with_claim_secret(request, claim_secret: str | None):
        if not isinstance(claim_secret, str):
            claim_secret = None
        request._claim_secret = claim_secret
        return request

    def _client_id_header_value(client_id: object) -> str | None:
        if not isinstance(client_id, str):
            return None
        normalized = client_id.strip()
        return normalized or None

    def _required_client_id(client_id: object) -> str:
        normalized = _client_id_header_value(client_id)
        if normalized is None:
            raise HTTPException(status_code=403, detail=f"{CLIENT_ID_HEADER} header is required")
        return normalized

    def _claim_owner_for_request(request: Request, client_id: str) -> ClaimOwner:
        principal = getattr(request.state, "auth_principal", None)
        subject = (
            principal.subject
            if isinstance(principal, TokenPrincipal)
            else f"embedded:{client_id}"
        )
        return ClaimOwner("rest", subject)

    def _http_origin_is_trusted(request: Request) -> bool:
        origin = request.headers.get("Origin")
        if not origin or origin in {"null", "*"}:
            return False
        return origin == f"{request.url.scheme}://{request.url.netloc}"

    def _request_claim_secret(
        request: Request,
        header_secret: str | None,
        *,
        require_origin: bool = True,
    ) -> str | None:
        cookie_secret = request.cookies.get(CLAIM_COOKIE_NAME)
        if (
            header_secret
            and cookie_secret
            and not secrets.compare_digest(header_secret, cookie_secret)
        ):
            raise HTTPException(status_code=401, detail="conflicting claim credentials")
        if cookie_secret and require_origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not _http_origin_is_trusted(request):
                raise HTTPException(status_code=403, detail="same-origin request required")
        return header_secret or cookie_secret

    def _set_claim_cookie(response: Response, claim_secret: str) -> None:
        response.set_cookie(
            CLAIM_COOKIE_NAME,
            claim_secret,
            secure=True,
            httponly=True,
            samesite="strict",
            path=CLAIM_COOKIE_PATH,
        )

    def _existing_controller(controller_id: str):
        parsed = parse_entity_id(controller_id)
        if parsed is None or not actor.world.has_entity(parsed):
            return None
        kind = actor._controller_kind(parsed)
        if kind == "unknown":
            raise HTTPException(status_code=400, detail="entity is not a controller")
        return actor.world.get_entity(parsed), kind

    def _fallback_controller(fallback: str, timeout: ClaimTimeoutComponent):
        selected = (
            fallback.strip() if fallback and fallback.strip() else timeout.fallback_controller
        )
        existing = _existing_controller(selected)
        if existing is not None:
            return existing
        normalized = selected.strip().lower().replace("_", "-")
        if normalized in {"suspend", "suspended", "offline"}:
            controller = spawn_transient_controller(
                actor.world,
                SuspendedControllerComponent(reason=timeout.fallback_reason),
            )
            return controller, "suspended"
        if normalized in {"llm", "ai", "agent"}:
            controller = spawn_transient_controller(
                actor.world,
                LLMControllerComponent(
                    profile_name=timeout.llm_profile_name or "default",
                    model=timeout.llm_model
                    or os.environ.get("BUNNYLAND_CHARACTER_MODEL", "deepseek-v4-flash"),
                    provider=timeout.llm_provider or "ollama",
                ),
            )
            return controller, "llm"
        raise HTTPException(status_code=400, detail="fallback_controller is not a controller")

    async def world_character_chat(
        service: CharacterChatService,
        id: str,
        request: CharacterChatRequest,
        *,
        chat_tools: tuple[ChatOnlyTool, ...] = (),
        access: CharacterChatAccess = CharacterChatAccess.PUBLIC,
    ) -> CharacterChatResponse:
        try:
            with telemetry.span("character.chat", {"character.id": id}) as span:
                try:
                    if chat_tools:
                        response = await service.chat(
                            id,
                            request,
                            chat_tools=chat_tools,
                            access=access,
                        )
                    elif access is CharacterChatAccess.PUBLIC:
                        response = await service.chat(id, request)
                    else:
                        response = await service.chat(id, request, access=access)
                except Exception as exc:
                    span.record_exception(exc)
                    telemetry.mark_span_error(str(exc), span)
                    raise
                telemetry.mark_span_ok(span)
                return response
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TypeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            detail = str(exc)
            status = 400 if detail == "entity is not a character" else 404
            raise HTTPException(status_code=status, detail=detail) from exc

    def _runtime_timing() -> dict:
        now = time.time()
        tick_seconds = getattr(loop, "tick_seconds", None) if loop is not None else None
        time_scale = getattr(loop, "time_scale", None) if loop is not None else None
        next_tick_at_unix = getattr(loop, "next_tick_at_unix", None) if loop is not None else None
        if (
            next_tick_at_unix is None
            and tick_seconds is not None
            and loop is not None
            and getattr(loop, "running", False)
            and not getattr(loop, "paused", False)
        ):
            next_tick_at_unix = now + float(tick_seconds)
        return {
            "generated_at_unix": now,
            "next_tick_at_unix": next_tick_at_unix,
            "tick_seconds": float(tick_seconds) if tick_seconds is not None else None,
            "time_scale": float(time_scale) if time_scale is not None else None,
            "game_seconds_per_tick": (
                float(tick_seconds) * float(time_scale)
                if tick_seconds is not None and time_scale is not None
                else None
            ),
        }

    def _runtime_response() -> WorldRuntimeResponse:
        if loop is None:
            raise HTTPException(status_code=409, detail="server runtime is not attached")
        return WorldRuntimeResponse(
            world_epoch=actor.epoch,
            paused=loop.paused,
            running=loop.running,
            **_runtime_timing(),
        )

    def _memory_document_view(document) -> MemoryDocumentView:
        return MemoryDocumentView(
            id=document.id,
            document=document.document,
            metadata=dict(document.metadata),
        )

    def _memory_characters_response() -> MemoryCharactersResponse:
        characters = []
        query = actor.world.query().with_all(
            [CharacterComponent, IdentityComponent, MemoryProfileComponent]
        )
        for character in query.execute_entities():
            identity = character.get_component(IdentityComponent)
            profile = character.get_component(MemoryProfileComponent)
            characters.append(
                MemoryCharacterView(
                    character_id=str(character.id),
                    name=identity.name,
                    private_collection=profile.vector_collection,
                    shared_collections=list(profile.shared_collections),
                )
            )
        characters.sort(key=lambda item: (item.name.lower(), item.character_id))
        return MemoryCharactersResponse(world_epoch=actor.epoch, characters=characters)

    def _memory_documents_response(collection: str) -> MemoryDocumentsResponse:
        store = _require_memory_store()
        documents = [
            _memory_document_view(document) for document in store.list_documents(collection)
        ]
        return MemoryDocumentsResponse(
            world_epoch=actor.epoch,
            collection=collection,
            documents=documents,
        )

    def _memory_update_response(
        collection: str,
        note_id: str,
        request: MemoryDocumentUpdateRequest,
    ) -> MemoryDocumentResponse:
        store = _require_memory_store()
        document = store.update_document(
            collection,
            note_id,
            document=request.document,
            metadata=request.metadata,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="memory document not found")
        return MemoryDocumentResponse(
            world_epoch=actor.epoch,
            collection=collection,
            document=_memory_document_view(document),
        )

    def _memory_create_response(
        collection: str,
        request: MemoryDocumentUpdateRequest,
    ) -> MemoryDocumentResponse:
        store = _require_memory_store()
        document = store.create_document(
            collection,
            document=request.document,
            metadata=request.metadata,
        )
        return MemoryDocumentResponse(
            world_epoch=actor.epoch,
            collection=collection,
            document=_memory_document_view(document),
        )

    def _delete_memory_document(collection: str, note_id: str) -> dict:
        store = _require_memory_store()
        if not store.delete(collection, note_id):
            raise HTTPException(status_code=404, detail="memory document not found")
        return {"ok": True, "schema_version": 1, "world_epoch": actor.epoch}

    async def _cancel_command_request(
        character_id: str,
        command_id: str,
    ) -> CommandCancelResponse:
        command = await actor.cancel_command(character_id, command_id)
        if command is None:
            return CommandCancelResponse(
                ok=False,
                command_id=command_id,
                cancelled=False,
                reason="command not found",
            )
        return CommandCancelResponse(ok=True, command_id=command_id, cancelled=True)

    async def _submit_command_request(request: CommandRequest, claim_context) -> CommandResponse:
        if request.command_type in CONTROL_COMMANDS:
            # Control verbs reassign a character's controller (spec 7.4) and deliberately
            # bypass the generation/ownership gates that protect normal actions, taking their
            # target controller straight from the payload. They are a server orchestration
            # primitive, not a player action: web clients change controllers through the
            # dedicated /world/controllers/web/* endpoints, which validate that the caller
            # owns the claim. Accepting them on the generic command surface would let any
            # claim holder repoint their character at an arbitrary controller entity.
            raise HTTPException(
                status_code=400,
                detail="control verbs are not accepted here; use the web controller endpoints",
            )
        command = request.to_submitted(submitted_at_epoch=actor.epoch)
        async with actor._lock:
            command = _resume_web_claim_for_command(command, claim_context)
        outcome = await actor.submit(command)
        return CommandResponse(
            queued=outcome.accepted,
            command_id=outcome.command_id,
            reason=outcome.reason,
        )

    def _web_claim_response(
        request: WebControllerFallbackRequest,
        *,
        controller,
        claim: ClaimedComponent,
        generation: int,
        claim_secret: str,
    ) -> WebControllerFallbackResponse:
        timeout = controller.get_component(ClaimTimeoutComponent)
        return WebControllerFallbackResponse(
            character_id=request.character_id,
            controller_id=str(controller.id),
            controller_generation=generation,
            claim_id=claim.claim_id,
            claim_secret=claim_secret,
            fallback_controller=timeout.fallback_controller,
            timeout_seconds=timeout.timeout_seconds,
        )

    def _web_controller_for_client(client_id: str):
        return matching_controller(
            actor,
            WebControllerComponent,
            lambda controller: controller.client_id == client_id,
        )

    async def _claim_web_controller_request(
        request: WebControllerClaimRequest,
        owner: ClaimOwner,
    ) -> WebControllerClaimResponse:
        character_id = parse_entity_id(request.character_id)
        if character_id is None or not actor.world.has_entity(character_id):
            raise HTTPException(status_code=404, detail="character does not exist")
        character = actor.world.get_entity(character_id)
        if not character.has_component(CharacterComponent):
            raise HTTPException(status_code=400, detail="entity is not a character")
        try:
            ensure_character_control_claim_allowed(actor, character)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        client_id = request.client_id.strip()
        _require_allowed_player_client_id(client_id)
        label = request.label.strip() or "web"

        with telemetry.span(
            "controller.web_claim",
            {
                "character.id": request.character_id,
                "client.id": client_id,
                "client.label": label,
            },
        ) as span:
            created = False
            async with actor._lock:
                active = current_controller(actor, character)
                active_controller = active[0] if active is not None else None
                active_claim = (
                    controller_claim(active_controller) if active_controller is not None else None
                )
                claim_id = None
                claim_secret = _claim_secret(request)
                validated_claim_secret = False
                if active_claim is not None:
                    if not claim_client_matches(active_claim, client_id):
                        raise HTTPException(
                            status_code=409,
                            detail="character is already claimed",
                        )
                    try:
                        ensure_claim_secret(
                            claim_secrets,
                            active_claim,
                            owner=owner,
                            claim_id=request.claim_id,
                            claim_secret=_claim_secret(request),
                        )
                    except PermissionError as exc:
                        raise HTTPException(status_code=403, detail=str(exc)) from exc
                    validated_claim_secret = True
                    claim_id = active_claim.claim_id

                controller = _web_controller_for_client(client_id)
                if controller is not None:
                    existing_claim = controller_claim(controller)
                    if existing_claim is not None and existing_claim.character_id != str(
                        character_id
                    ):
                        controller = None
                if controller is None:
                    created = True
                    controller = spawn_transient_controller(
                        actor.world,
                        WebControllerComponent(client_id=client_id, label=label),
                    )
                if active_claim is not None and active_controller is not None:
                    transfer_claim(active_controller, controller)
                claim = add_claim(
                    controller,
                    client_kind=CLIENT_KIND_WEB,
                    client_id=client_id,
                    character_id=str(character_id),
                    label=label,
                    claim_id=claim_id,
                    now_unix=int(time.time()),
                )
                if (
                    not validated_claim_secret
                    or claim_secret is None
                    or not claim_secrets.has_secret(claim.claim_id)
                ):
                    claim_secret = claim_secrets.issue(claim.claim_id, owner)
                apply_claim_timeout_settings(
                    controller,
                    now_unix=int(time.time()),
                    fallback_controller=request.fallback_controller,
                    fallback_reason=request.fallback_reason,
                    llm_profile_name=request.llm_profile_name,
                    llm_model=request.llm_model,
                    llm_provider=request.llm_provider,
                    timeout_seconds=request.timeout_seconds,
                    reset_activity=True,
                )

                generation = actor.current_generation(character_id, controller.id)
                assigned = generation is None
                if generation is None:
                    generation = actor.assign_controller(character_id, controller.id)
                    if character.has_component(SuspendedComponent):
                        character.remove_component(SuspendedComponent)
                    await actor.bus.publish(
                        ControllerChangedEvent(
                            **actor._event_base(
                                actor_id=str(character_id),
                                generation=generation,
                                controller_kind="web",
                            )
                        )
                    )
                    await actor.bus.publish(
                        CharacterClaimedEvent(
                            **actor._event_base(
                                actor_id=str(character_id),
                                character_id=str(character_id),
                                controller_id=str(controller.id),
                                generation=generation,
                            )
                        )
                    )
                if active_controller is not None:
                    retire_transient_controller(actor, active_controller.id)

            timeout = controller.get_component(ClaimTimeoutComponent)
            span.set_attribute("controller.id", str(controller.id))
            span.set_attribute("controller.generation", generation)
            span.set_attribute("controller.created", created)
            span.set_attribute("controller.assigned", assigned)
            span.set_attribute("claim.fallback_controller", timeout.fallback_controller)
            span.set_attribute("claim.timeout_seconds", timeout.timeout_seconds)
            logger.info(
                "web claim character=%s controller=%s generation=%s "
                "client_id=%s label=%s assigned=%s created=%s",
                character_id,
                controller.id,
                generation,
                client_id,
                label,
                assigned,
                created,
            )

            stream.broadcast({"type": "snapshot", "data": serialize_world(actor, meta)})
            response = _web_claim_response(
                request,
                controller=controller,
                claim=claim,
                generation=generation,
                claim_secret=claim_secret or "",
            )
            return WebControllerClaimResponse(**response.model_dump())

    async def _web_controller_fallback_request(
        request: WebControllerFallbackRequest,
        claim_context,
    ) -> WebControllerFallbackResponse:
        character, controller, edge, claim = claim_context
        client_id = request.client_id.strip()

        with telemetry.span(
            "controller.web_fallback",
            {"character.id": request.character_id, "client.id": client_id},
        ) as span:
            async with actor._lock:
                apply_claim_timeout_settings(
                    controller,
                    now_unix=int(time.time()),
                    fallback_controller=request.fallback_controller,
                    fallback_reason=request.fallback_reason,
                    llm_profile_name=request.llm_profile_name,
                    llm_model=request.llm_model,
                    llm_provider=request.llm_provider,
                    timeout_seconds=request.timeout_seconds,
                    reset_activity=False,
                )
                timeout = controller.get_component(ClaimTimeoutComponent)
                span.set_attribute("controller.id", str(controller.id))
                span.set_attribute("controller.generation", edge.generation)
                span.set_attribute("claim.fallback_controller", timeout.fallback_controller)
                span.set_attribute("claim.timeout_seconds", timeout.timeout_seconds)
                logger.info(
                    "web claim fallback character=%s controller=%s generation=%s "
                    "client_id=%s fallback=%s timeout_seconds=%s",
                    character.id,
                    controller.id,
                    edge.generation,
                    client_id,
                    timeout.fallback_controller,
                    timeout.timeout_seconds,
                )
                return _web_claim_response(
                    request,
                    controller=controller,
                    claim=claim,
                    generation=edge.generation,
                    claim_secret=_claim_secret(request) or "",
                )

    async def _release_web_controller_to_fallback_request(
        request: WebControllerFallbackRequest,
        claim_context,
    ) -> WebControllerFallbackResponse:
        character, controller, _edge, claim = claim_context
        timeout = (
            controller.get_component(ClaimTimeoutComponent)
            if controller.has_component(ClaimTimeoutComponent)
            else apply_claim_timeout_settings(
                controller,
                now_unix=int(time.time()),
                fallback_controller=request.fallback_controller,
            )
        )
        fallback = request.fallback_controller or timeout.fallback_controller
        async with actor._lock:
            new_controller, kind = _fallback_controller(fallback, timeout)
            transfer_claim(controller, new_controller)
            if kind == "suspended":
                generation = actor.suspend(
                    character.id,
                    new_controller.id,
                    reason=timeout.fallback_reason,
                )
            else:
                generation = actor.assign_controller(character.id, new_controller.id)
                if character.has_component(SuspendedComponent):
                    character.remove_component(SuspendedComponent)
            retire_transient_controller(actor, controller.id)
            await actor.bus.publish(
                ControllerChangedEvent(
                    **actor._event_base(
                        actor_id=str(character.id),
                        generation=generation,
                        controller_kind=kind,
                    )
                )
            )
        return WebControllerFallbackResponse(
            character_id=str(character.id),
            controller_id=str(new_controller.id),
            controller_generation=generation,
            claim_id=claim.claim_id,
            claim_secret=_claim_secret(request) or "",
            fallback_controller=kind,
            timeout_seconds=timeout.timeout_seconds,
        )

    async def _release_web_claim_request(
        request: WebControllerFallbackRequest,
        claim_context,
    ) -> ClaimReleaseResponse:
        character, controller, _edge, claim = claim_context
        remove_claim(controller, claim_secrets)
        return ClaimReleaseResponse(
            character_id=str(character.id),
            controller_id=str(controller.id),
            claim_id=claim.claim_id,
        )

    async def _patch_world_request(request: WorldPatchRequest) -> WorldPatchResponse:
        try:
            with telemetry.span("world.patch", {"operation.count": len(request.operations)}):
                async with actor._lock:
                    response = apply_world_patch(actor, request)
        except WorldPatchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        stream.broadcast(
            {
                "type": "patch",
                "data": {
                    "world_epoch": response.world_epoch,
                    "changed_entities": [entity["id"] for entity in response.changed_entities],
                    "deleted_entities": response.deleted_entities,
                },
            }
        )
        return response

    async def _assign_controller_request(
        request: ControllerAssignmentRequest,
    ) -> WorldPatchResponse:
        with telemetry.span(
            "controller.assign",
            {"character.id": request.character_id, "controller.id": request.controller_id},
        ):
            character_id = parse_entity_id(request.character_id)
            if character_id is None or not actor.world.has_entity(character_id):
                raise HTTPException(status_code=404, detail="character does not exist")
            controller_id = parse_entity_id(request.controller_id)
            if controller_id is None or not actor.world.has_entity(controller_id):
                raise HTTPException(status_code=404, detail="controller does not exist")

            async with actor._lock:
                character = actor.world.get_entity(character_id)
                if not character.has_component(CharacterComponent):
                    raise HTTPException(status_code=400, detail="entity is not a character")
                controller = actor.world.get_entity(controller_id)
                kind = actor._controller_kind(controller_id)
                if kind == "unknown":
                    raise HTTPException(status_code=400, detail="entity is not a controller")
                current = current_controller(actor, character)
                if current is not None and current[0].id != controller_id:
                    transfer_claim(current[0], controller)
                if kind == "suspended":
                    reason = controller.get_component(SuspendedControllerComponent).reason
                    generation = actor.suspend(character_id, controller_id, reason=reason)
                else:
                    generation = actor.assign_controller(character_id, controller_id)
                    if character.has_component(SuspendedComponent):
                        character.remove_component(SuspendedComponent)

                response = WorldPatchResponse(
                    world_epoch=actor.epoch,
                    changed_entities=[serialize_entity(actor, character)],
                )

            await actor.bus.publish(
                ControllerChangedEvent(
                    **actor._event_base(
                        actor_id=str(character_id),
                        generation=generation,
                        controller_kind=kind,
                    )
                )
            )
            stream.broadcast(
                {
                    "type": "patch",
                    "data": {
                        "world_epoch": response.world_epoch,
                        "changed_entities": [str(character_id)],
                        "deleted_entities": [],
                    },
                }
            )
            return response

    def _controller_definitions_response() -> ControllerDefinitionListResponse:
        return ControllerDefinitionListResponse(
            scripts=sorted(script_names()),
            behaviors=sorted(behavior_tree_names()),
            condition_library=sorted(condition_library_names()),
            action_library=sorted(action_library_names()),
            stored=StoredControllerDefinitions(**definition_store.snapshot()),
        )

    async def _register_script_request(spec: ScriptSpec) -> ControllerDefinitionListResponse:
        try:
            async with actor._lock:
                definition_store.add_script(spec)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _controller_definitions_response()

    async def _register_behavior_request(
        spec: BehaviorTreeSpec,
    ) -> ControllerDefinitionListResponse:
        try:
            async with actor._lock:
                definition_store.add_behavior(spec)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _controller_definitions_response()

    def _list_world_generators_response() -> WorldGeneratorListResponse:
        return WorldGeneratorListResponse(
            generators=[
                WorldGeneratorInfo(
                    name=generator.name,
                    description=generator.description,
                    group=generator.group,
                    uses_seed=generator.uses_seed,
                )
                for generator in sorted(generator_registry.values(), key=lambda item: item.name)
            ]
        )

    async def _world_generation_status_response() -> WorldGenerationStatusResponse:
        if generation_job is None:
            return idle_generation_status(actor)
        return generation_job.status_response(actor)

    def _internal_error(operation: str, exc: Exception) -> HTTPException:
        """Log the cause and return a generic 500.

        These paths used to put ``str(exc)`` straight into the response, which leaks file
        paths and upstream provider messages to the caller. Admin-scoped, but the detail
        belongs in the log.
        """

        logger.exception("%s failed", operation)
        return HTTPException(status_code=500, detail=f"{operation} failed")

    async def _generate_world_request(request: WorldGenerateRequest) -> WorldGenerateResponse:
        nonlocal generation_job
        if not request.confirm_reset:
            raise HTTPException(status_code=400, detail="confirm_reset must be true")
        if not plugins:
            raise HTTPException(
                status_code=409,
                detail="server was not started with a world generator registry",
            )
        if generation_job is not None and generation_job.status == "running":
            raise HTTPException(status_code=409, detail="world generation is already running")

        default_generator = "recursive" if "recursive" in generator_registry else "oneshot"
        generator_name = (request.generator or default_generator).strip()
        generator = generator_registry.get(generator_name)
        if generator is None:
            names = ", ".join(sorted(generator_registry)) or "(none)"
            raise HTTPException(
                status_code=400,
                detail=f"unknown generator {generator_name!r}; available: {names}",
            )

        options = worldgen_options or GenOptions()
        options = replace(options, generator_config=dict(request.generator_config))
        if request.max_rooms is not None:
            options = replace(options, max_rooms=request.max_rooms)
        if generator.uses_seed:
            seed = (request.seed or meta.seed or "a quiet marsh").strip() or "a quiet marsh"
        else:
            seed = generator.name
        try:
            generation_job = await start_world_generation(
                actor,
                plugins=plugins,
                generator=generator,
                seed=seed,
                options=options,
                meta=meta,
                save_path=save_path,
                save=request.save,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise _internal_error("world generation", exc) from exc

        stream.broadcast({"type": "snapshot", "data": serialize_world(actor, meta)})
        return generation_job.response(actor)

    async def _generate_room_request(
        request: WorldRoomGenerationRequest,
    ) -> WorldRoomGenerationResponse:
        try:
            return await generate_room_patch(actor, request, options=worldgen_options)
        except WorldPatchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise _internal_error("room generation", exc) from exc

    async def _generate_character_request(
        request: WorldCharacterGenerationRequest,
    ) -> WorldCharacterGenerationResponse:
        try:
            return await generate_character_patch(actor, request, options=worldgen_options)
        except WorldPatchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise _internal_error("character generation", exc) from exc

    async def _generate_item_request(
        request: WorldItemGenerationRequest,
    ) -> WorldItemGenerationResponse:
        try:
            return await generate_item_patch(actor, request, options=worldgen_options)
        except WorldPatchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise _internal_error("item generation", exc) from exc

    async def _generate_event_request(
        request: WorldEventGenerationRequest,
    ) -> WorldEventGenerationResponse:
        try:
            return await generate_event_patch(actor, request, options=worldgen_options)
        except WorldPatchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise _internal_error("event generation", exc) from exc

    async def _generate_image_request(
        request: WorldImageGenerationRequest,
    ) -> WorldImageGenerationResponse:
        service = _require_imagegen()
        purpose = _parse_purpose(request.purpose)
        job = await service.start(
            request.entity_id,
            purpose,
            template_name=request.template,
            extra=request.extra,
            alpha=request.alpha,
            force=request.force,
        )
        return _image_response(job)

    async def _generate_video_request(
        request: WorldVideoGenerationRequest,
    ) -> WorldImageGenerationResponse:
        service = _require_videogen()
        job = await service.start(
            request.entity_id,
            template_name=request.template,
            extra=request.extra,
            force=request.force,
        )
        return _image_response(job)

    async def _scene_image_request(
        character_id: str,
        event_id: str = "",
    ) -> WorldImageGenerationResponse | None:
        # Shared by the player scene-image endpoint and the MCP camera tool. Both callers
        # guarantee imagegen is configured and the character exists; this returns None only
        # when there is no room to illustrate, which each caller renders as its own error.
        service = _require_imagegen()
        try:
            job = await request_scene_image(
                actor, service, character_id=character_id, event_id=event_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if job is None:
            return None
        return _image_response(job)

    async def _scene_video_request(
        character_id: str,
        event_id: str = "",
    ) -> WorldImageGenerationResponse | None:
        service = _require_videogen()
        try:
            job = await request_scene_video(
                actor, service, character_id=character_id, event_id=event_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if job is None:
            return None
        return _image_response(job)

    async def save_world_now() -> WorldSaveResponse:
        if save_path is None:
            raise HTTPException(status_code=409, detail="server was not started with --save")
        try:
            async with actor._lock:
                return save_configured_world(actor, save_path, meta=meta)
        except Exception as exc:
            raise _internal_error("world save", exc) from exc

    # Formal v1 routers -----------------------------------------------------
    #
    # The preview handlers above remain useful private adapters while the domain services
    # are extracted.  Only these routers are retained in the returned application, so there
    # are no compatibility aliases at runtime.
    public_v1 = APIRouter(prefix="/v1/public", tags=["public"])
    auth_v1 = APIRouter(prefix="/v1/auth", tags=["auth"])
    profile_v1 = APIRouter(prefix="/v1/profile", tags=["profile"])
    chat_v1 = APIRouter(prefix="/v1/chat", tags=["chat"])
    play_v1 = APIRouter(prefix="/v1/play", tags=["play"])
    admin_v1 = APIRouter(prefix="/v1/admin", tags=["admin"])

    def _world_fields() -> dict[str, str | int]:
        return {"world_id": str(actor.world_id), "world_epoch": actor.epoch}

    def _job_status_v1(status: str) -> Literal["queued", "running", "succeeded", "failed"]:
        normalized = status.strip().lower()
        if normalized in {"queued", "running", "succeeded", "failed"}:
            return cast(Literal["queued", "running", "succeeded", "failed"], normalized)
        if normalized in {"complete", "completed", "duplicate", "skipped"}:
            return "succeeded"
        return "failed"

    def _problem_for_job_v1(detail: str) -> ProblemDetails:
        return ProblemDetails(
            title="Job failed",
            status=500,
            detail=detail or "job failed",
            code="job_failed",
        )

    def _without_preview_fields(value) -> dict:
        data = value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)
        data.pop("ok", None)
        data.pop("schema_version", None)
        return data

    def _refresh_media_job_v1(job: JobResource) -> JobResource:
        service = _require_videogen() if "video" in job.kind else _require_imagegen()
        current = service.job(job.id)
        if current is None:
            return job
        status = _job_status_v1(str(current.status))
        return job.model_copy(
            update={
                "world_epoch": actor.epoch,
                "status": status,
                "updated_at": datetime.now(UTC),
                "result": JOB_RESULT_ADAPTER.validate_python(
                    _without_preview_fields(_image_response(current))
                ),
                "failure": (
                    _problem_for_job_v1(
                        str(
                            current.error
                            or (
                                "video generation failed"
                                if job.kind == "video"
                                else "image generation failed"
                            )
                        )
                    )
                    if status == "failed"
                    else None
                ),
            }
        )

    async def _refresh_generation_job_v1(job: JobResource) -> JobResource:
        if job.kind in {"image", "video"}:
            return _refresh_media_job_v1(job)
        if job.kind != "world" or generation_job is None or generation_job.job_id != job.id:
            return job
        current = await _world_generation_status_response()
        status = _job_status_v1(current.status)
        return job.model_copy(
            update={
                "world_epoch": actor.epoch,
                "status": status,
                "updated_at": datetime.now(UTC),
                "result": JOB_RESULT_ADAPTER.validate_python(_without_preview_fields(current)),
                "failure": (
                    _problem_for_job_v1(str(current.error or "world generation failed"))
                    if status == "failed"
                    else None
                ),
            }
        )

    def _claim_context_v1(
        claim_id: str,
        claim_secret: str | None,
        client_id: str,
        owner: ClaimOwner,
    ):
        _require_allowed_player_client_id(client_id)
        for controller in actor.world.query().with_all([ClaimedComponent]).execute_entities():
            claim = controller.get_component(ClaimedComponent)
            if claim.claim_id != claim_id:
                continue
            if not claim_client_matches(claim, client_id):
                raise HTTPException(status_code=403, detail="claim belongs to another client")
            try:
                ensure_claim_secret(
                    claim_secrets,
                    claim,
                    owner=owner,
                    claim_id=claim_id,
                    claim_secret=claim_secret,
                )
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            character = _character_entity(claim.character_id)
            active = current_controller(actor, character)
            if active is None or active[0].id != controller.id:
                raise HTTPException(status_code=409, detail="claim controller is not active")
            return character, controller, active[1], claim
        raise HTTPException(status_code=404, detail="claim does not exist")

    def _claim_resource_v1(character, controller, edge, claim) -> ClaimResource:
        timeout = (
            controller.get_component(ClaimTimeoutComponent)
            if controller.has_component(ClaimTimeoutComponent)
            else ClaimTimeoutComponent()
        )
        return ClaimResource(
            **_world_fields(),
            id=claim.claim_id,
            character_id=str(character.id),
            client_id=claim.client_id,
            controller_id=str(controller.id),
            controller_generation=edge.generation,
            control="active" if controller.has_component(WebControllerComponent) else "fallback",
            fallback_controller=timeout.fallback_controller,
            timeout_seconds=timeout.timeout_seconds,
        )

    @public_v1.get("/health", status_code=204, response_class=Response)
    async def v1_health() -> Response:
        return Response(status_code=204)

    @public_v1.get("/features", response_model=FeatureStatusResponse)
    async def v1_features() -> FeatureStatusResponse:
        return _feature_status()

    @public_v1.get("/world", response_model=PublicWorldResource)
    async def v1_public_world() -> PublicWorldResource:
        return PublicWorldResource(
            **_world_fields(),
            title=actor.world_info.title,
            description=actor.world_info.description,
            content_flags=list(world_content_flags(actor)),
        )

    @auth_v1.post("/session", response_model=TokenResponse)
    async def v1_create_session(
        login: LoginRequest,
        request: Request,
        response: Response,
    ) -> TokenResponse:
        return await auth_login(login, request, response)

    @auth_v1.get("/session", response_model=AuthMeResponse)
    async def v1_get_session(request: Request) -> AuthMeResponse:
        if authenticator is None:
            raise HTTPException(status_code=503, detail="authentication is not configured")
        principal = request.state.auth_principal
        return AuthMeResponse(
            subject=principal.subject,
            scopes=sorted(principal.scopes),
            expires_at=principal.expires_at,
            rotate_after=principal.rotate_after,
            rotation_eligible=principal.can_rotate(),
        )

    @auth_v1.patch("/session", response_model=TokenResponse)
    async def v1_rotate_session(request: Request, response: Response) -> TokenResponse:
        return await auth_rotate(request, response, request.state.auth_principal)

    @auth_v1.delete("/session", status_code=204, response_class=Response)
    async def v1_delete_session(request: Request, response: Response) -> Response:
        await auth_logout(request, response, request.state.auth_principal)
        response.status_code = 204
        response.body = b""
        return response

    @play_v1.get("/characters", response_model=CharacterCollection)
    async def v1_characters() -> CharacterCollection:
        lobby = serialize_character_list(actor)
        return CharacterCollection(
            **_world_fields(),
            characters=[
                CharacterResource(
                    id=item.character_id,
                    name=item.name,
                    kind=item.kind,
                    suspended=item.suspended,
                )
                for item in lobby.characters
            ],
        )

    @profile_v1.get("/characters", response_model=CharacterCollection)
    async def v1_profile_characters() -> CharacterCollection:
        return await v1_characters()

    @profile_v1.get("/characters/{character_id}", response_model=CharacterProfileResource)
    async def v1_character_profile(
        character_id: str, request: Request
    ) -> CharacterProfileResource:
        principal = getattr(request.state, "auth_principal", None)
        reveal_controller_identity = isinstance(
            principal, TokenPrincipal
        ) and scope_granted(principal.scopes, WORLD_ADMIN_SCOPE)
        try:
            projection = serialize_character_projection(
                actor,
                character_id,
                reveal_controller_identity=reveal_controller_identity,
            )
        except ValueError as exc:
            detail = str(exc)
            status = 400 if detail == "entity is not a character" else 404
            raise HTTPException(status_code=status, detail=detail) from exc
        room = projection.room.model_copy(update={"entities": [], "exits": []})
        sheet = projection.sheet
        if not character_sheets:
            sheet = sheet.model_copy(
                update={
                    "status": [],
                    "vitals": [],
                    "needs": [],
                    "affect": [],
                    "relations": [],
                    "injuries": [],
                    "notes": [],
                }
            )
        return CharacterProfileResource(
            **_world_fields(),
            character_id=projection.character_id,
            character_name=projection.character_name,
            portrait=projection.portrait,
            room=room,
            points=projection.points,
            controller=projection.controller,
            sheet=sheet,
        )

    def _request_subject(request: Request) -> str | None:
        principal = getattr(request.state, "auth_principal", None)
        return principal.subject if isinstance(principal, TokenPrincipal) else None

    def _chat_job_attributes(character_id: str, subject: str | None) -> dict[str, str | None]:
        return {"character_id": character_id, "subject": subject}

    async def _start_chat_media_job(
        *,
        character_id: str,
        kind: Literal["chat_image", "chat_video"],
        direction: ChatMediaCreativeDirection,
        history_summary: str,
        history: list[CharacterChatHistoryMessage],
        current_message: str,
        client_id: str,
        subject: str | None,
        requester: Literal["player", "character"],
    ) -> JobResource:
        rate_key = subject if subject is not None else client_id
        allowed, retry_after = scene_image_rate_limiter.check(rate_key)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="chat media rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
        service = _require_videogen() if kind == "chat_video" else _require_imagegen()
        media_subject, scene = await capture_chat_media_scene(
            actor,
            service,
            character_id,
        )
        extra = chat_media_prompt_context(
            history_summary=history_summary,
            history=history,
            direction=direction,
            current_message=current_message,
        )
        requested_by = f"chat:{requester}:{subject or client_id}"
        if kind == "chat_video":
            generated = await service.start_ephemeral(
                character_id,
                subject=media_subject,
                scene=scene,
                requested_by=requested_by,
                extra=extra,
                on_complete=_chat_media_job_completed,
            )
        else:
            generated = await service.start_ephemeral(
                character_id,
                subject=media_subject,
                scene=scene,
                requested_by=requested_by,
                extra=extra,
                on_complete=_chat_media_job_completed,
            )
        now = datetime.now(UTC)
        job = JobResource(
            **_world_fields(),
            id=generated.job_id,
            kind=kind,
            status=_job_status_v1(generated.status),
            created_at=now,
            updated_at=now,
            result=JOB_RESULT_ADAPTER.validate_python(
                _without_preview_fields(_image_response(generated))
            ),
        )
        chat_media_job_registry.put(
            job,
            owner=client_id,
            attributes=_chat_job_attributes(character_id, subject),
        )
        return job

    def _character_media_tools(
        *,
        character_id: str,
        body: ChatJobRequest,
        client_id: str,
        subject: str | None,
    ) -> tuple[ChatOnlyTool, ...]:
        if not character_chat_media_tools or not body.allow_character_media:
            return ()

        def build(kind: Literal["chat_image", "chat_video"]) -> ChatOnlyTool:
            async def request_media(
                _parsed_character_id: EntityId,
                _request: CharacterChatRequest,
                arguments: dict[str, JsonValue],
            ) -> CharacterChatActionResult:
                direction = ChatMediaCreativeDirection.model_validate(arguments)
                try:
                    job = await _start_chat_media_job(
                        character_id=character_id,
                        kind=kind,
                        direction=direction,
                        history_summary=body.history_summary,
                        history=body.history,
                        current_message=body.message,
                        client_id=client_id,
                        subject=subject,
                        requester="character",
                    )
                except HTTPException as exc:
                    return CharacterChatActionResult(
                        status="rejected",
                        reason=str(exc.detail),
                    )
                except (TypeError, ValueError) as exc:
                    return CharacterChatActionResult(
                        status="rejected",
                        reason=str(exc),
                    )
                return CharacterChatActionResult(
                    status="executed",
                    reason=(
                        "Illustration queued. Visual actions do not happen in Bunnyland "
                        "and do not change world state."
                    ),
                    media_job=CharacterChatMediaJobReference(
                        id=job.id,
                        kind=kind,
                        status=job.status,
                    ),
                )

            return chat_media_tool(kind, request_media)

        tools: list[ChatOnlyTool] = []
        if imagegen is not None:
            tools.append(build("chat_image"))
        if videogen is not None:
            tools.append(build("chat_video"))
        return tuple(tools)

    def _chat_job_for_client(
        job_id: str, character_id: str, client_id: str, subject: str | None
    ) -> JobResource:
        job = character_chat_job_registry.get(
            job_id,
            owner=client_id,
            attributes=_chat_job_attributes(character_id, subject),
        )
        if job is None:
            raise HTTPException(status_code=404, detail="chat job does not exist")
        return job

    def _chat_job(job_id: str) -> JobResource | None:
        return character_chat_job_registry.get(job_id)

    def _chat_job_failure(character_id: str, job_id: str, detail: str) -> None:
        job = _chat_job(job_id)
        if job is None:
            return
        character_chat_job_registry.update(
            job.model_copy(
                update={
                    "status": "failed",
                    "updated_at": datetime.now(UTC),
                    "failure": ProblemDetails(
                        title="Chat failed",
                        status=409,
                        detail=detail,
                        code="chat_unavailable",
                        instance=f"/v1/chat/characters/{character_id}/jobs/{job_id}",
                    ),
                }
            )
        )

    async def _run_character_chat_job(
        service: CharacterChatService,
        character_id: str,
        job_id: str,
        body: ChatJobRequest,
        client_id: str,
        subject: str | None,
        access: CharacterChatAccess,
    ) -> None:
        def _patch_job(**update) -> None:
            current = _chat_job(job_id)
            if current is not None:
                character_chat_job_registry.update(
                    current.model_copy(update={"updated_at": datetime.now(UTC), **update})
                )

        _patch_job(status="running")
        try:
            result = await world_character_chat(
                service,
                character_id,
                CharacterChatRequest(
                    client_id=client_id,
                    claim_id=body.claim_id,
                    message=body.message,
                    history_summary=body.history_summary,
                    history=body.history,
                    allow_character_media=body.allow_character_media,
                ),
                chat_tools=_character_media_tools(
                    character_id=character_id,
                    body=body,
                    client_id=client_id,
                    subject=subject,
                ),
                access=access,
            )
        except Exception as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            _chat_job_failure(character_id, job_id, str(detail))
            return
        final_result = result
        if result.action.status == "queued" and result.action.command_id:
            _patch_job(result=JOB_RESULT_ADAPTER.validate_python(_without_preview_fields(result)))
            # Bounded wait. This used to be an unbounded `while True`, so a queued action
            # that never completed left the task polling at 4 Hz for the life of the
            # process, and every such job added another one.
            deadline = (
                asyncio.get_running_loop().time() + CHAT_JOB_PENDING_TIMEOUT_SECONDS
            )
            while True:
                await asyncio.sleep(CHAT_JOB_POLL_SECONDS)
                try:
                    pending = await service.pending_result(
                        character_id,
                        client_id,
                        result.action.command_id,
                    )
                except Exception as exc:
                    _chat_job_failure(character_id, job_id, str(exc))
                    return
                _patch_job(
                    result=JOB_RESULT_ADAPTER.validate_python(_without_preview_fields(pending))
                )
                if pending.complete:
                    final_result = pending
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    _chat_job_failure(
                        character_id,
                        job_id,
                        "the character did not finish this action in time",
                    )
                    return
        _patch_job(
            status="succeeded",
            result=JOB_RESULT_ADAPTER.validate_python(_without_preview_fields(final_result)),
        )

    @chat_v1.post(
        "/characters/{character_id}/jobs",
        response_model=JobResource,
        status_code=202,
    )
    async def v1_submit_character_chat_job(
        character_id: str,
        body: ChatJobRequest,
        response: Response,
        request: Request,
        client_id: str = Header(alias=CLIENT_ID_HEADER),
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
    ) -> JobResource:
        if character_chat is None:
            raise HTTPException(
                status_code=404,
                detail="character chat is not enabled",
                headers={"X-Bunnyland-Problem-Code": "chat_disabled"},
            )
        parsed = parse_entity_id(character_id)
        if parsed is None or not actor.world.has_entity(parsed):
            raise HTTPException(status_code=404, detail="character does not exist")
        character = actor.world.get_entity(parsed)
        if not character.has_component(CharacterComponent):
            raise HTTPException(status_code=400, detail="entity is not a character")
        if character.has_component(DeadComponent):
            raise HTTPException(status_code=409, detail="dead character is not available to chat")
        if character.has_component(DownedComponent):
            raise HTTPException(
                status_code=409,
                detail="unconscious character is not available to chat",
            )
        if character.has_component(
            SleepingComponent
        ) and not character_chat.allow_sleeping_character_chat:
            raise HTTPException(
                status_code=409,
                detail="sleeping character cannot be interrupted by chat",
            )
        if character.has_component(SuspendedComponent):
            raise HTTPException(
                status_code=409,
                detail="suspended character must be activated before chatting",
            )
        normalized_client_id = _required_client_id(client_id)
        principal = getattr(request.state, "auth_principal", None)
        access = (
            CharacterChatAccess.ADMIN
            if isinstance(principal, TokenPrincipal)
            and WORLD_ADMIN_SCOPE in principal.scopes
            else CharacterChatAccess.PUBLIC
        )
        if body.claim_id is not None and access is CharacterChatAccess.PUBLIC:
            claim_secret = _request_claim_secret(request, claim_secret)
            claimed_character, _claimed_controller, _edge, _claim = _claim_context_v1(
                body.claim_id,
                claim_secret,
                normalized_client_id,
                _claim_owner_for_request(request, normalized_client_id),
            )
            if claimed_character.id != parsed:
                raise HTTPException(status_code=403, detail="claim controls another character")
            access = CharacterChatAccess.CONTROLLER
        # Cap chat submissions per authenticated caller (falling back to client id when the
        # server runs unauthenticated). Chat is the most abuse-prone mutation because it
        # drives model inference or pages a human controller.
        subject = _request_subject(request)
        rate_key = subject if subject is not None else normalized_client_id
        allowed, retry_after = chat_job_rate_limiter.check(rate_key)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="character chat rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
        active = current_controller(actor, character)
        if active is None:
            raise HTTPException(status_code=409, detail="character has no controller")
        controller, _edge = active
        controller_kind = actor._controller_kind(controller.id)
        created = datetime.now(UTC)
        job = JobResource(
            **_world_fields(),
            id=uuid4().hex,
            kind="chat",
            status="queued",
            created_at=created,
            updated_at=created,
        )
        character_chat_job_registry.put(
            job,
            owner=normalized_client_id,
            attributes=_chat_job_attributes(character_id, subject),
        )
        response.headers["Location"] = f"/v1/chat/characters/{character_id}/jobs/{job.id}"
        if controller_kind == "llm":
            if not open_character_chat:
                _chat_job_failure(character_id, job.id, "open character chat is disabled")
            else:
                task = asyncio.create_task(
                    _run_character_chat_job(
                        character_chat,
                        character_id,
                        job.id,
                        body,
                        normalized_client_id,
                        subject,
                        access,
                    )
                )
                chat_tasks.add(task)
                chat_tasks_by_job[job.id] = task

                def _chat_task_done(done: asyncio.Task[None], *, job_id: str = job.id) -> None:
                    chat_tasks.discard(done)
                    chat_tasks_by_job.pop(job_id, None)

                task.add_done_callback(_chat_task_done)
        elif controller_kind in {"discord", "mcp", "web"}:
            await actor.bus.publish(
                CharacterChatRequestedEvent(
                    **event_base(
                        actor.epoch,
                        default_visibility=EventVisibility.PRIVATE,
                        target_ids=(character_id,),
                    ),
                    job_id=job.id,
                    message=body.message,
                )
            )
        else:
            _chat_job_failure(
                character_id,
                job.id,
                f"{controller_kind} character is not available to chat",
            )
        stored = _chat_job(job.id)
        return (stored or job).model_copy(deep=True)

    @chat_v1.get(
        "/characters/{character_id}/jobs/{job_id}",
        response_model=JobResource,
    )
    async def v1_character_chat_job(
        character_id: str,
        job_id: str,
        request: Request,
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> JobResource:
        if character_chat is None:
            raise HTTPException(
                status_code=404,
                detail="character chat is not enabled",
                headers={"X-Bunnyland-Problem-Code": "chat_disabled"},
            )
        normalized_client_id = _required_client_id(client_id)
        return _chat_job_for_client(
            job_id, character_id, normalized_client_id, _request_subject(request)
        )

    @chat_v1.post(
        "/characters/{character_id}/media-jobs",
        response_model=JobResource,
        status_code=202,
    )
    async def v1_submit_chat_media_job(
        character_id: str,
        body: ChatMediaJobRequest,
        request: Request,
        response: Response,
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> JobResource:
        if character_chat is None:
            raise HTTPException(status_code=404, detail="character chat is not enabled")
        normalized_client_id = _required_client_id(client_id)
        subject = _request_subject(request)
        try:
            job = await _start_chat_media_job(
                character_id=character_id,
                kind=body.kind,
                direction=ChatMediaCreativeDirection(
                    focus=body.focus,
                    scene_action=body.scene_action,
                    mood=body.mood,
                    composition=body.composition,
                    style_notes=body.style_notes,
                ),
                history_summary=body.history_summary,
                history=body.history,
                current_message="",
                client_id=normalized_client_id,
                subject=subject,
                requester="player",
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        response.headers["Location"] = (
            f"/v1/chat/characters/{character_id}/media-jobs/{job.id}"
        )
        return job

    @chat_v1.get(
        "/characters/{character_id}/media-jobs/{job_id}",
        response_model=JobResource,
    )
    async def v1_chat_media_job(
        character_id: str,
        job_id: str,
        request: Request,
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> JobResource:
        normalized_client_id = _required_client_id(client_id)
        job = chat_media_job_registry.get(
            job_id,
            owner=normalized_client_id,
            attributes=_chat_job_attributes(character_id, _request_subject(request)),
        )
        if job is None:
            raise HTTPException(status_code=404, detail="chat media job does not exist")
        refreshed = _refresh_media_job_v1(job)
        chat_media_job_registry.update(refreshed)
        return refreshed

    @play_v1.get("/catalog", response_model=CatalogResource)
    async def v1_catalog() -> CatalogResource:
        schema = world_schema(actor)
        return CatalogResource(
            **_world_fields(),
            components=schema.components,
            edges=schema.edges,
            content=load_content_library(),
            queries=[definition.name for definition in actor.perspective_queries.definitions()],
            capabilities=_feature_status(),
        )

    @play_v1.post("/claims", response_model=ClaimResource, status_code=201)
    async def v1_create_claim(
        body: ClaimCreateRequest,
        request: Request,
        response: Response,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> ClaimResource:
        normalized_client_id = _required_client_id(client_id)
        if body.delivery == "cookie" and not _http_origin_is_trusted(request):
            raise HTTPException(status_code=403, detail="same-origin request required")
        owner = _claim_owner_for_request(request, normalized_client_id)
        claim_secret = _request_claim_secret(request, claim_secret)
        request = WebControllerClaimRequest(
            character_id=body.character_id,
            client_id=normalized_client_id,
            claim_id=None,
            label=body.label,
            fallback_controller=body.fallback_controller,
            fallback_reason=body.fallback_reason,
            llm_profile_name=body.llm_profile_name,
            llm_model=body.llm_model,
            llm_provider=body.llm_provider,
            timeout_seconds=body.timeout_seconds,
        )
        _with_claim_secret(request, claim_secret)
        claimed = await _claim_web_controller_request(request, owner)
        response.headers["Location"] = f"/v1/play/claims/{claimed.claim_id}"
        if body.delivery == "cookie":
            _set_claim_cookie(response, claimed.claim_secret)
        else:
            response.headers["X-Bunnyland-Claim-Secret"] = claimed.claim_secret
        character, controller, edge, claim = _claim_context_v1(
            claimed.claim_id, claimed.claim_secret, normalized_client_id, owner
        )
        onboarding.claimed(claimed.claim_id, str(character.id))
        return _claim_resource_v1(character, controller, edge, claim)

    @play_v1.put("/claims/{claim_id}", response_model=ClaimResource)
    async def v1_reclaim_claim(
        claim_id: str,
        request: Request,
        response: Response,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> ClaimResource:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(request, normalized_client_id)
        claim_secret = _request_claim_secret(request, claim_secret)
        character, controller, _edge, claim = _claim_context_v1(
            claim_id,
            claim_secret,
            normalized_client_id,
            owner,
        )
        timeout = (
            controller.get_component(ClaimTimeoutComponent)
            if controller.has_component(ClaimTimeoutComponent)
            else ClaimTimeoutComponent()
        )
        request = WebControllerClaimRequest(
            character_id=str(character.id),
            client_id=claim.client_id,
            claim_id=claim_id,
            label=claim.label or "web",
            fallback_controller=timeout.fallback_controller,
            fallback_reason=timeout.fallback_reason,
            llm_profile_name=timeout.llm_profile_name,
            llm_model=timeout.llm_model,
            llm_provider=timeout.llm_provider,
            timeout_seconds=timeout.timeout_seconds or None,
        )
        _with_claim_secret(request, claim_secret)
        await _claim_web_controller_request(request, owner)
        replacement_secret = claim_secrets.rotate(claim_id, owner)
        response.headers["X-Bunnyland-Claim-Secret"] = replacement_secret
        context = _claim_context_v1(
            claim_id,
            replacement_secret,
            normalized_client_id,
            owner,
        )
        return _claim_resource_v1(*context)

    @play_v1.post("/claims/{claim_id}/recover", response_model=ClaimResource)
    async def v1_recover_claim(
        claim_id: str,
        request: Request,
        response: Response,
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> ClaimResource:
        if not _http_origin_is_trusted(request):
            raise HTTPException(status_code=403, detail="same-origin request required")
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(request, normalized_client_id)
        async with actor._lock:
            for controller in actor.world.query().with_all([ClaimedComponent]).execute_entities():
                claim = controller.get_component(ClaimedComponent)
                if claim.claim_id != claim_id:
                    continue
                if not claim_client_matches(claim, normalized_client_id):
                    raise HTTPException(status_code=403, detail="claim belongs to another client")
                if not claim_secrets.validate_owner(claim_id, owner):
                    raise HTTPException(status_code=403, detail="claim belongs to another owner")
                character = _character_entity(claim.character_id)
                active = current_controller(actor, character)
                if active is None or active[0].id != controller.id:
                    raise HTTPException(status_code=409, detail="claim controller is not active")
                replacement_secret = claim_secrets.rotate(claim_id, owner)
                _set_claim_cookie(response, replacement_secret)
                return _claim_resource_v1(character, controller, active[1], claim)
        raise HTTPException(status_code=404, detail="claim does not exist")

    @play_v1.patch("/claims/{claim_id}", response_model=ClaimResource)
    async def v1_patch_claim(
        claim_id: str,
        body: ClaimUpdateRequest,
        http_request: Request,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> ClaimResource:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(http_request, normalized_client_id)
        claim_secret = _request_claim_secret(http_request, claim_secret)
        character, controller, _edge, claim = _claim_context_v1(
            claim_id, claim_secret, normalized_client_id, owner
        )
        control_request = WebControllerFallbackRequest(
            character_id=str(character.id),
            client_id=claim.client_id,
            claim_id=claim_id,
        )
        _with_claim_secret(control_request, claim_secret)
        if body.kind == "fallback":
            control_request = WebControllerFallbackRequest(
                character_id=str(character.id),
                client_id=claim.client_id,
                claim_id=claim_id,
                fallback_controller=body.fallback_controller,
                fallback_reason=body.fallback_reason,
                llm_profile_name=body.llm_profile_name,
                llm_model=body.llm_model,
                llm_provider=body.llm_provider,
                timeout_seconds=body.timeout_seconds,
            )
            _with_claim_secret(control_request, claim_secret)
            await _web_controller_fallback_request(
                control_request, (character, controller, _edge, claim)
            )
        elif body.desired == "fallback":
            await _release_web_controller_to_fallback_request(
                control_request, (character, controller, _edge, claim)
            )
        elif not controller.has_component(WebControllerComponent):
            reclaim = WebControllerClaimRequest(
                character_id=str(character.id),
                client_id=claim.client_id,
                claim_id=claim_id,
                label=claim.label or "web",
            )
            _with_claim_secret(reclaim, claim_secret)
            await _claim_web_controller_request(reclaim, owner)
        return _claim_resource_v1(
            *_claim_context_v1(claim_id, claim_secret, normalized_client_id, owner)
        )

    @play_v1.delete("/claims/{claim_id}", status_code=204, response_class=Response)
    async def v1_delete_claim(
        claim_id: str,
        http_request: Request,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> Response:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(http_request, normalized_client_id)
        claim_secret = _request_claim_secret(http_request, claim_secret)
        character, _controller, _edge, claim = _claim_context_v1(
            claim_id, claim_secret, normalized_client_id, owner
        )
        control_request = WebControllerFallbackRequest(
            character_id=str(character.id), client_id=claim.client_id, claim_id=claim_id
        )
        _with_claim_secret(control_request, claim_secret)
        await _release_web_claim_request(
            control_request, (character, _controller, _edge, claim)
        )
        response = Response(status_code=204)
        response.delete_cookie(
            CLAIM_COOKIE_NAME,
            secure=True,
            httponly=True,
            samesite="strict",
            path=CLAIM_COOKIE_PATH,
        )
        return response

    @play_v1.post(
        "/claims/{claim_id}/chat-jobs/{job_id}",
        response_model=JobResource,
    )
    async def v1_reply_to_character_chat_job(
        claim_id: str,
        job_id: str,
        body: CharacterChatReplyRequest,
        request: Request,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> JobResource:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(request, normalized_client_id)
        claim_secret = _request_claim_secret(request, claim_secret)
        character, _controller, _edge, _claim = _claim_context_v1(
            claim_id, claim_secret, normalized_client_id, owner
        )
        # The claim holder is the character's controller, not necessarily the client that
        # opened the chat job, so this matches on the character rather than the job owner.
        job = character_chat_job_registry.get(
            job_id, attributes={"character_id": str(character.id)}
        )
        if job is None:
            raise HTTPException(status_code=404, detail="chat job does not exist")
        if job.status not in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="chat job is already complete")
        result = CharacterChatResponse(
            world_epoch=actor.epoch,
            character_id=str(character.id),
            reply=body.reply,
        )
        replied = job.model_copy(
            update={
                "status": "succeeded",
                "updated_at": datetime.now(UTC),
                "result": JOB_RESULT_ADAPTER.validate_python(_without_preview_fields(result)),
            }
        )
        character_chat_job_registry.update(replied)
        return replied

    @play_v1.get("/claims/{claim_id}/projection", response_model=ClaimProjectionResource)
    async def v1_claim_projection(
        claim_id: str,
        request: Request,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> ClaimProjectionResource:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(request, normalized_client_id)
        claim_secret = _request_claim_secret(request, claim_secret)
        character, controller, edge, claim = _claim_context_v1(
            claim_id, claim_secret, normalized_client_id, owner
        )
        projection = serialize_character_projection(actor, str(character.id))
        room_id = projection.room.id
        room_projection = serialize_room_projection(actor, room_id) if room_id else None
        scene = (
            ClaimSceneResource(
                world_epoch=room_projection.world_epoch,
                room=room_projection.room,
            )
            if room_projection is not None
            else None
        )
        queued = serialize_character_queued_commands(actor, str(character.id), **_runtime_timing())
        character_data = ClaimCharacterResource.model_validate(
            projection.model_dump(
                exclude={"ok", "schema_version", "sheet", "actions"},
                mode="json",
            )
        )
        return ClaimProjectionResource(
            **_world_fields(),
            claim=_claim_resource_v1(character, controller, edge, claim),
            character=character_data,
            scene=scene,
            commands=queued.commands,
            sheet=projection.sheet,
            actions=projection.actions,
        )

    @play_v1.post(
        "/claims/{claim_id}/commands",
        response_model=CommandResource,
        status_code=202,
        responses={
            409: {
                "model": ProblemDetails,
                "description": (
                    "The command was refused. `code` distinguishes the reason: "
                    "`insufficient_points` when the character cannot afford the action, "
                    "`cost_mismatch`/`lane_mismatch` when the client's stated cost or lane "
                    "disagrees with the action definition, `command_rejected` otherwise."
                ),
            },
            429: {
                "model": ProblemDetails,
                "description": "The bounded command queue is full; retry later.",
            },
        },
    )
    async def v1_submit_command(
        claim_id: str,
        body: ClaimCommandRequest,
        http_request: Request,
        response: Response,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> CommandResource:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(http_request, normalized_client_id)
        claim_secret = _request_claim_secret(http_request, claim_secret)
        character, controller, edge, _claim = _claim_context_v1(
            claim_id, claim_secret, normalized_client_id, owner
        )
        request = CommandRequest(
            character_id=str(character.id),
            controller_id=str(controller.id),
            controller_generation=edge.generation,
            claim_id=claim_id,
            command_type=body.command_type,
            payload=body.payload,
            cost=body.cost,
            lane=body.lane,
            on_insufficient_points=body.on_insufficient_points,
            expires_at_epoch=body.expires_at_epoch,
            expected_epoch=body.expected_epoch,
            command_id=body.id,
        )
        _with_claim_secret(request, claim_secret)
        # cost and lane are server-owned. A client may omit them; if it states them it is
        # asserting which action it thinks it is taking, so a disagreement is refused
        # instead of being executed as the server's (different, possibly costlier) action.
        try:
            actor.confirm_command_expectations(
                body.command_type,
                cost=request.expected_cost(),
                lane=body.lane,
            )
        except CommandExpectationError as exc:
            return _problem_response(
                http_request, 409, str(exc), code=f"{exc.field}_mismatch"
            )
        result = await _submit_command_request(request, (character, controller, _claim))
        if not result.queued:
            # A refused command is not "accepted". Returning 202 here made every synchronous
            # rejection -- an unaffordable action under DENY, an unknown verb, a failed
            # policy gate -- look like success to any client that reads the HTTP status
            # rather than the body. The reason keeps its own problem code so a client can
            # tell "you cannot afford this" from "that verb does not exist".
            overloaded = result.reason.endswith("command queue is full")
            return _problem_response(
                http_request,
                429 if overloaded else 409,
                result.reason or "command rejected",
                code=(
                    "queue_full"
                    if overloaded
                    else (
                        "insufficient_points"
                        if result.reason == "insufficient points"
                        else "command_rejected"
                    )
                ),
            )
        onboarding.command_submitted(claim_id, result.command_id, body.command_type)
        response.headers["Location"] = f"/v1/play/claims/{claim_id}/commands/{result.command_id}"
        return CommandResource(
            **_world_fields(),
            id=result.command_id,
            status="queued",
            reason=result.reason,
        )

    @play_v1.delete(
        "/claims/{claim_id}/commands/{command_id}",
        response_model=CommandResource,
    )
    async def v1_cancel_command(
        claim_id: str,
        command_id: str,
        request: Request,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> CommandResource:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(request, normalized_client_id)
        claim_secret = _request_claim_secret(request, claim_secret)
        character, controller, edge, _claim = _claim_context_v1(
            claim_id, claim_secret, normalized_client_id, owner
        )
        result = await _cancel_command_request(
            str(character.id),
            command_id,
        )
        return CommandResource(
            **_world_fields(),
            id=command_id,
            status="cancelled" if result.cancelled else "rejected",
            reason=result.reason,
        )

    @play_v1.post("/claims/{claim_id}/queries", response_model=PerspectiveQueryResult)
    async def v1_query_claim(
        claim_id: str,
        body: ClaimQueryRequest,
        request: Request,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> PerspectiveQueryResult:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(request, normalized_client_id)
        claim_secret = _request_claim_secret(request, claim_secret)
        character, _controller, _edge, _claim = _claim_context_v1(
            claim_id, claim_secret, normalized_client_id, owner
        )
        try:
            return actor.perspective_queries.execute(
                actor,
                body.query,
                body.arguments,
                actor_id=str(character.id),
                access="claim",
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (RuntimeError, TimeoutError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @play_v1.get("/claims/{claim_id}/events", response_model=EventCollection)
    async def v1_claim_events(
        claim_id: str,
        request: Request,
        since: int | None = None,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> EventCollection:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(request, normalized_client_id)
        claim_secret = _request_claim_secret(request, claim_secret)
        character, _controller, _edge, _claim = _claim_context_v1(
            claim_id, claim_secret, normalized_client_id, owner
        )
        if since is None:
            return EventCollection(
                **_world_fields(),
                events=recent_player_updates(actor, str(character.id), stream.recent_messages()),
            )
        events, complete, available_after = stream.changes_since(str(character.id), since)
        return EventCollection(
            **_world_fields(),
            events=events,
            complete=complete,
            available_after_epoch=available_after,
        )

    @play_v1.post("/claims/{claim_id}/jobs", response_model=JobResource, status_code=202)
    async def v1_submit_player_job(
        claim_id: str,
        body: SceneMediaJobRequest,
        request: Request,
        response: Response,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> JobResource:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(request, normalized_client_id)
        claim_secret = _request_claim_secret(request, claim_secret)
        character, _controller, _edge, _claim = _claim_context_v1(
            claim_id, claim_secret, normalized_client_id, owner
        )
        # Each of these starts GPU media generation, so cap them per caller the way chat
        # jobs are. The general request limiter is far too loose for work this expensive.
        subject = _request_subject(request)
        allowed, retry_after = scene_image_rate_limiter.check(
            subject if subject is not None else normalized_client_id
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=(
                    "scene video rate limit exceeded"
                    if body.kind == "scene_video"
                    else "scene image rate limit exceeded"
                ),
                headers={"Retry-After": str(retry_after)},
            )
        created = datetime.now(UTC)
        generated = (
            await _scene_video_request(str(character.id), body.event_id)
            if body.kind == "scene_video"
            else await _scene_image_request(str(character.id), body.event_id)
        )
        if generated is None:
            raise HTTPException(status_code=400, detail="character has no room to illustrate")
        job = JobResource(
            **_world_fields(),
            id=generated.job_id,
            kind=body.kind,
            status=_job_status_v1(generated.status),
            created_at=created,
            updated_at=datetime.now(UTC),
            result=JOB_RESULT_ADAPTER.validate_python(_without_preview_fields(generated)),
        )
        player_job_registry.put(job, owner=claim_id)
        response.headers["Location"] = f"/v1/play/claims/{claim_id}/jobs/{job.id}"
        return job

    @play_v1.get("/claims/{claim_id}/jobs", response_model=list[JobResource])
    async def v1_list_player_jobs(
        claim_id: str,
        request: Request,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> list[JobResource]:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(request, normalized_client_id)
        claim_secret = _request_claim_secret(request, claim_secret)
        _claim_context_v1(claim_id, claim_secret, normalized_client_id, owner)
        refreshed = [_refresh_media_job_v1(job) for job in player_job_registry.list_for(claim_id)]
        for job in refreshed:
            player_job_registry.update(job)
        return refreshed

    @play_v1.get("/claims/{claim_id}/jobs/{job_id}", response_model=JobResource)
    async def v1_get_player_job(
        claim_id: str,
        job_id: str,
        request: Request,
        claim_secret: str | None = Header(default=None, alias="X-Bunnyland-Claim-Secret"),
        client_id: str = Header(alias=CLIENT_ID_HEADER),
    ) -> JobResource:
        normalized_client_id = _required_client_id(client_id)
        owner = _claim_owner_for_request(request, normalized_client_id)
        claim_secret = _request_claim_secret(request, claim_secret)
        _claim_context_v1(claim_id, claim_secret, normalized_client_id, owner)
        job = player_job_registry.get(job_id, owner=claim_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job does not exist")
        refreshed = _refresh_media_job_v1(job)
        player_job_registry.update(refreshed)
        return refreshed

    @admin_v1.get("/world")
    async def v1_admin_world() -> dict:
        return {**_world_fields(), **_without_preview_fields(serialize_world_overview(actor))}

    @admin_v1.patch("/world")
    async def v1_patch_world(body: WorldPatchRequest) -> dict:
        return {**_world_fields(), **_without_preview_fields(await _patch_world_request(body))}

    @admin_v1.get("/world/snapshot")
    async def v1_world_snapshot() -> dict:
        with telemetry.span("world.snapshot"):
            snapshot = serialize_world(actor, meta)
        return {**snapshot, **_world_fields()}

    @admin_v1.get("/world/runtime")
    async def v1_runtime() -> dict:
        return {**_world_fields(), **_without_preview_fields(_runtime_response())}

    @admin_v1.patch("/world/runtime")
    async def v1_patch_runtime(body: RuntimePatchRequest) -> dict:
        if loop is None:
            raise HTTPException(status_code=409, detail="server runtime is not attached")
        publish = loop.pause() if body.paused else loop.resume()
        if publish is not None:
            await publish
        return {**_world_fields(), **_without_preview_fields(_runtime_response())}

    @admin_v1.post("/world/checkpoints", status_code=201)
    async def v1_checkpoint(body: CheckpointRequest | None = None) -> dict:
        del body
        return {**_world_fields(), **_without_preview_fields(await save_world_now())}

    @admin_v1.get("/world/events", response_model=EventCollection)
    async def v1_world_events() -> EventCollection:
        return EventCollection(**_world_fields(), events=stream.recent_messages())

    def _moderation_identity_resource(
        identity: ModerationIdentity,
    ) -> ModerationIdentityResource:
        return ModerationIdentityResource(kind=identity.kind, id=identity.id)

    def _moderation_action_resource(
        entry: ModerationAuditEntry,
    ) -> ModerationActionResource:
        return ModerationActionResource(
            id=entry.id,
            action=entry.action,
            target=_moderation_identity_resource(entry.target),
            administrator=_moderation_identity_resource(entry.actor),
            reason=entry.reason,
            created_at=entry.created_at,
            expires_at=entry.expires_at,
        )

    def _moderation_actor(request: Request) -> ModerationIdentity:
        principal = getattr(request.state, "auth_principal", None)
        if isinstance(principal, TokenPrincipal):
            return ModerationIdentity(IdentityKind.WEB, principal.subject)
        client_id = _required_client_id(request.headers.get(CLIENT_ID_HEADER))
        return ModerationIdentity(IdentityKind.CLIENT, client_id)

    @admin_v1.get(
        "/moderation/players",
        response_model=ModerationPlayerCollection,
    )
    async def v1_moderation_players(search: str | None = None) -> ModerationPlayerCollection:
        identities = (
            moderation_store.known_identities() | moderation_service.known_claim_identities()
        )
        admin_subjects: set[str] = set()
        if user_credentials is not None:
            for user in user_credentials.current_users():
                identities.add(ModerationIdentity(IdentityKind.WEB, user.username))
                if scope_granted(user.scopes, WORLD_ADMIN_SCOPE):
                    admin_subjects.add(user.username)
        if token_store is not None:
            for token in token_store.list_metadata():
                identities.add(ModerationIdentity(IdentityKind.WEB, token["subject"]))
        normalized_search = (search or "").strip().lower()
        players: list[ModerationPlayerResource] = []
        for identity in sorted(identities, key=lambda item: item.canonical.lower()):
            claims: list[ModerationClaimResource] = []
            for _controller, claim in moderation_service.claims_for(identity):
                character_name = ""
                parsed = parse_entity_id(claim.character_id)
                if parsed is not None and actor.world.has_entity(parsed):
                    character = actor.world.get_entity(parsed)
                    if character.has_component(IdentityComponent):
                        character_name = character.get_component(IdentityComponent).name
                claims.append(
                    ModerationClaimResource(
                        claim_id=claim.claim_id,
                        character_id=claim.character_id,
                        character_name=character_name,
                    )
                )
            restriction = moderation_store.restriction(identity)
            searchable = " ".join(
                [
                    identity.canonical,
                    *(claim.character_id for claim in claims),
                    *(claim.character_name for claim in claims),
                    restriction.kind.value if restriction is not None else "",
                ]
            ).lower()
            if normalized_search and normalized_search not in searchable:
                continue
            players.append(
                ModerationPlayerResource(
                    identity=_moderation_identity_resource(identity),
                    admin=(identity.kind is IdentityKind.WEB and identity.id in admin_subjects),
                    claims=claims,
                    restriction=(
                        ModerationRestrictionResource(
                            kind=restriction.kind,
                            reason=restriction.reason,
                            created_at=restriction.created_at,
                            expires_at=restriction.expires_at,
                        )
                        if restriction is not None
                        else None
                    ),
                )
            )
        return ModerationPlayerCollection(players=players)

    @admin_v1.get(
        "/moderation/actions",
        response_model=ModerationActionCollection,
    )
    async def v1_moderation_actions(
        target_kind: IdentityKind | None = None,
        target_id: str | None = None,
        action: ModerationActionKind | None = None,
        limit: int = 200,
    ) -> ModerationActionCollection:
        if (target_kind is None) != (target_id is None):
            raise HTTPException(
                status_code=422,
                detail="target_kind and target_id must be supplied together",
            )
        target = (
            ModerationIdentity(target_kind, target_id)
            if target_kind is not None and target_id is not None
            else None
        )
        return ModerationActionCollection(
            actions=[
                _moderation_action_resource(entry)
                for entry in moderation_store.history(target=target, action=action, limit=limit)
            ]
        )

    @admin_v1.post(
        "/moderation/actions",
        response_model=ModerationActionResource,
        status_code=201,
    )
    async def v1_create_moderation_action(
        body: ModerationActionRequest,
        request: Request,
    ) -> ModerationActionResource:
        try:
            target = ModerationIdentity(body.target.kind, body.target.id)
            entry = await moderation_service.execute(
                body.action,
                target,
                _moderation_actor(request),
                body.reason,
                duration_seconds=body.duration_seconds,
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=409,
                detail=str(exc),
                headers={"X-Bunnyland-Problem-Code": "moderation_self_target"},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _moderation_action_resource(entry)

    @admin_v1.get("/characters/{character_id}")
    async def v1_admin_character(character_id: str) -> dict:
        try:
            projection = serialize_dm_projection(actor, character_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**_world_fields(), **_without_preview_fields(projection)}

    @admin_v1.put("/characters/{character_id}/controller")
    async def v1_assign_controller(character_id: str, body: ControllerAssignment) -> dict:
        result = await _assign_controller_request(
            ControllerAssignmentRequest(character_id=character_id, controller_id=body.controller_id)
        )
        return {**_world_fields(), **_without_preview_fields(result)}

    @admin_v1.get("/controller-definitions", response_model=ControllerDefinitionsResource)
    async def v1_controller_definitions() -> ControllerDefinitionsResource:
        result = _controller_definitions_response()
        return ControllerDefinitionsResource(
            **_world_fields(),
            scripts=result.scripts,
            behaviors=result.behaviors,
            condition_library=result.condition_library,
            action_library=result.action_library,
            stored=result.stored,
        )

    @admin_v1.put(
        "/controller-definitions/{kind}/{name}",
        response_model=ControllerDefinitionsResource,
    )
    async def v1_put_controller_definition(
        kind: str, name: str, body: ControllerDefinitionRequest
    ) -> ControllerDefinitionsResource:
        data = {**body.definition, "name": name}
        if kind == "script":
            result = await _register_script_request(ScriptSpec.model_validate(data))
        elif kind == "behavior":
            result = await _register_behavior_request(BehaviorTreeSpec.model_validate(data))
        else:
            raise HTTPException(status_code=400, detail="kind must be script or behavior")
        return ControllerDefinitionsResource(
            **_world_fields(),
            scripts=result.scripts,
            behaviors=result.behaviors,
            condition_library=result.condition_library,
            action_library=result.action_library,
            stored=result.stored,
        )

    @admin_v1.get("/world/generators", response_model=GeneratorCollection)
    async def v1_generators() -> GeneratorCollection:
        result = _list_world_generators_response()
        return GeneratorCollection(**_world_fields(), generators=result.generators)

    @admin_v1.get("/media/workflows", response_model=list[WorkflowTemplate])
    async def v1_media_workflows() -> list[WorkflowTemplate]:
        return _require_workflow_templates().templates()

    @admin_v1.get("/media/workflows/{name}", response_model=WorkflowTemplate)
    async def v1_media_workflow(name: str) -> WorkflowTemplate:
        template = _require_workflow_templates().get(name)
        if template is None:
            raise HTTPException(status_code=404, detail="media workflow does not exist")
        return template

    @admin_v1.put("/media/workflows/{name}", response_model=WorkflowTemplate)
    async def v1_put_media_workflow(
        name: str, body: WorkflowTemplate
    ) -> WorkflowTemplate:
        if body.name != name:
            raise HTTPException(
                status_code=400,
                detail="media workflow name must match the request path",
            )
        return _require_workflow_templates().add_template(body)

    @admin_v1.delete("/media/workflows/{name}", status_code=204)
    async def v1_delete_media_workflow(name: str) -> Response:
        templates = _require_workflow_templates()
        if templates.get(name) is None:
            raise HTTPException(status_code=404, detail="media workflow does not exist")
        templates.remove_template(name)
        return Response(status_code=204)

    @admin_v1.post("/world/generation-jobs", response_model=JobResource, status_code=202)
    async def v1_submit_generation_job(
        body: GenerationJobRequest, response: Response
    ) -> JobResource:
        created = datetime.now(UTC)
        request_data = body.model_dump(exclude={"kind"})
        if body.kind == "world":
            result = await _generate_world_request(
                WorldGenerateRequest.model_validate(request_data)
            )
        elif body.kind == "room":
            result = await _generate_room_request(
                WorldRoomGenerationRequest.model_validate(request_data)
            )
        elif body.kind == "character":
            result = await _generate_character_request(
                WorldCharacterGenerationRequest.model_validate(request_data)
            )
        elif body.kind == "item":
            result = await _generate_item_request(
                WorldItemGenerationRequest.model_validate(request_data)
            )
        elif body.kind == "event":
            result = await _generate_event_request(
                WorldEventGenerationRequest.model_validate(request_data)
            )
        elif body.kind == "video":
            result = await _generate_video_request(
                WorldVideoGenerationRequest.model_validate(request_data)
            )
        else:
            result = await _generate_image_request(
                WorldImageGenerationRequest.model_validate(request_data)
            )
        result_data = _without_preview_fields(result)
        job_id = str(result_data.get("job_id") or uuid4().hex)
        status = _job_status_v1(str(result_data.get("status") or "succeeded"))
        job = JobResource(
            **_world_fields(),
            id=job_id,
            kind=body.kind,
            status=status,
            created_at=created,
            updated_at=datetime.now(UTC),
            result=JOB_RESULT_ADAPTER.validate_python(result_data),
        )
        generation_job_registry.put(job, owner=GENERATION_JOB_OWNER)
        response.headers["Location"] = f"/v1/admin/world/generation-jobs/{job.id}"
        refreshed = await _refresh_generation_job_v1(job)
        generation_job_registry.update(refreshed)
        return refreshed

    @admin_v1.get("/world/generation-jobs/{job_id}", response_model=JobResource)
    async def v1_generation_job(job_id: str) -> JobResource:
        job = generation_job_registry.get(job_id, owner=GENERATION_JOB_OWNER)
        if job is None:
            raise HTTPException(status_code=404, detail="generation job does not exist")
        refreshed = await _refresh_generation_job_v1(job)
        generation_job_registry.update(refreshed)
        return refreshed

    @admin_v1.get("/memory/collections")
    async def v1_memory_collections() -> dict:
        response = _memory_characters_response()
        collections = sorted(
            {
                collection
                for character in response.characters
                for collection in [character.private_collection, *character.shared_collections]
            }
        )
        return {
            **_world_fields(),
            "collections": collections,
            "characters": [item.model_dump(mode="json") for item in response.characters],
        }

    @admin_v1.get("/memory/collections/{collection}/documents")
    async def v1_memory_documents(collection: str) -> dict:
        return _without_preview_fields(_memory_documents_response(collection))

    @admin_v1.post("/memory/collections/{collection}/documents", status_code=201)
    async def v1_create_memory_document(
        collection: str, body: MemoryDocumentUpdateRequest, response: Response
    ) -> dict:
        result = _memory_create_response(collection, body)
        response.headers["Location"] = (
            f"/v1/admin/memory/collections/{collection}/documents/{result.document.id}"
        )
        return _without_preview_fields(result)

    @admin_v1.get("/memory/collections/{collection}/documents/{document_id}")
    async def v1_memory_document(collection: str, document_id: str) -> dict:
        result = _memory_documents_response(collection)
        document = next((item for item in result.documents if item.id == document_id), None)
        if document is None:
            raise HTTPException(status_code=404, detail="memory document not found")
        return {**_world_fields(), "collection": collection, "document": document.model_dump()}

    @admin_v1.put("/memory/collections/{collection}/documents/{document_id}")
    @admin_v1.patch("/memory/collections/{collection}/documents/{document_id}")
    async def v1_update_memory_document(
        collection: str, document_id: str, body: MemoryDocumentUpdateRequest
    ) -> dict:
        return _without_preview_fields(_memory_update_response(collection, document_id, body))

    @admin_v1.delete(
        "/memory/collections/{collection}/documents/{document_id}",
        status_code=204,
        response_class=Response,
    )
    async def v1_delete_memory_document(collection: str, document_id: str) -> Response:
        _delete_memory_document(collection, document_id)
        return Response(status_code=204)

    @admin_v1.put("/media/{target_kind}/{target_id}/{purpose}")
    async def v1_upload_media(
        target_kind: str, target_id: str, purpose: str, request: Request
    ) -> dict:
        if target_kind != "character" or purpose not in {"portrait", "sprite"}:
            raise HTTPException(status_code=400, detail="unsupported media upload target")
        form = await request.form()
        try:
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(status_code=400, detail="multipart file field is required")
            content_type = str(getattr(upload, "content_type", "")).lower()
            extension = UPLOAD_IMAGE_TYPES.get(content_type)
            if extension is None:
                raise HTTPException(
                    status_code=400, detail="upload must be a PNG, JPEG, or WebP image"
                )
            data = await upload.read()
        finally:
            await form.close()
        if not data:
            raise HTTPException(status_code=400, detail="upload body is empty")
        if len(data) > MAX_UPLOAD_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="upload image is too large")
        # The multipart content type is caller-supplied and previously decided the stored
        # extension on its own, so arbitrary bytes could be stored as .png and served from
        # the public media route. Trust the bytes instead.
        sniffed = sniff_image_extension(data)
        if sniffed is None or sniffed != extension:
            raise HTTPException(
                status_code=400,
                detail="upload content does not match its declared image type",
            )
        async with actor._lock:
            character = _character_entity(target_id)
            segment = SEGMENT_PORTRAITS if purpose == "portrait" else SEGMENT_SPRITES
            name = media_store.new_name(extension)
            media_store.write(segment, name, data)
            url = media_store.url_for(segment, name)
            if purpose == "portrait":
                replace_component(
                    character,
                    PortraitImageComponent(
                        url=url, prompt="uploaded", generated_at_epoch=actor.epoch
                    ),
                )
            else:
                replace_component(character, SpriteImageComponent(url=url))
        return {
            **_world_fields(),
            "target_kind": target_kind,
            "target_id": target_id,
            "purpose": purpose,
            "url": url,
            "content_type": content_type,
        }

    for router in (public_v1, auth_v1, profile_v1, chat_v1, play_v1, admin_v1):
        # Bunnyland audits a flat route table; FastAPI 0.140 keeps included
        # routers nested and lazy, while these internal routers are already prefixed.
        app.router.routes.extend(router.routes)

    def _websocket_host(websocket: WebSocket) -> str:
        return getattr(getattr(websocket, "client", None), "host", "") or "unknown"

    @app.websocket("/v1/admin/world/stream")
    async def world_updates(websocket: WebSocket) -> None:
        if not websocket_origin_is_trusted(websocket):
            await websocket.close(code=1008)
            return
        if authenticator is None and not allow_unauthenticated_embedding:
            await websocket.close(code=1013)
            return
        # Held from before accept until the socket closes, so unauthenticated sockets parked
        # in the auth window count against the same budget as established ones.
        with websocket_host_limiter.slot(_websocket_host(websocket)) as host_slot:
            if not host_slot:
                await websocket.close(code=WEBSOCKET_OVERLOADED_CLOSE_CODE)
                return
            await websocket.accept()
            try:
                auth = await asyncio.wait_for(
                    websocket.receive_json(), timeout=PLAYER_WEBSOCKET_AUTH_SECONDS
                )
                data = auth.get("data") if isinstance(auth, dict) else None
                if (
                    not isinstance(auth, dict)
                    or auth.get("type") != "authenticate"
                    or not isinstance(data, dict)
                ):
                    raise ValueError("invalid authentication frame")
                client_id = _client_id_header_value(
                    data.get("client_id") or websocket.headers.get(CLIENT_ID_HEADER)
                )
                if client_id is None:
                    raise HTTPException(status_code=403, detail="client identity is required")
                _require_allowed_admin_client_id(client_id)
                access_token = None
                principal = None
                if authenticator is not None:
                    access_token, principal = _authenticate_websocket_frame(
                        websocket, data, AuthorizationSurface.ADMIN
                    )
            except (HTTPException, TimeoutError, ValueError, TypeError, WebSocketDisconnect):
                await websocket.close(code=1008)
                return
            moderation_identity = (
                ModerationIdentity(IdentityKind.WEB, principal.subject)
                if principal is not None
                else ModerationIdentity(IdentityKind.CLIENT, client_id)
            )
            try:
                moderation_service.require_allowed(moderation_identity)
            except ModerationRestrictedError:
                await websocket.close(code=1008, reason="account restricted")
                return
            identity_key = principal.subject if principal is not None else client_id
            with websocket_identity_limiter.slot(identity_key) as identity_slot:
                if not identity_slot:
                    await websocket.close(code=WEBSOCKET_OVERLOADED_CLOSE_CODE)
                    return
                moderation_service.connections.add(moderation_identity, websocket)
                try:
                    await _stream_admin_updates(websocket, access_token)
                finally:
                    moderation_service.connections.discard(moderation_identity, websocket)

    async def _stream_admin_updates(websocket: WebSocket, access_token: str | None) -> None:
        def _admin_still_authorized() -> bool:
            # Re-check on every frame so a mid-stream downgrade (world:admin -> world:play),
            # revocation, or expiry tears the socket down instead of continuing to stream
            # full-world admin snapshots. verify_token refreshes scopes from the credential
            # file, so a downgraded operator must lose the admin stream, not just a revoked
            # one. (The admin client-id allowlist is static startup config and cannot change
            # mid-stream, so it is not re-evaluated here.)
            if access_token is None:
                return True
            principal = authenticator.verify_token(access_token)
            return principal is not None and scope_granted(principal.scopes, WORLD_ADMIN_SCOPE)

        subscription = stream.subscribe()
        try:
            projection_started = time.perf_counter()
            with telemetry.span("websocket.snapshot"):
                snapshot = serialize_world(actor, meta)
            stream.record_projection_latency(time.perf_counter() - projection_started)
            await websocket.send_json(
                subscription.frame(actor, {"type": "snapshot", "data": snapshot})
            )
            while True:
                if not _admin_still_authorized():
                    await websocket.close(code=1008)
                    return
                message = await next_websocket_update(actor, subscription)
                if not _admin_still_authorized():
                    await websocket.close(code=1008)
                    return
                await websocket.send_json(subscription.frame(actor, message))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            subscription.close()

    @app.websocket("/v1/play/claims/{claim_id}/stream")
    async def world_character_updates(websocket: WebSocket, claim_id: str) -> None:
        # Explicit-header clients authenticate in the first frame; browsers use the
        # HttpOnly claim cookie. Neither path puts credentials in URLs or telemetry.
        if not websocket_origin_is_trusted(websocket):
            await websocket.close(code=1008)
            return
        if authenticator is None and not allow_unauthenticated_embedding:
            await websocket.close(code=1013)
            return
        with websocket_host_limiter.slot(_websocket_host(websocket)) as host_slot:
            if not host_slot:
                await websocket.close(code=WEBSOCKET_OVERLOADED_CLOSE_CODE)
                return
            await websocket.accept()
            try:
                auth = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=PLAYER_WEBSOCKET_AUTH_SECONDS,
                )
            except (TimeoutError, ValueError, TypeError, WebSocketDisconnect):
                await websocket.close(code=1008)
                return
            data = auth.get("data") if isinstance(auth, dict) else None
            if (
                not isinstance(auth, dict)
                or auth.get("type") != "authenticate"
                or not isinstance(data, dict)
                or not isinstance(data.get("claim_secret"), (str, type(None)))
            ):
                await websocket.close(code=1008)
                return
            frame_claim_secret = data.get("claim_secret")
            cookie_claim_secret = getattr(websocket, "cookies", {}).get(CLAIM_COOKIE_NAME)
            if (
                frame_claim_secret
                and cookie_claim_secret
                and not secrets.compare_digest(frame_claim_secret, cookie_claim_secret)
            ):
                await websocket.close(code=1008)
                return
            claim_secret = frame_claim_secret or cookie_claim_secret
            client_id = _client_id_header_value(
                data.get("client_id") or websocket.headers.get(CLIENT_ID_HEADER)
            )
            access_token = None
            try:
                if client_id is None:
                    raise HTTPException(status_code=403, detail="client identity is required")
                access_token, principal = _authenticate_websocket_frame(
                    websocket, data, AuthorizationSurface.PLAY
                )
                owner = ClaimOwner(
                    "rest",
                    principal.subject if principal is not None else f"embedded:{client_id}",
                )
                claim_context = _claim_context_v1(claim_id, claim_secret, client_id, owner)
            except HTTPException:
                await websocket.close(code=1008)
                return
            moderation_identity = (
                ModerationIdentity(IdentityKind.WEB, principal.subject)
                if principal is not None
                else ModerationIdentity(IdentityKind.CLIENT, client_id)
            )
            try:
                moderation_service.require_allowed(moderation_identity)
            except ModerationRestrictedError:
                await websocket.close(code=1008, reason="account restricted")
                return
            identity_key = principal.subject if principal is not None else client_id
            with websocket_identity_limiter.slot(identity_key) as identity_slot:
                if not identity_slot:
                    await websocket.close(code=WEBSOCKET_OVERLOADED_CLOSE_CODE)
                    return
                moderation_service.connections.add(moderation_identity, websocket)
                try:
                    await _stream_character_updates(
                        websocket,
                        claim_id=claim_id,
                        claim_secret=claim_secret,
                        client_id=client_id,
                        owner=owner,
                        access_token=access_token,
                        character_id=str(claim_context[0].id),
                    )
                finally:
                    moderation_service.connections.discard(moderation_identity, websocket)

    async def _stream_character_updates(
        websocket: WebSocket,
        *,
        claim_id: str,
        claim_secret: str | None,
        client_id: str,
        owner: ClaimOwner,
        access_token: str | None,
        character_id: str,
    ) -> None:
        onboarding.connected(claim_id)
        subscription = stream.subscribe()
        # Revalidating the claim meant re-scanning every claimed controller in the world on
        # every frame, so cost scaled with claims x sockets x events. The authorization still
        # has to be rechecked so a revoked token or released claim tears the socket down;
        # it just does not need to happen more often than this.
        next_recheck = 0.0

        async def send_frame(frame: dict) -> bool:
            nonlocal next_recheck
            now = time.monotonic()
            if now >= next_recheck:
                try:
                    if (
                        access_token is not None
                        and authenticator.verify_token(access_token) is None
                    ):
                        raise HTTPException(status_code=401, detail="invalid bearer token")
                    _claim_context_v1(claim_id, claim_secret, client_id, owner)
                except HTTPException:
                    await websocket.close(code=1008)
                    return False
                next_recheck = now + WEBSOCKET_REAUTHORIZE_SECONDS
            await websocket.send_json(subscription.frame(actor, frame))
            return True

        try:
            with telemetry.span("websocket.character", {"character.id": character_id}):
                if not await send_frame(
                    {
                        "type": "ready",
                        "data": {
                            "character_id": character_id,
                            "world_epoch": actor.epoch,
                        },
                    }
                ):
                    return
                while True:
                    update_task = asyncio.create_task(
                        next_player_update(actor, subscription, character_id)
                    )
                    receive_task = asyncio.create_task(websocket.receive())
                    done, pending = await asyncio.wait(
                        {update_task, receive_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if receive_task in done:
                        message = receive_task.result()
                        if message["type"] == "websocket.disconnect":
                            return
                        continue
                    frame = update_task.result()
                    if not await send_frame(frame):
                        return
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            subscription.close()

    async def _mcp_chat_request(
        character_id: str,
        request: CharacterChatRequest,
        claim_secret: str | None,
    ) -> dict:
        del claim_secret
        response = await character_chat.chat(
            character_id,
            request,
            access=CharacterChatAccess.CONTROLLER,
        )
        return _without_preview_fields(response)

    if mcp_enabled(plugins):
        mcp_app = create_bunnyland_mcp_app(
            actor=actor,
            meta=meta,
            loop=loop,
            player_client_ids=allowed_player_client_ids,
            admin_client_ids=allowed_admin_client_ids,
            save_path=save_path,
            patch_world=_patch_world_request,
            generate_world=_generate_world_request,
            generation_status=_world_generation_status_response,
            generate_room=_generate_room_request,
            generate_character=_generate_character_request,
            generate_item=_generate_item_request,
            generate_event=_generate_event_request,
            generate_image=_generate_image_request,
            scene_image=_scene_image_request if imagegen is not None else None,
            chat=_mcp_chat_request if character_chat is not None else None,
            assign_controller=_assign_controller_request,
            list_generators=lambda: _without_preview_fields(_list_world_generators_response()),
            register_script=_register_script_request,
            register_behavior=_register_behavior_request,
            list_controller_definitions=_controller_definitions_response,
            fragment_providers=collect_prompt_fragments(plugins or ()),
            persona_providers=collect_persona_fragments(plugins or ()),
            worldgen_options=worldgen_options,
            claim_secrets=claim_secrets,
            plugins=plugins or (),
            trusted_origins=trusted_origins,
        )
        mcp_session_manager = getattr(mcp_app, "bunnyland_mcp_session_manager", None)
        mcp_event_bridge = getattr(mcp_app, "bunnyland_mcp_event_bridge", None)

        app.mount(
            MCP_MOUNT_PATH,
            mcp_app,
            name="mcp",
        )

    route_surface_matrix(app)
    # Registered last so Starlette makes CORS the outermost middleware. Early 401/403,
    # body-limit 413, and rate-limit 429 responses must all carry trusted-origin headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=trusted_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            CLIENT_ID_HEADER,
            "X-Bunnyland-Claim-Id",
            "X-Bunnyland-Claim-Secret",
            "Mcp-Protocol-Version",
            "Mcp-Session-Id",
        ],
        expose_headers=["X-Bunnyland-Claim-Secret", "Location"],
    )
    return app


__all__ = [
    "AuthorizationSurface",
    "SURFACE_SCOPES",
    "classify_authorization_surface",
    "create_app",
    "route_surface_matrix",
    "websocket_origin_is_trusted",
]
