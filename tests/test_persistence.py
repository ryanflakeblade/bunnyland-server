"""Tests for world persistence (spec 26): save, reload, autosave, and provenance."""

from __future__ import annotations

import asyncio
import json
import sys
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest
from pydantic import BaseModel
from relics import EntityId, World

from bunnyland import persistence
from bunnyland.core import (
    CommandCost,
    ContainmentMode,
    Contains,
    ExitTo,
    Lane,
    RegionComponent,
    RestingComponent,
    RoomComponent,
    SleepingComponent,
    WorldActor,
    WorldInfoComponent,
    build_submitted_command,
    container_of,
    replace_component,
    spawn_entity,
)
from bunnyland.core.components import CharacterComponent, IdentityComponent, MemoryProfileComponent
from bunnyland.core.controllers import LLMControllerComponent
from bunnyland.core.ecs import contents
from bunnyland.core.edges import ControlledBy
from bunnyland.discord.components import DiscordRoomFeedComponent
from bunnyland.engine import GameLoop
from bunnyland.foundation.environment.mechanics import MoistureComponent, ShelterComponent
from bunnyland.foundation.meters.mechanics import Meter
from bunnyland.foundation.needs.mechanics import HungerComponent
from bunnyland.llm_agents import ControllerDispatch, ScriptedAgent, ToolCall
from bunnyland.migrations import WorldMigrationError, migrate_snapshot
from bunnyland.offline import advance_offline_life
from bunnyland.persistence import (
    MemoryManifest,
    OperationalJournal,
    RecoveryManifest,
    WorldMeta,
    YAMLPersistenceDriver,
    _format_for_path,
    _jsonable,
    _snapshot,
    build_recovery_manifest,
    clone_world_identity,
    load_world,
    read_world_meta,
    reload_world,
    save_world,
    type_registries,
    write_recovery_manifest,
)
from bunnyland.plugins import PluginRegistry, apply_plugins, bunnyland_plugins
from bunnyland.prompts.builder import PromptBuilder
from bunnyland.simpacks.barbariansim.mechanics import WetnessComponent
from bunnyland.simpacks.toonsim.mechanics import ToonRoomComponent
from bunnyland.worldgen import StubWorldBuilder, instantiate


class _YamlFlavor(Enum):
    SWEET = "sweet"


class _YamlNestedModel(BaseModel):
    label: str


@dataclass
class _YamlNestedData:
    flavor: _YamlFlavor
    hidden: str
    _private: str = "ignored"


async def _build_and_play():
    """A fully wired stub world after a couple of in-place actions (no movement)."""
    actor = WorldActor()
    apply_plugins(bunnyland_plugins(), actor)
    result = await instantiate(actor, await StubWorldBuilder().propose("a quiet marsh"))
    # Hazel eats and takes the paper but stays in the burrow.
    agent = ScriptedAgent(
        [
            ToolCall("eat", {"item_id": "three berries"}),
            ToolCall("take", {"item_id": "a scrap of paper"}),
        ]
    )
    dispatch = ControllerDispatch(actor, PromptBuilder(actor.world), agent)
    await GameLoop(actor, dispatch).run(max_ticks=3)
    return actor, result


def _inventory(actor, character_id):
    return sorted(
        actor.world.get_entity(c).get_component(IdentityComponent).name
        for c in contents(actor.world.get_entity(character_id))
    )


def _schema_v1_generated_quest_snapshot():
    return {
        "metadata": {"version": "1.0", "epoch": 12},
        "bunnyland": {"schema_version": 1, "plugins": ["bunnyland.dragonsim"]},
        "prefabs": {"entity": {"components": {}}},
        "entities": {
            "entity_1": {"prefab": "entity", "created_epoch": 0},
            "entity_2": {"prefab": "entity", "created_epoch": 0},
            "entity_3": {"prefab": "entity", "created_epoch": 0},
        },
        "components": {
            "GeneratedQuestComponent": {
                "entity_1": {
                    "title": "Carry the Letter",
                    "objective": "deliver it",
                    "status": "active",
                    "accepted_by": "entity_2",
                }
            },
            "QuestDeadlineComponent": {"entity_1": {"due_at_epoch": 99}},
            "DaggerQuestRewardComponent": {
                "entity_1": {"item_name": "guild writ", "claimed": False}
            },
            "WorldClockComponent": {
                "entity_3": {
                    "game_time_seconds": 12,
                    "tick_index": 0,
                    "time_scale": 1.0,
                }
            },
        },
        "relationships": {},
        "relics": [],
    }


def test_schema_v1_quest_snapshot_migrates_to_canonical_graph_without_mutating_source():
    source = _schema_v1_generated_quest_snapshot()

    migrated = migrate_snapshot(source)

    assert source["bunnyland"]["schema_version"] == 1
    assert migrated["bunnyland"]["schema_version"] == 5
    assert (
        not {
            "GeneratedQuestComponent",
            "QuestDeadlineComponent",
            "DaggerQuestRewardComponent",
        }
        & migrated["components"].keys()
    )
    assert migrated["components"]["QuestComponent"]["entity_1"] == {
        "quest_id": "entity_1",
        "title": "Carry the Letter",
        "description": "deliver it",
    }
    assert migrated["components"]["QuestStateComponent"]["entity_1"] == {
        "status": "active",
        "due_at_epoch": 99,
    }
    assert migrated["relationships"]["QuestAcceptedBy"]["entity_1"][0]["target"] == ("entity_2")
    objective_id = migrated["relationships"]["QuestHasObjective"]["entity_1"][0]["target"]
    reward_id = migrated["relationships"]["QuestHasReward"]["entity_1"][0]["target"]
    assert objective_id == "quest_objective_1"
    assert reward_id == "quest_reward_1"


def test_schema_migration_rejects_future_and_ambiguous_worlds():
    with pytest.raises(WorldMigrationError, match="newer than supported"):
        migrate_snapshot({"bunnyland": {"schema_version": 6}})

    snapshot = _schema_v1_generated_quest_snapshot()
    snapshot["components"]["QuestComponent"] = {
        "entity_1": {"quest_id": "duplicate", "title": "Duplicate"},
        "entity_2": {"quest_id": "duplicate", "title": "Also Duplicate"},
    }
    with pytest.raises(WorldMigrationError, match="refers to both"):
        migrate_snapshot(snapshot)


def test_load_schema_v1_migrates_in_memory_and_next_save_is_v5(tmp_path):
    source = tmp_path / "world-v1.json"
    dest = tmp_path / "world-v5.json"
    snapshot = _schema_v1_generated_quest_snapshot()
    source.write_text(json.dumps(snapshot))

    actor, meta = load_world(source, registry=PluginRegistry(bunnyland_plugins()))

    from bunnyland.simpacks.dragonsim.mechanics import (
        QuestAcceptedBy,
        QuestComponent,
        QuestHasObjective,
        QuestHasReward,
        QuestStateComponent,
    )

    quest = actor.world.get_entity(EntityId.parse("entity_1"))
    assert quest.has_component(QuestComponent)
    assert quest.get_component(QuestStateComponent).due_at_epoch == 99
    assert quest.has_relationship(QuestAcceptedBy, EntityId.parse("entity_2"))
    assert len(quest.get_relationships(QuestHasObjective)) == 1
    assert len(quest.get_relationships(QuestHasReward)) == 1
    assert meta.schema_version == 5
    assert json.loads(source.read_text())["bunnyland"]["schema_version"] == 1

    save_world(actor, dest, meta=meta)

    assert json.loads(dest.read_text())["bunnyland"]["schema_version"] == 5


def test_schema_v4_load_drops_empty_relationship_bucket(tmp_path):
    from bunnyland.simpacks.dinosim.mechanics import BuiltBy, EnclosureComponent

    actor = WorldActor()
    enclosure = spawn_entity(actor.world, [EnclosureComponent()])
    builder = spawn_entity(actor.world, [])
    enclosure.add_relationship(BuiltBy(), builder.id)
    enclosure.remove_relationship(BuiltBy, builder.id)
    enclosure.remove_component(EnclosureComponent)
    assert actor.world.export_entity(enclosure.id)["relationships"]["BuiltBy"] == []

    snapshot = _snapshot(actor, WorldMeta())
    assert "BuiltBy" not in snapshot["relationships"]
    snapshot["relationships"]["BuiltBy"] = {str(enclosure.id): []}
    source = tmp_path / "world-v4-empty-edge.json"
    source.write_text(json.dumps(snapshot))

    loaded, meta = load_world(source, registry=PluginRegistry(bunnyland_plugins()))

    assert loaded.world.has_entity(enclosure.id)
    assert meta.schema_version == 5


def _schema_v4_travel_snapshot():
    return {
        "bunnyland": {"schema_version": 4},
        "entities": {
            "entity_1": {"prefab": "entity", "created_epoch": 0},
            "entity_2": {"prefab": "entity", "created_epoch": 0},
        },
        "components": {
            "TravelHubComponent": {"entity_2": {"name": "Moss Gate", "region_id": "moss"}}
        },
        "relationships": {
            "TravelingToDestination": {
                "entity_1": [
                    {
                        "target": "entity_2",
                        "edge": {
                            "started_at_epoch": 10,
                            "arrive_at_epoch": 20,
                            "mode": "cart",
                            "route_label": "moss road",
                        },
                    }
                ]
            }
        },
    }


def test_schema_v4_travel_state_migrates_to_indexed_component_without_mutating_source():
    source = _schema_v4_travel_snapshot()

    migrated = migrate_snapshot(source)

    assert source["bunnyland"]["schema_version"] == 4
    assert source["relationships"]["TravelingToDestination"]["entity_1"][0]["edge"]
    assert migrated["bunnyland"]["schema_version"] == 5
    assert migrated["components"]["TravelingComponent"]["entity_1"] == {
        "started_at_epoch": 10,
        "arrive_at_epoch": 20,
        "mode": "cart",
        "route_label": "moss road",
    }
    assert migrated["relationships"]["TravelingToDestination"]["entity_1"] == [
        {"target": "entity_2", "edge": {}}
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["components"].__setitem__(
                "TravelingComponent", {"entity_1": {}}
            ),
            "already contains TravelingComponent",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"].__setitem__(
                "missing",
                value["relationships"]["TravelingToDestination"].pop("entity_1"),
            ),
            "source 'missing' does not exist",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"]["entity_1"].append(
                value["relationships"]["TravelingToDestination"]["entity_1"][0]
            ),
            "requires one destination",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"][
                "entity_1"
            ].__setitem__(0, []),
            "edge for 'entity_1' must be a mapping",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"]["entity_1"][0].update(
                {"target": "missing"}
            ),
            "refers to missing target 'missing'",
        ),
        (
            lambda value: value["components"]["TravelHubComponent"].clear(),
            "is not a travel hub",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"]["entity_1"][0][
                "edge"
            ].update({"arrive_at_epoch": True}),
            "arrive_at_epoch.*must be an integer",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"]["entity_1"][0][
                "edge"
            ].update({"mode": 7}),
            "text fields.*must be strings",
        ),
    ],
)
def test_schema_v4_travel_migration_rejects_ambiguous_or_malformed_state(mutate, message):
    source = _schema_v4_travel_snapshot()
    mutate(source)

    with pytest.raises(WorldMigrationError, match=message):
        migrate_snapshot(source)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["components"]["TravelingComponent"].__setitem__(
                "missing", dict(value["components"]["TravelingComponent"]["entity_1"])
            ),
            "owner 'missing' does not exist",
        ),
        (
            lambda value: value["components"]["TravelingComponent"].__setitem__(
                "entity_1", []
            ),
            "TravelingComponent fields.*must be a mapping",
        ),
        (
            lambda value: value["components"]["TravelingComponent"]["entity_1"].pop(
                "route_label"
            ),
            "TravelingComponent fields.*are invalid",
        ),
        (
            lambda value: value["components"]["TravelingComponent"]["entity_1"].update(
                {"arrive_at_epoch": True}
            ),
            "arrive_at_epoch.*must be an integer",
        ),
        (
            lambda value: value["components"]["TravelingComponent"]["entity_1"].update(
                {"mode": 7}
            ),
            "TravelingComponent text fields.*must be strings",
        ),
        (
            lambda value: value["components"]["TravelingComponent"].clear(),
            "lacks TravelingComponent",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"].__setitem__(
                "entity_1", {}
            ),
            "source 'entity_1' requires one destination",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"][
                "entity_1"
            ].__setitem__(0, []),
            "edge for 'entity_1' must be a mapping",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"].clear(),
            "requires one TravelingToDestination edge",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"]["entity_1"][0][
                "edge"
            ].update({"mode": "cart"}),
            "must not contain properties",
        ),
        (
            lambda value: value["relationships"]["TravelingToDestination"]["entity_1"][0].update(
                {"target": "missing"}
            ),
            "refers to missing target 'missing'",
        ),
        (
            lambda value: value["components"]["TravelHubComponent"].clear(),
            "target 'entity_2' is not a travel hub",
        ),
    ],
)
def test_schema_v5_travel_state_requires_one_propertyless_destination(mutate, message):
    snapshot = migrate_snapshot(_schema_v4_travel_snapshot())
    mutate(snapshot)

    with pytest.raises(WorldMigrationError, match=message):
        migrate_snapshot(snapshot)


def _schema_v4_live_relationship_snapshot():
    entity = {"prefab": "entity", "created_epoch": 0}
    return {
        "bunnyland": {"schema_version": 4},
        "entities": {f"entity_{index}": dict(entity) for index in range(1, 9)},
        "components": {
            "CharacterComponent": {"entity_1": {}},
            "RoomComponent": {"entity_2": {"title": "Archive"}},
            "WorldHistoryRecordComponent": {
                "entity_3": {
                    "summary": "A deed.",
                    "source_event_id": "event-1",
                    "event_type": "TestEvent",
                    "created_at_epoch": 10,
                    "location_id": "entity_2",
                    "tags": ["test"],
                    "salience": 0.7,
                }
            },
            "PhysicalMarkComponent": {
                "entity_4": {
                    "text": "A mark.",
                    "mark_type": "writing",
                    "author_id": "entity_1",
                    "source_event_id": "event-1",
                    "created_at_epoch": 10,
                }
            },
            "CreatorSignatureComponent": {
                "entity_4": {
                    "creator_id": "entity_1",
                    "source_event_id": "event-1",
                    "created_at_epoch": 10,
                    "circumstance": "writing on archive",
                },
                "entity_5": {
                    "creator_id": "entity_1",
                    "source_event_id": "event-2",
                    "created_at_epoch": 11,
                    "circumstance": "crafting",
                },
            },
            "DeedReputationComponent": {
                "entity_1": {
                    "scores": {"test": 0.7},
                    "deed_ids": ["entity_3"],
                    "known_for": ["A deed."],
                }
            },
            "SampleComponent": {
                "entity_6": {
                    "sample_type": "moss",
                    "studied_by": ["entity_1"],
                }
            },
            "DeathConsequenceComponent": {
                "entity_7": {
                    "character_id": "entity_1",
                    "cause": "testing",
                    "source_event_id": "event-3",
                    "created_at_epoch": 12,
                    "location_id": "entity_2",
                    "summary": "A test death.",
                }
            },
            "DecorationSource3DComponent": {
                "entity_8": {
                    "room_id": "entity_2",
                    "recipe_key": "bunnyland.3d/meadow",
                    "role": "bunnyland.3d/flora",
                    "recipe_version": 2,
                }
            },
        },
        "relationships": {
            "CreatedBy": {
                "entity_4": [
                    {
                        "target": "entity_1",
                        "edge": {
                            "source_event_id": "event-1",
                            "created_at_epoch": 10,
                            "circumstance": "writing on archive",
                        },
                    }
                ],
                "entity_5": [
                    {
                        "target": "entity_1",
                        "edge": {
                            "source_event_id": "event-2",
                            "created_at_epoch": 11,
                            "circumstance": "crafting",
                        },
                    }
                ],
            },
            "HasDecoration3D": {
                "entity_2": [
                    {
                        "target": "entity_8",
                        "edge": {"role": "bunnyland.3d/flora"},
                    }
                ]
            },
        },
    }


def test_schema_v4_live_component_references_migrate_to_edges():
    source = _schema_v4_live_relationship_snapshot()

    migrated = migrate_snapshot(source)

    assert source["components"]["PhysicalMarkComponent"]["entity_4"]["author_id"] == (
        "entity_1"
    )
    assert "entity_4" not in migrated["components"]["CreatorSignatureComponent"]
    assert "author_id" not in migrated["components"]["PhysicalMarkComponent"]["entity_4"]
    assert "creator_id" not in migrated["components"]["CreatorSignatureComponent"]["entity_5"]
    assert "location_id" not in migrated["components"]["WorldHistoryRecordComponent"]["entity_3"]
    assert "deed_ids" not in migrated["components"]["DeedReputationComponent"]["entity_1"]
    assert "studied_by" not in migrated["components"]["SampleComponent"]["entity_6"]
    assert "character_id" not in migrated["components"]["DeathConsequenceComponent"]["entity_7"]
    assert "location_id" not in migrated["components"]["DeathConsequenceComponent"]["entity_7"]
    assert "room_id" not in migrated["components"]["DecorationSource3DComponent"]["entity_8"]
    assert migrated["relationships"]["HistoryLocation"]["entity_3"] == [
        {"target": "entity_2", "edge": {}}
    ]
    assert migrated["relationships"]["CreditedDeed"]["entity_1"] == [
        {"target": "entity_3", "edge": {"score": 0.7}}
    ]
    assert migrated["relationships"]["StudiedBy"]["entity_6"] == [
        {"target": "entity_1", "edge": {}}
    ]
    assert migrated["relationships"]["DeathOf"]["entity_7"] == [
        {"target": "entity_1", "edge": {}}
    ]
    assert migrated["relationships"]["DeathAt"]["entity_7"] == [
        {"target": "entity_2", "edge": {}}
    ]


