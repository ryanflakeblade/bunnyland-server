# Saving & reloading worlds

A world can be saved to disk and reloaded later. A save is a **single JSON or YAML file**
that holds both the Relics ECS snapshot (every entity, component, and edge) and the Bunnyland
provenance that produced it — the **seed**, the literal **DM system prompt** the model was
given (empty for the deterministic offline builder), and which **generator** built it.
Reloading restores the world exactly: room graph, inventories, needs meters, moods,
controllers, and the game clock.

> The volatile command queues are intentionally **not** saved — a reloaded world resumes
> with empty queues and keeps playing from the saved game time.

On Windows, checkpoint files are flushed through writable file handles. Directory
flushing is unavailable through Python's Windows file API, so Windows does not have
the same directory-entry durability guarantee across power loss as POSIX platforms.
Checksums and checkpoint recovery validation still apply.

Offline life is explicit and bounded. After loading, callers may run
`advance_offline_life(actor, elapsed_seconds)` to advance a capped number of coarse ticks
with cheap background controllers. The helper uses the normal actor tick, controller
dispatch, command validation, and handlers; any resulting changes are persisted by the next
`save_world(...)`.

Checkpoint publication is crash-recoverable. The server fsyncs the new snapshot and checksum
before publishing a durable transaction marker. If the process or host stops during
publication, the next load either completes that verified new checkpoint or retains the last
verified old checkpoint; it never accepts a mismatched or partial snapshot/checksum pair.

## Saving from the server

```bash
# Save on exit:
uv run bunnyland serve --ticks 50 --save worlds/marsh.json

# Autosave every 20 ticks, and also on exit:
uv run bunnyland serve --ticks 0 --save worlds/marsh.json --autosave-every 20
```

`--autosave-every N` checkpoints the world every N ticks so an interrupted server loses at
most N ticks of play. It requires `--save` to know where to write.

## Reloading

```bash
uv run bunnyland serve --load worlds/marsh.json --ticks 50
```

`--load` skips generation and resumes the saved world (you'll see its seed, generator, and
game epoch echoed back). Combine with `--save` to keep checkpointing the reloaded world, and
with `--llm` to keep driving its characters:

```bash
uv run bunnyland serve --load worlds/marsh.json --llm --save worlds/marsh.json \
  --autosave-every 20 --ticks 0
```

If the saved world's LLM controllers should use OpenRouter, add `--llm-provider openrouter`
and set `OPENROUTER_API_KEY`. New LLM worlds can also use OpenRouter by adding
`--worldgen-provider openrouter`.

## Reset / fresh start

From the CLI, a reset is launching **without** `--load`, which generates a fresh world. To
discard a save, delete the file or point `--save` at a new path to keep the old one.

With the server API enabled, admins can also replace the live world through
`POST /admin/world/generate` or the web client's `world-generator.html` page. That request
starts an async generation job; `GET /admin/world/generation` reports when it has finished.
If the request uses `"save": true`, the completed replacement is written to the configured
`--save` path.

## What is and isn't persisted

| Persisted                                              | Not persisted                              |
|--------------------------------------------------------|--------------------------------------------|
| All entities, components, edges (rooms, items, characters, controllers) | Volatile command queues / inbox |
| The game clock (epoch) and per-character needs/moods   | In-memory private notes and recall data |
| Seed, generation prompt, generator name, and schema version | Plugin code (discovered from installed package entry points on load) |
| Shared world-history records and their actor/target links | Narration-only prose that was never committed to ECS |
| Chroma private notes and recall data, when `--memory-backend=chroma` is configured | |

Chroma memory is stored outside the world file. If `--memory-backend=chroma` is used
without `--memory-path`, and the server also has `--save worlds/marsh.json`, memory is
persisted beside the save at `worlds/marsh.memory/chroma`. Set `--memory-path` to choose a
different Chroma directory. The `in-memory` backend is still non-persistent. Plugins are
*code*, not data: install the same plugin wheels before loading. With no explicit
`--plugin` selection, every discovered `default_enabled` plugin is applied; with an explicit
selection, include every plugin id required by the save.

## Schema v4 and sequential migration

