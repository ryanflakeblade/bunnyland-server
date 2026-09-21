# Wild Boar Forest execution paths

This authored scene lives in `src/bunnyland/worldgen/wild_boar_forest.py` and is
registered by `foundation/worldgen/plugin.py`. Worldgen declares its core/persona/social
integration, and this generator checks those plugins before creating entities. Other
generators remain available in existing minimal installations. The current repository uses plugin-owned action
catalogues rather than the older central `DEFAULT_ACTION_DEFINITIONS` layout.

1. `cli._load_or_generate_world` selects `wild-boar-forest` and calls
   `traced_generate`. The generator supplies a validated `WorldProposal` to async
   `instantiate`, then attaches scene membership, private knowledge, and social bonds.
2. `ControllerDispatch` builds a character-scoped `PromptContext`. The registered
   `wild-boar-forest` behavior tree reads only these facts and visible characters;
   it has no reference to the actor or raw world. It returns ordinary `ToolCall` values.
3. `command_from_tool_call` maps tool arguments to the plugin action catalogue.
   `WorldActor.submit` and `tick` enforce ownership generation, points, argument
   requirements, and lifecycle gates. `ForestSceneHandler` validates local scene
   membership and state, returning an atomic mutation plan and directed domain events.
4. The native `move` handler emits `ActorMovedEvent`. The scene's movement reactor
   follows the moving resident's links and records escape when Lin leaves the clearing.
   Neither path scans the world or uses a new periodic system.
5. `save_world` serializes components and edges. `load_world` restores them and the
   clock, while plugin startup re-registers the behavior tree, scene edge type,
   handler, and movement subscriber. Decisions have no volatile script cursor to restore.

`ForestSceneComponent` owns one scene's progress on its clearing. `ForestResident`
links each participant to the clearing with its role; this authored generator creates
exactly one link per character and one character per role. Removing an endpoint removes
its relationships through Relics. No migration of older saves is needed. Repeated memories
use separate `GossipClaimComponent` entities and character-to-claim `KnowsGossip` edges.
`SocialBond` remains the directed relationship model; `GoalComponent` remains singleton
active-goal state. The generator never stores live entity IDs in new component fields.

Public location descriptions contain no secrets. `forest_facts` is a self-only provider,
excluded by the existing inspection filter when inspecting a different character. Scene
stage facts are available only in the clearing. Transition memories and events go only to
the residents who witnessed them; escape includes Lin and those left in the clearing.
The complete save is authoritative server data and must not be supplied to a player agent.

Controller handoff uses existing `ControlledBy` generations and actor assignment; it does
not rebuild a character. Human and autonomous choices reach the same handler. Default
behavior produces rescue; a human can escape as Lin or keep Lu away to allow an ambush.
This is an intentionally simplified literary adaptation, not a novel compiler or a general
combat simulator. Engine UI and generic narration are not fully localized.

`tests/test_wild_boar_forest.py` covers each branch, rejected actions, prompt/event privacy,
handoff, deterministic generation, and persistence. Run:

```bash
uv run --no-sync -m pytest tests/test_wild_boar_forest.py tests/test_plugins.py tests/test_worldgen.py
```