def test_schema_v5_rejects_legacy_live_reference_fields():
    snapshot = migrate_snapshot(_schema_v4_live_relationship_snapshot())
    snapshot["components"]["SampleComponent"]["entity_6"]["studied_by"] = ["entity_1"]

    with pytest.raises(WorldMigrationError, match="legacy SampleComponent.studied_by"):
        migrate_snapshot(snapshot)


@pytest.mark.parametrize(
    ("component_name", "entity_id"),
    [
        ("WorldHistoryRecordComponent", "entity_3"),
        ("PhysicalMarkComponent", "entity_4"),
        ("CreatorSignatureComponent", "entity_5"),
        ("DeedReputationComponent", "entity_1"),
        ("DeathConsequenceComponent", "entity_7"),
        ("SampleComponent", "entity_6"),
        ("DecorationSource3DComponent", "entity_8"),
    ],
)
def test_schema_v4_live_reference_migration_requires_component_mappings(
    component_name, entity_id
):
    snapshot = _schema_v4_live_relationship_snapshot()
    snapshot["components"][component_name][entity_id] = []

    with pytest.raises(WorldMigrationError, match=f"{component_name} fields"):
        migrate_snapshot(snapshot)


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("source_event_id", 1, "source_event_id.*must be a string"),
        ("created_at_epoch", True, "created_at_epoch.*must be an integer"),
    ],
)
def test_schema_v4_creator_provenance_requires_typed_fields(field_name, value, message):
    snapshot = _schema_v4_live_relationship_snapshot()
    snapshot["components"]["CreatorSignatureComponent"]["entity_5"][field_name] = value

    with pytest.raises(WorldMigrationError, match=message):
        migrate_snapshot(snapshot)


@pytest.mark.parametrize(
    ("component_name", "entity_id", "field_name"),
    [
        ("DeedReputationComponent", "entity_1", "deed_ids"),
        ("SampleComponent", "entity_6", "studied_by"),
    ],
)
def test_schema_v4_repeatable_live_references_require_sequences(
    component_name, entity_id, field_name
):
    snapshot = _schema_v4_live_relationship_snapshot()
    snapshot["components"][component_name][entity_id][field_name] = "entity_1"

    with pytest.raises(WorldMigrationError, match=f"{component_name}.{field_name}.*sequence"):
        migrate_snapshot(snapshot)


def test_schema_v4_live_reference_migration_discards_stale_optional_targets():
    snapshot = _schema_v4_live_relationship_snapshot()
    snapshot["components"]["PhysicalMarkComponent"]["entity_4"]["author_id"] = "entity_99"
    del snapshot["components"]["CreatorSignatureComponent"]["entity_4"]
    snapshot["components"]["CreatorSignatureComponent"]["entity_5"]["creator_id"] = "entity_99"
    snapshot["components"]["DeedReputationComponent"]["entity_1"]["deed_ids"] = [
        "entity_99"
    ]
    snapshot["components"]["WorldHistoryRecordComponent"]["entity_3"][
        "location_id"
    ] = "entity_99"
    snapshot["components"]["DeathConsequenceComponent"]["entity_7"].update(
        {"character_id": "entity_99", "location_id": "entity_99"}
    )
    snapshot["components"]["SampleComponent"]["entity_6"]["studied_by"] = ["entity_99"]

    migrated = migrate_snapshot(snapshot)

    assert "entity_4" not in migrated["relationships"]["CreatedBy"]
    assert "entity_5" not in migrated["relationships"]["CreatedBy"]
    assert "entity_1" not in migrated["relationships"]["CreditedDeed"]
    assert "entity_7" not in migrated["relationships"]["DeathOf"]
    assert "entity_7" not in migrated["relationships"]["DeathAt"]
    assert "entity_6" not in migrated["relationships"]["StudiedBy"]
    assert "entity_3" not in migrated["relationships"]["HistoryLocation"]


def test_schema_v4_decoration_owner_backfills_and_rejects_conflicts():
    snapshot = _schema_v4_live_relationship_snapshot()
    snapshot["relationships"]["HasDecoration3D"].clear()
    snapshot["relationships"]["HasDecoration3D"]["entity_1"] = [
        {"target": "entity_3", "edge": {"role": "bunnyland.3d/detail"}}
    ]

    migrated = migrate_snapshot(snapshot)

    assert migrated["relationships"]["HasDecoration3D"]["entity_2"] == [
        {"target": "entity_8", "edge": {"role": "bunnyland.3d/flora"}}
    ]

    malformed_signature = _schema_v4_live_relationship_snapshot()
    malformed_signature["components"]["CreatorSignatureComponent"]["entity_4"] = []
    with pytest.raises(WorldMigrationError, match="CreatorSignatureComponent fields"):
        migrate_snapshot(malformed_signature)

    malformed = _schema_v4_live_relationship_snapshot()
    malformed["relationships"]["HasDecoration3D"]["entity_2"] = {}
    with pytest.raises(WorldMigrationError, match="HasDecoration3D edges.*must be a list"):
        migrate_snapshot(malformed)

    conflict = _schema_v4_live_relationship_snapshot()
    conflict["entities"]["entity_9"] = {"prefab": "entity", "created_epoch": 0}
    conflict["components"]["RoomComponent"]["entity_9"] = {"title": "Other"}
    conflict["relationships"]["HasDecoration3D"]["entity_9"] = conflict["relationships"][
        "HasDecoration3D"
    ].pop("entity_2")
    with pytest.raises(WorldMigrationError, match="conflicts with HasDecoration3D"):
        migrate_snapshot(conflict)


def _append_relationship(snapshot, edge_name, source_id, target_id, edge=None):
    snapshot["relationships"].setdefault(edge_name, {}).setdefault(source_id, []).append(
        {"target": target_id, "edge": edge or {}}
    )


def test_schema_v5_relationship_validation_rejects_malformed_records():
    base = migrate_snapshot(_schema_v4_live_relationship_snapshot())

    missing_source = deepcopy(base)
    missing_source["relationships"]["CreatedBy"]["entity_99"] = missing_source[
        "relationships"
    ]["CreatedBy"].pop("entity_5")
    with pytest.raises(WorldMigrationError, match="CreatedBy source.*does not exist"):
        migrate_snapshot(missing_source)

    non_list = deepcopy(base)
    non_list["relationships"]["CreatedBy"]["entity_5"] = {}
    with pytest.raises(WorldMigrationError, match="CreatedBy edges.*must be a list"):
        migrate_snapshot(non_list)

    non_mapping = deepcopy(base)
    non_mapping["relationships"]["CreatedBy"]["entity_5"] = [[]]
    with pytest.raises(WorldMigrationError, match="CreatedBy edge.*must be a mapping"):
        migrate_snapshot(non_mapping)

    missing_target = deepcopy(base)
    missing_target["relationships"]["CreatedBy"]["entity_5"][0]["target"] = "entity_99"
    with pytest.raises(WorldMigrationError, match="refers to missing target"):
        migrate_snapshot(missing_target)

    duplicate = deepcopy(base)
    duplicate["relationships"]["CreatedBy"]["entity_5"].append(
        deepcopy(duplicate["relationships"]["CreatedBy"]["entity_5"][0])
    )
    with pytest.raises(WorldMigrationError, match="duplicate CreatedBy edge"):
        migrate_snapshot(duplicate)


def test_schema_v5_created_by_validation_enforces_provenance_contract():
    base = migrate_snapshot(_schema_v4_live_relationship_snapshot())

    wrong_source = deepcopy(base)
    wrong_source["relationships"]["CreatedBy"]["entity_2"] = wrong_source[
        "relationships"
    ]["CreatedBy"].pop("entity_5")
    with pytest.raises(WorldMigrationError, match="lacks creator provenance"):
        migrate_snapshot(wrong_source)

    multiple = deepcopy(base)
    multiple["entities"]["entity_9"] = {"prefab": "entity", "created_epoch": 0}
    multiple["components"]["CharacterComponent"]["entity_9"] = {}
    _append_relationship(
        multiple,
        "CreatedBy",
        "entity_5",
        "entity_9",
        {
            "source_event_id": "event-2",
            "created_at_epoch": 11,
            "circumstance": "crafting",
        },
    )
    with pytest.raises(WorldMigrationError, match="multiple CreatedBy targets"):
        migrate_snapshot(multiple)

    wrong_target = deepcopy(base)
    wrong_target["relationships"]["CreatedBy"]["entity_5"][0]["target"] = "entity_2"
    with pytest.raises(WorldMigrationError, match="CreatedBy target.*not a character"):
        migrate_snapshot(wrong_target)

    wrong_fields = deepcopy(base)
    del wrong_fields["relationships"]["CreatedBy"]["entity_5"][0]["edge"]["circumstance"]
    with pytest.raises(WorldMigrationError, match="CreatedBy fields.*invalid"):
        migrate_snapshot(wrong_fields)

    wrong_types = deepcopy(base)
    wrong_types["relationships"]["CreatedBy"]["entity_5"][0]["edge"][
        "created_at_epoch"
    ] = True
    with pytest.raises(WorldMigrationError, match="CreatedBy fields.*invalid types"):
        migrate_snapshot(wrong_types)


def test_schema_v5_history_location_and_credit_validation_enforces_graph_contracts():
    base = migrate_snapshot(_schema_v4_live_relationship_snapshot())

    wrong_location_source = deepcopy(base)
    wrong_location_source["relationships"]["HistoryLocation"]["entity_5"] = (
        wrong_location_source["relationships"]["HistoryLocation"].pop("entity_3")
    )
    with pytest.raises(WorldMigrationError, match="HistoryLocation source.*lacks"):
        migrate_snapshot(wrong_location_source)

    multiple_locations = deepcopy(base)
    _append_relationship(multiple_locations, "HistoryLocation", "entity_3", "entity_1")
    with pytest.raises(WorldMigrationError, match="multiple HistoryLocation targets"):
        migrate_snapshot(multiple_locations)

    wrong_location_target = deepcopy(base)
    wrong_location_target["relationships"]["HistoryLocation"]["entity_3"][0][
        "target"
    ] = "entity_1"
    with pytest.raises(WorldMigrationError, match="HistoryLocation target.*not a room"):
        migrate_snapshot(wrong_location_target)

    location_properties = deepcopy(base)
    location_properties["relationships"]["HistoryLocation"]["entity_3"][0]["edge"] = {
        "old": True
    }
    with pytest.raises(WorldMigrationError, match="HistoryLocation edge.*properties"):
        migrate_snapshot(location_properties)

    wrong_credit_source = deepcopy(base)
    wrong_credit_source["relationships"]["CreditedDeed"]["entity_2"] = wrong_credit_source[
        "relationships"
    ]["CreditedDeed"].pop("entity_1")
    with pytest.raises(WorldMigrationError, match="lacks character reputation"):
        migrate_snapshot(wrong_credit_source)

    wrong_credit_target = deepcopy(base)
    wrong_credit_target["relationships"]["CreditedDeed"]["entity_1"][0][
        "target"
    ] = "entity_2"
    with pytest.raises(WorldMigrationError, match="not a history record"):
        migrate_snapshot(wrong_credit_target)

    wrong_score = deepcopy(base)
    wrong_score["relationships"]["CreditedDeed"]["entity_1"][0]["edge"]["score"] = True
    with pytest.raises(WorldMigrationError, match="score.*must be numeric"):
        migrate_snapshot(wrong_score)


@pytest.mark.parametrize(
    ("edge_name", "wrong_target", "expected"),
    [("DeathOf", "entity_2", "character"), ("DeathAt", "entity_1", "room")],
)
def test_schema_v5_death_relationship_validation(edge_name, wrong_target, expected):
    base = migrate_snapshot(_schema_v4_live_relationship_snapshot())

    wrong_source = deepcopy(base)
    wrong_source["relationships"][edge_name]["entity_5"] = wrong_source["relationships"][
        edge_name
    ].pop("entity_7")
    with pytest.raises(WorldMigrationError, match=f"{edge_name} source.*lacks"):
        migrate_snapshot(wrong_source)

    multiple = deepcopy(base)
    _append_relationship(multiple, edge_name, "entity_7", wrong_target)
    with pytest.raises(WorldMigrationError, match=f"multiple {edge_name} targets"):
        migrate_snapshot(multiple)

    wrong_type = deepcopy(base)
    wrong_type["relationships"][edge_name]["entity_7"][0]["target"] = wrong_target
    with pytest.raises(WorldMigrationError, match=f"not a {expected}"):
        migrate_snapshot(wrong_type)

    properties = deepcopy(base)
    properties["relationships"][edge_name]["entity_7"][0]["edge"] = {"old": True}
    with pytest.raises(WorldMigrationError, match=f"{edge_name} edge.*properties"):
        migrate_snapshot(properties)


def test_schema_v5_rejects_duplicate_signature_state_and_non_mapping_legacy_component():
    base = migrate_snapshot(_schema_v4_live_relationship_snapshot())

    duplicate_signature = deepcopy(base)
    duplicate_signature["components"]["CreatorSignatureComponent"]["entity_4"] = {
        "source_event_id": "event-1",
        "created_at_epoch": 10,
        "circumstance": "writing",
    }
    with pytest.raises(WorldMigrationError, match="physical mark.*CreatorSignatureComponent"):
        migrate_snapshot(duplicate_signature)

    non_mapping = deepcopy(base)
    non_mapping["components"]["SampleComponent"]["entity_6"] = []
    with pytest.raises(WorldMigrationError, match="SampleComponent fields.*must be a mapping"):
        migrate_snapshot(non_mapping)


def test_jsonable_stringifies_non_json_boundary_values():
    assert _jsonable(Path("worlds/example.json")) == "worlds/example.json"


def test_schema_v1_regular_quest_collections_become_ordered_edges():
    snapshot = _schema_v1_generated_quest_snapshot()
    snapshot["components"].pop("GeneratedQuestComponent")
    snapshot["components"].pop("DaggerQuestRewardComponent")
    snapshot["components"]["QuestComponent"] = {
        "entity_1": {
            "quest_id": "letter",
            "title": "Carry the Letter",
            "status": "active",
            "accepted_by": "entity_2",
            "completed_at_epoch": 20,
        }
    }
    snapshot["entities"].update(
        {
            "entity_4": {"prefab": "entity", "created_epoch": 0},
            "entity_5": {"prefab": "entity", "created_epoch": 0},
            "entity_6": {"prefab": "entity", "created_epoch": 0},
        }
    )
    snapshot["components"]["QuestStageComponent"] = {
        "entity_4": {
            "quest_id": "letter",
            "stage": 2,
            "branch": "honest",
            "tracked_by": ["entity_2"],
        }
    }
    snapshot["components"]["QuestObjectiveComponent"] = {
        "entity_4": {"quest_id": "letter", "description": "Deliver it"}
    }
    snapshot["components"]["QuestRewardComponent"] = {
        "entity_5": {
            "quest_id": "letter",
            "description": "A writ",
            "item_ids": ["entity_6"],
        }
    }

    migrated = migrate_snapshot(snapshot)

    assert migrated["components"]["QuestComponent"]["entity_1"]["description"] == ""
    assert migrated["components"]["QuestStateComponent"]["entity_1"] == {
        "status": "active",
        "completed_at_epoch": 20,
        "stage": 2,
        "branch": "honest",
        "due_at_epoch": 99,
    }
    assert migrated["relationships"]["TracksQuest"]["entity_2"][0]["target"] == "entity_1"
    assert migrated["relationships"]["QuestHasObjective"]["entity_1"][0]["target"] == ("entity_4")
    assert migrated["relationships"]["QuestHasReward"]["entity_1"][0]["target"] == ("entity_5")
    assert migrated["relationships"]["QuestRewardGrants"]["entity_5"][0]["target"] == ("entity_6")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update({"components": []}), "section 'components'"),
        (
            lambda value: value["components"].update({"GeneratedQuestComponent": []}),
            "GeneratedQuestComponent.*mapping",
        ),
        (
            lambda value: value["components"].update({"GeneratedQuestComponent": {"entity_1": []}}),
            "fields.*mapping",
        ),
        (
            lambda value: value["relationships"].update({"QuestAcceptedBy": {"entity_1": {}}}),
            "QuestAcceptedBy edges.*list",
        ),
        (
            lambda value: value["components"].update(
                {"QuestObjectiveComponent": {"entity_4": {"quest_id": "missing"}}}
            ),
            "unknown quest",
        ),
        (
            lambda value: value["components"].update({"QuestStageComponent": []}),
            "QuestStageComponent.*mapping",
        ),
        (
            lambda value: value["components"].update({"QuestDeadlineComponent": []}),
            "QuestDeadlineComponent.*mapping",
        ),
        (
            lambda value: value["components"].update({"DaggerQuestRewardComponent": []}),
            "DaggerQuestRewardComponent.*mapping",
        ),
    ],
)
def test_schema_v1_migration_rejects_malformed_relationships(mutate, message):
    snapshot = _schema_v1_generated_quest_snapshot()
    mutate(snapshot)
    with pytest.raises(WorldMigrationError, match=message):
        migrate_snapshot(snapshot)


