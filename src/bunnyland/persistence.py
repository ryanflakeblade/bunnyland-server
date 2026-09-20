"""Saving and reloading worlds (spec 26).

A save is a single file containing the Relics ECS snapshot *and* the bunnyland metadata
that produced it (seed, prompt, generator). JSON saves use the layout Relics' own loader
understands, so reloading is just ``relics.load``. YAML saves use Bunnyland's optional
compact Relics persistence driver, where each entity is a record and edges are written as
``EdgeType -> target_entity`` subrecords.

Relics' serializer only walks top-level component fields, so bunnyland's nested value
objects (``Meter``, ``AffectVector``) are flattened to plain dicts on save with
``_jsonable``; on load, pydantic coerces those dicts back into the value objects. The
volatile command queues are intentionally *not* persisted (spec 26): a reloaded world
resumes with empty queues.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
from collections import deque
from collections.abc import Mapping
from dataclasses import is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, JsonValue

from . import telemetry
from .core.queue import CommandQueues
from .core.world_actor import WorldActor
from .migrations import CURRENT_SCHEMA_VERSION, migrate_snapshot
from .persistence_yaml import YAMLPersistenceDriver
from .plugins.loader import PluginError, apply_plugins
from .plugins.model import PluginRuntimeContext
from .plugins.registry import PluginRegistry

SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
PersistenceFormat = Literal["json", "yaml"]
type TupleTree = (
    None | bool | int | float | str | dict[str, JsonValue] | tuple["TupleTree", ...]
)
CHECKSUM_SUFFIX = ".sha256"
CHECKPOINT_TRANSACTION_SUFFIX = ".checkpoint-transaction.json"
DEFAULT_BACKUP_COUNT = 3
DEFAULT_JOURNAL_RECORDS = 5000
DEFAULT_JOURNAL_SEGMENT_RECORDS = 100


class MemoryManifest(BaseModel):
    """Versioned authority boundary between world saves and rebuildable memory indexes."""

    version: int = 1
    world_namespace: str = "main"
    backend: str = ""
    checkpoint_epoch: int = 0
    collection_namespace: str = "main"
    embedding_implementation: str = ""
    high_watermark: int = 0


class WorldMeta(BaseModel):
    """The provenance of a world, saved beside the ECS data."""

    schema_version: int = SCHEMA_VERSION
    seed: str = ""
    prompt: str = ""  # the literal DM system prompt used to build the world ("" for stub)
    generator: str = ""  # which world generator produced it
    plugins: tuple[str, ...] = ()  # plugin ids loaded for this world, e.g. module_foo.bar
    saved_at_epoch: int = 0  # bunnyland game epoch at save time
    saved_at: datetime | None = None  # wall-clock save time
    world_id: str = Field(default_factory=lambda: uuid4().hex)
    world_contract_version: int = 1
    memory: MemoryManifest = Field(default_factory=MemoryManifest)
    rng_stream_state: dict[str, JsonValue] = Field(default_factory=dict)


class RecoveryManifest(BaseModel):
    """One portable restore boundary for a pinned preview release."""

    version: int = 1
    release: str
    release_pins: dict[str, str]
    world_id: str
    world_epoch: int
    world_checksum_sha256: str
    memory_backend: str
    memory_namespace: str
    memory_checkpoint_epoch: int
    memory_high_watermark: int
    media_manifest_checksum_sha256: str
    rollback_checkpoint: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OperationalJournal:
    """Bounded segmented JSONL audit trail adjacent to a canonical snapshot."""

    def __init__(
        self,
        save_path: str | Path,
        *,
        max_records: int = DEFAULT_JOURNAL_RECORDS,
        segment_records: int = DEFAULT_JOURNAL_SEGMENT_RECORDS,
    ):
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        if segment_records <= 0:
            raise ValueError("segment_records must be positive")
        self.save_path = Path(save_path)
        self.path = self.save_path.with_name(f"{self.save_path.name}.journal.jsonl")
        self.max_records = max_records
        self.segment_records = min(segment_records, max_records)
        self._completed: list[tuple[int, Path]] = []
        self._active_records = 0
        self._migration_performed = False
        self._initialize()

    def append(self, record_type: str, **fields: object) -> None:
        record = {
            "journal_version": 1,
            "record_type": record_type,
            "recorded_at": datetime.now(UTC).isoformat(),
            **_jsonable(fields),
        }
        serialized = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        migrated = self._migration_performed
        with telemetry.span(
            "journal.append",
            {
                "record.type": record_type,
                "journal.migrated": migrated,
                "journal.rotated": False,
            },
        ) as append_span:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._make_room_for_record()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            self._active_records += 1
            rotated = self._rotate_active_if_full()
            append_span.set_attribute("journal.rotated", rotated)
            telemetry.mark_span_ok(append_span)
        self._migration_performed = False

    def records(self) -> list[dict[str, JsonValue]]:
        records: list[dict[str, JsonValue]] = []
        paths = [path for _index, path in self._completed]
        if self.path.exists():
            paths.append(self.path)
        for path in paths:
            with path.open(encoding="utf-8") as handle:
                records.extend(json.loads(line) for line in handle if line.strip())
        return records[-self.max_records :]

    def _initialize(self) -> None:
        self._completed = self._completed_segments()
        legacy_paths = sorted(self.path.parent.glob(f".{self.path.name}.legacy-recovery-*"))
        for legacy_path in legacy_paths:
            self._recover_legacy(legacy_path)
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            self._active_records = sum(1 for line in handle if line.strip())
        if self._active_records >= self.segment_records:
            self._start_legacy_migration()

    def _completed_segments(self) -> list[tuple[int, Path]]:
        prefix = f"{self.save_path.name}.journal."
        completed: list[tuple[int, Path]] = []
        for path in self.save_path.parent.glob(f"{prefix}*.jsonl"):
            index_text = path.name[len(prefix) : -len(".jsonl")]
            if index_text.isdigit():
                completed.append((int(index_text), path))
        return sorted(completed)

    def _segment_path(self, index: int) -> Path:
        return self.save_path.with_name(f"{self.save_path.name}.journal.{index:08d}.jsonl")

    def _next_segment_index(self) -> int:
        return self._completed[-1][0] + 1 if self._completed else 1

    def _legacy_path(self, start_index: int) -> Path:
        return self.path.with_name(
            f".{self.path.name}.legacy-recovery-{start_index:08d}"
        )

    def _start_legacy_migration(self) -> None:
        start_index = self._next_segment_index()
        legacy_path = self._legacy_path(start_index)
        os.replace(self.path, legacy_path)
        _fsync_directory(self.path.parent)
        self._active_records = 0
        self._recover_legacy(legacy_path)

    def _recover_legacy(self, legacy_path: Path) -> None:
        start_text = legacy_path.name.rsplit("-", 1)[-1]
        if not start_text.isdigit():
            raise ValueError(f"invalid journal recovery filename: {legacy_path.name}")
        start_index = int(start_text)
        for index, path in tuple(self._completed):
            if index >= start_index:
                path.unlink(missing_ok=True)
        self._completed = [item for item in self._completed if item[0] < start_index]
        self.path.unlink(missing_ok=True)

        retained: deque[str] = deque(maxlen=self.max_records)
        with legacy_path.open(encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    retained.append(line if line.endswith("\n") else f"{line}\n")
        while len(self._completed) * self.segment_records + len(retained) > self.max_records:
            _index, oldest = self._completed.pop(0)
            oldest.unlink()

        lines = list(retained)
        completed_count = len(lines) // self.segment_records
        temporary_targets: list[tuple[Path, Path]] = []
        for offset in range(completed_count):
            target = self._segment_path(start_index + offset)
            temporary = target.with_name(f".{target.name}.migration.tmp")
            begin = offset * self.segment_records
            self._write_migration_file(
                temporary,
                lines[begin : begin + self.segment_records],
            )
            temporary_targets.append((temporary, target))
        active_lines = lines[completed_count * self.segment_records :]
        if active_lines:
            active_temporary = self.path.with_name(f".{self.path.name}.migration.tmp")
            self._write_migration_file(active_temporary, active_lines)
            temporary_targets.append((active_temporary, self.path))

        for temporary, target in temporary_targets:
            os.replace(temporary, target)
        _fsync_directory(self.path.parent)
        legacy_path.unlink()
        _fsync_directory(self.path.parent)
        self._completed = self._completed_segments()
        self._active_records = len(active_lines)
        self._migration_performed = True

    @staticmethod
    def _write_migration_file(path: Path, lines: list[str]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())

    def _make_room_for_record(self) -> None:
        retained = len(self._completed) * self.segment_records + self._active_records
        while retained >= self.max_records:
            _index, oldest = self._completed.pop(0)
            oldest.unlink()
            _fsync_directory(self.path.parent)
            retained -= self.segment_records

    def _rotate_active_if_full(self) -> bool:
        if self._active_records < self.segment_records:
            return False
        index = self._next_segment_index()
        completed = self._segment_path(index)
        os.replace(self.path, completed)
        _fsync_directory(self.path.parent)
        self._completed.append((index, completed))
        self._active_records = 0
        return True


def _jsonable(value: object) -> JsonValue:
    """Recursively convert a value to JSON-native form (dataclasses -> dicts, enums -> values)."""
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, BaseModel):
        return {k: _jsonable(v) for k, v in value.model_dump().items()}
    if is_dataclass(value) and not isinstance(value, type):
        fields = getattr(value, "__pydantic_fields__", None) or getattr(
            value, "__dataclass_fields__", {}
        )
        return {
            name: _jsonable(getattr(value, name)) for name in fields if not name.startswith("_")
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def type_registries(
    registry: PluginRegistry,
) -> tuple[dict[str, type], dict[str, type]]:
    """Build ``(component_registry, edge_registry)`` keyed by class name for ``relics.load``."""
    return (
        {name: component for name, (_owner, component) in registry.components.items()},
        {name: edge for name, (_owner, edge) in registry.edges.items()},
    )


def _snapshot(actor: WorldActor, meta: WorldMeta) -> dict[str, JsonValue]:
    world = actor.world
    entities: dict[str, JsonValue] = {}
    components: dict[str, JsonValue] = {}
    relationships: dict[str, JsonValue] = {}

    for entity in world.query().execute_entities():
        eid = str(entity.id)
        export = world.export_entity(entity.id)
        entities[eid] = {"prefab": entity.id.prefab, "created_epoch": 0}
        for type_name, fields in export.get("components", {}).items():
            components.setdefault(type_name, {})[eid] = _jsonable(fields)
        for edge_name, edges in export.get("relationships", {}).items():
            if not edges:
                continue
            bucket = relationships.setdefault(edge_name, {}).setdefault(eid, [])
            for edge in edges:
                bucket.append({"target": edge["target"], "edge": _jsonable(edge["edge"])})

    return {
        "metadata": {"version": "1.0", "epoch": world.epoch},
        "bunnyland": _jsonable(meta),
        "prefabs": {"entity": {"components": {}}},
        "entities": entities,
        "components": components,
        "relationships": relationships,
        "relics": [],
    }


def _format_for_path(path: Path, format: PersistenceFormat | None) -> PersistenceFormat:
    if format is not None:
        if format not in ("json", "yaml"):
            raise ValueError(f"unknown persistence format: {format}")
        return format
    if path.suffix.lower() in {".yaml", ".yml"}:
        return "yaml"
    return "json"


def save_world(
    actor: WorldActor,
    path: str | Path,
    *,
    meta: WorldMeta,
    format: PersistenceFormat | None = None,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> WorldMeta:
    """Write the world (ECS + provenance) to ``path``. Returns the stamped meta."""
    path = Path(path)
    resolved_format = _format_for_path(path, format)
    attrs = {"operation": "save", "format": resolved_format}
    with (
        telemetry.record_duration(telemetry.record_persist, attrs),
        telemetry.span("world.save", {**attrs, "path": str(path)}) as save_span,
    ):
        try:
            _recover_checkpoint_transaction(path)
            memory = meta.memory.model_copy(
                update={
                    "checkpoint_epoch": actor.epoch,
                    "high_watermark": max(meta.memory.high_watermark, actor.epoch),
                }
            )
            stamped = meta.model_copy(
                update={
                    "saved_at_epoch": actor.epoch,
                    "saved_at": datetime.now(UTC),
                    "memory": memory,
                    "rng_stream_state": {"command_order": _jsonable(actor._rng.getstate())},
                }
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            snapshot = _snapshot(actor, stamped)
            temporary = _checkpoint_temporary_path(path)
            if resolved_format == "yaml":
                YAMLPersistenceDriver().save_snapshot(snapshot, temporary)
            else:
                temporary.write_text(json.dumps(snapshot, indent=2, default=str))
            _fsync_file(temporary)
            checksum = _checksum(temporary)
            checksum_path = _checksum_path(path)
            checksum_temporary = _checkpoint_temporary_path(checksum_path)
            checksum_temporary.write_text(f"{checksum}  {path.name}\n")
            _fsync_file(checksum_temporary)
            _rotate_backups(path, backup_count)
            _rotate_backups(checksum_path, backup_count)
            _publish_checkpoint(
                path,
                temporary=temporary,
                checksum_temporary=checksum_temporary,
                checksum=checksum,
            )
            _actor_journal(actor, path).append(
                "checkpoint",
                world_epoch=actor.epoch,
                checksum=checksum,
                rng_stream_state=stamped.rng_stream_state,
                memory_checkpoint_epoch=stamped.memory.checkpoint_epoch,
            )
            if telemetry.enabled():
                save_span.set_attribute(
                    "entity.count", len(list(actor.world.query().execute_entities()))
                )
            telemetry.mark_span_ok(save_span)
            return stamped
        except Exception as exc:
            save_span.record_exception(exc)
            telemetry.mark_span_error(str(exc), save_span)
            raise


def load_world(
    path: str | Path,
    *,
    registry: PluginRegistry,
    plugin_context: PluginRuntimeContext | None = None,
    format: PersistenceFormat | None = None,
) -> tuple[WorldActor, WorldMeta]:
    """Reload a world from ``path``. Applies ``plugins`` (handlers/systems) before loading."""
    path = Path(path)
    selected_format = _format_for_path(path, format)
    attrs = {"operation": "load", "format": selected_format}
    with (
        telemetry.record_duration(telemetry.record_persist, attrs),
        telemetry.span("world.load", {**attrs, "path": str(path)}) as load_span,
    ):
        try:
            _verify_checksum(path)
            yaml_driver = YAMLPersistenceDriver() if selected_format == "yaml" else None
            data = (
                yaml_driver.read_snapshot(path)
                if yaml_driver is not None
                else json.loads(path.read_text())
            )
            data = migrate_snapshot(data)
            meta = WorldMeta.model_validate(data.get("bunnyland", {}))

            actor = WorldActor()
            available = set(registry.plugins)
            missing = tuple(plugin_id for plugin_id in meta.plugins if plugin_id not in available)
            if missing:
                names = ", ".join(repr(plugin_id) for plugin_id in missing)
                raise PluginError(f"saved world depends on missing plugin(s): {names}")
            plugins = tuple(registry.plugins.values())
            apply_plugins(plugins, actor, plugin_context)

            component_registry, edge_registry = type_registries(registry)
            (yaml_driver or YAMLPersistenceDriver()).load_snapshot(
                actor.world,
                data,
                component_registry,
                edge_registry,
            )
            actor.bind_clock()  # the __init__ clock was cleared by load; rebind to the saved one
            actor.world_id = meta.world_id
            state = meta.rng_stream_state.get("command_order")
            if state:
                actor._rng.setstate(_tuples(state))
            if telemetry.enabled():
                load_span.set_attribute(
                    "entity.count", len(list(actor.world.query().execute_entities()))
                )
            telemetry.mark_span_ok(load_span)
            return actor, meta
        except Exception as exc:
            load_span.record_exception(exc)
            telemetry.mark_span_error(str(exc), load_span)
            raise


def read_world_meta(
    path: str | Path,
    *,
    format: PersistenceFormat | None = None,
) -> WorldMeta:
    """Read and validate saved-world metadata without constructing the ECS world."""

    path = Path(path)
    _verify_checksum(path)
    selected_format = _format_for_path(path, format)
    data = (
        YAMLPersistenceDriver().read_snapshot(path)
        if selected_format == "yaml"
        else json.loads(path.read_text())
    )
    migrated = migrate_snapshot(data)
    return WorldMeta.model_validate(migrated.get("bunnyland", {}))


def reload_world(
    actor: WorldActor,
    path: str | Path,
    *,
    meta: WorldMeta,
    registry: PluginRegistry,
    plugin_context: PluginRuntimeContext | None = None,
    format: PersistenceFormat | None = None,
) -> WorldMeta:
    """Reload ``path`` into a live actor, preserving actor services and plugin wiring.

    The caller must already own ``actor._lock`` when needed. Tick after-hooks run while
    the lock is held, so this helper intentionally does not acquire it itself.
    """

    replacement, loaded_meta = load_world(
        path,
        registry=registry,
        plugin_context=plugin_context,
        format=format,
    )
    actor.world = replacement.world
    actor.world_id = loaded_meta.world_id
    actor.bind_clock()
    actor.queues = CommandQueues()
    actor._inbox = asyncio.Queue()

    memory_store = getattr(actor, "memory_store", None)
    if memory_store is not None:
        from .core.components import MemoryProfileComponent
        from .memory import quarantine_after_epoch

        collections = set()
        for character in actor.world.query().with_all([MemoryProfileComponent]).execute_entities():
            profile = character.get_component(MemoryProfileComponent)
            collections.add(profile.vector_collection)
            collections.update(profile.shared_collections)
        quarantine = quarantine_after_epoch(
            memory_store,
            tuple(sorted(collections)),
            checkpoint_epoch=loaded_meta.memory.checkpoint_epoch,
            world_namespace=loaded_meta.memory.world_namespace or loaded_meta.world_id,
        )
        if quarantine.quarantined:
            _actor_journal(actor, path).append(
                "memory_quarantine",
                world_id=loaded_meta.world_id,
                checkpoint_epoch=quarantine.checkpoint_epoch,
                quarantined=quarantine.quarantined,
                collections=quarantine.collections,
            )

    updates = loaded_meta.model_dump()
    for key, value in updates.items():
        setattr(meta, key, value)
    actor.configure_persistence(
        save_path=path,
        meta=meta,
        plugins=tuple(registry.plugins.values()),
        plugin_context=plugin_context,
    )
    return meta


def _actor_journal(actor: WorldActor, path: str | Path) -> OperationalJournal:
    path = Path(path)
    journal = actor.persistence.journal
    if journal is None or journal.save_path != path:
        journal = OperationalJournal(path)
        actor.persistence.journal = journal
    return journal


def clone_world_identity(
    actor: WorldActor,
    meta: WorldMeta,
    *,
    world_id: str | None = None,
) -> WorldMeta:
    """Assign a clone its own world id and active memory collection namespace."""

    from .core.components import MemoryProfileComponent
    from .core.ecs import replace_component

    clone_id = world_id or uuid4().hex
    previous_namespaces = tuple(
        dict.fromkeys(
            value
            for value in (
                meta.memory.collection_namespace,
                meta.memory.world_namespace,
            )
            if value and value != "main"
        )
    )

    def rebase(collection: str) -> str:
        base = collection
        for previous in previous_namespaces:
            prefix = f"{previous}--"
            if base.startswith(prefix):
                base = base[len(prefix) :]
                break
        return f"{clone_id}--{base}"

    for character in actor.world.query().with_all([MemoryProfileComponent]).execute_entities():
        profile = character.get_component(MemoryProfileComponent)
        replace_component(
            character,
            profile.__class__(
                vector_collection=rebase(profile.vector_collection),
                shared_collections=tuple(rebase(name) for name in profile.shared_collections),
                last_event_seen_id=profile.last_event_seen_id,
                last_reflection_epoch=profile.last_reflection_epoch,
            ),
        )
    memory = meta.memory.model_copy(
        update={
            "world_namespace": clone_id,
            "collection_namespace": clone_id,
            "checkpoint_epoch": actor.epoch,
            "high_watermark": actor.epoch,
        }
    )
    actor.world_id = clone_id
    return meta.model_copy(
        update={
            "world_id": clone_id,
            "saved_at_epoch": actor.epoch,
            "saved_at": None,
            "memory": memory,
        }
    )


def build_recovery_manifest(
    snapshot_path: str | Path,
    *,
    meta: WorldMeta,
    release: str,
    release_pins: dict[str, str],
    media_manifest_path: str | Path,
    rollback_checkpoint: str,
) -> RecoveryManifest:
    """Build recovery metadata from the exact snapshot and media manifest bytes."""

    snapshot = Path(snapshot_path)
    media_manifest = Path(media_manifest_path)
    return RecoveryManifest(
        release=release,
        release_pins=dict(sorted(release_pins.items())),
        world_id=meta.world_id,
        world_epoch=meta.saved_at_epoch,
        world_checksum_sha256=_checksum(snapshot),
        memory_backend=meta.memory.backend,
        memory_namespace=meta.memory.collection_namespace,
        memory_checkpoint_epoch=meta.memory.checkpoint_epoch,
        memory_high_watermark=meta.memory.high_watermark,
        media_manifest_checksum_sha256=_checksum(media_manifest),
        rollback_checkpoint=rollback_checkpoint,
    )


def write_recovery_manifest(path: str | Path, manifest: RecoveryManifest) -> None:
    """Atomically write and checksum a recovery manifest."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    _fsync_file(temporary)
    checksum = _checksum(temporary)
    checksum_path = _checksum_path(path)
    checksum_temporary = checksum_path.with_name(f".{checksum_path.name}.tmp")
    checksum_temporary.write_text(f"{checksum}  {path.name}\n", encoding="utf-8")
    _fsync_file(checksum_temporary)
    os.replace(temporary, path)
    os.replace(checksum_temporary, checksum_path)
    _fsync_directory(path.parent)