Schema v2 introduced repeatable live relationships as typed edges. The holder is the edge source,
the referenced entity is the target, and per-target values live on the edge. Examples include
`MemberOfFaction`, `MemberOfInstitution`, `MemberOfCaravan`, `MemberOfFestival`,
`MemberOfAwayTeam`, `StoredIn`, `HasAccessToService`, `AllowedIn`, `WantedByFaction`, the
standing edges, rumor source/subject/listener edges, `DescendsFromParent`, and
`DependsOnIngredient`. Components remain for singleton state; immutable history/event
snapshots and external identifiers may remain scalar values.

Schema v3 moves Lifesim's remaining live ownership and pregnancy references to `OwnsHome`,
`ClaimsRoom`, and `PregnancyCoParent` edges. `PregnancyComponent` retains only timing and
source-event provenance.

Schema v4 removes the remaining live reference fields covered by the Storyteller, Dinosim,
Neonsim, and Voidsim audit. Incident location is authoritative `Contains` state; Dinosim
lineage, care, ownership, behavior, provenance, and assignment links are typed edges; Neon
evidence uses `EvidenceSubject` and `RecordedByDevice`; and Void orbit/navigation targets use
`OrbitsBody` and `NavigatesTo`.

Saves migrate sequentially from v1 to v2 to v3 to v4, with v2 and v3 saves joining at the
appropriate step before type deserialization; the source file is never modified. Migration
covers moved quest records, `StealthComponent` to `SneakingComponent`, legacy relationship
fields/maps, and legacy 3D decoration roles. Every migrated live target is checked for
existence and endpoint type. Missing targets, duplicate cardinality, or malformed records fail
with the owning entity, persisted type, and field in the error rather than guessing.

Use the explicit converter when you want a separate migrated file:

```bash
uv run bunnyland migrate-world worlds/marsh-v1.json worlds/marsh-v4.json
```

Loading v1, v2, or v3 yields an in-memory v4 world; the next normal save writes v4. JSON and
YAML migration fixtures cover the same conversion contract.

World history is normal ECS state (`WorldHistoryRecordComponent`, `HistoryActor`, and
`HistoryTarget`). Durable marks are normal ECS state too (`PhysicalMarkComponent` and
`MarkOn`). Creator signatures are stored with `CreatorSignatureComponent` and `CreatedBy`.
Deed reputation is stored with `DeedReputationComponent`. Death presentation state is
stored with `DeathConsequenceComponent` and `DeathOf`.
They are created from notable domain events such as writing, crafting, and death; prompts
read those records as presentation state rather than inventing history.

Life-sim inheritance is also normal ECS state. A death can transfer existing ownership
links and balances to an heir, then store `InheritanceRecordComponent` plus an
`InheritedFrom` edge so the lineage/audit trail survives save and reload.

Social obligations are persisted ECS state as well. Speech can create
`ObligationComponent` entities linked with `ObligationDebtor` and `ObligationCreditor`;
resolution updates the component status and relationship state instead of relying on
remembered prose alone.

Cross-player impact should be validated the same way: one controller changes shared ECS
state, the world is saved and reloaded, and another controller's prompt reads that state
through normal prompt fragments.

## From Python

The same operations are available programmatically:

```python
from bunnyland.offline import advance_offline_life
from bunnyland.persistence import save_world, load_world, WorldMeta
from bunnyland.plugins import bunnyland_plugins

# Save (provenance travels with the ECS data):
save_world(actor, "worlds/marsh.json",
           meta=WorldMeta(seed="a quiet marsh", prompt="...", generator="recursive"))

# Reload — pass the plugins whose components/edges the world uses, so the loader can
# reconstruct their types and re-register their handlers/systems:
actor, meta = load_world("worlds/marsh.json", plugins=bunnyland_plugins())
print(meta.seed, meta.generator, meta.saved_at_epoch)
await advance_offline_life(actor, elapsed_seconds=6 * 3600)
```

`load_world` returns a ready `WorldActor` (clock rebound, plugins applied) and the
`WorldMeta` provenance. For an admin force-save mid-run, call `save_world(actor, path,
meta=...)` at any point — it stamps the current game epoch and wall-clock time.

## Format notes

The file is the layout Relics' own loader understands, preserving entity ids so edges survive.
Bunnyland flattens nested value objects to plain JSON/YAML values on save; Pydantic rebuilds
them only after schema migration. JSON provenance lives under `bunnyland`; the compact YAML
dialect uses its reserved Bunnyland metadata section.