def test_schema_migration_validates_version_and_v2_sections():
    with pytest.raises(WorldMigrationError, match="must be a mapping"):
        migrate_snapshot([])
    with pytest.raises(WorldMigrationError, match="must be an integer"):
        migrate_snapshot({"bunnyland": {"schema_version": "2"}})
    with pytest.raises(WorldMigrationError, match="unsupported world schema"):
        migrate_snapshot({"bunnyland": {"schema_version": 0}})
    with pytest.raises(WorldMigrationError, match="section 'entities'"):
        migrate_snapshot({"bunnyland": {"schema_version": 2}, "entities": []})
    with pytest.raises(WorldMigrationError, match="section 'relationships'"):
        migrate_snapshot({"bunnyland": {"schema_version": 2}, "relationships": []})


def _lifesim_v2_snapshot(version: int = 2):
    return {
        "bunnyland": {"schema_version": version},
        "entities": {
            "entity_1": {"prefab": "entity"},
            "entity_2": {"prefab": "entity"},
            "entity_3": {"prefab": "entity"},
        },
        "components": {
            "CharacterComponent": {"entity_1": {}, "entity_2": {}},
            "RoomComponent": {"entity_3": {"title": "Moss Home"}},
            "HomeComponent": {"entity_3": {"owner_id": "entity_1", "household_id": "moss"}},
            "RoomClaimComponent": {
                "entity_3": {"claimed_by_id": "entity_1", "claimed_at_epoch": 7}
            },
            "PregnancyComponent": {
                "entity_1": {
                    "started_at_epoch": 5,
                    "due_at_epoch": 50,
                    "co_parent_ids": ["entity_2"],
                    "source_event_id": "event-1",
                }
            },
        },
        "relationships": {},
    }


@pytest.mark.parametrize("version", [1, 2])
def test_lifesim_v1_and_v2_relationship_fields_migrate_sequentially_to_v5(version):
    source = _lifesim_v2_snapshot(version)

    migrated = migrate_snapshot(source)

    assert source["bunnyland"]["schema_version"] == version
    assert migrated["bunnyland"]["schema_version"] == 5
    assert "HomeComponent" not in migrated["components"]
    assert "RoomClaimComponent" not in migrated["components"]
    assert migrated["components"]["PregnancyComponent"]["entity_1"] == {
        "started_at_epoch": 5,
        "due_at_epoch": 50,
        "source_event_id": "event-1",
    }
    assert migrated["relationships"]["OwnsHome"]["entity_1"] == [
        {"target": "entity_3", "edge": {"household_id": "moss"}}
    ]
    assert migrated["relationships"]["ClaimsRoom"]["entity_1"] == [
        {"target": "entity_3", "edge": {"claimed_at_epoch": 7}}
    ]
    assert migrated["relationships"]["PregnancyCoParent"]["entity_1"] == [
        {"target": "entity_2", "edge": {}}
    ]


def _world_database_v3_snapshot():
    entity_ids = [f"entity_{index}" for index in range(1, 13)]
    return {
        "bunnyland": {"schema_version": 3},
        "entities": {entity_id: {"prefab": "entity"} for entity_id in entity_ids},
        "components": {
            "CharacterComponent": {"entity_1": {}, "entity_2": {}, "entity_4": {}},
            "RoomComponent": {"entity_3": {"title": "Den"}},
            "FossilFragmentComponent": {"entity_5": {}},
            "AncientSampleComponent": {
                "entity_6": {"species_name": "raptor", "source_fossil_id": "entity_5"}
            },
            "EggComponent": {"entity_7": {"species_name": "raptor", "laid_at_epoch": 0}},
            "CloneCandidateComponent": {
                "entity_7": {"species_name": "raptor", "source_sample_id": "entity_6"}
            },
            "HatchlingComponent": {"entity_4": {"hatched_at_epoch": 2, "egg_id": "entity_7"}},
            "FossilSurveyComponent": {"entity_5": {"surveyed_by": ["entity_1"]}},
            "IncubationComponent": {"entity_7": {"brooded_by": "entity_1"}},
            "EggInspectionComponent": {"entity_7": {"inspected_by": "entity_1"}},
            "ImprintComponent": {"entity_4": {"imprinted_by": "entity_1", "bond": 2}},
            "JuvenileCareComponent": {"entity_4": {"cared_by": "entity_1"}},
            "WaterStudyComponent": {"entity_4": {"studied_by": ["entity_1", "entity_2"]}},
            "BroodingComponent": {"entity_7": {"brooder_id": "entity_1"}},
            "TrackComponent": {"entity_4": {"room_id": "entity_3"}},
            "TerritoryComponent": {"entity_3": {"marked_by": "entity_1"}},
            "NestComponent": {"entity_3": {"prepared_by": "entity_1"}},
            "BaitComponent": {"entity_8": {"set_by_id": "entity_1"}},
            "TamingComponent": {"entity_4": {"tamer_id": "entity_1", "tamed": True}},
            "CommandComponent": {
                "entity_4": {
                    "command_name": "hunt",
                    "commanded_by_id": "entity_1",
                    "target_id": "velociraptor",
                }
            },
            "MountComponent": {"entity_4": {"rider_id": "entity_1", "mounted": True}},
            "CompanionComponent": {"entity_4": {"owner_id": "entity_1", "role": "guard"}},
            "GuardBehaviorComponent": {"entity_4": {"location_id": "entity_3"}},
            "RecallComponent": {"entity_4": {"home_room_id": "entity_3"}},
            "EnclosureComponent": {"entity_3": {"built_by_id": "entity_1"}},
            "KaijuComponent": {"entity_4": {"target_room_id": "entity_3"}},
            "GrappleComponent": {"entity_4": {"target_id": "entity_2"}},
            "CreatureProductComponent": {
                "entity_8": {"product_type": "milk", "source_creature_id": "entity_4"}
            },
            "RanchLaborComponent": {
                "entity_4": {"target_id": "entity_3", "assigned_by_id": "entity_1"}
            },
            "GuardAnimalComponent": {
                "entity_4": {"location_id": "entity_3", "assigned_by_id": "entity_1"}
            },
            "IncidentComponent": {
                "entity_9": {
                    "kind": "raid",
                    "budget_spent": 2,
                    "started_at_epoch": 1,
                    "room_id": "entity_3",
                }
            },
            "IdentityComponent": {
                "entity_10": {"name": "camera", "kind": "device"},
                "entity_11": {"name": "moon", "kind": "orbital-body"},
                "entity_12": {"name": "Vega", "kind": "star-system"},
            },
            "DeviceComponent": {"entity_10": {"device_type": "camera"}},
            "RecordedEvidenceComponent": {
                "entity_8": {"subject_id": "entity_1", "device_id": "entity_10"}
            },
            "ShipComponent": {"entity_4": {"name": "Courier"}},
            "OrbitalBodyComponent": {"entity_11": {"body_type": "moon"}},
            "OrbitComponent": {"entity_4": {"body_id": "entity_11", "altitude": "orbit"}},
            "StarSystemComponent": {"entity_12": {"name": "Vega"}},
            "NavigationRouteComponent": {
                "entity_4": {"destination_id": "entity_12", "status": "plotted"}
            },
        },
        "relationships": {},
    }


def test_schema_v3_world_database_fields_migrate_to_v5_edges_without_mutating_source():
    source = _world_database_v3_snapshot()

    migrated = migrate_snapshot(source)

    assert source["bunnyland"]["schema_version"] == 3
    assert migrated["bunnyland"]["schema_version"] == 5
    expected = {
        "SurveyedBy": ("entity_5", "entity_1"),
        "SampledFromFossil": ("entity_6", "entity_5"),
        "ClonedFromSample": ("entity_7", "entity_6"),
        "HatchedFromEgg": ("entity_4", "entity_7"),
        "InspectedBy": ("entity_7", "entity_1"),
        "ImprintedBy": ("entity_4", "entity_1"),
        "CaredForBy": ("entity_4", "entity_1"),
        "StudiedBy": ("entity_4", "entity_1"),
        "BroodedBy": ("entity_7", "entity_1"),
        "TrackedAt": ("entity_4", "entity_3"),
        "MarkedBy": ("entity_3", "entity_1"),
        "PreparedBy": ("entity_3", "entity_1"),
        "SetBy": ("entity_8", "entity_1"),
        "TamedBy": ("entity_4", "entity_1"),
        "CommandedBy": ("entity_4", "entity_1"),
        "MountedBy": ("entity_4", "entity_1"),
        "CompanionOf": ("entity_4", "entity_1"),
        "GuardsLocation": ("entity_4", "entity_3"),
        "RecallHome": ("entity_4", "entity_3"),
        "BuiltBy": ("entity_3", "entity_1"),
        "KaijuTargets": ("entity_4", "entity_3"),
        "Grappling": ("entity_4", "entity_2"),
        "ProductFromCreature": ("entity_8", "entity_4"),
        "AssignedBy": ("entity_4", "entity_1"),
        "RanchWorkTarget": ("entity_4", "entity_3"),
        "EvidenceSubject": ("entity_8", "entity_1"),
        "RecordedByDevice": ("entity_8", "entity_10"),
        "OrbitsBody": ("entity_4", "entity_11"),
        "NavigatesTo": ("entity_4", "entity_12"),
    }
    for edge_name, (owner_id, target_id) in expected.items():
        records = migrated["relationships"][edge_name][owner_id]
        assert any(record["target"] == target_id for record in records), edge_name
    assert migrated["components"]["CommandComponent"]["entity_4"]["target_key"] == ("velociraptor")
    assert migrated["relationships"]["CompanionOf"]["entity_4"] == [
        {"target": "entity_1", "edge": {"role": "guard"}}
    ]
    assert migrated["relationships"]["Contains"]["entity_3"][0]["target"] == "entity_9"
    assert migrated["components"]["EggComponent"]["entity_7"]["hatched"] is True


def test_schema_v3_world_database_migration_has_json_yaml_parity():
    import yaml

    source = _world_database_v3_snapshot()
    from_json = migrate_snapshot(json.loads(json.dumps(source)))
    from_yaml = migrate_snapshot(yaml.safe_load(yaml.safe_dump(source)))

    assert from_json == from_yaml


def test_schema_v3_hatched_lineage_restores_consumed_legacy_egg_state():
    source = _world_database_v3_snapshot()
    source["components"]["EggComponent"].pop("entity_7")
    source["components"]["DinosaurComponent"] = {"entity_4": {"species_name": "raptor"}}

    migrated = migrate_snapshot(source)

    assert migrated["components"]["EggComponent"]["entity_7"] == {
        "species_name": "raptor",
        "laid_at_epoch": 0,
        "fertilized": True,
        "source": "legacy",
        "hatched": True,
    }


@pytest.mark.parametrize(
    ("component", "field"),
    [
        ("IncidentComponent", "room_id"),
        ("TamingComponent", "tamer_id"),
        ("RecordedEvidenceComponent", "subject_id"),
        ("OrbitComponent", "body_id"),
        ("NavigationRouteComponent", "destination_id"),
    ],
)
def test_schema_v3_world_database_migration_rejects_missing_live_targets(component, field):
    source = _world_database_v3_snapshot()
    owner_id = next(iter(source["components"][component]))
    source["components"][component][owner_id][field] = "entity_999"

    with pytest.raises(
        WorldMigrationError,
        match=rf"entity {owner_id!r} component {component}\.{field}.*missing entity",
    ):
        migrate_snapshot(source)


def test_schema_v3_world_database_migration_covers_legacy_shapes_and_semantic_targets():
    def check_error(mutate, message):
        source = _world_database_v3_snapshot()
        mutate(source)
        with pytest.raises(WorldMigrationError, match=message):
            migrate_snapshot(source)

    check_error(
        lambda value: value["components"]["AncientSampleComponent"].update({"entity_6": []}),
        "AncientSampleComponent fields.*mapping",
    )
    check_error(
        lambda value: value["components"]["FossilSurveyComponent"]["entity_5"].update(
            {"surveyed_by": "entity_1"}
        ),
        "surveyed_by.*sequence",
    )
    check_error(
        lambda value: value["components"]["IncidentComponent"].update({"entity_9": []}),
        "IncidentComponent fields.*mapping",
    )
    check_error(
        lambda value: value["components"]["IncidentComponent"]["entity_9"].update(
            {"room_id": "entity_1"}
        ),
        "without RoomComponent",
    )
    check_error(
        lambda value: value["relationships"].update({"Contains": {"entity_3": {}}}),
        "Contains edges.*list",
    )
    check_error(
        lambda value: value["relationships"].update(
            {"Contains": {"entity_1": [{"target": "entity_9", "edge": {"mode": "room_content"}}]}}
        ),
        "conflicts with incoming Contains",
    )
    check_error(
        lambda value: value["components"]["HatchlingComponent"].update({"entity_4": []}),
        "HatchlingComponent fields.*mapping",
    )
    check_error(
        lambda value: value["components"]["EggComponent"].update({"entity_7": []}),
        "EggComponent fields.*mapping",
    )
    check_error(
        lambda value: value["components"]["CompanionComponent"].update({"entity_4": []}),
        "CompanionComponent fields.*mapping",
    )
    check_error(
        lambda value: value["components"]["CompanionComponent"]["entity_4"].update({"role": ""}),
        "CompanionComponent.role",
    )
    check_error(
        lambda value: value["components"]["CommandComponent"].update({"entity_4": []}),
        "CommandComponent fields.*mapping",
    )
    check_error(
        lambda value: value["components"]["CommandComponent"]["entity_4"].update(
            {"command_name": "guard", "target_id": "entity_999"}
        ),
        "CommandComponent.target_id.*missing entity",
    )
    check_error(
        lambda value: value["components"]["NavigationRouteComponent"].update({"entity_4": []}),
        "NavigationRouteComponent fields.*mapping",
    )

    no_sample_source = _world_database_v3_snapshot()
    no_sample_source["components"]["AncientSampleComponent"]["entity_6"].pop("source_fossil_id")
    assert migrate_snapshot(no_sample_source)["bunnyland"]["schema_version"] == 5

    optional_none = _world_database_v3_snapshot()
    optional_none["components"]["TamingComponent"]["entity_4"]["tamer_id"] = None
    assert migrate_snapshot(optional_none)["relationships"]["TamedBy"] == {}

    unlocated_incident = _world_database_v3_snapshot()
    unlocated_incident["components"]["IncidentComponent"]["entity_9"].pop("room_id")
    assert migrate_snapshot(unlocated_incident)["bunnyland"]["schema_version"] == 5

    existing_containment = _world_database_v3_snapshot()
    existing_containment["relationships"]["Contains"] = {
        "entity_3": [
            {"target": "entity_8", "edge": {"mode": "room_content"}},
            {"target": "entity_9", "edge": {"mode": "room_content"}},
        ]
    }
    assert (
        migrate_snapshot(existing_containment)["relationships"]["Contains"]
        == (existing_containment["relationships"]["Contains"])
    )

    unrelated_containment = _world_database_v3_snapshot()
    unrelated_containment["relationships"]["Contains"] = {
        "entity_3": [{"target": "entity_8", "edge": {"mode": "room_content"}}]
    }
    migrated_containment = migrate_snapshot(unrelated_containment)["relationships"]["Contains"]
    assert migrated_containment["entity_3"][-1]["target"] == "entity_9"

    no_lineage = _world_database_v3_snapshot()
    no_lineage["components"]["HatchlingComponent"]["entity_4"].pop("egg_id")
    assert migrate_snapshot(no_lineage)["relationships"]["HatchedFromEgg"] == {}

    identity_species = _world_database_v3_snapshot()
    identity_species["components"]["EggComponent"].pop("entity_7")
    identity_species["components"]["IdentityComponent"]["entity_4"] = {
        "name": "raptor hatchling",
        "kind": "character",
    }
    restored = migrate_snapshot(identity_species)
    assert restored["components"]["EggComponent"]["entity_7"]["species_name"] == "raptor"

    live_command_target = _world_database_v3_snapshot()
    live_command_target["components"]["CommandComponent"]["entity_4"]["target_id"] = "entity_3"
    assert migrate_snapshot(live_command_target)["relationships"]["CommandTarget"]

    blank_command_relationships = _world_database_v3_snapshot()
    blank_command_relationships["components"]["CommandComponent"]["entity_4"].update(
        {"commanded_by_id": "", "target_id": ""}
    )
    migrated_blank_command = migrate_snapshot(blank_command_relationships)
    assert migrated_blank_command["relationships"]["CommandedBy"] == {}
    assert migrated_blank_command["relationships"]["CommandTarget"] == {}

    blank_route = _world_database_v3_snapshot()
    blank_route["components"]["NavigationRouteComponent"]["entity_4"]["destination_id"] = ""
    with pytest.raises(WorldMigrationError, match="invalid destination"):
        migrate_snapshot(blank_route)

    semantic_route = _world_database_v3_snapshot()
    semantic_route["components"]["NavigationRouteComponent"]["entity_4"]["destination_id"] = (
        "Outer Rim"
    )
    route = migrate_snapshot(semantic_route)["components"]["NavigationRouteComponent"]["entity_4"]
    assert route["destination_key"] == "Outer Rim"