def _checksum_path(path: Path) -> Path:
    return path.with_name(f"{path.name}{CHECKSUM_SUFFIX}")


def _checkpoint_transaction_path(path: Path) -> Path:
    return path.with_name(f".{path.name}{CHECKPOINT_TRANSACTION_SUFFIX}")


def _checkpoint_temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.checkpoint-new")


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    # Windows requires a writable descriptor to flush file buffers.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    # Python's Windows file API cannot open and fsync a directory. Snapshot,
    # checksum, and transaction files are still flushed before publication.
    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rotate_backups(path: Path, count: int) -> None:
    if count <= 0 or not path.exists():
        return
    oldest = path.with_name(f"{path.name}.bak.{count}")
    oldest.unlink(missing_ok=True)
    for index in range(count - 1, 0, -1):
        source = path.with_name(f"{path.name}.bak.{index}")
        if source.exists():
            os.replace(source, path.with_name(f"{path.name}.bak.{index + 1}"))
    shutil.copy2(path, path.with_name(f"{path.name}.bak.1"))


def _write_checksum(path: Path, checksum: str, snapshot_name: str) -> None:
    temporary = path.with_name(f".{path.name}.recovery-tmp")
    temporary.write_text(f"{checksum}  {snapshot_name}\n", encoding="utf-8")
    _fsync_file(temporary)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _publish_checkpoint(
    path: Path,
    *,
    temporary: Path,
    checksum_temporary: Path,
    checksum: str,
) -> None:
    transaction_path = _checkpoint_transaction_path(path)
    transaction_temporary = transaction_path.with_name(f".{transaction_path.name}.tmp")
    old_checksum = _checksum(path) if path.exists() else None
    transaction = {
        "version": 1,
        "snapshot": path.name,
        "new_checksum": checksum,
        "old_checksum": old_checksum,
    }
    transaction_temporary.write_text(
        json.dumps(transaction, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _fsync_file(transaction_temporary)
    os.replace(transaction_temporary, transaction_path)
    _fsync_directory(path.parent)

    os.replace(temporary, path)
    _fsync_directory(path.parent)
    os.replace(checksum_temporary, _checksum_path(path))
    _fsync_directory(path.parent)
    transaction_path.unlink()
    _fsync_directory(path.parent)


def _recover_checkpoint_transaction(path: Path) -> None:
    transaction_path = _checkpoint_transaction_path(path)
    if not transaction_path.exists():
        return
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    if (
        not isinstance(transaction, dict)
        or transaction.get("version") != 1
        or transaction.get("snapshot") != path.name
    ):
        raise ValueError(f"invalid checkpoint transaction for {path}")
    new_checksum = transaction.get("new_checksum")
    old_checksum = transaction.get("old_checksum")
    if not isinstance(new_checksum, str) or (
        old_checksum is not None and not isinstance(old_checksum, str)
    ):
        raise ValueError(f"invalid checkpoint transaction for {path}")

    temporary = _checkpoint_temporary_path(path)
    if path.exists() and _checksum(path) == new_checksum:
        _write_checksum(_checksum_path(path), new_checksum, path.name)
    elif temporary.exists() and _checksum(temporary) == new_checksum:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        _write_checksum(_checksum_path(path), new_checksum, path.name)
    elif path.exists() and old_checksum is not None and _checksum(path) == old_checksum:
        _write_checksum(_checksum_path(path), old_checksum, path.name)
    else:
        raise ValueError(f"checkpoint transaction cannot recover {path}")

    temporary.unlink(missing_ok=True)
    _checkpoint_temporary_path(_checksum_path(path)).unlink(missing_ok=True)
    transaction_path.unlink()
    _fsync_directory(path.parent)


def _verify_checksum(path: Path) -> None:
    _recover_checkpoint_transaction(path)
    checksum_path = _checksum_path(path)
    if not checksum_path.exists():
        return  # Backward compatibility for schema-v2 saves made before checksums.
    expected = checksum_path.read_text().split(maxsplit=1)[0]
    actual = _checksum(path)
    if actual != expected:
        raise ValueError(f"checksum mismatch for {path}")


def _tuples(value: JsonValue) -> TupleTree:
    if isinstance(value, list):
        return tuple(_tuples(item) for item in value)
    return value


__all__ = [
    "SCHEMA_VERSION",
    "PersistenceFormat",
    "MemoryManifest",
    "OperationalJournal",
    "RecoveryManifest",
    "WorldMeta",
    "YAMLPersistenceDriver",
    "build_recovery_manifest",
    "clone_world_identity",
    "load_world",
    "read_world_meta",
    "reload_world",
    "save_world",
    "type_registries",
    "write_recovery_manifest",
]