def test_schema_v4_world_database_validation_rejects_malformed_edges_and_endpoints():
    def check_error(mutate, message):
        value = migrate_snapshot(_world_database_v3_snapshot())
        mutate(value)
        with pytest.raises(WorldMigrationError, match=message):
            migrate_snapshot(value)

    def missing_source(value):
        records = value["relationships"]["TamedBy"].pop("entity_4")
        value["relationships"]["TamedBy"]["entity_999"] = records

    def duplicate_target(value):
        value["relationships"]["TamedBy"]["entity_4"].append({"target": "entity_1", "edge": {}})

    def second_target(value):
        value["relationships"]["TamedBy"]["entity_4"].append({"target": "entity_2", "edge": {}})

    cases = (
        (
            lambda value: value["components"]["TamingComponent"].update({"entity_4": []}),
            "TamingComponent fields.*mapping",
        ),
        (
            lambda value: value["components"]["TamingComponent"]["entity_4"].update(
                {"tamer_id": "entity_1"}
            ),
            "legacy TamingComponent.tamer_id",
        ),
        (missing_source, "TamedBy source.*does not exist"),
        (
            lambda value: value["relationships"]["TamedBy"].update(
                {"entity_3": [{"target": "entity_1", "edge": {}}]}
            ),
            "TamedBy source.*lacks TamingComponent",
        ),
        (
            lambda value: value["relationships"]["BuiltBy"].update(
                {"entity_2": [{"target": "entity_1", "edge": {}}]}
            ),
            "BuiltBy source.*lacks EnclosureComponent",
        ),
        (
            lambda value: value["relationships"].update({"BuiltBy": []}),
            "persisted type 'BuiltBy'.*mapping",
        ),
        (
            lambda value: value["relationships"]["TamedBy"].update({"entity_4": {}}),
            "TamedBy edges.*list",
        ),
        (
            lambda value: value["relationships"]["TamedBy"].update(
                {"entity_4": [{"target": "entity_1", "edge": []}]}
            ),
            "TamedBy edge.*mapping",
        ),
        (
            lambda value: value["relationships"]["TamedBy"]["entity_4"][0].update(
                {"target": "entity_999"}
            ),
            "missing target",
        ),
        (duplicate_target, "duplicate TamedBy edge"),
        (
            lambda value: value["relationships"]["TamedBy"]["entity_4"][0].update(
                {"target": "entity_3"}
            ),
            "TamedBy target.*not a character",
        ),
        (
            lambda value: value["relationships"]["TrackedAt"]["entity_4"][0].update(
                {"target": "entity_1"}
            ),
            "TrackedAt target.*not a room",
        ),
        (
            lambda value: value["relationships"]["RecordedByDevice"]["entity_8"][0].update(
                {"target": "entity_1"}
            ),
            "RecordedByDevice target.*wrong type",
        ),
        (
            lambda value: value["components"]["EggComponent"]["entity_7"].update(
                {"hatched": False}
            ),
            "HatchedFromEgg target.*not hatched",
        ),
        (
            lambda value: value["relationships"]["ProductFromCreature"]["entity_8"][0].update(
                {"target": "entity_3"}
            ),
            "ProductFromCreature target.*not a creature",
        ),
        (
            lambda value: value["relationships"]["CompanionOf"]["entity_4"][0].update({"edge": {}}),
            "CompanionOf edge.*requires role",
        ),
        (
            lambda value: value["relationships"]["TamedBy"]["entity_4"][0].update(
                {"edge": {"unexpected": True}}
            ),
            "TamedBy edge.*must not contain properties",
        ),
        (second_target, "multiple TamedBy targets"),
        (
            lambda value: value["relationships"]["EvidenceSubject"].pop("entity_8"),
            "requires one EvidenceSubject",
        ),
        (
            lambda value: value["relationships"]["RecordedByDevice"].pop("entity_8"),
            "requires one RecordedByDevice",
        ),
        (
            lambda value: value["relationships"]["OrbitsBody"].pop("entity_4"),
            "requires one OrbitsBody",
        ),
        (
            lambda value: value["relationships"]["NavigatesTo"].pop("entity_4"),
            "invalid destination",
        ),
    )
    for mutate, message in cases:
        check_error(mutate, message)


def test_schema_v4_drops_empty_relationship_buckets_without_mutating_source():
    source = migrate_snapshot(_world_database_v3_snapshot())
    source["relationships"]["BuiltBy"]["entity_2"] = []

    migrated = migrate_snapshot(source)

    assert source["relationships"]["BuiltBy"]["entity_2"] == []
    assert "entity_2" not in migrated["relationships"]["BuiltBy"]
    assert migrated["relationships"]["BuiltBy"]["entity_3"] == [
        {"target": "entity_1", "edge": {}}
    ]


def test_lifesim_v2_json_and_yaml_migrations_are_identical():
    import yaml

    source = _lifesim_v2_snapshot()
    from_json = migrate_snapshot(json.loads(json.dumps(source)))
    from_yaml = migrate_snapshot(yaml.safe_load(yaml.safe_dump(source)))

    assert from_json == from_yaml


@pytest.mark.parametrize(
    ("component", "field", "value"),
    [
        ("HomeComponent", "owner_id", "entity_999"),
        ("RoomClaimComponent", "claimed_by_id", "entity_999"),
        ("PregnancyComponent", "co_parent_ids", ["entity_999"]),
    ],
)
def test_lifesim_v2_migration_rejects_missing_live_targets(component, field, value):
    source = _lifesim_v2_snapshot()
    owner = "entity_1" if component == "PregnancyComponent" else "entity_3"
    source["components"][component][owner][field] = value

    with pytest.raises(WorldMigrationError, match=rf"{component}\.{field}.*entity_999"):
        migrate_snapshot(source)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["components"].update({"HomeComponent": []}),
            "HomeComponent.*mapping",
        ),
        (
            lambda value: value["components"]["HomeComponent"].update({"entity_3": []}),
            "HomeComponent fields.*mapping",
        ),
        (
            lambda value: value["entities"].pop("entity_3"),
            "HomeComponent.entity.*missing entity 'entity_3'",
        ),
        (
            lambda value: (
                value["components"].pop("HomeComponent"),
                value["components"]["RoomComponent"].pop("entity_3"),
            ),
            "RoomClaimComponent.entity.*does not have RoomComponent",
        ),
        (
            lambda value: value["components"]["HomeComponent"]["entity_3"].update(
                {"owner_id": "entity_3"}
            ),
            "HomeComponent.owner_id.*without CharacterComponent",
        ),
        (
            lambda value: value["components"].update({"RoomClaimComponent": []}),
            "RoomClaimComponent.*mapping",
        ),
        (
            lambda value: value["components"]["RoomClaimComponent"].update({"entity_3": []}),
            "RoomClaimComponent fields.*mapping",
        ),
        (
            lambda value: value["components"]["RoomComponent"].pop("entity_3"),
            "HomeComponent.entity.*does not have RoomComponent",
        ),
        (
            lambda value: value["components"]["RoomClaimComponent"]["entity_3"].update(
                {"claimed_by_id": "entity_3"}
            ),
            "RoomClaimComponent.claimed_by_id.*without CharacterComponent",
        ),
        (
            lambda value: value["components"]["RoomClaimComponent"]["entity_3"].update(
                {"claimed_at_epoch": True}
            ),
            "claimed_at_epoch.*integer",
        ),
        (
            lambda value: value["components"]["PregnancyComponent"].update({"entity_1": []}),
            "PregnancyComponent fields.*mapping",
        ),
        (
            lambda value: value["components"]["PregnancyComponent"]["entity_1"].update(
                {"co_parent_ids": "entity_2"}
            ),
            "co_parent_ids.*sequence",
        ),
        (
            lambda value: value["components"]["PregnancyComponent"]["entity_1"].update(
                {"co_parent_ids": ["entity_3"]}
            ),
            "co_parent_ids.*without CharacterComponent",
        ),
    ],
)
def test_lifesim_v2_migration_rejects_malformed_relationship_fields(mutate, message):
    source = _lifesim_v2_snapshot()
    mutate(source)

    with pytest.raises(WorldMigrationError, match=message):
        migrate_snapshot(source)


def test_schema_v3_rejects_duplicate_cardinality_and_pregnancy_endpoint_violations():
    duplicate = migrate_snapshot(_lifesim_v2_snapshot())
    duplicate["relationships"]["OwnsHome"]["entity_2"] = [
        {"target": "entity_3", "edge": {"household_id": None}}
    ]
    with pytest.raises(WorldMigrationError, match="multiple incoming OwnsHome"):
        migrate_snapshot(duplicate)

    invalid_pregnancy = migrate_snapshot(_lifesim_v2_snapshot())
    invalid_pregnancy["components"]["PregnancyComponent"].pop("entity_1")
    with pytest.raises(WorldMigrationError, match="does not have PregnancyComponent"):
        migrate_snapshot(invalid_pregnancy)

    duplicate_edge = migrate_snapshot(_lifesim_v2_snapshot())
    duplicate_edge["relationships"]["ClaimsRoom"]["entity_1"] *= 2
    with pytest.raises(WorldMigrationError, match="duplicate ClaimsRoom"):
        migrate_snapshot(duplicate_edge)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["components"].update({"CharacterComponent": []}),
            "CharacterComponent.*mapping",
        ),
        (
            lambda value: value["components"]["PregnancyComponent"].update({"entity_1": []}),
            "PregnancyComponent fields.*mapping",
        ),
        (
            lambda value: value["components"]["PregnancyComponent"]["entity_1"].update(
                {"co_parent_ids": []}
            ),
            "legacy PregnancyComponent.co_parent_ids",
        ),
        (
            lambda value: value["components"].update({"HomeComponent": {"entity_3": {}}}),
            "legacy HomeComponent",
        ),
        (
            lambda value: value["components"].update({"RoomClaimComponent": {"entity_3": {}}}),
            "legacy RoomClaimComponent",
        ),
        (
            lambda value: value["relationships"]["OwnsHome"].update(
                {"entity_999": [{"target": "entity_3", "edge": {}}]}
            ),
            "source 'entity_999'.*does not exist",
        ),
        (
            lambda value: value["relationships"]["OwnsHome"].update({"entity_1": {}}),
            "OwnsHome edges.*must be a list",
        ),
        (
            lambda value: value["relationships"]["OwnsHome"].update({"entity_1": ["bad"]}),
            "OwnsHome edge.*must be a mapping",
        ),
        (
            lambda value: value["relationships"]["OwnsHome"].update(
                {"entity_1": [{"target": "entity_3", "edge": []}]}
            ),
            "OwnsHome edge.*must be a mapping",
        ),
        (
            lambda value: value["relationships"]["OwnsHome"].update(
                {"entity_1": [{"target": "entity_999", "edge": {}}]}
            ),
            "missing target 'entity_999'",
        ),
        (
            lambda value: value["relationships"]["OwnsHome"].update(
                {"entity_3": [{"target": "entity_3", "edge": {}}]}
            ),
            "source 'entity_3'.*not a character",
        ),
        (
            lambda value: value["relationships"]["OwnsHome"].update(
                {"entity_1": [{"target": "entity_2", "edge": {}}]}
            ),
            "target 'entity_2'.*not a room",
        ),
        (
            lambda value: value["relationships"]["OwnsHome"]["entity_1"][0]["edge"].update(
                {"household_id": 7}
            ),
            "OwnsHome.household_id.*string or null",
        ),
        (
            lambda value: value["relationships"]["ClaimsRoom"]["entity_1"][0]["edge"].update(
                {"claimed_at_epoch": True}
            ),
            "ClaimsRoom.claimed_at_epoch.*integer",
        ),
        (
            lambda value: value["relationships"]["PregnancyCoParent"]["entity_1"][0].update(
                {"target": "entity_3"}
            ),
            "PregnancyCoParent target 'entity_3'.*not a character",
        ),
        (
            lambda value: value["relationships"]["PregnancyCoParent"]["entity_1"][0]["edge"].update(
                {"role": "parent"}
            ),
            "PregnancyCoParent edge.*must not contain properties",
        ),
    ],
)
def test_schema_v3_rejects_malformed_lifesim_relationship_records(mutate, message):
    source = migrate_snapshot(_lifesim_v2_snapshot())
    mutate(source)

    with pytest.raises(WorldMigrationError, match=message):
        migrate_snapshot(source)


def test_schema_v1_dragon_stealth_name_migrates_without_mutating_source():
    source = _schema_v1_generated_quest_snapshot()
    source["components"]["StealthComponent"] = {"entity_2": {"sneaking": True, "since_epoch": 7}}

    migrated = migrate_snapshot(source)

    assert "StealthComponent" in source["components"]
    assert "StealthComponent" not in migrated["components"]
    assert migrated["components"]["SneakingComponent"] == {
        "entity_2": {"sneaking": True, "since_epoch": 7}
    }

    ambiguous = _schema_v1_generated_quest_snapshot()
    ambiguous["components"]["StealthComponent"] = {"entity_2": {"sneaking": True, "since_epoch": 7}}
    ambiguous["components"]["SneakingComponent"] = {
        "entity_3": {"sneaking": False, "since_epoch": 0}
    }
    with pytest.raises(WorldMigrationError, match="both StealthComponent and SneakingComponent"):
        migrate_snapshot(ambiguous)


def test_schema_v1_cure_quest_hook_name_migrates_to_affliction_request():
    source = _schema_v1_generated_quest_snapshot()
    source["components"]["CureQuestHookComponent"] = {
        "entity_2": {"affliction_type": "moon-form", "quest_id": None}
    }

    migrated = migrate_snapshot(source)

    assert "CureQuestHookComponent" in source["components"]
    assert "CureQuestHookComponent" not in migrated["components"]
    assert migrated["components"]["CureRequestComponent"] == {
        "entity_2": {"affliction_type": "moon-form", "quest_id": None}
    }

    source["components"]["CureRequestComponent"] = {}
    with pytest.raises(WorldMigrationError, match="both CureQuestHookComponent"):
        migrate_snapshot(source)


def test_schema_v1_potion_recipe_ingredients_migrate_to_ordered_edges():
    source = _schema_v1_generated_quest_snapshot()
    source["entities"]["entity_4"] = {"prefab": "entity", "created_epoch": 0}
    source["components"]["PotionRecipeComponent"] = {
        "entity_2": {
            "name": "moon tonic",
            "potion_name": "Moon Tonic",
            "ingredient_ids": ["entity_4"],
        }
    }

    migrated = migrate_snapshot(source)

    assert source["components"]["PotionRecipeComponent"]["entity_2"]["ingredient_ids"] == [
        "entity_4"
    ]
    assert "ingredient_ids" not in migrated["components"]["PotionRecipeComponent"]["entity_2"]
    assert migrated["relationships"]["DependsOnIngredient"]["entity_2"] == [
        {"target": "entity_4", "edge": {"order": 0}}
    ]

    missing = _schema_v1_generated_quest_snapshot()
    missing["components"]["PotionRecipeComponent"] = {
        "entity_2": {"name": "bad", "potion_name": "Bad", "ingredient_ids": ["missing"]}
    }
    with pytest.raises(
        WorldMigrationError,
        match=r"entity 'entity_2'.*PotionRecipeComponent\.ingredient_ids.*'missing'",
    ):
        migrate_snapshot(missing)

    malformed = _schema_v1_generated_quest_snapshot()
    malformed["components"]["PotionRecipeComponent"] = {
        "entity_2": {"name": "bad", "potion_name": "Bad", "ingredient_ids": 7}
    }
    with pytest.raises(WorldMigrationError, match="ingredient_ids.*must be a sequence"):
        migrate_snapshot(malformed)


def test_schema_v1_egg_parents_migrate_to_ordered_edges():
    source = _schema_v1_generated_quest_snapshot()
    source["components"]["EggComponent"] = {
        "entity_1": {
            "species_name": "raptor",
            "laid_at_epoch": 3,
            "parent_ids": ["entity_2"],
        }
    }

    migrated = migrate_snapshot(source)

    assert source["components"]["EggComponent"]["entity_1"]["parent_ids"] == ["entity_2"]
    assert "parent_ids" not in migrated["components"]["EggComponent"]["entity_1"]
    assert migrated["relationships"]["DescendsFromParent"]["entity_1"] == [
        {"target": "entity_2", "edge": {"order": 0}}
    ]

    missing = _schema_v1_generated_quest_snapshot()
    missing["components"]["EggComponent"] = {
        "entity_1": {"species_name": "raptor", "laid_at_epoch": 3, "parent_ids": ["missing"]}
    }
    with pytest.raises(WorldMigrationError, match=r"EggComponent\.parent_ids.*'missing'"):
        migrate_snapshot(missing)

    malformed = _schema_v1_generated_quest_snapshot()
    malformed["components"]["EggComponent"] = {
        "entity_1": {"species_name": "raptor", "laid_at_epoch": 3, "parent_ids": 7}
    }
    with pytest.raises(WorldMigrationError, match="parent_ids.*must be a sequence"):
        migrate_snapshot(malformed)


@pytest.mark.parametrize("suffix", ["json", "yaml"])
def test_schema_v1_relationship_fixtures_load_and_resave_as_v5(tmp_path, suffix):
    import yaml

    source = Path(__file__).parent / "fixtures" / "migrations" / f"relationships-v1.{suffix}"
    before = source.read_text()
    raw = json.loads(before) if suffix == "json" else yaml.safe_load(before)

    migrated = migrate_snapshot(raw)

    assert source.read_text() == before
    assert migrated["bunnyland"]["schema_version"] == 5
    expected = {
        "AllowedIn": ("entity_1", "entity_2", {}),
        "MemberOfCaravan": ("entity_1", "entity_3", {}),
        "StoredIn": ("entity_4", "entity_3", {}),
        "HasAccessToService": ("entity_1", "entity_5", {}),
        "MemberOfFestival": ("entity_1", "entity_3", {}),
        "MemberOfAwayTeam": ("entity_1", "entity_3", {}),
        "DependsOnIngredient": ("entity_5", "entity_4", {"order": 0}),
        "DescendsFromParent": ("entity_6", "entity_1", {"order": 0}),
        "RumorHeardBy": ("entity_4", "entity_1", {}),
        "OriginatesFromSource": ("entity_4", "entity_2", {}),
        "RefersToSubject": ("entity_4", "entity_6", {}),
        "HasStandingWithFaction": ("entity_1", "entity_2", {"score": 5}),
        "HasStandingWithInstitution": ("entity_1", "entity_3", {"score": 3}),
        "HasStandingInRegion": ("entity_1", "entity_7", {"score": 2}),
        "HasLegalStandingInRegion": ("entity_1", "entity_7", {"score": -4}),
        "WantedByFaction": ("entity_1", "entity_2", {"amount": 10}),
        "GuardsForFaction": ("entity_6", "entity_2", {"bribe_amount": 7}),
        "JailedByFaction": (
            "entity_1",
            "entity_2",
            {"release_epoch": 20, "reason": "theft"},
        ),
        "TravelingToDestination": (
            "entity_1",
            "entity_2",
            {},
        ),
        "AnchoredToRoom": ("entity_1", "entity_2", {}),
        "OpensIntoRoom": ("entity_4", "entity_2", {}),
        "EnteredThroughRoom": ("entity_3", "entity_7", {}),
    }
    for edge_name, (source_id, target_id, edge) in expected.items():
        record = migrated["relationships"][edge_name][source_id][0]
        assert record == {"target": target_id, "edge": edge}
    assert migrated["components"]["TravelingComponent"]["entity_1"] == {
        "started_at_epoch": 2,
        "arrive_at_epoch": 8,
        "mode": "cart",
        "route_label": "road",
    }

    actor, meta = load_world(source, registry=PluginRegistry(bunnyland_plugins()))
    destination = tmp_path / f"relationships-v4.{suffix}"
    save_world(actor, destination, meta=meta)
    saved = (
        json.loads(destination.read_text())
        if suffix == "json"
        else yaml.safe_load(destination.read_text())
    )
    metadata_key = "bunnyland" if suffix == "json" else "__bunnyland__"
    assert saved[metadata_key]["schema_version"] == 5


@pytest.mark.parametrize("suffix", ["json", "yaml"])
def test_schema_v1_3d_decoration_roles_migrate_to_namespaced_values(suffix):
    import yaml

    source = Path(__file__).parent / "fixtures" / "migrations" / f"decorations-3d-v1.{suffix}"
    before = source.read_text()
    raw = json.loads(before) if suffix == "json" else yaml.safe_load(before)

    migrated = migrate_snapshot(raw)

    assert source.read_text() == before
    assert migrated["components"]["DecorationSource3DComponent"]["entity_2"]["role"] == (
        "bunnyland.3d/particles"
    )
    edge = migrated["relationships"]["HasDecoration3D"]["entity_1"][0]
    assert edge["edge"]["role"] == "bunnyland.3d/particles"


def test_schema_v1_3d_decoration_roles_preserve_namespaced_values():
    source = _schema_v1_generated_quest_snapshot()
    source["components"]["DecorationSource3DComponent"] = {
        "entity_1": {"role": "vendor.pack/trees"}
    }

    migrated = migrate_snapshot(source)

    assert migrated["components"]["DecorationSource3DComponent"]["entity_1"]["role"] == (
        "vendor.pack/trees"
    )


@pytest.mark.parametrize(
    "components, relationships, message",
    [
        ({"DecorationSource3DComponent": {"entity_1": []}}, {}, "must be a mapping"),
        (
            {"DecorationSource3DComponent": {"entity_1": {"role": "unknown"}}},
            {},
            "unknown decoration role",
        ),
        ({}, {"HasDecoration3D": {"entity_1": {}}}, "must be a list"),
        ({}, {"HasDecoration3D": {"entity_1": [[]]}}, "must be a mapping"),
        (
            {},
            {"HasDecoration3D": {"entity_1": [{"edge": {"role": "unknown"}}]}},
            "unknown decoration role",
        ),
    ],
)
def test_schema_v1_3d_decoration_roles_reject_malformed_values(components, relationships, message):
    source = _schema_v1_generated_quest_snapshot()
    source["components"].update(components)
    source["relationships"].update(relationships)

    with pytest.raises(WorldMigrationError, match=message):
        migrate_snapshot(source)


@pytest.mark.parametrize(
    ("component", "field"),
    [
        ("AllowedAreaComponent", "room_ids"),
        ("CaravanComponent", "member_ids"),
        ("SafeStorageComponent", "item_ids"),
        ("ServiceAccessComponent", "service_ids"),
        ("FestivalComponent", "joined_character_ids"),
        ("AwayTeamComponent", "member_ids"),
        ("RumorComponent", "heard_by"),
    ],
)
def test_schema_v1_relationship_sequences_reject_malformed_and_missing_targets(component, field):
    malformed = _schema_v1_generated_quest_snapshot()
    malformed["components"][component] = {"entity_1": {field: 7}}
    with pytest.raises(WorldMigrationError, match=rf"{field}.*must be a sequence"):
        migrate_snapshot(malformed)

    missing = _schema_v1_generated_quest_snapshot()
    missing["components"][component] = {"entity_1": {field: ["missing"]}}
    with pytest.raises(
        WorldMigrationError,
        match=rf"entity 'entity_1'.*{component}\.{field}.*'missing'",
    ):
        migrate_snapshot(missing)


@pytest.mark.parametrize("component", ["AllowedAreaComponent", "ServiceAccessComponent"])
def test_schema_v1_empty_relationship_wrappers_must_be_mappings(component):
    source = _schema_v1_generated_quest_snapshot()
    source["components"][component] = []
    with pytest.raises(WorldMigrationError, match=rf"type '{component}'.*mapping"):
        migrate_snapshot(source)


@pytest.mark.parametrize(
    ("component", "field"),
    [("RumorSourceComponent", "source_id"), ("RumorTargetComponent", "target_id")],
)
def test_schema_v1_rumor_relationship_wrappers_reject_missing_targets(component, field):
    source = _schema_v1_generated_quest_snapshot()
    source["components"][component] = {"entity_1": {field: "missing"}}
    with pytest.raises(
        WorldMigrationError,
        match=rf"entity 'entity_1'.*{component}\.{field}.*'missing'",
    ):
        migrate_snapshot(source)


@pytest.mark.parametrize("component", ["RumorSourceComponent", "RumorTargetComponent"])
def test_schema_v1_rumor_relationship_wrappers_must_be_mappings(component):
    source = _schema_v1_generated_quest_snapshot()
    source["components"][component] = []
    with pytest.raises(WorldMigrationError, match=rf"type '{component}'.*mapping"):
        migrate_snapshot(source)


def test_schema_v1_rumor_without_source_drops_empty_wrapper():
    source = _schema_v1_generated_quest_snapshot()
    source["components"]["RumorSourceComponent"] = {"entity_1": {"source_id": None}}

    migrated = migrate_snapshot(source)

    assert "RumorSourceComponent" not in migrated["components"]
    assert "OriginatesFromSource" not in migrated["relationships"]


@pytest.mark.parametrize(
    ("component", "field", "target_component"),
    [
        ("FactionReputationComponent", "scores", "FactionComponent"),
        ("InstitutionReputationComponent", "scores", "InstitutionComponent"),
        ("RegionalReputationComponent", "scores", "LawRegionComponent"),
        ("LegalReputationComponent", "scores", "LawRegionComponent"),
        ("WantedComponent", "amounts", "FactionComponent"),
    ],
)
def test_schema_v1_standing_maps_reject_invalid_targets_and_non_integer_values(
    component, field, target_component
):
    missing = _schema_v1_generated_quest_snapshot()
    missing["components"][component] = {"entity_1": {field: {"missing": 1}}}
    with pytest.raises(
        WorldMigrationError,
        match=rf"entity 'entity_1'.*{component}\.{field}.*'missing'",
    ):
        migrate_snapshot(missing)

    malformed = _schema_v1_generated_quest_snapshot()
    malformed["components"][component] = {"entity_1": {field: {"entity_2": "high"}}}
    with pytest.raises(WorldMigrationError, match=rf"{component}\.{field}.*integer values"):
        migrate_snapshot(malformed)

    wrong_type = _schema_v1_generated_quest_snapshot()
    wrong_type["components"][component] = {"entity_1": {field: {"entity_2": 1}}}
    with pytest.raises(WorldMigrationError, match=rf"entity_2.*without {target_component}"):
        migrate_snapshot(wrong_type)


def test_schema_v1_standing_maps_reject_ambiguous_labels_and_malformed_records():
    ambiguous = _schema_v1_generated_quest_snapshot()
    ambiguous["components"]["FactionComponent"] = {
        "entity_2": {"name": "Wardens"},
        "entity_3": {"name": "Wardens"},
    }
    ambiguous["components"]["FactionReputationComponent"] = {"entity_1": {"scores": {"Wardens": 1}}}
    with pytest.raises(WorldMigrationError, match="ambiguous FactionComponent.name"):
        migrate_snapshot(ambiguous)

    malformed_fields = _schema_v1_generated_quest_snapshot()
    malformed_fields["components"]["FactionReputationComponent"] = {"entity_1": []}
    with pytest.raises(WorldMigrationError, match="fields for 'entity_1'.*mapping"):
        migrate_snapshot(malformed_fields)

    malformed_scores = _schema_v1_generated_quest_snapshot()
    malformed_scores["components"]["FactionReputationComponent"] = {"entity_1": {"scores": []}}
    with pytest.raises(WorldMigrationError, match=r"FactionReputationComponent\.scores.*mapping"):
        migrate_snapshot(malformed_scores)


@pytest.mark.parametrize(
    "component",
    [
        "FactionReputationComponent",
        "InstitutionReputationComponent",
        "RegionalReputationComponent",
        "LegalReputationComponent",
        "WantedComponent",
    ],
)
def test_schema_v1_standing_component_tables_must_be_mappings(component):
    source = _schema_v1_generated_quest_snapshot()
    source["components"][component] = []

    with pytest.raises(WorldMigrationError, match=rf"persisted type '{component}'.*mapping"):
        migrate_snapshot(source)


@pytest.mark.parametrize(
    ("component", "value_field"),
    [("GuardComponent", "bribe_amount"), ("JailComponent", "release_epoch")],
)
def test_schema_v1_faction_role_edges_reject_malformed_records(component, value_field):
    malformed_table = _schema_v1_generated_quest_snapshot()
    malformed_table["components"][component] = []
    with pytest.raises(WorldMigrationError, match=rf"persisted type '{component}'.*mapping"):
        migrate_snapshot(malformed_table)

    malformed_fields = _schema_v1_generated_quest_snapshot()
    malformed_fields["components"][component] = {"entity_1": []}
    with pytest.raises(WorldMigrationError, match=rf"{component} fields.*mapping"):
        migrate_snapshot(malformed_fields)

    missing_target = _schema_v1_generated_quest_snapshot()
    missing_target["components"][component] = {
        "entity_1": {"faction_id": "missing", value_field: 1}
    }
    with pytest.raises(WorldMigrationError, match=rf"{component}\.faction_id.*'missing'"):
        migrate_snapshot(missing_target)

    wrong_type = _schema_v1_generated_quest_snapshot()
    wrong_type["components"][component] = {"entity_1": {"faction_id": "entity_2", value_field: 1}}
    with pytest.raises(WorldMigrationError, match="entity_2.*without FactionComponent"):
        migrate_snapshot(wrong_type)

    malformed_value = _schema_v1_generated_quest_snapshot()
    malformed_value["components"]["FactionComponent"] = {"entity_2": {"name": "Wardens"}}
    malformed_value["components"][component] = {
        "entity_1": {"faction_id": "entity_2", value_field: "many"}
    }
    with pytest.raises(WorldMigrationError, match=rf"{component}\.{value_field}.*integer"):
        migrate_snapshot(malformed_value)


def test_schema_v1_faction_role_edges_apply_default_properties():
    source = _schema_v1_generated_quest_snapshot()
    source["components"]["FactionComponent"] = {"entity_2": {"name": "Wardens"}}
    source["components"]["GuardComponent"] = {"entity_3": {"faction_id": "Wardens"}}
    source["components"]["JailComponent"] = {
        "entity_1": {"faction_id": "Wardens", "release_epoch": 12}
    }

    migrated = migrate_snapshot(source)

    assert migrated["relationships"]["GuardsForFaction"]["entity_3"] == [
        {"target": "entity_2", "edge": {"bribe_amount": 10}}
    ]
    assert migrated["relationships"]["JailedByFaction"]["entity_1"] == [
        {"target": "entity_2", "edge": {"release_epoch": 12, "reason": "sentence"}}
    ]


@pytest.mark.parametrize("component", ["TravelPlanComponent", "RecallAnchorComponent"])
def test_schema_v1_room_reference_wrapper_tables_must_be_mappings(component):
    source = _schema_v1_generated_quest_snapshot()
    source["components"][component] = []
    with pytest.raises(WorldMigrationError, match=rf"persisted type '{component}'.*mapping"):
        migrate_snapshot(source)


@pytest.mark.parametrize(
    ("component", "field"),
    [
        ("TravelPlanComponent", "destination_id"),
        ("RecallAnchorComponent", "room_id"),
        ("SecretDoorComponent", "target_room_id"),
        ("DungeonComponent", "entry_room_id"),
    ],
)
def test_schema_v1_room_reference_edges_reject_missing_targets(component, field):
    source = _schema_v1_generated_quest_snapshot()
    fields = {field: "missing"}
    if component == "TravelPlanComponent":
        fields.update(started_at_epoch=0, arrive_at_epoch=1)
    source["components"][component] = {"entity_1": fields}
    with pytest.raises(WorldMigrationError, match=rf"{component}\.{field}.*'missing'"):
        migrate_snapshot(source)


@pytest.mark.parametrize(
    "component",
    [
        "TravelPlanComponent",
        "RecallAnchorComponent",
        "SecretDoorComponent",
        "DungeonComponent",
    ],
)
def test_schema_v1_room_reference_records_must_be_mappings(component):
    source = _schema_v1_generated_quest_snapshot()
    source["components"][component] = {"entity_1": []}
    with pytest.raises(WorldMigrationError, match=rf"{component} fields.*mapping"):
        migrate_snapshot(source)


def test_schema_v1_travel_plan_rejects_wrong_target_type_and_timestamps():
    wrong_target = _schema_v1_generated_quest_snapshot()
    wrong_target["components"]["TravelPlanComponent"] = {
        "entity_1": {
            "destination_id": "entity_2",
            "started_at_epoch": 0,
            "arrive_at_epoch": 1,
        }
    }
    with pytest.raises(WorldMigrationError, match="entity_2.*without TravelHubComponent"):
        migrate_snapshot(wrong_target)

    for field in ("started_at_epoch", "arrive_at_epoch"):
        malformed = _schema_v1_generated_quest_snapshot()
        malformed["components"]["TravelHubComponent"] = {"entity_2": {"name": "Market"}}
        malformed["components"]["TravelPlanComponent"] = {
            "entity_1": {
                "destination_id": "entity_2",
                "started_at_epoch": 0,
                "arrive_at_epoch": 1,
                field: "soon",
            }
        }
        with pytest.raises(WorldMigrationError, match=rf"TravelPlanComponent\.{field}.*integer"):
            migrate_snapshot(malformed)


@pytest.mark.parametrize(
    ("component", "field", "target_component"),
    [
        ("RecallAnchorComponent", "room_id", "RoomComponent"),
        ("SecretDoorComponent", "target_room_id", "RoomComponent"),
        ("DungeonComponent", "entry_room_id", "DungeonRoomComponent"),
    ],
)
def test_schema_v1_room_reference_edges_reject_wrong_target_types(
    component, field, target_component
):
    source = _schema_v1_generated_quest_snapshot()
    source["components"][component] = {"entity_1": {field: "entity_2"}}
    with pytest.raises(WorldMigrationError, match=rf"entity_2.*without {target_component}"):
        migrate_snapshot(source)


def test_schema_v1_dungeon_without_entry_keeps_external_dungeon_key_only():
    source = _schema_v1_generated_quest_snapshot()
    source["components"]["DungeonComponent"] = {
        "entity_1": {"dungeon_id": "stable-key", "entry_room_id": None}
    }

    migrated = migrate_snapshot(source)

    assert migrated["components"]["DungeonComponent"]["entity_1"] == {"dungeon_id": "stable-key"}
    assert "EnteredThroughRoom" not in migrated["relationships"]


def test_schema_v1_migration_handles_collisions_and_unaccepted_generated_quests():
    snapshot = _schema_v1_generated_quest_snapshot()
    snapshot["components"]["GeneratedQuestComponent"]["entity_1"].pop("accepted_by")
    snapshot["entities"]["quest_objective_1"] = {
        "prefab": "quest_objective",
        "created_epoch": 0,
    }

    migrated = migrate_snapshot(snapshot)

    assert migrated["relationships"]["QuestHasObjective"]["entity_1"][0]["target"] == (
        "quest_objective_2"
    )
    assert "QuestAcceptedBy" not in migrated["relationships"]


def test_schema_v1_migration_rejects_ambiguous_quest_state():
    both_families = _schema_v1_generated_quest_snapshot()
    both_families["components"]["QuestComponent"] = {
        "entity_1": {"quest_id": "other", "title": "Other"}
    }
    with pytest.raises(WorldMigrationError, match="both quest component families"):
        migrate_snapshot(both_families)

    invalid_participants = _schema_v1_generated_quest_snapshot()
    invalid_participants["components"].pop("GeneratedQuestComponent")
    invalid_participants["components"]["QuestComponent"] = {
        "entity_1": {
            "quest_id": "letter",
            "title": "Letter",
            "accepted_by": 7,
        }
    }
    with pytest.raises(WorldMigrationError, match="accepted_by.*sequence"):
        migrate_snapshot(invalid_participants)

    duplicate_stages = _schema_v1_generated_quest_snapshot()
    duplicate_stages["components"]["QuestStageComponent"] = {
        "entity_4": {"quest_id": "entity_1"},
        "entity_5": {"quest_id": "entity_1"},
    }
    with pytest.raises(WorldMigrationError, match="multiple lifecycle"):
        migrate_snapshot(duplicate_stages)

    conflicting_deadline = _schema_v1_generated_quest_snapshot()
    conflicting_deadline["components"]["QuestStateComponent"] = {"entity_1": {"due_at_epoch": 50}}
    with pytest.raises(WorldMigrationError, match="conflicting deadlines"):
        migrate_snapshot(conflicting_deadline)


def test_format_for_path_respects_explicit_format_and_suffixes(tmp_path):
    assert _format_for_path(tmp_path / "world.yml", None) == "yaml"
    assert _format_for_path(tmp_path / "world.yaml", None) == "yaml"
    assert _format_for_path(tmp_path / "world.json", None) == "json"
    assert _format_for_path(tmp_path / "world.yml", "json") == "json"
    assert _format_for_path(tmp_path / "world.json", "yaml") == "yaml"

    with pytest.raises(ValueError, match="unknown persistence format"):
        _format_for_path(tmp_path / "world.json", "toml")


async def test_save_reload_preserves_world(tmp_path):
    actor, result = await _build_and_play()
    replace_component(
        actor._clock_entity,
        WorldInfoComponent(
            title="Clover City",
            description="Mind the foxes after dark.",
            content_flags=frozenset({"adult:violence"}),
        ),
    )
    hazel = result.characters["hazel"]
    before_room = container_of(actor.world.get_entity(hazel))
    before_inventory = _inventory(actor, hazel)
    before_epoch = actor.epoch
    before_hunger = actor.world.get_entity(hazel).get_component(HungerComponent).meter.value

    path = tmp_path / "world.json"
    save_world(actor, path, meta=WorldMeta(seed="a quiet marsh", prompt="p", generator="oneshot"))
    actor2, meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))

    # Topology, containment, the clock, and nested value objects all survive.
    assert container_of(actor2.world.get_entity(hazel)) == before_room
    assert _inventory(actor2, hazel) == before_inventory
    assert "a scrap of paper" in before_inventory
    assert actor2.epoch == before_epoch
    assert actor2.world_info == WorldInfoComponent(
        title="Clover City",
        description="Mind the foxes after dark.",
        content_flags=frozenset({"adult:violence"}),
    )
    hunger = actor2.world.get_entity(hazel).get_component(HungerComponent)
    assert hunger.meter.value == before_hunger  # Meter is a nested value object

    # Provenance is restored.
    assert (meta.seed, meta.prompt, meta.generator) == ("a quiet marsh", "p", "oneshot")
    assert meta.saved_at_epoch == before_epoch


def test_shelter_moisture_and_wetness_round_trip_with_legacy_shelter_defaults(tmp_path):
    actor = WorldActor()
    apply_plugins(bunnyland_plugins(), actor)
    room = spawn_entity(
        actor.world,
        [
            RoomComponent(title="Damp shelter"),
            ShelterComponent(temperature_buffer=8.0, rain_protection=0.75, wind_protection=0.5),
            MoistureComponent(wetness=Meter(value=30.0), humidity=0.8),
        ],
    )
    character = spawn_entity(
        actor.world,
        [WetnessComponent(meter=Meter(value=45.0), last_updated_epoch=12)],
    )
    path = tmp_path / "environment.json"
    save_world(actor, path, meta=WorldMeta(seed="wet shelter"))

    loaded, _meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))
    assert loaded.world.get_entity(room.id).get_component(ShelterComponent) == (
        ShelterComponent(8.0, 0.75, 0.5)
    )
    assert loaded.world.get_entity(room.id).get_component(MoistureComponent) == (
        MoistureComponent(wetness=Meter(value=30.0), humidity=0.8)
    )
    assert loaded.world.get_entity(character.id).get_component(WetnessComponent) == (
        WetnessComponent(meter=Meter(value=45.0), last_updated_epoch=12)
    )

    snapshot = json.loads(path.read_text())
    shelter_data = snapshot["components"]["ShelterComponent"][str(room.id)]
    shelter_data.pop("rain_protection")
    shelter_data.pop("wind_protection")
    legacy_path = tmp_path / "legacy-environment.json"
    legacy_path.write_text(json.dumps(snapshot))
    legacy, _meta = load_world(legacy_path, registry=PluginRegistry(bunnyland_plugins()))

    assert legacy.world.get_entity(room.id).get_component(ShelterComponent) == (
        ShelterComponent(temperature_buffer=8.0)
    )


def test_recovery_state_round_trips_and_legacy_sleep_defaults_remain_compatible(tmp_path):
    actor = WorldActor()
    apply_plugins(bunnyland_plugins(), actor)
    resting = spawn_entity(
        actor.world,
        [RestingComponent(started_at_epoch=10, until_epoch=20.5, session_id="rest-1")],
    )
    sleeping = spawn_entity(
        actor.world,
        [SleepingComponent(started_at_epoch=12, until_epoch=30.5)],
    )
    path = tmp_path / "recovery.json"
    save_world(actor, path, meta=WorldMeta(seed="recovery"))

    loaded, _meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))
    assert loaded.world.get_entity(resting.id).get_component(RestingComponent) == (
        RestingComponent(started_at_epoch=10, until_epoch=20.5, session_id="rest-1")
    )
    assert loaded.world.get_entity(sleeping.id).get_component(SleepingComponent) == (
        SleepingComponent(started_at_epoch=12, until_epoch=30.5)
    )

    snapshot = json.loads(path.read_text())
    snapshot["components"]["SleepingComponent"][str(sleeping.id)].pop("until_epoch")
    legacy_path = tmp_path / "legacy-recovery.json"
    legacy_path.write_text(json.dumps(snapshot))
    legacy, _meta = load_world(
        legacy_path,
        registry=PluginRegistry(bunnyland_plugins()),
    )
    assert legacy.world.get_entity(sleeping.id).get_component(SleepingComponent).until_epoch is None


async def test_reloaded_world_keeps_playing(tmp_path):
    actor, result = await _build_and_play()
    hazel = result.characters["hazel"]
    path = tmp_path / "world.json"
    save_world(actor, path, meta=WorldMeta(seed="s"))

    actor2, _meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))
    # The controller edge (and its generation) survived, so dispatch can drive Hazel.
    agent = ScriptedAgent([ToolCall("move", {"direction": "north"})])
    dispatch = ControllerDispatch(actor2, PromptBuilder(actor2.world), agent)
    await GameLoop(actor2, dispatch).run(max_ticks=2)

    assert container_of(actor2.world.get_entity(hazel)) == result.rooms["tunnel"]


def test_llm_controller_assignment_survives_save_load(tmp_path):
    actor = WorldActor()
    apply_plugins(bunnyland_plugins(), actor)
    character = spawn_entity(actor.world, [CharacterComponent()])
    controller = spawn_entity(
        actor.world,
        [
            LLMControllerComponent(
                profile_name="default",
                model="deepseek-v4-flash",
                provider="ollama",
            )
        ],
    )
    generation = actor.assign_controller(character.id, controller.id)
    path = tmp_path / "llm-assignment.json"

    save_world(actor, path, meta=WorldMeta(seed="llm-assignment"))
    loaded, _meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))

    loaded_controller = loaded.world.get_entity(controller.id)
    assert loaded_controller.get_component(LLMControllerComponent) == (
        controller.get_component(LLMControllerComponent)
    )
    assert loaded.world.get_entity(character.id).get_relationships(ControlledBy) == [
        (ControlledBy(generation=generation, since_epoch=actor.epoch), controller.id)
    ]


async def test_offline_life_advances_reloaded_world_and_persists_changes(tmp_path):
    actor = WorldActor()
    apply_plugins(bunnyland_plugins(), actor)
    result = await instantiate(actor, await StubWorldBuilder().propose("a quiet marsh"))
    hazel = result.characters["hazel"]
    path = tmp_path / "world.json"
    save_world(actor, path, meta=WorldMeta(seed="offline"))

    loaded, meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))
    ticks = await advance_offline_life(loaded, 2 * 3600.0, max_ticks=2)
    save_world(loaded, path, meta=meta)
    reloaded, _meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))

    assert ticks == 2
    assert "a scrap of paper" in _inventory(reloaded, hazel)
    assert reloaded.epoch > actor.epoch


async def test_offline_life_is_bounded_by_max_ticks(tmp_path):
    actor = WorldActor()
    apply_plugins(bunnyland_plugins(), actor)
    await instantiate(actor, await StubWorldBuilder().propose("a quiet marsh"))

    ticks = await advance_offline_life(actor, 24 * 3600.0, step_seconds=3600.0, max_ticks=3)

    assert ticks == 3
    assert actor.epoch == 3 * 3600


async def test_offline_life_returns_zero_for_nonpositive_elapsed():
    actor = WorldActor()
    apply_plugins(bunnyland_plugins(), actor)

    # Non-positive elapsed time short-circuits before any tick runs.
    assert await advance_offline_life(actor, 0.0) == 0
    assert await advance_offline_life(actor, -5.0) == 0
    assert actor.epoch == 0


async def test_save_file_embeds_ecs_and_provenance(tmp_path):
    actor, _result = await _build_and_play()
    path = tmp_path / "world.json"
    save_world(actor, path, meta=WorldMeta(seed="marsh", prompt="p", generator="recursive"))

    data = json.loads(path.read_text())
    # One file holds both the Relics ECS sections and the bunnyland provenance.
    assert {"components", "entities", "relationships", "bunnyland"} <= set(data)
    assert data["bunnyland"]["seed"] == "marsh"
    assert data["bunnyland"]["generator"] == "recursive"


async def test_yaml_save_file_uses_compact_entity_records(tmp_path):
    actor, _result = await _build_and_play()
    path = tmp_path / "world.yaml"
    save_world(actor, path, meta=WorldMeta(seed="marsh", prompt="p", generator="recursive"))

    text = path.read_text()
    assert "__metadata__:" in text
    assert "__bunnyland__:" in text
    assert "IdentityComponent:" in text
    assert "Contains -> " in text
    assert '"seed": "marsh"' in text
    assert '"generator": "recursive"' in text
    assert '"name": "a scrap of paper"' in text
    assert '"kind": "paper"' in text


async def test_yaml_save_reload_preserves_world(tmp_path):
    actor, result = await _build_and_play()
    hazel = result.characters["hazel"]
    before_room = container_of(actor.world.get_entity(hazel))
    before_inventory = _inventory(actor, hazel)
    before_epoch = actor.epoch
    before_hunger = actor.world.get_entity(hazel).get_component(HungerComponent).meter.value

    path = tmp_path / "world.yaml"
    save_world(actor, path, meta=WorldMeta(seed="a quiet marsh", prompt="p", generator="oneshot"))
    actor2, meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))

    assert container_of(actor2.world.get_entity(hazel)) == before_room
    assert _inventory(actor2, hazel) == before_inventory
    assert actor2.epoch == before_epoch
    hunger = actor2.world.get_entity(hazel).get_component(HungerComponent)
    assert hunger.meter.value == before_hunger
    assert (meta.seed, meta.prompt, meta.generator) == ("a quiet marsh", "p", "oneshot")


def test_yaml_driver_defers_edge_resolution(tmp_path):
    path = tmp_path / "manual.yaml"
    path.write_text(
        """
__metadata__: {"version": "1.0", "epoch": 7}
__prefabs__: {"entity": {"components": {}}}
entity_1:
  IdentityComponent: {"name": "Source \\"quoted\\" ☃", "kind": "room", "tags": ["start"]}
  ExitTo -> entity_2: {
    "direction": "north",
    "label": "North",
    "locked": false,
    "hidden": false,
    "action_cost": 1
  }
entity_2:
  IdentityComponent: {"name": "Target", "kind": "room", "tags": []}
""".lstrip()
    )
    components, edges = type_registries(PluginRegistry(bunnyland_plugins()))
    world = World()
    YAMLPersistenceDriver().load(world, path, components, edges)

    source_id = EntityId.parse("entity_1")
    target_id = EntityId.parse("entity_2")
    source = world.get_entity(source_id)
    assert world.epoch == 7
    assert source.get_component(IdentityComponent).name == 'Source "quoted" ☃'
    assert source.get_relationships(ExitTo) == [
        (ExitTo(direction="north", label="North"), target_id)
    ]


def test_yaml_driver_save_serializes_world_prefabs_entities_and_relationships(tmp_path):
    driver = YAMLPersistenceDriver()
    world = World()
    world.register_edge_type(ExitTo)
    world.register_prefab("room", {IdentityComponent: IdentityComponent(name="Room", kind="room")})
    source = world.spawn("room", {IdentityComponent: IdentityComponent(name="Source", kind="room")})
    target = world.spawn("room", {IdentityComponent: IdentityComponent(name="Target", kind="room")})
    source.add_relationship(ExitTo(direction="north", label="North"), target.id)

    path = tmp_path / "world.yaml"
    driver.save(world, path, relic_name="saved-room")
    snapshot = driver.read_snapshot(path)

    assert snapshot["metadata"]["relic_name"] == "saved-room"
    assert snapshot["prefabs"]["room"]["components"]["IdentityComponent"]["name"] == "Room"
    assert snapshot["entities"][str(source.id)]["prefab"] == "room"
    assert snapshot["components"]["IdentityComponent"][str(source.id)]["name"] == "Source"
    assert snapshot["relationships"]["ExitTo"][str(source.id)] == [
        {
            "target": str(target.id),
            "edge": {
                "action_cost": 1,
                "direction": "north",
                "hidden": False,
                "label": "North",
                "locked": False,
            },
        }
    ]


def test_yaml_driver_save_omits_relic_name_and_temporary_components(tmp_path):
    from dataclasses import dataclass as _dataclass

    from relics import Component
    from relics.shared import temporary_component

    @temporary_component
    @_dataclass
    class ScratchComponent(Component):
        value: int = 0

    driver = YAMLPersistenceDriver()
    world = World()
    world.register_prefab("room", {IdentityComponent: IdentityComponent(name="Room", kind="room")})
    room = world.spawn("room", {IdentityComponent: IdentityComponent(name="Source", kind="room")})
    room.add_component(ScratchComponent(value=7))

    path = tmp_path / "world.yaml"
    driver.save(world, path)  # no relic_name -> metadata branch 99->102 skipped
    snapshot = driver.read_snapshot(path)

    assert "relic_name" not in snapshot["metadata"]
    # Temporary component skipped during serialization (line 114).
    assert "ScratchComponent" not in snapshot["components"]
    assert "IdentityComponent" in snapshot["components"]


def test_yaml_driver_dumps_plain_values_and_quoted_keys():
    driver = YAMLPersistenceDriver()

    text = driver.dumps_snapshot(
        {
            "metadata": None,
            "bunnyland": {
                "model": _YamlNestedModel(label="nested"),
                "data": _YamlNestedData(flavor=_YamlFlavor.SWEET, hidden="visible"),
                "items": {"tuple": ("a", _YamlFlavor.SWEET), "set": {"b"}},
                7: "numeric key",
            },
            "prefabs": {},
            "entities": {
                "plain_1": {},
                "room-with-dash_2": {},
            },
            "components": {
                "Odd Component": {
                    "room-with-dash_2": {"value": _YamlFlavor.SWEET},
                },
            },
        }
    )

    assert "__metadata__: {}" in text
    assert '"7": "numeric key"' in text
    assert '"flavor": "sweet"' in text
    assert '"label": "nested"' in text
    assert '"_private"' not in text
    assert "plain_1: {}" in text
    assert '"Odd Component": {"value": "sweet"}' in text


def test_yaml_driver_parses_compact_reserved_sections_and_empty_yaml(tmp_path):
    driver = YAMLPersistenceDriver()
    path = tmp_path / "empty.yaml"
    path.write_text("")

    assert driver.read_snapshot(path) == {
        "metadata": {},
        "bunnyland": {},
        "prefabs": {},
        "entities": {},
        "components": {},
        "relationships": {},
        "relics": [],
    }

    snapshot = driver.snapshot_from_document(
        {
            "__metadata__": {"epoch": 3},
            "__bunnyland__": {"seed": "marsh"},
            "__prefabs__": {"room": {"components": {}}},
            "room_1": {
                "IdentityComponent": {"name": "Room", "kind": "room"},
                "ExitTo -> room_2": {"direction": "east", "label": "East"},
            },
            "room_2": {},
        }
    )

    assert snapshot["metadata"] == {"epoch": 3}
    assert snapshot["bunnyland"] == {"seed": "marsh"}
    assert snapshot["prefabs"] == {"room": {"components": {}}}
    assert snapshot["components"]["IdentityComponent"]["room_1"]["name"] == "Room"
    assert snapshot["relationships"]["ExitTo"]["room_1"] == [
        {"target": "room_2", "edge": {"direction": "east", "label": "East"}}
    ]


def test_yaml_driver_rejects_malformed_snapshot_sections():
    driver = YAMLPersistenceDriver()

    with pytest.raises(ValueError, match="metadata must be a YAML mapping"):
        driver.dumps_snapshot({"metadata": []})

    with pytest.raises(ValueError, match="YAML document must be a YAML mapping"):
        driver.snapshot_from_document(["not", "a", "mapping"])

    with pytest.raises(ValueError, match="Contains edges for entity_1 must be a list"):
        driver.dumps_snapshot(
            {
                "entities": {"entity_1": {}},
                "relationships": {"Contains": {"entity_1": {"target": "entity_2"}}},
            }
        )

    with pytest.raises(ValueError, match="Contains edge must be a YAML mapping"):
        driver.dumps_snapshot(
            {
                "entities": {"entity_1": {}},
                "relationships": {"Contains": {"entity_1": ["not a mapping"]}},
            }
        )

    with pytest.raises(ValueError, match="entity_1.IdentityComponent must be a YAML mapping"):
        driver.snapshot_from_document({"entity_1": {"IdentityComponent": ["bad"]}})

    with pytest.raises(ValueError, match="__bunnyland__ must be a YAML mapping"):
        driver.snapshot_from_document({"__bunnyland__": []})


def test_yaml_driver_handles_relic_listing_errors_and_duplicates(tmp_path):
    driver = YAMLPersistenceDriver()
    relics_dir = tmp_path / "relics"
    world = World()

    assert driver.list_relics(relics_dir) == []

    driver.save_relic(world, "smoke", relics_dir)
    with pytest.raises(FileExistsError, match="Relic 'smoke' already exists"):
        driver.save_relic(world, "smoke", relics_dir)

    (relics_dir / "_hidden.yaml").write_text('__metadata__: {"relic_name": "hidden"}\n')
    (relics_dir / "broken.yaml").write_text("- not\n- a\n- mapping\n")

    relics = driver.list_relics(relics_dir)
    assert [relic.name for relic in relics] == ["smoke"]

    driver.load_relic(World(), "smoke", relics_dir)

    with pytest.raises(FileNotFoundError, match="Relic 'missing' not found"):
        driver.load_relic(world, "missing", relics_dir)


def test_yaml_driver_loads_yml_relic_fallback(tmp_path):
    driver = YAMLPersistenceDriver()
    relics_dir = tmp_path / "relics"
    relics_dir.mkdir()
    (relics_dir / "smoke.yml").write_text(
        """
__metadata__: {"version": "1.0", "epoch": 5}
__prefabs__: {}
entity_1:
  IdentityComponent: {"name": "Smoke", "kind": "room"}
""".lstrip()
    )
    world = World()

    driver.load_relic(
        world, "smoke", relics_dir, *type_registries(PluginRegistry(bunnyland_plugins()))
    )

    assert world.epoch == 5
    assert (
        world.get_entity(EntityId.parse("entity_1")).get_component(IdentityComponent).name
        == "Smoke"
    )


def test_yaml_driver_returns_regular_relics_snapshot_unchanged():
    snapshot = {"metadata": {"epoch": 12}, "entities": {"entity_1": {}}}

    assert YAMLPersistenceDriver().snapshot_from_document(snapshot) == snapshot


def test_yaml_driver_load_snapshot_uses_world_registries_and_skips_unknowns():
    driver = YAMLPersistenceDriver()
    world = World()
    world.register_component_type(IdentityComponent)
    world.register_edge_type(ExitTo)

    driver.load_snapshot(
        world,
        {
            "metadata": {"epoch": 9},
            "prefabs": {
                "room": {
                    "components": {
                        "IdentityComponent": {"name": "Prefab Room", "kind": "room"},
                        "MissingComponent": {"ignored": True},
                    }
                }
            },
            "entities": {
                "room_1": {},
                "room_2": {"prefab": "room"},
            },
            "components": {
                "IdentityComponent": {
                    "room_1": {"name": "Start", "kind": "room"},
                    "missing_1": {"name": "Missing", "kind": "room"},
                },
                "MissingComponent": {
                    "room_1": {"ignored": True},
                },
            },
            "relationships": {
                "ExitTo": {
                    "room_1": [
                        {
                            "target": "room_2",
                            "edge": {"direction": "north", "label": "North"},
                        },
                        {
                            "target": "missing_1",
                            "edge": {"direction": "south", "label": "Missing"},
                        },
                    ],
                    "missing_1": [
                        {
                            "target": "room_2",
                            "edge": {"direction": "east", "label": "Ignored"},
                        }
                    ],
                },
                "MissingEdge": {
                    "room_1": [{"target": "room_2", "edge": {}}],
                },
            },
        },
    )

    room_1 = world.get_entity(EntityId.parse("room_1"))
    room_2_id = EntityId.parse("room_2")
    assert world.epoch == 9
    assert world._prefabs["room"][IdentityComponent].name == "Prefab Room"
    assert room_1.get_component(IdentityComponent).name == "Start"
    assert room_1.get_relationships(ExitTo) == [
        (ExitTo(direction="north", label="North"), room_2_id)
    ]


def test_yaml_driver_load_snapshot_rejects_malformed_sections():
    driver = YAMLPersistenceDriver()
    world = World()
    world.register_edge_type(ExitTo)

    with pytest.raises(ValueError, match="prefab room components must be a YAML mapping"):
        driver.load_snapshot(
            world,
            {
                "prefabs": {"room": {"components": []}},
            },
        )

    with pytest.raises(ValueError, match="ExitTo edges for room_1 must be a list"):
        driver.load_snapshot(
            world,
            {
                "entities": {"room_1": {}, "room_2": {}},
                "relationships": {"ExitTo": {"room_1": {"target": "room_2"}}},
            },
        )


async def test_reload_starts_with_empty_command_queues(tmp_path):
    actor, _result = await _build_and_play()
    path = tmp_path / "world.json"
    save_world(actor, path, meta=WorldMeta())
    actor2, _meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))

    # Volatile queues are never persisted (spec 26): a reload resumes with empty queues.
    assert actor2.queues.characters_with_pending() == []
    assert actor2._inbox.empty()


async def test_game_loop_autosaves_every_n_ticks(tmp_path):
    actor = WorldActor()
    apply_plugins(bunnyland_plugins(), actor)
    await instantiate(actor, await StubWorldBuilder().propose("seed"))
    path = tmp_path / "auto.json"

    saved_at: list[int] = []

    def autosave(ticks: int) -> None:
        saved_at.append(ticks)
        save_world(actor, path, meta=WorldMeta(seed="seed"))

    dispatch = ControllerDispatch(actor, PromptBuilder(actor.world), ScriptedAgent([]))
    loop = GameLoop(actor, dispatch, autosave=autosave, autosave_every=2)
    await loop.run(max_ticks=5)

    assert saved_at == [2, 4]
    assert path.exists()


def test_type_registries_cover_core_and_plugin_types():
    components, edges = type_registries(PluginRegistry(bunnyland_plugins()))
    assert {
        "IdentityComponent",
        "HungerComponent",
        "RegionComponent",
        "RoomComponent",
        "ToonRoomComponent",
    } <= set(components)
    assert {"Contains", "ExitTo", "ControlledBy"} <= set(edges)


def test_toon_room_default_start_reloads_with_plugin_registry(tmp_path):
    actor = WorldActor()
    room = spawn_entity(
        actor.world,
        [RoomComponent(title="Landing Room"), ToonRoomComponent(default_start=True)],
    )

    path = tmp_path / "toon-room.json"
    save_world(actor, path, meta=WorldMeta(seed="toon"))
    loaded, _meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))

    loaded_room = loaded.world.get_entity(room.id)
    assert loaded_room.get_component(ToonRoomComponent).default_start is True


def test_region_hierarchy_uses_contains_region_mode_and_reloads(tmp_path):
    actor = WorldActor()
    chain = [
        spawn_entity(
            actor.world,
            [
                RegionComponent(
                    name="Lapin Prime",
                    kind="planet",
                    population=8_400_000_000,
                    climate="temperate",
                    terrain="mixed biomes",
                )
            ],
        ),
        spawn_entity(actor.world, [RegionComponent(name="Mossreach", kind="continent")]),
        spawn_entity(actor.world, [RegionComponent(name="Burrowmark", kind="country")]),
        spawn_entity(actor.world, [RegionComponent(name="North Warren", kind="region")]),
        spawn_entity(actor.world, [RegionComponent(name="Clover City", kind="city")]),
        spawn_entity(actor.world, [RegionComponent(name="Old Market", kind="area")]),
        spawn_entity(actor.world, [RegionComponent(name="Greenhill", kind="neighborhood")]),
        spawn_entity(actor.world, [RegionComponent(name="Sunstem Zone", kind="zone")]),
        spawn_entity(actor.world, [RegionComponent(name="Carrot Street", kind="street")]),
        spawn_entity(actor.world, [RegionComponent(name="Moonroot Tower", kind="building")]),
        spawn_entity(actor.world, [RegionComponent(name="Story Three", kind="story")]),
        spawn_entity(actor.world, [RoomComponent(title="Observatory Room", indoor=True)]),
    ]
    for parent, child in zip(chain, chain[1:], strict=False):
        parent.add_relationship(Contains(mode=ContainmentMode.REGION), child.id)

    path = tmp_path / "regions.json"
    save_world(actor, path, meta=WorldMeta(seed="regional"))
    loaded, _meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))

    loaded_root = loaded.world.get_entity(chain[0].id).get_component(RegionComponent)
    assert loaded_root.population == 8_400_000_000
    assert loaded_root.climate == "temperate"
    assert loaded_root.terrain == "mixed biomes"

    for parent, child in zip(chain, chain[1:], strict=False):
        relationships = loaded.world.get_entity(parent.id).get_relationships(Contains)
        assert any(
            target == child.id and edge.mode == ContainmentMode.REGION
            for edge, target in relationships
        )


def test_discord_room_feed_component_survives_save_load(tmp_path):
    actor = WorldActor()
    room = spawn_entity(
        actor.world,
        [
            RoomComponent(title="Feed Room"),
            DiscordRoomFeedComponent(channel_id=123456789),
        ],
    )

    path = tmp_path / "discord-room-feed.json"
    save_world(actor, path, meta=WorldMeta(seed="discord-feed"))
    loaded, _meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))

    component = loaded.world.get_entity(room.id).get_component(DiscordRoomFeedComponent)
    assert component.channel_id == 123456789


def test_yaml_module_missing_extra_raises(monkeypatch):
    from bunnyland.persistence_yaml import _yaml_module

    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(RuntimeError, match="YAML persistence requires PyYAML"):
        _yaml_module()


def test_fsync_file_preserves_contents_and_requires_existing_file(tmp_path):
    path = tmp_path / "snapshot.json"
    contents = b'{"checkpoint": "complete"}\n'
    path.write_bytes(contents)
    persistence._fsync_file(path)
    assert path.read_bytes() == contents
    with pytest.raises(FileNotFoundError):
        persistence._fsync_file(tmp_path / "missing.json")


def test_windows_directory_sync_does_not_open_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence.sys, "platform", "win32")

    def unexpected_open(*args, **kwargs):
        pytest.fail("Windows directory sync must not open a file descriptor")

    monkeypatch.setattr(persistence.os, "open", unexpected_open)
    persistence._fsync_directory(tmp_path)


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_save_is_checksummed_rotated_and_restorable(tmp_path, suffix):
    actor = WorldActor()
    path = tmp_path / f"world{suffix}"

    first = save_world(actor, path, meta=WorldMeta(seed="first"))
    save_world(actor, path, meta=first.model_copy(update={"seed": "second"}))

    checksum_path = path.with_name(f"{path.name}.sha256")
    assert checksum_path.exists()
    assert path.with_name(f"{path.name}.bak.1").exists()
    loaded, meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))
    assert loaded.epoch == actor.epoch
    assert meta.seed == "second"
    assert meta.memory.checkpoint_epoch == actor.epoch
    assert meta.rng_stream_state["command_order"]

    path.write_text(path.read_text() + "\ncorrupt")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_world(path, registry=PluginRegistry(bunnyland_plugins()))


@pytest.mark.parametrize("interrupted_replace", [1, 2, 3])
def test_interrupted_checkpoint_publication_recovers_complete_snapshot(
    tmp_path, monkeypatch, interrupted_replace
):
    actor = WorldActor()
    path = tmp_path / "world.json"
    original = save_world(actor, path, meta=WorldMeta(seed="old"), backup_count=0)
    real_replace = persistence.os.replace
    replacements = 0

    def interrupt_after_replace(source, destination):
        nonlocal replacements
        real_replace(source, destination)
        replacements += 1
        if replacements == interrupted_replace:
            raise OSError("simulated power loss")

    monkeypatch.setattr(persistence.os, "replace", interrupt_after_replace)
    with pytest.raises(OSError, match="simulated power loss"):
        save_world(
            actor,
            path,
            meta=original.model_copy(update={"seed": "new"}),
            backup_count=0,
        )

    monkeypatch.setattr(persistence.os, "replace", real_replace)
    _loaded, recovered = load_world(path, registry=PluginRegistry(bunnyland_plugins()))
    assert recovered.seed in {"old", "new"}
    assert persistence._checksum(path) == (
        persistence._checksum_path(path).read_text().split(maxsplit=1)[0]
    )
    assert not persistence._checkpoint_transaction_path(path).exists()


def test_checkpoint_recovery_rejects_unverifiable_transaction(tmp_path):
    path = tmp_path / "world.json"
    path.write_text("partial")
    persistence._checkpoint_transaction_path(path).write_text(
        json.dumps(
            {
                "version": 1,
                "snapshot": path.name,
                "new_checksum": "new",
                "old_checksum": "old",
            }
        )
    )

    with pytest.raises(ValueError, match="checkpoint transaction cannot recover"):
        load_world(path, registry=PluginRegistry(bunnyland_plugins()))


@pytest.mark.parametrize(
    "transaction",
    [
        [],
        {"version": 2},
        {
            "version": 1,
            "snapshot": "wrong.json",
            "new_checksum": "new",
            "old_checksum": "old",
        },
        {
            "version": 1,
            "snapshot": "world.json",
            "new_checksum": None,
            "old_checksum": "old",
        },
        {
            "version": 1,
            "snapshot": "world.json",
            "new_checksum": "new",
            "old_checksum": 1,
        },
    ],
)
def test_checkpoint_recovery_rejects_invalid_transaction_shape(tmp_path, transaction):
    path = tmp_path / "world.json"
    persistence._checkpoint_transaction_path(path).write_text(json.dumps(transaction))

    with pytest.raises(ValueError, match="invalid checkpoint transaction"):
        load_world(path, registry=PluginRegistry(bunnyland_plugins()))


def test_checkpoint_recovery_can_retain_verified_old_snapshot(tmp_path):
    actor = WorldActor()
    path = tmp_path / "world.json"
    save_world(actor, path, meta=WorldMeta(seed="old"), backup_count=0)
    old_checksum = persistence._checksum(path)
    persistence._checkpoint_temporary_path(path).write_text("incomplete new snapshot")
    persistence._checkpoint_transaction_path(path).write_text(
        json.dumps(
            {
                "version": 1,
                "snapshot": path.name,
                "new_checksum": "unavailable",
                "old_checksum": old_checksum,
            }
        )
    )

    _loaded, recovered = load_world(path, registry=PluginRegistry(bunnyland_plugins()))

    assert recovered.seed == "old"
    assert persistence._checksum_path(path).read_text().startswith(old_checksum)


def test_pre_checksum_schema_v2_save_remains_loadable(tmp_path):
    actor = WorldActor()
    path = tmp_path / "legacy.json"
    save_world(actor, path, meta=WorldMeta(seed="legacy"))
    path.with_name(f"{path.name}.sha256").unlink()

    _loaded, meta = load_world(path, registry=PluginRegistry(bunnyland_plugins()))
    assert meta.seed == "legacy"


def test_operational_journal_is_bounded_and_checkpointed(tmp_path):
    actor = WorldActor()
    path = tmp_path / "world.json"
    save_world(actor, path, meta=WorldMeta(seed="journal"))

    journal = OperationalJournal(path, max_records=2, segment_records=1)
    journal.append("epoch_transition", from_epoch=0, to_epoch=1)
    journal.append("epoch_transition", from_epoch=1, to_epoch=2)

    records = journal.records()
    assert len(records) == 2
    assert [record["to_epoch"] for record in records] == [1, 2]


def test_operational_journal_rotates_aggregates_and_drops_whole_oldest_segment(tmp_path):
    path = tmp_path / "world.json"
    journal = OperationalJournal(path, max_records=5, segment_records=2)

    for sequence in range(6):
        journal.append("sequence", sequence=sequence)

    completed = sorted(tmp_path.glob("world.json.journal.*.jsonl"))
    assert [segment.name for segment in completed] == [
        "world.json.journal.00000002.jsonl",
        "world.json.journal.00000003.jsonl",
    ]
    assert not journal.path.exists()
    assert [record["sequence"] for record in journal.records()] == [2, 3, 4, 5]
    assert all(
        len(segment.read_text(encoding="utf-8").splitlines()) == 2
        for segment in completed
    )
    assert all(
        json.loads(line)["journal_version"] == 1
        for segment in completed
        for line in segment.read_text(encoding="utf-8").splitlines()
    )


def test_operational_journal_append_never_opens_completed_segments(tmp_path, monkeypatch):
    path = tmp_path / "world.json"
    journal = OperationalJournal(path, max_records=5, segment_records=2)
    for sequence in range(5):
        journal.append("sequence", sequence=sequence)
    completed = {segment for _index, segment in journal._completed}
    original_open = Path.open

    def guarded_open(candidate, *args, **kwargs):
        if candidate in completed:
            raise AssertionError(f"append opened completed segment {candidate}")
        return original_open(candidate, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", guarded_open)
        journal.append("sequence", sequence=5)

    assert [record["sequence"] for record in journal.records()] == [2, 3, 4, 5]


def test_operational_journal_scans_active_once_and_serializes_each_append_once(
    tmp_path, monkeypatch
):
    from bunnyland import persistence

    path = tmp_path / "world.json"
    active = path.with_name(f"{path.name}.journal.jsonl")
    active.write_text('{"sequence":0}\n', encoding="utf-8")
    original_open = Path.open
    active_reads = 0

    def tracked_open(candidate, mode="r", *args, **kwargs):
        nonlocal active_reads
        if candidate == active and mode == "r":
            active_reads += 1
        return original_open(candidate, mode, *args, **kwargs)

    original_dumps = json.dumps
    serializations = 0

    def tracked_dumps(value, *args, **kwargs):
        nonlocal serializations
        serializations += 1
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    journal = OperationalJournal(path, segment_records=10)
    monkeypatch.setattr(persistence.json, "dumps", tracked_dumps)
    journal.append("sequence", sequence=1)
    journal.append("sequence", sequence=2)

    assert active_reads == 1
    assert serializations == 2


@pytest.mark.parametrize("legacy_records", [5000, 5107])
def test_operational_journal_migrates_monolith_once_and_keeps_newest_records(
    tmp_path, legacy_records
):
    path = tmp_path / "world.json"
    active = path.with_name(f"{path.name}.journal.jsonl")
    active.write_text(
        "".join(json.dumps({"sequence": sequence}) + "\n" for sequence in range(legacy_records)),
        encoding="utf-8",
    )

    journal = OperationalJournal(path)

    completed = sorted(tmp_path.glob("world.json.journal.*.jsonl"))
    assert len(completed) == 50
    assert not active.exists()
    assert not list(tmp_path.glob(".*legacy-recovery-*"))
    records = journal.records()
    assert len(records) == 5000
    assert records[0]["sequence"] == legacy_records - 5000
    assert records[-1]["sequence"] == legacy_records - 1
    assert all(len(segment.read_text().splitlines()) == 100 for segment in completed)

    restarted = OperationalJournal(path)
    assert [record["sequence"] for record in restarted.records()] == [
        record["sequence"] for record in records
    ]


def test_operational_journal_recovers_interrupted_legacy_migration(tmp_path):
    path = tmp_path / "world.json"
    active = path.with_name(f"{path.name}.journal.jsonl")
    segment_one = tmp_path / "world.json.journal.00000001.jsonl"
    segment_one.write_text('{"sequence":-2}\n{"sequence":-1}\n', encoding="utf-8")
    stale_segment = tmp_path / "world.json.journal.00000002.jsonl"
    stale_segment.write_text("interrupted\n", encoding="utf-8")
    active.write_text("interrupted\n", encoding="utf-8")
    legacy = tmp_path / ".world.json.journal.jsonl.legacy-recovery-00000002"
    legacy.write_text(
        "".join(json.dumps({"sequence": sequence}) + "\n" for sequence in range(7)),
        encoding="utf-8",
    )
    stale_temporary = tmp_path / ".world.json.journal.00000002.jsonl.migration.tmp"
    stale_temporary.write_text("interrupted\n", encoding="utf-8")

    journal = OperationalJournal(path, max_records=5, segment_records=2)

    assert [record["sequence"] for record in journal.records()] == [2, 3, 4, 5, 6]
    assert not legacy.exists()
    assert not stale_temporary.exists()
    assert all(
        json.loads(line)
        for segment in [*sorted(tmp_path.glob("world.json.journal.*.jsonl")), active]
        if segment.exists()
        for line in segment.read_text().splitlines()
    )


def test_operational_journal_ignores_nonsegment_files_and_blank_legacy_lines(tmp_path):
    path = tmp_path / "world.json"
    ignored = tmp_path / "world.json.journal.notes.jsonl"
    ignored.write_text("operator notes\n", encoding="utf-8")
    legacy = tmp_path / ".world.json.journal.jsonl.legacy-recovery-00000001"
    legacy.write_text('\n{"sequence":1}\n', encoding="utf-8")

    journal = OperationalJournal(path, segment_records=2)

    assert [record["sequence"] for record in journal.records()] == [1]
    assert ignored.read_text(encoding="utf-8") == "operator notes\n"


def test_operational_journal_rejects_invalid_recovery_filename(tmp_path):
    invalid = tmp_path / ".world.json.journal.jsonl.legacy-recovery-invalid"
    invalid.write_text('{"sequence":1}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid journal recovery filename"):
        OperationalJournal(tmp_path / "world.json")


@pytest.mark.parametrize(
    ("options", "message"),
    [({"max_records": 0}, "max_records"), ({"segment_records": 0}, "segment_records")],
)
def test_operational_journal_rejects_nonpositive_bounds(tmp_path, options, message):
    with pytest.raises(ValueError, match=message):
        OperationalJournal(tmp_path / "world.json", **options)


def test_recovery_manifest_pins_world_memory_media_and_release(tmp_path):
    actor = WorldActor()
    snapshot = tmp_path / "world.json"
    media_manifest = tmp_path / "media.sha256"
    media_manifest.write_text("portrait.png abc123\n", encoding="utf-8")
    stamped = save_world(
        actor,
        snapshot,
        meta=WorldMeta(
            seed="recovery",
            memory=MemoryManifest(
                world_namespace="preview-world",
                backend="chroma",
                collection_namespace="preview-world",
                high_watermark=17,
            ),
        ),
    )

    manifest = build_recovery_manifest(
        snapshot,
        meta=stamped,
        release="bunnyland-ecs-agent-preview-2026-07",
        release_pins={"server": "abc", "web": "def"},
        media_manifest_path=media_manifest,
        rollback_checkpoint="world.json.bak.1",
    )
    path = tmp_path / "recovery.json"
    write_recovery_manifest(path, manifest)
    loaded = RecoveryManifest.model_validate_json(path.read_text(encoding="utf-8"))

    assert loaded.version == 1
    assert loaded.release_pins == {"server": "abc", "web": "def"}
    assert loaded.world_id == stamped.world_id
    assert loaded.world_epoch == actor.epoch
    assert loaded.world_checksum_sha256 == snapshot.with_name(
        "world.json.sha256"
    ).read_text().split()[0]
    assert loaded.memory_backend == "chroma"
    assert loaded.memory_namespace == "preview-world"
    assert loaded.memory_checkpoint_epoch == actor.epoch
    assert loaded.memory_high_watermark == 17
    assert loaded.media_manifest_checksum_sha256
    assert loaded.rollback_checkpoint == "world.json.bak.1"
    assert path.with_name("recovery.json.sha256").exists()


def test_read_world_meta_supports_checksummed_yaml(tmp_path):
    snapshot = tmp_path / "world.yaml"
    stamped = save_world(
        WorldActor(),
        snapshot,
        meta=WorldMeta(seed="yaml metadata", generator="oneshot"),
    )

    loaded = read_world_meta(snapshot)

    assert loaded.world_id == stamped.world_id
    assert loaded.seed == "yaml metadata"
    assert loaded.generator == "oneshot"


def test_reload_automatically_quarantines_future_memory_documents(tmp_path):
    from bunnyland.memory import InMemoryStore

    plugins = bunnyland_plugins()
    actor = WorldActor()
    apply_plugins(plugins, actor)
    spawn_entity(
        actor.world,
        [MemoryProfileComponent(vector_collection="character-memory")],
    )
    store = InMemoryStore()
    actor.memory_store = store
    snapshot = tmp_path / "world.json"
    stamped = save_world(
        actor,
        snapshot,
        meta=WorldMeta(
            seed="older-world",
            memory=MemoryManifest(
                world_namespace="older-world",
                collection_namespace="older-world",
            ),
        ),
    )
    old = store.add("character-memory", text="before checkpoint", created_at_epoch=0)
    store.add("character-memory", text="after checkpoint", created_at_epoch=1)

    reload_world(
        actor,
        snapshot,
        meta=stamped,
        registry=PluginRegistry(plugins),
    )

    assert [entry.id for entry in store.search("character-memory")] == [old.id]
    quarantined = store.list_documents("older-world.quarantine.character-memory")
    assert [document.document for document in quarantined] == ["after checkpoint"]
    assert OperationalJournal(snapshot).records()[-1]["record_type"] == "memory_quarantine"


def test_actor_owned_journal_records_checkpoints_receipts_and_memory_quarantine(tmp_path):
    from bunnyland.memory import InMemoryStore

    plugins = bunnyland_plugins()
    actor = WorldActor()
    apply_plugins(plugins, actor)
    spawn_entity(actor.world, [MemoryProfileComponent(vector_collection="character-memory")])
    store = InMemoryStore()
    actor.memory_store = store
    snapshot = tmp_path / "world.json"
    stamped = save_world(
        actor,
        snapshot,
        meta=WorldMeta(
            seed="actor-journal",
            memory=MemoryManifest(
                world_namespace="actor-journal",
                collection_namespace="actor-journal",
            ),
        ),
    )
    journal = actor.persistence.journal
    assert journal is not None
    actor.configure_persistence(save_path=snapshot, meta=stamped, plugins=plugins)
    assert actor.persistence.journal is journal

    command = build_submitted_command(
        character_id="entity_999999",
        controller_id="entity_999998",
        controller_generation=0,
        command_type="move",
        cost=CommandCost(),
        lane=Lane.WORLD,
        command_id="actor-journal-rejection",
    )
    assert asyncio.run(actor.submit(command)).accepted is False
    store.add("character-memory", text="future memory", created_at_epoch=1)

    reload_world(actor, snapshot, meta=stamped, registry=PluginRegistry(plugins))

    assert actor.persistence.journal is journal
    assert [record["record_type"] for record in journal.records()] == [
        "checkpoint",
        "command_receipt",
        "memory_quarantine",
    ]


def test_reload_with_memory_store_and_no_future_documents_skips_quarantine_journal(tmp_path):
    from bunnyland.memory import InMemoryStore

    plugins = bunnyland_plugins()
    actor = WorldActor()
    apply_plugins(plugins, actor)
    spawn_entity(actor.world, [MemoryProfileComponent(vector_collection="character-memory")])
    actor.memory_store = InMemoryStore()
    snapshot = tmp_path / "world.json"
    stamped = save_world(actor, snapshot, meta=WorldMeta(seed="current-world"))

    reload_world(
        actor,
        snapshot,
        meta=stamped,
        registry=PluginRegistry(plugins),
    )

    assert all(
        record["record_type"] != "memory_quarantine"
        for record in OperationalJournal(snapshot).records()
    )


def test_cloning_rebases_active_memory_collections_to_the_new_world():
    actor = WorldActor()
    character = spawn_entity(
        actor.world,
        [
            MemoryProfileComponent(
                vector_collection="source-world--private",
                shared_collections=("source-world--notice-board",),
            )
        ],
    )
    source = WorldMeta(
        world_id="source-world",
        memory=MemoryManifest(
            world_namespace="source-world",
            collection_namespace="source-world",
        ),
    )

    first = clone_world_identity(actor, source, world_id="clone-one")
    first_profile = actor.world.get_entity(character.id).get_component(MemoryProfileComponent)
    assert first.world_id == "clone-one"
    assert first.memory.collection_namespace == "clone-one"
    assert first_profile.vector_collection == "clone-one--private"
    assert first_profile.shared_collections == ("clone-one--notice-board",)

    second = clone_world_identity(actor, first, world_id="clone-two")
    second_profile = actor.world.get_entity(character.id).get_component(MemoryProfileComponent)
    assert second.world_id == "clone-two"
    assert second_profile.vector_collection == "clone-two--private"
    assert second_profile.shared_collections == ("clone-two--notice-board",)


def test_cloning_rebases_matching_later_namespace_and_unprefixed_collection():
    actor = WorldActor()
    character = spawn_entity(
        actor.world,
        [
            MemoryProfileComponent(
                vector_collection="source-world--private",
                shared_collections=("plain-board",),
            )
        ],
    )
    source = WorldMeta(
        world_id="source-world",
        memory=MemoryManifest(
            world_namespace="source-world",
            collection_namespace="active-memory",
        ),
    )

    clone_world_identity(actor, source, world_id="clone")

    profile = actor.world.get_entity(character.id).get_component(MemoryProfileComponent)
    assert profile.vector_collection == "clone--private"
    assert profile.shared_collections == ("clone--plain-board",)


def test_operational_journal_empty_receipt_and_deeper_backup_rotation(tmp_path):
    actor = WorldActor(receipt_cache_size=1)
    path = tmp_path / "world.json"
    journal = OperationalJournal(path)
    assert journal.records() == []
    meta = WorldMeta(seed="one")
    meta = save_world(actor, path, meta=meta)
    meta = save_world(actor, path, meta=meta.model_copy(update={"seed": "two"}))
    save_world(actor, path, meta=meta.model_copy(update={"seed": "three"}))
    assert path.with_name(f"{path.name}.bak.2").exists()

    actor.configure_persistence(save_path=path, meta=meta)
    command = build_submitted_command(
        character_id="entity_999999",
        controller_id="entity_999998",
        controller_generation=0,
        command_type="move",
        cost=CommandCost(),
        lane=Lane.WORLD,
        command_id="journal-rejection",
    )
    outcome = asyncio.run(actor.submit(command))
    assert outcome.accepted is False
    assert any(
        record["record_type"] == "command_receipt"
        and record["receipt"]["command_id"] == "journal-rejection"
        for record in journal.records()
    )

    no_backup = tmp_path / "no-backup.json"
    save_world(actor, no_backup, meta=meta, backup_count=0)
    save_world(actor, no_backup, meta=meta, backup_count=0)
    assert not no_backup.with_name(f"{no_backup.name}.bak.1").exists()
