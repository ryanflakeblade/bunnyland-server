Below is the first-pass **master mechanics catalogue** for bunnyland. This is intentionally broad. It is not the MVP list. It is the long-term backlog of systems, components, actions, events, services, projections, and plugin classes we may eventually want.

## Current v1 implemented-pack status

The `worldgen` foundation also includes `wild-boar-forest`, a manually authored
Simplified Chinese scene with Lin Chong, Lu Zhishen, Dong Chao, and Xue Ba. It reuses
persona goals, private social knowledge, directed bonds, and controller handoff, and adds
validated rescue/escape/ambush branches. See [the player guide](player/wild-boar-forest.md).
General novel-to-world compilation remains unimplemented.

The peaceful starter pack now has playable v1 coverage for the core catalogue loops:

- `sandbox`: the deterministic `bunnyland-sandbox` generator builds Crossroads Arrival,
  claimable New Arrivals, Commons, and one enrichment-driven region for each loaded bundled
  simpack. The optional After Dark district uses explicit acknowledge, enter, leave, and
  withdrawal actions backed by the existing boundary policy; it adds no routes or
  transport-specific behavior.

- `core_verbs`: shared stateful rest and sleep recovery with optional durations, explicit
  waking, action and danger interruption, typed lifecycle events, prompt state, bounded
  stress relief thoughts, and `wait` as a state-free turn yield.

- `lifesim`: hunger, thirst, fatigue, hygiene, comfort, fun, social contact, privacy,
  safety, eat/drink, self-care verbs, sleep fatigue recovery, social recovery through
  speech, affect, homes, claims, careers, business, bills, skills, routines,
  character profiles, whims, home object use/maintenance, invitations, relationships,
  family, pregnancy, birth, adoption, aging stages, and player-facing natural-aging
  policy controls.
- `colonysim`: resources, resource stacks, stockpiles, filters, hauling, reservations,
  ownership, jobs, work priorities, allowed areas, workstation recipes, baking through the
  shared recipe engine, entity outputs for edible/drinkable products, room quality,
  colony wealth/expectations, pawn profiles, passions/backstories, job bills, prisoners
  and recruitment, research/tech unlocks, incidents, trade offers, caravans, diplomacy,
  body-part health, surgery/prosthetics, wound tending, medicine uses, rescue to medical
  bed, bed rest, infection progress, and mental breaks/inspirations.
- `gardensim`: soil, tilling, planting, watering, fertilizer, growth, seasonal withering,
  greenhouse exceptions, crop quality, regrowth, pests, weeding, inspection,
  edible/resource harvests, tree tapping and sap, processing machines with cancel/repair
  and breakdown state, farm animals, feeding, petting, breeding, animal products, fishing,
  mining, mine levels, ladders, geodes, foraging, gifts/friendship, mail, quests,
  festivals, bundles, shipping, collections, museum donation, rewards, and daily farm
  reset.

The outer-ring parity pass has also implemented the package status lines marked below,
including `neon-sim`. The remaining catalogue text is still broader than v1 outside these
implemented surfaces. Treat unimplemented headings below as backlog unless a component,
handler, consequence/system, event, prompt fragment, and test exists in
`src/bunnyland/mechanics/` and `tests/`.

The design target is not to clone any one game. The target is to extract the loops that make those games sticky: needs, relationships, work, scarcity, seasons, growth, danger, exploration, memory, and the joy of small systems colliding.

The inspirations break down cleanly:

| Inspiration    | bunnyland package | Core fantasy                                                                        |
| -------------- | ----------------- | ----------------------------------------------------------------------------------- |
| The Sims       | `life-sim`        | characters have needs, moods, relationships, homes, routines, families, memories    |
| RimWorld       | `colony-sim`      | characters organize work, survive incidents, build settlements, spiral emotionally  |
| Conan Exiles   | `barbarian-sim`   | harsh survival, weather, combat, crafting, thralls, bases, raids                    |
| Stardew Valley | `garden-sim`      | seasons, crops, animals, gifts, villagers, festivals, cozy production chains        |
| Skyrim         | `dragon-sim`      | open-world exploration, quests, factions, skills-by-use, dungeons, dragons, shouts  |
| Daggerfall     | `dagger-sim`      | procedural realm expands through rumors, guilds, banks, law, travel, and dungeons   |
| FTL            | `void-sim`        | crews survive ships, stations, planets, alien contact, tech, contracts, and hazards |
| Fallout        | `nuke-sim`        | wasteland survival, radiation, mutation, scavenging, settlement salvage, jury-rigged crafting |
| Deus Ex / Watch Dogs / Cyberpunk 2077 / Blade Runner / Shadows of Doubt | `neon-sim` | hackers, surveillance, street economies, corporate intrigue, cybernetics, reputation |
| Jurassic Park / ARK / Dino Crisis / Pacific Rim | `dino-sim` | fossil and species identification, cloning, egg handling, reptile procreation, hatching, and kaiju storyteller incidents |
| Dwarf Fortress | `fortress-sim`    | deep settlement simulation, materials, history, disasters, artifacts, absurd detail |

For source grounding: The Sims uses traits, emotions, whims, aspirations, skills, careers, crafting hobbies, and life-state systems; RimWorld centers on colonist moods, needs, wounds, illnesses, addictions, social bonds, storyteller incidents, work priorities, and mental breaks; Conan Exiles emphasizes survival, building, thralls, weather, temperature, hunger/thirst, PvP/siege, mounts, pets, purges, and world bosses; Stardew Valley revolves around seasonal crops, fertilizer, skills, villagers, gifts, friendship, farming, fishing, mining, and festivals; Skyrim’s replay loop is open-world exploration, factions, skills, combat, magic, stealth, followers, crafting, dragons, and shouts; Daggerfall contributes procedural scale, guilds, banks, services, law, travel, generated quests, and generated dungeons; FTL contributes crewed-ship pressure, subsystem damage, sector travel, distress signals, resource scarcity, and cascading emergencies; Dwarf Fortress goes deeper than all of these with generated worlds, histories, fortress management, geology, migrants, nobles, justice, strange moods, artifacts, tantrum spirals, and many more systems; Deus Ex, Watch Dogs, Cyberpunk 2077, Blade Runner, and Shadows of Doubt contribute cyberpunk infiltration, hacking, surveillance, corporate power, street economies, cybernetics, investigation, noir atmosphere, and reputation pressure; Jurassic Park, ARK, and Dino Crisis contribute dangerous animals, eggs, taming, escapes, containment failure, and monster-scale emergency response. ([Electronic Arts Inc.][1])

---

# 1. Shared engine mechanics

These are not tied to any one game package. Every plugin builds on them.

## 1.1 Entity identity and description

### Mechanics

Every meaningful thing in the world is an entity:

```text
character
room
item
container
building
job
memory
note
faction
quest
spell
crop
animal
workstation
resource node
event source
controller
generator
```

### Components

```python
IdentityComponent
DescriptionComponent
TagComponent
LifecycleComponent
PersistenceComponent
GeneratedContentComponent
```

### Systems and services

```text
EntityNamingService
DescriptionRenderService
TagQueryService
LifecycleSystem
PersistenceProjection
GeneratedContentValidationService
```

### Events

```text
EntityCreatedEvent
EntityDestroyedEvent
EntityRenamedEvent
DescriptionChangedEvent
TagsChangedEvent
```

---

## 1.2 Physical containment and spatial graph

### Mechanics

Canonical physical state uses `Contains`.

```text
room contains character
room contains chest
chest contains apple
character contains sword
character holding sword
character wearing cloak
```

Rooms form a graph through exits.

### Edges

```python
Contains
ExitTo
Holding
Wearing
Owns
ReservedBy
```

### Components

```python
PhysicalComponent
WeightComponent
ContainerComponent
InventoryComponent
PortableComponent
InteractiveComponent
HoldableComponent
WearableComponent
DroppableComponent
```

### Systems and services

```text
ContainmentIntegritySystem
ContainmentQueryService
ReachabilityService
InventoryCapacitySystem
EquipmentIntegritySystem
OwnershipPolicySystem
ReservationSystem
RoomGraphService
PathfindingService
```

### Actions

```text
take
drop
put
hold
unhold
wear
remove
inspect
look
open
close
lock
unlock
```

### Events

```text
EntityContainedEvent
EntityRemovedFromContainerEvent
ItemTakenEvent
ItemDroppedEvent
ItemPutEvent
ItemHeldEvent
ItemWornEvent
ContainerOpenedEvent
ContainerClosedEvent
OwnershipChangedEvent
ReservationCreatedEvent
ReservationExpiredEvent
```

---

## 1.3 Time, tick, calendar, and scheduling

### Mechanics

The game is asynchronous and real-time-ish, not fixed-turn. Characters regenerate points over real time. Simulation systems run before queued character commands.

### Components

```python
WorldInfoComponent
WorldClockComponent
CalendarComponent
TimeOfDayComponent
SeasonComponent
ScheduleComponent
CooldownComponent
InitiativeComponent
```

### Systems

```text
WorldClockSystem
CalendarSystem
TimeOfDaySystem
SeasonSystem
CooldownSystem
ScheduleSystem
InitiativeOrderingSystem
```

### Rules

```text
Simulation systems run before characters act.
Characters with executable queued commands act by InitiativeComponent.score.
Ties are randomized each tick.
One world-lane command per character per tick for MVP.
Focus-lane commands may run before that character’s world command.
```

### Events

```text
TickStartedEvent
TickCompletedEvent
DayChangedEvent
SeasonChangedEvent
ScheduleChangedEvent
CooldownExpiredEvent
InitiativeRolledEvent
```

---

## 1.4 Action and Focus points

### Mechanics

Two regenerated resources:

```text
Action = world-facing effort
Focus = private mental effort / memory / note / dialogue concentration
```

Both regenerate in real time for every character, regardless of controller. Suspended characters still regenerate but do not spend.

### Components

```python
ActionPointsComponent
FocusPointsComponent
PointRegenModifierComponent
PointOverflowModifierComponent
```

### Systems

```text
ActionPointRegenSystem
FocusPointRegenSystem
PointModifierSystem
PointOverflowSystem
```

### Command costs

```python
CommandCost(action=0, focus=0)
```

Examples:

```text
move: Action 1
take: Action 1
eat/drink: Action 1
say/tell: Action 1 + Focus 1
start/end conversation and conversation line: Focus 1
take note: Focus 1
remember/search notes: Focus 1
write on physical object: Action 1 + Focus 1
```

### Events

```text
ActionPointsChangedEvent
FocusPointsChangedEvent
PointRegenModifiedEvent
PointSpentEvent
PointOverflowGrantedEvent
```

---

## 1.5 Volatile command queues

### Mechanics

Players and LLMs can submit commands at any time.

If affordable and valid, the command runs on the next tick. If not affordable, the submitter can choose to queue or deny. Queued commands do not survive restart.

### Runtime classes

```python
QueuedCommand
CommandCost
CommandQueue
CommandLane
CommandExecutionResult
CommandValidator
CommandDispatcher
```

### Lanes

```text
Focus lane:
  start conversation
  conversation line
  end conversation
  take note
  remember/search
  private reflection tools

World lane:
  move
  take
  drop
  put
  use
  eat
  drink
  say
  tell
  write
  sleep
  wake
  fight
  craft
```

### Systems and services

```text
CommandIngestService
CommandQueueService
CommandValidationService
CommandExecutionService
CommandExpirationService
ControllerGenerationValidator
```

### Events

```text
CommandSubmittedEvent
CommandQueuedEvent
CommandDeniedEvent
CommandAcceptedEvent
CommandRejectedEvent
CommandExpiredEvent
CommandExecutedEvent
CommandCancelledEvent
```

---

## 1.6 Controllers

### Mechanics

Characters persist. Controllers are replaceable.

Controllers:

```text
Discord human controller
LLM agent controller
Suspended/no-op controller
```

### Components

```python
DiscordControllerComponent
LLMControllerComponent
SuspendedControllerComponent
SuspendedComponent
ControlPolicyComponent
```

### Edges

```python
ControlledBy
DefaultController
```

### Systems and services

```text
ControllerRegistry
ControllerHandoffService
ControllerGenerationService
DiscordControllerProvider
LLMControllerProvider
SuspendedControllerProvider
ControllerPromptDispatchService
```

### Rules

```text
Every controller change increments generation.
Stale commands are rejected.
Suspended controller submits no commands.
Suspended characters are ignored by most harmful systems.
Suspended characters regenerate Action and Focus.
```

### Events

```text
ControllerChangedEvent
CharacterTakenOverEvent
CharacterReleasedToLLMEvent
CharacterSuspendedEvent
CharacterResumedEvent
StaleControllerCommandRejectedEvent
```

---

## 1.7 Prompt generation

### Mechanics

Humans and LLM agents receive the same foundation prompt.

Prompt sections:

```text
who you are
where you are
what you can see
what you are holding/wearing/carrying
what you feel
recent context
private notes/memory recall
available actions
Action/Focus points
important boundaries/policies
```

### Components

```python
PromptProfileComponent
PromptDirtyComponent
RecentContextComponent
MemoryProfileComponent
RoomSummaryComponent
WorldHistoryRecordComponent
PhysicalMarkComponent
CreatorSignatureComponent
DeedReputationComponent
```

### Services and projections

```text
PromptContextBuilder
PromptPartRegistry
PromptRenderer
RoomSummaryProjection
InventoryPromptPart
NeedPromptPart
AffectPromptPart
MemoryPromptPart
WorldHistoryPromptPart
PhysicalMarkPromptPart
CreatorSignaturePromptPart
DeedReputationPromptPart
ActionPromptPart
PolicyPromptPart
```

### Events

```text
PromptContextBuiltEvent
PromptRenderedEvent
PromptDeliveredEvent
PromptDirtyEvent
PromptFailedEvent
```

---

## 1.8 Typed events and event bus

### Mechanics

Events are typed models, not arbitrary blobs. They drive projections, memory, notifications, LLM context, and debugging.
Notable events can also project into durable world-history ECS records, which are shared
state rather than private memory or narration-only prose.

### Base classes

```python
DomainEvent
EventVisibility
EventBus
EventHandler
EventProjection
```

### Visibility

```text
PUBLIC
ROOM
DIRECTED
PRIVATE
SYSTEM
```

### Common events

```text
ActorMovedEvent
SpeechSaidEvent
SpeechToldEvent
ItemTakenEvent
FoodEatenEvent
DrinkConsumedEvent
NeedChangedEvent
ThoughtCreatedEvent
AffectChangedEvent
NoteTakenEvent
MemoryWrittenEvent
RoomSummaryInvalidatedEvent
```

### Services

```text
TypedEventBus
EventPersistenceService
EventVisibilityService
EventReplayService
EventSubscriptionRegistry
WorldHistoryReactor
```

### Durable history state

```python
WorldHistoryRecordComponent
PhysicalMarkComponent
CreatorSignatureComponent
DeedReputationComponent
DeathConsequenceComponent
HistoryActor
HistoryTarget
MarkOn
CreatedBy
DeathOf
```

World history records store a concise summary, source event id, event type, epoch,
location id, tags, and salience. Actor and target links are ECS edges so later mechanics
can inspect who did what and which artifacts or characters were involved. Current
recorded sources include physical writing, crafting, and death. Physical marks are
separate mark entities linked with `MarkOn`, so one object can accumulate several visible
marks without duplicate components. Crafted outputs and authored mark entities carry
`CreatorSignatureComponent` and `CreatedBy` links so visible artifacts can expose maker
and circumstance. History records also project `DeedReputationComponent` onto actors, so
later services, guards, and dialogue can react to explicit deed tags and scores. Death
events also create `DeathConsequenceComponent` entities linked with `DeathOf`; prompt
rendering presents those entities as visible consequences while lifecycle systems remain
responsible for deciding whether a character actually died.

---

## 1.9 Plugins and extension units

A plugin is a loadable extension bundle. A mechanic is only one possible thing a plugin can provide.

### Plugin contribution types

```text
ECS components
ECS edges
ECS systems
commands
actions
typed events
controllers
generators
integrations
projections
prompt parts
policy tags
world defaults
configuration schemas
```

### Classes

```python
Plugin
EcsContribution
CommandContribution
RuntimeContribution
ContentContribution
PolicyContribution
PluginRegistry
PluginLoader
PluginDependencyResolver
PluginConfig
```

### CLI shape

```bash
bunnyland serve \
  --plugin vendor.foo \
  --plugin vendor.bar
```

### Events

```text
PluginLoadedEvent
PluginEnabledEvent
PluginDisabledEvent
PluginDependencyFailedEvent
```

---

# 2. `lifesim` package — The Sims-inspired mechanics

The Sims replay loop is character life: needs, emotions, whims, traits, skills, careers, relationships, homes, family, autonomy, aspirations, and emergent social drama. EA’s own materials describe emotions producing emotional whims, skills unlocking actions/rewards, careers using skills, and expansion systems around aspirations, hobbies, businesses, and life stages. ([Electronic Arts Inc.][1])

## 2.1 Character creation and identity

### Mechanics

```text
species
appearance
pronouns
voice/style
biography
traits
aspirations
preferences
life stage
household
family tree
romance boundaries
personal inventory
private notes
public reputation
```

### Components

```python
CharacterComponent
AppearanceComponent
PronounComponent
VoiceStyleComponent
BiographyComponent
LifeStageComponent
TraitSetComponent
PreferenceComponent
AspirationComponent
HouseholdComponent
FamilyProfileComponent
ReputationComponent
CharacterBoundaryComponent
```

### Systems

```text
CharacterCreationSystem
TraitInitializationSystem
PreferenceInitializationSystem
LifeStageSystem
ReputationSystem
HouseholdMembershipSystem
FamilyTreeSystem
BoundaryPolicySystem
```

### Actions

```text
set preference
choose aspiration
join household
leave household
introduce self
describe self
```

### Events

```text
CharacterCreatedEvent
TraitAddedEvent
PreferenceChangedEvent
AspirationChosenEvent
LifeStageChangedEvent
HouseholdJoinedEvent
BoundaryChangedEvent
```

---

## 2.2 Needs

**Implemented status:** Core Verbs owns `RestingComponent`, timed sleep state, `rest`,
`sleep`, `wake`, lifecycle events, and interruption. Lifesim integrates fatigue recovery
at 4/hour while resting and 12/hour while sleeping. Both recovery states relieve stress;
home sleep remains the only source of Lifesim's well-rested learning bonus. Hygiene,
comfort, and the other daily needs keep their existing independent recovery actions.

### Mechanics

Core life needs:

```text
hunger
thirst
sleep/fatigue
hygiene
comfort
social
fun/boredom
bladder/elimination, optional
environmental comfort
privacy
safety
```

Hunger and thirst remain separate. They are fed differently and affected by different environmental conditions.

### Components

```python
HungerComponent
ThirstComponent
SleepNeedComponent
SleepingComponent
RestingComponent
HygieneComponent
ComfortNeedComponent
SocialNeedComponent
FunNeedComponent
PrivacyNeedComponent
SafetyNeedComponent
```

### Shared value classes

```python
Meter
ThresholdBand
NeedModifier
NeedDecayRate
```

### Systems

```text
HungerSystem
ThirstSystem
SleepNeedSystem
SleepingSystem
HygieneSystem
ComfortNeedSystem
SocialNeedSystem
FunNeedSystem
PrivacyNeedSystem
SafetyNeedSystem
NeedThresholdSystem
NeedPromptSystem
```

### Actions

```text
eat
drink
sleep
wake
bathe
wash
clean self
talk
play
relax
rest
seek privacy
seek safety
```

### Events

```text
HungerChangedEvent
ThirstChangedEvent
SleepChangedEvent
RestStartedEvent
RestEndedEvent
SleepStartedEvent
CharacterFellAsleepEvent
CharacterWokeEvent
HygieneChangedEvent
ComfortChangedEvent
LonelinessChangedEvent
BoredomChangedEvent
NeedBecameUrgentEvent
NeedCrisisEvent
```

---

## 2.3 Affect, moodlets, thoughts, and emotions

### Mechanics

Mood is multidimensional, not a single number.

Use affect vectors:

```text
valence
arousal
stress
fear
anger
sadness
confidence
sociability
curiosity
focus
```

Thoughts/moodlets apply temporary or lasting affect deltas.

### Components

```python
AffectComponent
ThoughtComponent
MoodletComponent
StressComponent
EmotionLabelComponent
SentimentComponent
```

### Value classes

```python
AffectVector
AffectDelta
EmotionBand
MoodletDuration
```

### Systems

```text
ThoughtCreationSystem
ThoughtDecaySystem
AffectAggregationSystem
EmotionLabelSystem
StressResponseSystem
SentimentSystem
MoodPromptSystem
```

### Mechanics examples

```text
Ate good meal -> valence up, stress down
Was insulted -> anger up, sadness up
Very thirsty -> stress up, focus down
Slept badly -> valence down, focus down
Beautiful room -> valence up, curiosity up
Friend died -> sadness/stress spike
```

### Events

```text
ThoughtCreatedEvent
ThoughtExpiredEvent
AffectChangedEvent
EmotionLabelChangedEvent
StressThresholdCrossedEvent
SentimentFormedEvent
```

---

## 2.4 Wants, whims, goals, and aspirations

### Mechanics

Characters generate desires from:

```text
current needs
current affect
traits
aspirations
relationships
environment
recent events
memories
```

### Components

```python
GoalComponent
WhimComponent
AspirationComponent
MilestoneComponent
DesireComponent
```

### Systems

```text
WhimGenerationSystem
GoalScoringSystem
AspirationProgressSystem
MilestoneSystem
AutonomousDesireSystem
GoalPromptSystem
```

### Actions

```text
pin goal
abandon goal
pursue goal
reflect on goal
complete milestone
```

### Events

```text
WhimGeneratedEvent
GoalAddedEvent
GoalCompletedEvent
AspirationProgressedEvent
MilestoneCompletedEvent
```

---

## 2.5 Social relationships

### Mechanics

Relationships are not one number.

Track:

```text
familiarity
affinity
trust
respect
fear
resentment
attraction
loyalty
jealousy
debt
kinship
romance status
friendship status
rivalry status
```

### Components and edges

```python
SocialBond
GossipClaimComponent
ObligationComponent
RelationshipStatusComponent
ConversationComponent
ConversationParticipant
RomanceStatusComponent
FamilyRelationshipComponent
JealousyComponent
SocialMemoryComponent
KnowsGossip
ObligationDebtor
ObligationCreditor
```

Speech with `promise`, `offer`, `request`, or `threat` intent creates explicit obligation
state. Promises, offers, and threats make the speaker the debtor; requests make the
listener the debtor. `resolve-obligation` marks an obligation fulfilled, failed, or
canceled. Fulfillment raises trust/affinity toward the debtor; failure lowers trust and
adds resentment. Open obligations are prompt-visible ECS state, not inferred from memory
text.

### Systems

```text
ConversationSystem
RelationshipUpdateSystem
SocialBondDecaySystem
SocialInterpretationSystem
JealousySystem
GossipSystem
ReputationSystem
SocialMemorySystem
```

### Actions

Dialogue covers most social behavior:

```text
say
tell
start-conversation
conversation-line
end-conversation
```

Focused conversations allow one active thread per participant. They end explicitly by
default; callers may request a positive world-time timeout, and terminal conversation
entities are removed by the indexed core-verbs lifecycle consequence.

Speech intent can be:

```text
neutral
inform
question
request
offer
joke
insult
threat
comfort
apology
praise
flirt
confession
promise
gossip
```

### Events

```text
SpeechSaidEvent
SpeechToldEvent
ConversationStartedEvent
ConversationLineEvent
ConversationEndedEvent
DialogueActInterpretedEvent
RelationshipChangedEvent
FriendshipFormedEvent
RivalryFormedEvent
RomanceProposedEvent
RomanceAcceptedEvent
RomanceRejectedEvent
GossipSpreadEvent
```

---

## 2.6 Romance, adult boundaries, pregnancy, and family

### Mechanics

These are policy-gated life-sim systems.

Flirting is not a separate action. It is dialogue with `intent=flirt`.

Pregnancy and family changes require validated events/actions, not freeform LLM narration.

### Components

```python
CharacterBoundaryComponent
RomanceStatusComponent
LifeStageComponent
ReproductiveComponent
PregnancyComponent
BirthDueComponent
InheritanceRecordComponent
ParentOf
ChildOf
PartnerOf
HouseholdComponent
InheritedFrom
```

Death events can project inheritance through lifesim family and household state. The
first living child inherits, then a living partner, then a living household member. The
projection moves existing explicit ownership state rather than duplicating it: inventory
`Contains` links, `HouseholdFundsComponent` balance, `OwnsHome` ownership,
`ClaimsRoom` claims, `OwnsBusiness`, colony `Owns`, and dagger-sim
`OwnsProperty`/`PropertyDeedComponent` when those packs are present. An
`InheritanceRecordComponent` entity and `InheritedFrom` edge preserve the lineage/audit
trail for prompts and persistence.

### Policy tags

```text
flirting
romance
adult
pregnancy
```

### Systems

```text
RomanceBoundarySystem
RomanceProgressionSystem
ConsentPolicySystem
PregnancyPrerequisiteSystem
PregnancyProgressionSystem
BirthEventSystem
FamilyTreeSystem
HouseholdUpdateSystem
```

### Rules

```text
Denied boundary tags always win.
Admins cannot override boundaries.
Pregnancy cannot begin while a participant is suspended.
Pregnancy can progress while suspended.
Birth is deferred until the relevant character becomes active.
```

### Events

```text
BoundaryChangedEvent
RomanceStateChangedEvent
PregnancyStartedEvent
PregnancyProgressedEvent
BirthDueEvent
BirthOccurredEvent
FamilyRelationshipCreatedEvent
```

---

## 2.7 Skills, hobbies, and learning

### Mechanics

Skills improve by use and unlock new actions, better results, recipes, or prompt options.

Skill categories:

```text
cooking
gardening
fishing
foraging
handiness
crafting
social/charisma
music
art
writing
programming
magic
combat
medicine
animal care
research
construction
mining
alchemy
stealth
```

### Components

```python
SkillSetComponent
SkillXPComponent
SkillAffinityComponent
PassionComponent
TrainingComponent
MentorshipComponent
```

### Systems

```text
SkillXPSystem
SkillLevelUpSystem
SkillUnlockSystem
MentorshipSystem
PracticeSystem
SkillPromptSystem
```

### Actions

```text
practice
study
mentor
take class
read skill book
craft
repair
cook
garden
perform
research
```

### Events

```text
SkillXPChangedEvent
SkillLeveledEvent
SkillUnlockedActionEvent
MentorshipStartedEvent
TrainingCompletedEvent
```

---

## 2.8 Careers, jobs, businesses, and money

### Mechanics

The Sims-like version is character-centered: jobs, shifts, promotions, paychecks, businesses, customers, hobbies becoming income.

### Components

```python
CareerComponent
JobScheduleComponent
PromotionProgressComponent
IncomeComponent
BusinessOwnerComponent
BusinessInventoryComponent
CustomerComponent
BillComponent
HouseholdFundsComponent
```

### Systems

```text
CareerScheduleSystem
JobPerformanceSystem
PromotionSystem
PaycheckSystem
BusinessCustomerSystem
BusinessSalesSystem
BillsSystem
HouseholdEconomySystem
```

### Actions

```text
find job
go to work
work from home
quit job
open business
sell item
set price
pay bills
promote business
```

### Events

```text
CareerStartedEvent
WorkShiftStartedEvent
WorkShiftCompletedEvent
PromotionEarnedEvent
PaycheckReceivedEvent
BusinessSaleEvent
BillDueEvent
BillPaidEvent
```

---

## 2.9 Homes, rooms, decor, and object affordances

### Mechanics

Rooms affect comfort, mood, privacy, safety, social interactions, and available actions.

### Components

```python
OwnsHome
ClaimsRoom
RoomRoleComponent
RoomQualityComponent
DecorComponent
CleanlinessComponent
ComfortComponent
PrivacyComponent
FurnitureComponent
ApplianceComponent
AffordanceComponent
```

### Systems

```text
RoomRoleDetectionSystem
RoomQualitySystem
DecorAffectSystem
ComfortSystem
CleanlinessSystem
ObjectAffordanceSystem
HomeOwnershipSystem
```

### Actions

```text
sit
sleep in bed
cook at stove
wash at sink
clean room
decorate
repair object
upgrade object
claim room
invite over
```

### Events

```text
RoomRoleChangedEvent
RoomQualityChangedEvent
ObjectUsedEvent
ObjectBrokenEvent
ObjectRepairedEvent
RoomCleanedEvent
HomeClaimedEvent
```

---

# 3. `colonysim` package — RimWorld-inspired mechanics

RimWorld’s core replayability comes from pawn individuality, needs, thoughts, work priorities, rooms, production chains, incidents, injuries, medicine, relationships, and storyteller pressure. RimWorld’s own materials describe managing moods, needs, wounds, illnesses, addictions, social bonds, relationships, prosthetics, combat threats, and an AI storyteller that creates raids, resource drops, and other events. ([Steam Store][2])

## 3.1 Colonist pawn profile

### Mechanics

```text
skills
passions
traits
backstory
incapable work types
health conditions
relationships
expectations
ideology/beliefs
work priorities
schedule
allowed areas
```

### Components

```python
ColonistComponent
BackstoryComponent
WorkCapabilityComponent
PassionComponent
ExpectationComponent
AllowedAreaComponent
WorkPriorityComponent
PawnProfileComponent
```

### Systems

```text
ColonistInitializationSystem
WorkCapabilitySystem
ExpectationSystem
AllowedAreaSystem
PawnPromptSystem
```

---

## 3.2 Work priorities and jobs

### Mechanics

RimWorld-style work is one of the most important systems for emergence.

### Components

```python
WorkTypeComponent
WorkPriorityComponent
JobComponent
TaskComponent
BillComponent
WorkGiverComponent
AssignableComponent
ReservationComponent
```

### Systems

```text
JobDiscoverySystem
JobScoringSystem
WorkPrioritySystem
JobAssignmentSystem
ReservationSystem
TaskProgressSystem
BillGenerationSystem
WorkInterruptSystem
```

### Work types

```text
doctor
patient
bed rest
basic
warden
handle animals
cook
hunt
construct
grow
mine
plant cut
smith
tailor
art
craft
haul
clean
research
recreation
social
guard
repair
```

### Actions

```text
prioritize work
assign work type
create bill
cancel job
reserve target
haul item
clean room
construct object
repair object
research topic
```

### Events

```text
JobCreatedEvent
JobAssignedEvent
JobStartedEvent
JobInterruptedEvent
JobCompletedEvent
BillCreatedEvent
ReservationFailedEvent
WorkPriorityChangedEvent
```

---

## 3.3 Stockpiles, storage, logistics, and hauling

### Mechanics

This is the hidden backbone of colony games.

### Components

```python
StockpileComponent
StorageFilterComponent
HaulableComponent
HaulingJobComponent
ForbiddenComponent
PreferredStorageComponent
StackComponent
SpoilageComponent
```

### Systems

```text
StockpileDiscoverySystem
StorageFilterSystem
HaulingJobSystem
StackMergeSystem
ItemForbiddenSystem
SpoilageSystem
StorageOverflowSystem
```

### Actions

```text
create stockpile
set storage filter
forbid item
allow item
haul item
split stack
merge stack
```

### Events

```text
StockpileCreatedEvent
StorageFilterChangedEvent
ItemHauledEvent
StackMergedEvent
ItemForbiddenEvent
ItemSpoiledEvent
```

---

## 3.4 Rooms, beauty, cleanliness, comfort, impressiveness

### Mechanics

Rooms should affect mood, work speed, rest quality, social opportunities, and disease.

### Components

```python
RoomRoleComponent
RoomQualityComponent
BeautyComponent
CleanlinessComponent
SpaceComponent
WealthComponent
ComfortComponent
LightingComponent
TemperatureComponent
```

### Systems

```text
RoomRoleSystem
RoomQualityAggregationSystem
BeautySystem
CleanlinessSystem
ComfortSystem
ImpressivenessSystem
RoomThoughtSystem
```

### Room roles

```text
bedroom
barracks
dining room
rec room
kitchen
workshop
hospital
prison cell
storage
temple
library
tavern
nursery
throne room
```

### Events

```text
RoomRoleChangedEvent
RoomQualityChangedEvent
RoomBecameImpressiveEvent
RoomBecameFilthyEvent
RoomThoughtCreatedEvent
```

---

## 3.5 Mental breaks and inspirations

### Mechanics

Low affect/stress can trigger breaks; high mood can trigger inspirations.

RimWorld uses mood thresholds for mental breaks, and high mood can produce inspirations. ([RimWorld Wiki][3])

### Components

```python
MentalBreakRiskComponent
MentalStateComponent
InspirationComponent
ImpulseComponent
BreakThresholdComponent
```

### Systems

```text
MentalBreakRiskSystem
MentalBreakTriggerSystem
MentalStateSystem
InspirationTriggerSystem
InspirationExpirationSystem
RecoverySystem
```

### Break types

```text
hide in room
sad wander
food binge
insult spree
social fight
tantrum
item destruction
run wild
berserk
catatonic breakdown
crisis of belief
```

### Inspiration types

```text
inspired creativity
inspired trade
inspired recruitment
inspired surgery
inspired crafting
inspired taming
inspired research
```

### Events

```text
MentalBreakRiskChangedEvent
MentalBreakStartedEvent
MentalBreakEndedEvent
InspirationStartedEvent
InspirationUsedEvent
InspirationExpiredEvent
```

---

## 3.6 Health, injuries, medicine, and surgery

### Mechanics

```text
body parts
wounds
bleeding
pain
infection
disease
immunity
medicine quality
bed rest
doctor skill
surgery
prosthetics
bionics
organ replacement
scars
permanent injuries
```

### Components

```python
HealthComponent
BodyPlanComponent
BodyPartComponent
InjuryComponent
BleedingComponent
PainComponent
DiseaseComponent
ImmunityComponent
MedicineComponent
TreatmentComponent
SurgeryComponent
ProstheticComponent
```

### Systems

```text
HealthTickSystem
BleedingSystem
PainSystem
DiseaseProgressionSystem
ImmunitySystem
TreatmentSystem
SurgerySystem
RestHealingSystem
PermanentInjurySystem
DownedSystem
DeathSystem
```

### Actions

```text
tend wound
rescue
carry to bed
operate
administer medicine
install prosthetic
remove body part
diagnose
```

### Events

```text
InjuryAddedEvent
BleedingChangedEvent
DiseaseStartedEvent
DiseaseRecoveredEvent
TreatmentAppliedEvent
SurgerySucceededEvent
SurgeryFailedEvent
CharacterDownedEvent
CharacterDiedEvent
CharacterRevivedEvent
```

---

## 3.7 Prisoners, recruitment, and captivity

### Mechanics

Useful for colony drama, but policy-sensitive.

```text
capture
imprison
warden work
conversation
resistance
recruitment
release
ransom
conversion
escape
```

### Components

```python
PrisonerComponent
CaptiveComponent
CellComponent
ResistanceComponent
RecruitmentProgressComponent
WardenAssignmentComponent
EscapeRiskComponent
```

### Systems

```text
CaptureSystem
PrisonerNeedsSystem
WardenInteractionSystem
ResistanceDecaySystem
RecruitmentSystem
EscapeAttemptSystem
PrisonerPolicySystem
```

### Actions

```text
capture
imprison
release
recruit
convert
feed prisoner
talk to prisoner
```

### Events

```text
CharacterCapturedEvent
PrisonerEscapedEvent
RecruitmentProgressedEvent
PrisonerRecruitedEvent
PrisonerReleasedEvent
```

---

## 3.8 Research and technology

### Mechanics

Research unlocks recipes, rooms, equipment, defenses, crops, medicine, magic, and travel.

### Components

```python
ResearchProjectComponent
ResearchProgressComponent
TechLevelComponent
ResearchBenchComponent
KnowledgeComponent
UnlockedRecipeComponent
```

### Systems

```text
ResearchJobSystem
ResearchProgressSystem
ResearchUnlockSystem
TechPrerequisiteSystem
KnowledgeDiffusionSystem
```

### Actions

```text
select research
research
read technical book
teach technology
```

### Events

```text
ResearchStartedEvent
ResearchProgressedEvent
ResearchCompletedEvent
TechnologyUnlockedEvent
```

---

## 3.9 Storyteller and incidents

### Mechanics

This is one of the biggest replayability engines.

### Components

```python
StorytellerComponent
IncidentBudgetComponent
ThreatPointsComponent
ColonyWealthComponent
TensionComponent
IncidentHistoryComponent
```

### Systems

```text
ThreatBudgetSystem
ColonyWealthSystem
TensionCurveSystem
IncidentSelectionSystem
IncidentProposalSystem
IncidentResolutionSystem
IncidentCooldownSystem
```

### Incidents

```text
raid
animal attack
disease outbreak
meteorite
resource drop
wanderer joins
trader arrives
refugee request
storm
fire
food poisoning
crop blight
mad animal
psychic drone
eclipse
cold snap
heat wave
siege
infestation
ancient danger
relationship drama
birth
death
betrayal
```

### Events

```text
IncidentProposedEvent
IncidentStartedEvent
IncidentResolvedEvent
RaidStartedEvent
TraderArrivedEvent
DiseaseOutbreakEvent
WeatherIncidentEvent
```

---

## 3.10 Factions, trade, caravans, and diplomacy

### Mechanics

```text
faction relations
visitors
traders
caravans
gifts
raids
alliances
reputation
world map travel
settlement visits
quests
```

### Components

```python
FactionComponent
FactionRelationComponent
TraderComponent
TradeInventoryComponent
CaravanComponent
WorldMapLocationComponent
DiplomacyComponent
```

### Systems

```text
FactionRelationSystem
TraderArrivalSystem
TradeSystem
CaravanAssemblySystem
CaravanTravelSystem
DiplomacySystem
WorldMapEventSystem
```

### Actions

```text
trade
gift item
form caravan
travel
visit settlement
attack settlement
negotiate
request aid
```

### Events

```text
FactionRelationChangedEvent
TraderArrivedEvent
TradeCompletedEvent
CaravanFormedEvent
CaravanArrivedEvent
DiplomacyChangedEvent
```

---

# 4. `barbariansim` package — Conan Exiles-inspired mechanics

Conan Exiles contributes harsh survival, hunger/thirst, heat/cold, stamina, climbing, building, crafting, thralls, pets, mounts, PvP, sieges, purges, dungeons, religion/sorcery, and world bosses. Official Conan materials list survival, building, thralls, defenses, siege weapons, mounts, pet systems, climbing, combat, purges, farming, dynamic weather, dyes, journeys, perks, fast travel, world bosses, and storyline; the wiki also calls out temperature, hunger, thirst, cold/heat status effects, and weather impacts. ([conanexiles.com][4])

## 4.1 Harsh survival

**Implemented status:** Environment owns the single canonical `ShelterComponent` and its
temperature, rain, and wind resolver. Barbariansim re-exports that exact type for source
compatibility and implements private character wetness with dry, damp, wet, and soaked
states. The generation capability `bunnyland.barbariansim.shelter` remains advertised but
is deprecated and normalized to `bunnyland.environment.shelter`.

Barbariansim's stamina integration doubles normal regeneration while the shared Core
Verbs rest or sleep state is active. It does not own a separate rest action.

Flooding/drainage, structural weather damage, weather-driven room temperatures, and
wetness-driven cold penalties remain future work. There is no separate shelter marker,
clothing system, or `seek-shelter` action.

### Mechanics

```text
hunger
thirst
stamina
exposure
heat
cold
wetness
shelter
rest
food spoilage
poison
disease
corruption
encumbrance
```

### Components

```python
HungerComponent
ThirstComponent
StaminaComponent
TemperatureExposureComponent
HeatResistanceComponent
ColdResistanceComponent
WetnessComponent
ShelterComponent
EncumbranceComponent
CorruptionComponent
PoisonComponent
```

### Systems

```text
HungerSystem
ThirstSystem
StaminaRegenSystem
TemperatureExposureSystem
HeatStatusSystem
ColdStatusSystem
WetnessConsequence
resolve_shelter_protection
EncumbranceSystem
CorruptionSystem
PoisonSystem
```

### Actions

```text
eat
drink
rest
light fire
wear clothing
remove clothing
cool down
warm up
cleanse corruption
```

### Events

```text
ExposureChangedEvent
WetnessChangedEvent
HeatstrokeStartedEvent
FrostbiteStartedEvent
StaminaChangedEvent
CharacterPoisonedEvent
CorruptionGainedEvent
CorruptionCleansedEvent
```

---

## 4.2 Combat

### Mechanics

```text
melee attacks
ranged attacks
blocking
dodging
stagger
armor
weapon durability
bleeding
cripple
knockback
downed state
execution, policy-gated
nonlethal combat
PvP flag
```

### Components

```python
CombatantComponent
WeaponComponent
ArmorComponent
ShieldComponent
AttackProfileComponent
DefenseProfileComponent
DamageTypeComponent
StatusEffectComponent
DownedComponent
DeadComponent
PvPPolicyComponent
```

### Systems

```text
CombatTargetingSystem
AttackResolutionSystem
DefenseResolutionSystem
DamageSystem
ArmorMitigationSystem
StaggerSystem
BleedingSystem
DownedSystem
DeathSystem
PvPPolicySystem
```

### Actions

```text
attack
block
dodge
shoot
throw
grapple
disarm
flee
aid downed character
execute, if policy allows
```

### Events

```text
AttackStartedEvent
AttackHitEvent
AttackMissedEvent
DamageAppliedEvent
BlockSucceededEvent
DodgeSucceededEvent
CharacterDownedEvent
CharacterRevivedEvent
CharacterDiedEvent
PvPActionBlockedEvent
```

---

## 4.3 Building, bases, and claim

### Mechanics

```text
place building pieces
walls
doors
foundations
roofs
crafting stations
decay
repair
ownership
clan/shared base
defense
traps
siege damage
claim radius
```

### Components

```python
BuildingPieceComponent
StructureComponent
StructuralSupportComponent
BaseClaimComponent
DecayComponent
RepairableComponent
TrapComponent
DoorComponent
LockableComponent
CraftingStationComponent
```

### Systems

```text
BuildPlacementSystem
StructuralSupportSystem
BaseClaimSystem
BuildingDecaySystem
RepairSystem
TrapTriggerSystem
SiegeDamageSystem
DoorLockSystem
```

### Actions

```text
build
demolish
repair
upgrade
open door
lock door
place trap
disarm trap
claim base
join clan base
```

### Events

```text
BuildingPlacedEvent
BuildingDestroyedEvent
StructureCollapsedEvent
BaseClaimedEvent
TrapTriggeredEvent
BuildingRepairedEvent
SiegeDamageAppliedEvent
```

---

## 4.4 Crafting and resource gathering

### Mechanics

```text
gather wood/stone/fiber/ore
tool quality
resource nodes
recipes
crafting stations
fuel
crafting time
durability
repair
item tiers
rare resources
```

### Components

```python
ResourceNodeComponent
HarvestableComponent
ToolComponent
RecipeComponent
CraftingStationComponent
CraftingTaskComponent
FuelComponent
DurabilityComponent
ItemTierComponent
```

### Systems

```text
ResourceRespawnSystem
HarvestSystem
ToolEfficiencySystem
CraftingTaskSystem
FuelConsumptionSystem
DurabilitySystem
RepairSystem
RecipeUnlockSystem
```

### Actions

```text
harvest
mine
chop
skin
craft
smelt
cook
repair
refine
dismantle
```

### Events

```text
ResourceHarvestedEvent
ResourceNodeDepletedEvent
CraftingStartedEvent
CraftingCompletedEvent
FuelConsumedEvent
ItemRepairedEvent
ItemBrokenEvent
```

---

## 4.5 Thralls, followers, pets, and mounts

### Mechanics

In bunnyland terms, this becomes followers, retainers, companions, workers, guards, and pets. Conan has combat/crafting thralls, pets, mounts, and base defense utility. ([Conan Exiles Wiki][5])

### Components

```python
FollowerComponent
ThrallComponent
PetComponent
MountComponent
TamingProgressComponent
TrainingComponent
FollowerOrdersComponent
GuardPostComponent
CompanionBondComponent
```

### Systems

```text
FollowerOrderSystem
TamingSystem
TrainingSystem
CompanionBondSystem
GuardPostSystem
MountTravelSystem
FollowerCombatSystem
FollowerWorkSystem
```

### Actions

```text
recruit follower
tame animal
train
assign guard post
order follow
order wait
mount
dismount
feed pet
dismiss follower
```

### Events

```text
FollowerRecruitedEvent
PetTamedEvent
FollowerOrderChangedEvent
MountRiddenEvent
FollowerDownedEvent
FollowerDiedEvent
```

---

## 4.6 Purges, raids, sieges

### Mechanics

```text
base threat accumulation
enemy waves
siege engines
traps
defense readiness
loot
thrall rescue/capture
damage to structures
PvP raid rules
```

### Components

```python
PurgeMeterComponent
RaidTargetComponent
SiegeWeaponComponent
DefenseRatingComponent
WaveSpawnerComponent
BaseWealthComponent
```

### Systems

```text
PurgeMeterSystem
RaidSelectionSystem
WaveSpawnSystem
SiegeResolutionSystem
DefenseRatingSystem
LootRewardSystem
```

### Actions

```text
prepare defenses
sound alarm
man siege weapon
repair under attack
surrender
counterattack
```

### Events

```text
PurgeWarningEvent
RaidStartedEvent
RaidWaveSpawnedEvent
SiegeWeaponFiredEvent
RaidResolvedEvent
BaseBreachedEvent
```

---

## 4.7 Religion, sorcery, corruption, and rituals

### Mechanics

Make this legally distinct: cults, spirits, relics, shrines, curses, offerings.

```text
shrines
faith/reputation with deity/spirit
offerings
rituals
sacrifices, policy-gated
corruption
curses
blessings
summons
forbidden knowledge
```

### Components

```python
FaithComponent
ShrineComponent
OfferingComponent
RitualComponent
CurseComponent
BlessingComponent
CorruptionComponent
ForbiddenKnowledgeComponent
```

### Systems

```text
OfferingSystem
RitualResolutionSystem
FaithProgressionSystem
CurseSystem
BlessingSystem
CorruptionSystem
ForbiddenKnowledgeSystem
```

### Actions

```text
pray
make offering
perform ritual
cleanse
curse
bless
study forbidden text
```

### Events

```text
OfferingMadeEvent
RitualStartedEvent
RitualCompletedEvent
BlessingGrantedEvent
CurseAppliedEvent
CorruptionChangedEvent
```

---

## 4.8 Exploration, climbing, dungeons, bosses

### Mechanics

```text
climbing
stamina-based traversal
dangerous zones
dungeons
boss arenas
loot chests
keys
ancient ruins
environmental hazards
```

### Components

```python
ClimbableComponent
TraversalCostComponent
DungeonComponent
BossComponent
KeyComponent
LockedRewardComponent
HazardComponent
DiscoveryComponent
```

### Systems

```text
TraversalSystem
ClimbingSystem
DungeonProgressSystem
BossEncounterSystem
LootChestSystem
HazardSystem
DiscoverySystem
```

### Actions

```text
climb
descend
enter dungeon
open chest
use key
fight boss
loot
disarm hazard
```

### Events

```text
DungeonEnteredEvent
BossAwakenedEvent
BossDefeatedEvent
TreasureOpenedEvent
HazardTriggeredEvent
LocationDiscoveredEvent
```

---

# 5. `gardensim` package — Stardew Valley-inspired mechanics

Stardew’s replayability comes from seasons, crop growth, watering, fertilizer, farming skill, animals, processing machines, villagers, gifting, friendship, heart events, festivals, fishing, mining, foraging, bundles/collections, and cozy economic optimization. The official wiki describes seasonal crops that grow from seeds and wither when seasons change, fertilizer types that affect quality/speed/water retention, skills with ten levels, villagers with routines and gifts, and friendship leading to heart events, gifts, and marriage. ([Stardew Valley Wiki][6])

## 5.1 Soil, fields, and crop growth

### Mechanics

```text
till soil
plant seeds
water crops
fertilize
growth stages
seasonal crops
multi-season crops
crop withering
crop quality
regrowth crops
giant crops, maybe
crop disease
pests
greenhouses
```

### Components

```python
SoilComponent
TilledComponent
WateredComponent
FertilizerComponent
SeedComponent
CropComponent
CropGrowthComponent
CropQualityComponent
HarvestableComponent
GreenhouseComponent
PestComponent
```

### Systems

```text
SoilMoistureSystem
CropGrowthSystem
CropWitheringSystem
FertilizerEffectSystem
CropQualitySystem
CropHarvestSystem
PestSystem
GreenhouseSystem
```

### Actions

```text
till
plant
water
fertilize
weed
harvest
clear dead crop
inspect crop
```

### Events

```text
SoilTilledEvent
SeedPlantedEvent
CropWateredEvent
CropGrewEvent
CropReadyEvent
CropWitheredEvent
CropHarvestedEvent
FertilizerAppliedEvent
```

---

## 5.2 Seasons, weather, and daily farm rhythm

### Mechanics

```text
spring/summer/fall/winter
rain
storms
snow
sunny days
crop calendars
seasonal forage
seasonal fish
festivals
shop schedules
daily reset
```

### Components

```python
SeasonComponent
WeatherComponent
DailyResetComponent
FestivalCalendarComponent
SeasonalAvailabilityComponent
```

### Systems

```text
SeasonSystem
WeatherSystem
DailyFarmResetSystem
FestivalCalendarSystem
SeasonalAvailabilitySystem
```

### Events

```text
SeasonChangedEvent
WeatherChangedEvent
RainStartedEvent
FestivalStartedEvent
DailyResetEvent
```

---

## 5.3 Farm animals

### Mechanics

```text
coops
barns
animal friendship
feeding
petting
mood
growth
animal products
pregnancy/breeding
sickness
quality of products
```

### Components

```python
FarmAnimalComponent
AnimalHomeComponent
AnimalFriendshipComponent
AnimalMoodComponent
FeedComponent
AnimalProductComponent
BreedingComponent
```

### Systems

```text
AnimalFeedingSystem
AnimalFriendshipSystem
AnimalMoodSystem
AnimalProductSystem
AnimalGrowthSystem
AnimalBreedingSystem
AnimalSicknessSystem
```

### Actions

```text
feed animal
pet animal
milk
shear
collect egg
open coop
close coop
assign animal home
```

### Events

```text
AnimalFedEvent
AnimalPettedEvent
AnimalProductCreatedEvent
AnimalGrewEvent
AnimalBornEvent
AnimalSickEvent
```

---

## 5.4 Processing machines and artisan goods

### Mechanics

```text
keg
preserves jar
cheese press
mayonnaise machine
furnace
seed maker
loom
oil maker
dehydrator
aging barrel
```

### Components

```python
MachineComponent
ProcessingRecipeComponent
ProcessingTaskComponent
InputSlotComponent
OutputSlotComponent
QualityTransformComponent
```

### Systems

```text
MachineInputSystem
ProcessingProgressSystem
ProcessingCompletionSystem
QualityTransformSystem
MachineBreakdownSystem
```

### Actions

```text
load machine
collect output
cancel processing
repair machine
```

### Events

```text
MachineLoadedEvent
ProcessingStartedEvent
ProcessingCompletedEvent
ProductCollectedEvent
MachineBrokeEvent
```

---

## 5.5 Fishing

### Mechanics

```text
water bodies
fish availability by season/weather/time
bait
tackle
fish difficulty
treasure
trash
legendary fish
fish quality
```

### Components

```python
FishingSpotComponent
FishPopulationComponent
BaitComponent
TackleComponent
FishComponent
FishingSkillComponent
FishingAttemptComponent
```

### Systems

```text
FishAvailabilitySystem
FishingAttemptSystem
FishingDifficultySystem
CatchResolutionSystem
TreasureCatchSystem
FishQualitySystem
```

### Actions

```text
cast line
use bait
use tackle
catch fish
release fish
```

### Events

```text
FishingStartedEvent
FishHookedEvent
FishCaughtEvent
FishEscapedEvent
TreasureCaughtEvent
```

---

## 5.6 Mining and caves

### Mechanics

```text
mine levels
ore nodes
gem nodes
rocks
ladders
monsters
elevator/checkpoints
treasure rooms
geodes
stamina cost
tool upgrades
```

### Components

```python
MineLevelComponent
OreNodeComponent
GemNodeComponent
RockComponent
LadderComponent
MineMonsterComponent
GeodeComponent
ToolTierComponent
```

### Systems

```text
MineGenerationSystem
OreSpawnSystem
MiningSystem
LadderDiscoverySystem
MineCombatSystem
GeodeSystem
MineResetSystem
```

### Actions

```text
mine rock
break ore
descend ladder
use elevator
open geode
fight monster
```

### Events

```text
RockBrokenEvent
OreMinedEvent
LadderFoundEvent
MineLevelEnteredEvent
GeodeFoundEvent
GeodeOpenedEvent
```

---

## 5.7 Villagers, gifts, heart events, and festivals

### Mechanics

```text
villager schedules
gift preferences
friendship points
heart thresholds
heart events
dating
marriage
jealousy
festivals
mail gifts
quests
```

### Components

```python
VillagerComponent
DailyRoutineComponent
GiftPreferenceComponent
FriendshipComponent
HeartEventComponent
FestivalComponent
MailComponent
QuestGiverComponent
```

### Systems

```text
VillagerScheduleSystem
GiftReactionSystem
FriendshipSystem
HeartEventTriggerSystem
FestivalSystem
MailSystem
QuestGenerationSystem
```

### Actions

```text
give gift
talk
ask out
propose
attend festival
read mail
accept quest
complete quest
```

### Events

```text
GiftGivenEvent
GiftReactionEvent
FriendshipChangedEvent
HeartEventStartedEvent
FestivalStartedEvent
MailReceivedEvent
QuestAcceptedEvent
QuestCompletedEvent
```

---

## 5.8 Collections, bundles, museum, shipping

### Mechanics

```text
shipping bin
daily sales
collections
bundle completion
museum donations
achievements
completion goals
```

### Components

```python
CollectionComponent
BundleComponent
DonationComponent
ShippingBinComponent
DailySalesComponent
CompletionTrackerComponent
```

### Systems

```text
ShippingSystem
CollectionTrackingSystem
BundleCompletionSystem
MuseumDonationSystem
AchievementSystem
DailyProfitSystem
```

### Actions

```text
ship item
donate item
complete bundle
view collection
claim reward
```

### Events

```text
ItemShippedEvent
DailySalesResolvedEvent
BundleCompletedEvent
DonationMadeEvent
CollectionUpdatedEvent
RewardClaimedEvent
```

---

# 6. `dragonsim` package — Skyrim-inspired mechanics

Skyrim’s replayability comes from wandering, discovering locations, joining factions, following questlines, building skills by use, collecting loot, crafting, stealth, magic, followers, ancient beasts, voice powers, books/lore, and repeatable adventure quests. Bunnyland should keep those broad loops while using its own presentation: voice phrases can be written, painted, or carved on ordinary writable/carvable world objects instead of depending on a special fixed monument type. ([Wikipedia][7])

## 6.1 Open-world exploration and discovery

### Mechanics

```text
points of interest
roads
holds/regions
landmarks
dungeons
caves
ruins
shrines
encounters
fast travel
world map
rumors
```

### Components

```python
RegionComponent
PointOfInterestComponent
DiscoveryComponent
MapMarkerComponent
FastTravelComponent
RumorComponent
EncounterZoneComponent
```

### Systems

```text
DiscoverySystem
MapMarkerSystem
RumorGenerationSystem
EncounterZoneSystem
FastTravelSystem
ExplorationPromptSystem
```

### Actions

```text
travel
explore
inspect landmark
ask about rumors
fast travel
mark map
```

### Events

```text
LocationDiscoveredEvent
MapMarkerAddedEvent
RumorHeardEvent
EncounterTriggeredEvent
```

---

## 6.2 Quests and radiant objectives

### Mechanics

```text
main quests
side quests
misc objectives
radiant quests
quest stages
choices
rewards
branching outcomes
quest items
```

### Components

```python
QuestComponent
QuestStateComponent
QuestObjectiveComponent
QuestRewardComponent
QuestHasObjective
QuestHasReward
QuestItemComponent
RadiantQuestTemplateComponent
```

### Systems

```text
QuestProgressSystem
QuestObjectiveSystem
RadiantQuestGenerationSystem
QuestRewardSystem
QuestBranchingSystem
QuestPromptSystem
```

### Actions

```text
accept quest
track quest
complete objective
turn in quest
decline quest
choose branch
```

### Events

```text
QuestOfferedEvent
QuestAcceptedEvent
QuestObjectiveCompletedEvent
QuestStageChangedEvent
QuestCompletedEvent
QuestFailedEvent
```

---

## 6.3 Factions, reputation, and law

### Mechanics

```text
guilds
holds
jarl/government
faction rank
reputation
crime
bounty
guards
jail
forgiveness
civil conflict
```

### Components

```python
FactionComponent
FactionRankComponent
FactionReputationComponent
CrimeComponent
BountyComponent
LawRegionComponent
GuardComponent
JailComponent
```

### Systems

```text
FactionMembershipSystem
FactionReputationSystem
CrimeDetectionSystem
BountySystem
GuardResponseSystem
JailSystem
FactionQuestSystem
```

### Actions

```text
join faction
leave faction
pay bounty
serve jail time
bribe guard
persuade guard
report crime
```

### Events

```text
FactionJoinedEvent
FactionRankChangedEvent
CrimeReportedEvent
BountyAddedEvent
GuardConfrontedEvent
JailedEvent
BountyClearedEvent
```

---

## 6.4 Skill-by-use and perks

### Mechanics

```text
use skill to improve skill
level up
perk points
perk trees
build identity through action
```

### Components

```python
SkillUseComponent
SkillTreeComponent
PerkComponent
PerkPointComponent
CharacterLevelComponent
```

### Systems

```text
SkillUseXPSystem
CharacterLevelSystem
PerkUnlockSystem
PerkEffectSystem
BuildSuggestionSystem
```

### Skill trees

```text
one-handed
two-handed
archery
block
heavy armor
light armor
sneak
lockpicking
pickpocket, maybe disabled initially
speech
alchemy
smithing
enchanting
destruction
restoration
illusion
conjuration
alteration
```

### Actions

```text
spend perk point
practice skill
train with mentor
read skill book
```

### Events

```text
SkillUsedEvent
SkillLeveledEvent
CharacterLeveledEvent
PerkUnlockedEvent
```

---

## 6.5 Combat, stealth, and crime

### Mechanics

```text
melee
archery
magic combat
dual wield
blocking
power attacks
sneak
backstab
lockpicking
pickpocketing, not MVP
trespassing
theft
crime witnesses
```

### Components

```python
SneakingComponent
VisibilityComponent
LockpickComponent
LockDifficultyComponent
CrimeWitnessComponent
TrespassComponent
SneakAttackComponent
```

### Systems

```text
StealthSystem
SneakDetectionSystem
LockpickingSystem
CrimeWitnessSystem
TheftSystem
TrespassSystem
CombatSkillSystem
```

### Actions

```text
sneak
hide
pick lock
steal
pickpocket, disabled initially
backstab
surrender
```

### Events

```text
SneakStartedEvent
DetectedEvent
LockPickedEvent
TheftCommittedEvent
CrimeWitnessedEvent
TrespassDetectedEvent
```

---

## 6.6 Magic, enchantment, alchemy, and artifacts

### Mechanics

```text
spells
mana/magic
spell schools
enchanting
soul gems, adapted as spirit vessels
alchemy
potions
poisons
artifacts
curses
blessings
```

### Components

```python
MagicComponent
SpellComponent
SpellSchoolComponent
EnchantmentComponent
AlchemyIngredientComponent
PotionComponent
PoisonComponent
ArtifactComponent
SoulVesselComponent
```

### Systems

```text
MagicRegenSystem
SpellCastSystem
SpellEffectSystem
EnchantmentSystem
AlchemySystem
PotionEffectSystem
ArtifactPowerSystem
```

### Actions

```text
cast spell
learn spell
brew potion
apply poison
enchant item
identify artifact
use artifact
```

### Events

```text
SpellCastEvent
SpellLearnedEvent
PotionBrewedEvent
EnchantmentAppliedEvent
ArtifactDiscoveredEvent
```

---

## 6.7 Ancient beasts, voice phrases, and souls

### Mechanics

Legally distinct version:

```text
ancient beasts
voice powers
word fragments
soul/essence absorption
cooldowns
ranked power levels
world events
```

### Components

```python
AncientBeastComponent
VoicePowerComponent
WordOfPowerComponent
VoiceInscriptionComponent
CarvableComponent
EssenceComponent
VoicePowerCooldownComponent
GreatSoulComponent
```

### Systems

```text
AncientBeastEncounterSystem
VoiceInscriptionSystem
EssenceAbsorptionSystem
VoicePowerUnlockSystem
VoicePowerCooldownSystem
AncientBeastThreatSystem
```

### Actions

```text
inscribe voice phrase
study voice inscription
absorb essence
use voice power
fight ancient beast
negotiate with ancient beast
ride ancient beast, later
```

### Events

```text
DragonAppearedEvent
DragonDefeatedEvent
EssenceAbsorbedEvent
WordDiscoveredEvent
VoicePowerUnlockedEvent
VoicePowerUsedEvent
```

---

## 6.8 Books, lore, and world memory

### Mechanics

```text
readable books
skill books
journals
maps
letters
lore fragments
world histories
quest clues
```

### Components

```python
ReadableComponent
BookComponent
SkillBookComponent
MapComponent
LetterComponent
LoreFragmentComponent
```

### Systems

```text
ReadingSystem
SkillBookSystem
LoreIndexSystem
QuestClueSystem
MapReadingSystem
```

### Actions

```text
read
copy note
study
take map
quote book
```

### Events

```text
BookReadEvent
SkillBookConsumedEvent
LoreDiscoveredEvent
QuestClueFoundEvent
```

---

# 7. `daggersim` package — Daggerfall-inspired procedural RPG mechanics

`daggersim` is the procedural RPG expansion package. It builds on the generic
DM/worldgen infrastructure to support ongoing world expansion through rumors, travel,
procedural quests, institutions, settlements, dungeons, services, guilds, temples, banks,
laws, courts, property, class-making, spell-making, reputation, and supernatural
afflictions. Where `worldgen` creates validated content, `daggersim` defines why and when
that content should be created as part of gameplay.

The key boundary is:

```text
worldgen / DM system = generic content generation infrastructure
daggersim = procedural realm gameplay built on top of that infrastructure
```

## 7.1 Expandable world frontiers

### Mechanics

Not every town, dungeon, road, bank, or guildhall needs to exist in full ECS detail
immediately. `daggersim` introduces unrealized content: stubs that become full ECS content
when rumors, quests, travel, factions, incidents, or services need them.

```text
rumor
need for content
generated proposal
validation
ECS instantiation
quest/service/reputation hooks
player action
events and consequences
```

### Components

```python
ProceduralSiteComponent
UnrealizedLocationComponent
ExpansionHookComponent
```

### Systems and projections

```text
ExpansionNeedSystem
ProceduralSiteInstantiationSystem
GeneratedContentValidationSystem
WorldExpansionProjection
```

### Events

```text
ExpansionRequestedEvent
GeneratedContentProposedEvent
GeneratedContentValidatedEvent
GeneratedSiteInstantiatedEvent
GeneratedSiteRejectedEvent
```

## 7.2 Regions and settlements

### Mechanics

`daggersim` defines the civic map above the room graph:

```text
realm
region
province
city
town
village
hamlet
district
neighborhood
institution
road
wilderness route
```

### Components

```python
RealmComponent
RegionComponent
SettlementComponent
SettlementDistrictComponent
RoadNodeComponent
TravelHubComponent
CivicInstitutionComponent
ServiceDirectoryComponent
```

### Systems and generators

```text
RegionGenerator
SettlementGenerator
DistrictGenerator
RoadNetworkGenerator
ServiceDirectorySystem
SettlementExpansionSystem
```

### Actions

```text
ask directions
ask about services
travel to settlement
visit district
look for work
ask about rumors
```

## 7.3 Rumors as expansion seeds

### Mechanics

Rumors can be real, false, outdated, exaggerated, or generated into truth later. They are
asynchronous handles for unrealized content.

### Components

```python
RumorComponent
RumorSourceComponent
RumorReliabilityComponent
RumorTargetComponent
```

### Systems

```text
RumorGenerationSystem
RumorSpreadSystem
RumorValidationSystem
RumorToQuestSystem
RumorToSiteExpansionSystem
```

### Actions

```text
ask for rumors
spread rumor
investigate rumor
take note about rumor
verify rumor
```

### Events

```text
RumorCreatedEvent
RumorHeardEvent
RumorSpreadEvent
RumorVerifiedEvent
RumorDisprovenEvent
RumorBecameQuestEvent
```

## 7.4 Procedural quests

### Mechanics

Procedural quests are the main loop that turns the world into a generator of obligations.
They may require generated settlements, NPCs, dungeons, services, items, or routes.

```text
delivery
escort
retrieval
dungeon crawl
find person
rescue
debt collection
curse cure
monster hunt
investigation
legal errand
guild errand
pilgrimage
reputation repair
property dispute
lost heirloom
map fragment
```

Timed quests are allowed to fail. Failure should matter through reputation loss, rank
delay, legal trouble, relationship damage, debt, new rumors, rivals, or follow-up quests.

### Components

```python
QuestTemplateComponent
QuestComponent
QuestStateComponent
QuestGiverComponent
QuestObjectiveComponent
QuestRewardComponent
QuestFailureConsequenceComponent
QuestFactionComponent
QuestHasObjective
QuestHasReward
QuestAcceptedBy
TracksQuest
RequiresQuest
```

### Systems

```text
ProceduralQuestGenerator
QuestDeadlineSystem
QuestStageSystem
QuestFailureSystem
QuestRewardSystem
QuestDifficultyScaler
QuestTargetPlacementSystem
```

### Actions

```text
ask for work
accept quest
refuse quest
abandon quest
turn in quest
request extension
lie about completion
```

### Events

```text
QuestGeneratedEvent
QuestOfferedEvent
QuestAcceptedEvent
QuestDeadlineApproachingEvent
QuestCompletedEvent
QuestFailedEvent
QuestRewardGrantedEvent
```

## 7.5 Guilds, temples, orders, and services

### Mechanics

Institutions make the procedural realm useful.

```text
guild
temple
bank
court
noble house
knightly order
merchant league
underworld faction
academy
healer
inn
shop
archive
cartographer
```

### Components

```python
InstitutionComponent
GuildComponent
TempleComponent
OrderComponent
BankComponent
CourtComponent
InstitutionServiceComponent
MembershipComponent
InstitutionRankComponent
InstitutionRuleComponent
DuesComponent
```

### Systems

```text
InstitutionMembershipSystem
InstitutionRankSystem
InstitutionServiceUnlockSystem
InstitutionQuestSystem
InstitutionDuesSystem
InstitutionRuleSystem
ServiceAccessSystem
```

### Actions

```text
join institution
leave institution
request promotion
pay dues
use service
ask for institutional quest
donate
request training
request healing
```

### Events

```text
InstitutionJoinedEvent
InstitutionLeftEvent
InstitutionRankChangedEvent
InstitutionServiceUnlockedEvent
InstitutionServiceUsedEvent
InstitutionRuleViolatedEvent
```

## 7.6 Regional and institutional reputation

### Mechanics

`SocialBond` covers character-to-character relationships. `daggersim` adds civic-scale
reputation: regional, institutional, guild, temple, merchant, legal, underworld, and noble.

### Components

```python
RegionalReputationComponent
FactionReputationComponent
InstitutionReputationComponent
LegalReputationComponent
ServiceAccessComponent
DeedReputationComponent
RankEligibilityComponent
ReputationDecayComponent
```

### Systems

```text
ReputationChangeSystem
ReputationDecaySystem
RegionalReactionSystem
InstitutionReactionSystem
ServiceAccessSystem
QuestEligibilitySystem
RankEligibilitySystem
NPCDispositionSystem
DeedRequirementGate
```

### Events

```text
RegionalReputationChangedEvent
InstitutionReputationChangedEvent
LegalReputationChangedEvent
ServiceUnlockedEvent
ServiceDeniedEvent
RankEligibilityChangedEvent
```

## 7.7 Banks, loans, debt, and property

### Mechanics

```text
bank accounts
regional banks
deposits
withdrawals
loans
interest
due dates
debt
letters of credit
property deeds
houses
ships/boats/carts
safe storage
debt reputation
debt collectors
```

### Components

```python
BankComponent
BankAccountComponent
RegionalAccountComponent
LoanComponent
InterestComponent
DueDateComponent
DebtComponent
LetterOfCreditComponent
PropertyDeedComponent
HouseComponent
ShipComponent
SafeStorageComponent
```

### Systems

```text
BankingSystem
LoanIssuanceSystem
LoanInterestSystem
LoanDueSystem
DebtCollectionSystem
LetterOfCreditSystem
PropertyPurchaseSystem
SafeStorageSystem
```

### Actions

```text
deposit
withdraw
take loan
repay loan
buy letter of credit
cash letter of credit
buy property
sell property
store item
retrieve item
```

### Events

```text
AccountOpenedEvent
DepositMadeEvent
WithdrawalMadeEvent
LoanIssuedEvent
LoanRepaidEvent
LoanDefaultedEvent
LetterOfCreditIssuedEvent
PropertyPurchasedEvent
DebtCollectorDispatchedEvent
```

## 7.8 Crime, courts, law, and punishment

### Mechanics

`dragonsim` can use crime as adventure flavor. `daggersim` owns civic law.

```text
crime types
witnesses
guards
arrest
surrender
resist arrest
trial
plea
verdict
fine
jail
confiscation
banishment
bounty
legal reputation
regional laws
forbidden magic
debt crimes
trespassing
grave-robbing
```

### Components

```python
CrimeComponent
CrimeRecordComponent
WitnessComponent
ArrestWarrantComponent
GuardComponent
CourtCaseComponent
PleaComponent
SentenceComponent
PrisonComponent
BanishmentComponent
BountyComponent
ConfiscatedPropertyComponent
```

### Systems

```text
CrimeDetectionSystem
WitnessSystem
GuardResponseSystem
ArrestSystem
CourtCaseSystem
VerdictSystem
SentenceSystem
FineSystem
ImprisonmentSystem
BanishmentSystem
BountyHunterSystem
```

### Actions

```text
surrender
resist arrest
plead guilty
plead not guilty
pay fine
serve sentence
escape jail
bribe official
hire advocate
appeal
```

### Events

```text
CrimeCommittedEvent
CrimeWitnessedEvent
GuardSummonedEvent
ArrestedEvent
CourtCaseOpenedEvent
VerdictIssuedEvent
FinePaidEvent
PrisonSentenceStartedEvent
PrisonSentenceCompletedEvent
BanishmentIssuedEvent
BountyPostedEvent
```

## 7.9 Travel logistics

### Mechanics

Scale requires travel to be a mechanic with cost, risk, time, supplies, deadlines, fatigue,
weather delays, lodging, and interruption.

### Components

```python
TravelModeComponent
TravelingComponent
TravelingToDestination
TravelCostComponent
TravelRiskComponent
TravelHubComponent
RoadNodeComponent
LodgingComponent
CampingComponent
SuppliesComponent
MountComponent
VehicleComponent
```

### Systems

```text
TravelPlanningSystem
FastTravelSystem
TravelTimeSystem
TravelCostSystem
TravelEncounterSystem
TravelFatigueSystem
LodgingSystem
CampingRiskSystem
RegionalBorderSystem
```

### Actions

```text
plan travel
travel
fast travel
travel cautiously
travel recklessly
camp
book inn
buy supplies
ride mount
use cart
sail
```

### Events

```text
TravelStartedEvent
TravelInterruptedEvent
TravelCompletedEvent
TravelEncounterEvent
LodgingPurchasedEvent
CampMadeEvent
TravelDeadlineMissedEvent
```

## 7.10 Procedural dungeons

**Implemented status:** Dagger Sim contributes room `RestRiskComponent` state and a gate
over Core Verbs' shared `rest` action. Low-risk rooms permit rest; high-risk and ambush
rooms reject it. Procedural rest ambush generation remains planned.

### Mechanics

Worldgen can generate rooms. `daggersim` decides when a dungeon needs to exist and what
constraints it must satisfy.

```text
procedural dungeon graph
quest target placement
locked doors
keys
secret doors
danger zones
rest ambushes
automap
breadcrumbs
recall anchor
exit hinting
```

### Components

```python
DungeonComponent
DungeonNodeComponent
DungeonRoomComponent
DungeonLevelComponent
DungeonObjectiveComponent
SecretDoorComponent
LockedDoorComponent
AutomapComponent
BreadcrumbComponent
RecallAnchorComponent
RestRiskComponent
```

### Systems

```text
DungeonExpansionSystem
DungeonGraphGenerator
DungeonObjectivePlacementSystem
DungeonExplorationSystem
AutomapProjection
SecretDiscoverySystem
RestAmbushSystem
RecallSystem
ExitPathHintSystem
```

### Actions

```text
enter dungeon
search room
open secret door
mark path
view map
set recall
use recall
rest
leave dungeon
```

### Events

```text
DungeonRequestedEvent
DungeonGeneratedEvent
DungeonEnteredEvent
DungeonRoomDiscoveredEvent
SecretDoorFoundEvent
RecallAnchorSetEvent
RecallUsedEvent
DungeonObjectiveFoundEvent
DungeonExitedEvent
```

## 7.11 ClassMaker and custom builds

### Mechanics

```text
custom class
primary skills
major skills
minor skills
advantages
disadvantages
advancement rate
starting reputation modifiers
forbidden gear
weaknesses
resistances
special talents
roleplay constraints
```

### Components

```python
ClassTemplateComponent
CustomClassComponent
PrimarySkillComponent
MajorSkillComponent
MinorSkillComponent
AdvantageComponent
DisadvantageComponent
AdvancementRateComponent
BuildConstraintComponent
StartingReputationComponent
```

### Systems

```text
ClassMakerValidationSystem
AdvantageDisadvantageBalanceSystem
AdvancementRateSystem
BuildConstraintSystem
StartingReputationSystem
SkillGroupSystem
```

### Actions

```text
create custom class
choose primary skill
choose major skill
choose minor skill
choose advantage
choose disadvantage
finalize class
```

## 7.12 SpellMaker, PotionMaker, and EnchantMaker

### Mechanics

LLMs can propose names and flavor, but the engine validates a mechanical effect schema.

```text
custom spell creation
custom spell naming
spell effect composition
magnitude
duration
range
area
target type
cost calculation
skill requirements
custom enchantments
charges
cursed items
custom potions
ingredient effect discovery
```

### Components

```python
SpellTemplateComponent
SpellEffectComponent
SpellMagnitudeComponent
SpellDurationComponent
SpellRangeComponent
SpellAreaComponent
SpellCostComponent
CustomSpellComponent
EnchantmentComponent
EnchantmentCapacityComponent
ItemChargeComponent
PotionRecipeComponent
IngredientEffectComponent
PotionComponent
```

### Systems

```text
SpellMakerSystem
SpellCostSolver
SpellValidationSystem
CustomSpellRegistry
SpellCastingSystem
EnchantingSystem
ItemChargeSystem
PotionMakerSystem
IngredientDiscoverySystem
PotionEffectSystem
```

### Actions

```text
create spell
name spell
learn spell
cast spell
enchant item
recharge item
brew potion
identify ingredient
drink potion
apply potion
```

### Events

```text
SpellCreatedEvent
SpellLearnedEvent
SpellCastEvent
SpellFailedEvent
ItemEnchantedEvent
ItemChargeChangedEvent
PotionBrewedEvent
PotionConsumedEvent
```

## 7.13 Etiquette, Streetwise, and social approach

### Mechanics

This extends `say`/`tell` with an approach in addition to intent.

```text
casual
polite
formal
deferential
blunt
threatening
underworld
courtly
commercial
```

### Components

```python
DialogueApproachComponent
EtiquetteSkillComponent
StreetwiseSkillComponent
SocialRegisterComponent
NPCSocialClassComponent
ConversationToneComponent
```

### Systems

```text
DialogueApproachSystem
EtiquetteCheckSystem
StreetwiseCheckSystem
SpeechIntentInferenceSystem
SocialRegisterReactionSystem
CourtSpeechSystem
```

## 7.14 Language skills and creature pacification

### Mechanics

Language skill should enable nonviolent monster and creature encounters.

```text
animal languages
monster languages
spirit languages
dragon language
undead language
underworld cant
pacification
creature negotiation
nonviolent monster encounters
```

### Components

```python
LanguageSkillComponent
CreatureLanguageComponent
PacifiableComponent
PacifiedComponent
HostilityComponent
ComprehendLanguageEffectComponent
```

### Systems

```text
LanguageSkillCheckSystem
PacificationSystem
CreatureDialogueSystem
HostilitySuppressionSystem
CreatureReactionSystem
LanguageLearningSystem
```

### Actions

```text
speak language
attempt pacify
ask creature question
offer food
lower weapon
use comprehend language
```

### Events

```text
PacificationAttemptedEvent
CreaturePacifiedEvent
CreatureHostilityResumedEvent
CreatureUnderstoodSpeechEvent
LanguageSkillUsedEvent
```

## 7.15 Supernatural afflictions

### Mechanics

This overlaps with `barbariansim` and `dragonsim`, but `daggersim` owns the RPG
curse/state structure.

```text
vampirism
lycanthropy
moon-form
ghostbound
curse incubation
feeding need
transformation
cure quests
secret factions
social stigma
special powers
weaknesses
```

### Components

```python
SupernaturalAfflictionComponent
VampireComponent
LycanthropeComponent
WereformComponent
CurseIncubationComponent
FeedingNeedComponent
TransformationCooldownComponent
SunlightVulnerabilityComponent
MoonPhaseSensitivityComponent
CureQuestComponent
SecretLineageComponent
```

### Systems

```text
AfflictionInfectionSystem
CurseIncubationSystem
TransformationSystem
FeedingNeedSystem
SunlightEffectSystem
MoonPhaseSystem
SupernaturalPowerSystem
CureQuestSystem
SocialStigmaSystem
SecretFactionSystem
```

### Actions

```text
feed
resist feeding
transform
hide condition
seek cure
embrace curse
use supernatural power
```

### Events

```text
AfflictionContractedEvent
CurseIncubatedEvent
TransformationStartedEvent
TransformationEndedEvent
FeedingNeedChangedEvent
CureQuestStartedEvent
AfflictionCuredEvent
SecretRevealedEvent
```

## 7.16 How `daggersim` uses worldgen

```text
daggersim does not own all generation.
daggersim owns procedural RPG reasons for generation.
worldgen owns content creation.
core owns validation and instantiation.
```

Example flows:

```text
guild work -> quest template -> dungeon expansion request -> worldgen proposal -> validation -> generated dungeon objective
rumor -> unrealized hamlet -> travel -> settlement expansion -> service directory update -> usable healer
loan default -> debt collector request -> generated collector NPC and route -> later dialogue/action consequences
```

---

# 8. `voidsim` package — FTL-inspired sci-fi frontier mechanics

`void-sim` is the sci-fi frontier package. The internal package name should be `voidsim`,
matching the existing no-hyphen Python package style while the public-facing package label
uses the same hyphenated style as `life-sim`, `dragon-sim`, and `dagger-sim`.

Its main inspiration is **FTL**: crews under pressure, ship systems taking damage,
scarce fuel and supplies, sector travel, distress signals, boarding, hazards, and
emergencies that cascade across small systems. The package broadens that into bunnyland's
science-fiction layer: ships, stations, planets, alien contact, tech systems, hazards,
contracts, salvage, and exploration without turning the core engine into a space game.

## 8.1 Ships, stations, and habitats

### Mechanics

```text
starship
station
outpost
habitat module
airlock
corridor
cargo bay
bridge
engineering
med bay
lab
hydroponics
hangar
escape pod
life-support zone
```

### Components

```python
ShipComponent
StationComponent
HabitatModuleComponent
AirlockComponent
BulkheadComponent
PressurizedComponent
LifeSupportComponent
ShipSystemComponent
PowerGridComponent
OxygenComponent
RadiationShieldComponent
```

### Systems

```text
LifeSupportSystem
PressureSystem
AirlockSystem
PowerGridSystem
ShipIntegritySystem
ModuleDamageSystem
HabitatComfortSystem
StationDockingSystem
```

### Actions

```text
open airlock
seal bulkhead
repair system
reroute power
cycle airlock
dock
undock
inspect ship system
evacuate module
```

### Events

```text
AirlockCycledEvent
PressureChangedEvent
LifeSupportFailedEvent
PowerReroutedEvent
ShipSystemDamagedEvent
ShipSystemRepairedEvent
DockingCompletedEvent
ModuleEvacuatedEvent
```

## 8.2 Space travel, orbits, and navigation

### Mechanics

```text
sector
star system
orbit
planet
moon
asteroid belt
jump route
fuel
travel window
navigation hazard
scan range
distress signal
```

### Components

```python
StarSystemComponent
OrbitalBodyComponent
OrbitComponent
NavigationRouteComponent
JumpDriveComponent
FuelComponent
SensorComponent
DistressSignalComponent
AstrogationComponent
```

### Systems

```text
RoutePlanningSystem
JumpTravelSystem
FuelConsumptionSystem
OrbitTransferSystem
SensorScanSystem
DistressSignalSystem
NavigationHazardSystem
TravelWindowSystem
```

### Actions

```text
plot course
jump
scan
answer distress signal
refuel
enter orbit
leave orbit
land
launch
```

### Events

```text
CoursePlottedEvent
JumpStartedEvent
JumpCompletedEvent
FuelChangedEvent
SignalDetectedEvent
NavigationHazardEncounteredEvent
OrbitEnteredEvent
LandingCompletedEvent
```

## 8.3 Crew roles, duty shifts, and shipboard work

### Mechanics

```text
captain
pilot
engineer
doctor
scientist
security
quartermaster
comms
away team
duty shift
watch rotation
morale
mutiny risk
```

### Components

```python
CrewComponent
CrewRoleComponent
DutyShiftComponent
WatchStationComponent
AwayTeamComponent
MoraleComponent
MutinyRiskComponent
CommandAuthorityComponent
```

### Systems

```text
CrewAssignmentSystem
DutyShiftSystem
WatchRotationSystem
AwayTeamSystem
MoraleSystem
CommandAuthoritySystem
MutinyRiskSystem
```

### Actions

```text
assign role
start shift
end shift
form away team
issue ship order
relieve crew member
hold briefing
```

### Events

```text
CrewRoleAssignedEvent
DutyShiftStartedEvent
DutyShiftEndedEvent
AwayTeamFormedEvent
ShipOrderIssuedEvent
MoraleChangedEvent
MutinyRiskChangedEvent
```

## 8.4 Technology, research, fabrication, and upgrades

### Mechanics

```text
tech level
blueprints
research topics
fabricators
nanoforge
components
ship upgrades
cybernetics
robots
drones
AI cores
hacking
data salvage
```

### Components

```python
TechLevelComponent
BlueprintComponent
ResearchTopicComponent
FabricatorComponent
UpgradeSlotComponent
CyberneticComponent
RobotComponent
DroneComponent
AICoreComponent
HackingComponent
DataCacheComponent
```

### Systems

```text
ResearchUnlockSystem
FabricationSystem
UpgradeInstallationSystem
CyberneticSurgerySystem
RobotCommandSystem
DroneControlSystem
HackingSystem
DataSalvageSystem
```

### Actions

```text
research technology
fabricate item
install upgrade
repair drone
command robot
hack terminal
recover data
install cybernetic
```

### Events

```text
TechnologyResearchedEvent
ItemFabricatedEvent
UpgradeInstalledEvent
DroneRepairedEvent
RobotCommandedEvent
HackSucceededEvent
HackFailedEvent
DataRecoveredEvent
```

## 8.5 Alien contact, diplomacy, and xenobiology

### Mechanics

```text
alien species
first contact
translation
xenobiology
alien ecology
contamination
quarantine
cultural protocol
diplomacy
trade mission
hostile encounter
artifact study
```

### Components

```python
AlienSpeciesComponent
FirstContactComponent
TranslationMatrixComponent
XenobiologyComponent
ContaminationComponent
QuarantineComponent
CulturalProtocolComponent
DiplomaticMissionComponent
AlienArtifactComponent
```

### Systems

```text
FirstContactSystem
TranslationSystem
XenobiologySystem
ContaminationSystem
QuarantineSystem
CulturalProtocolSystem
DiplomacySystem
AlienArtifactSystem
```

### Actions

```text
initiate contact
attempt translation
study organism
quarantine sample
follow protocol
negotiate
trade with aliens
study artifact
```

### Events

```text
FirstContactEvent
TranslationProgressedEvent
ContaminationDetectedEvent
QuarantineStartedEvent
DiplomacyChangedEvent
AlienTradeCompletedEvent
ArtifactStudiedEvent
```

## 8.6 Space hazards, damage control, and emergencies

### Mechanics

```text
vacuum exposure
decompression
radiation storm
solar flare
meteor strike
reactor leak
fire
toxic atmosphere
gravity failure
medical emergency
boarding action
system cascade failure
```

### Components

```python
VacuumExposureComponent
RadiationComponent
ReactorComponent
GravityComponent
DamageControlComponent
EmergencyComponent
BoardingThreatComponent
HazardSuitComponent
```

### Systems

```text
VacuumExposureSystem
DecompressionSystem
RadiationSystem
ReactorLeakSystem
GravityFailureSystem
DamageControlSystem
EmergencyResponseSystem
BoardingDefenseSystem
```

### Actions

```text
don hazard suit
patch hull
fight fire
stabilize reactor
restore gravity
treat radiation
repel boarders
call emergency
```

### Events

```text
DecompressionStartedEvent
RadiationStormEvent
MeteorStrikeEvent
ReactorLeakEvent
GravityFailedEvent
HullPatchedEvent
EmergencyResolvedEvent
BoardingRepelledEvent
```

## 8.7 Contracts, salvage, cargo, and frontier economy

### Mechanics

```text
cargo contracts
passenger transport
salvage rights
bounty contracts
mining claims
survey contracts
smuggling
customs inspection
black market
insurance
ship mortgage
station fees
```

### Components

```python
ContractComponent
CargoComponent
PassengerComponent
SalvageClaimComponent
BountyContractComponent
MiningClaimComponent
SurveyDataComponent
SmugglingComponent
CustomsComponent
InsuranceComponent
ShipMortgageComponent
StationFeeComponent
```

### Systems

```text
ContractGenerationSystem
CargoManifestSystem
PassengerSystem
SalvageRightsSystem
BountyContractSystem
MiningClaimSystem
CustomsInspectionSystem
BlackMarketSystem
InsuranceSystem
MortgageSystem
```

### Actions

```text
accept contract
load cargo
unload cargo
claim salvage
scan cargo
smuggle item
pay station fee
file insurance claim
make mortgage payment
```

### Events

```text
ContractAcceptedEvent
ContractCompletedEvent
CargoLoadedEvent
CargoDeliveredEvent
SalvageClaimedEvent
CustomsInspectionEvent
SmugglingDetectedEvent
InsurancePaidEvent
MortgageDefaultedEvent
```

## 8.8 How `voidsim` uses worldgen

`voidsim` should use the same expansion pattern as `dagger-sim`, but with space frontiers
instead of civic fantasy frontiers.

```text
distress signal -> generated derelict -> salvage contract -> boarding hazard -> data/loot consequences
survey contract -> generated planet site -> alien ecology -> sample quarantine -> research unlock
ship mortgage default -> creditor faction pressure -> bounty/impound event -> legal or underworld choices
```

---

# 9. `nukesim` package — Fallout-inspired wasteland mechanics

`nuke-sim` is the post-disaster wasteland package. The internal package name should be
`nukesim`, matching the existing no-hyphen Python package style while the public-facing
package label uses the same hyphenated style as `void-sim`, `garden-sim`, and
`dragon-sim`.

Its main inspiration is **Fallout**: contaminated ruins, radiation pressure, strange
mutations, scavenged junk, jury-rigged gear, settlement survival, faction salvage rights,
and dangerous old-world technology. The package should not make bunnyland a combat-only
RPG. It should add environmental risk and resourceful crafting loops that interact with
colony work, void-sim radiation shields, barbarian-sim survival pressure, and storyteller
incidents.

## 9.1 Radiation exposure and protection

### Mechanics

```text
radiation source
fallout zone
hotspot
contaminated water
irradiated food
dosimeter
hazard suit
rad shielding
decontamination station
radiation sickness
```

### Components

```python
RadiationSourceComponent
RadiationDoseComponent
RadiationSicknessComponent
DecontaminationComponent
RadProtectionComponent
RadiationShieldComponent
RadiationMutationPressureComponent
```

### Systems

```text
RadiationExposureSystem
RadiationDecaySystem
RadiationSicknessSystem
ProtectionAggregationSystem
DecontaminationSystem
```

### Actions

```text
scan radiation
decontaminate
use rad medicine
mark hotspot
seal radiation source
```

### Events

```text
RadiationExposureEvent
RadiationSicknessChangedEvent
RadiationScannedEvent
DecontaminationAppliedEvent
HotspotMarkedEvent
RadiationSourceSealedEvent
```

## 9.2 Mutation pressure and outcomes

### Mechanics

```text
mutation pressure
mutation threshold
beneficial mutation
harmful mutation
unstable mutation
mutation resistance
mutation suppressant
adapted creature
feral transformation
```

### Components

```python
MutationComponent
MutationResistanceComponent
MutationSuppressantComponent
MutationThresholdComponent
RadiationMutationPressureComponent
ChaosMutationPressureComponent
CyberneticMutationPressureComponent
```

### Systems

```text
MutationPressureSystem
MutationResolutionSystem
MutationSuppressionSystem
MutationInteractionSystem
```

### Actions

```text
stabilize mutation
suppress mutation
study mutation
harvest mutant sample
```

### Events

```text
MutationPressureChangedEvent
MutationManifestedEvent
MutationStabilizedEvent
MutationSuppressedEvent
MutantSampleHarvestedEvent
```

Mutation pressure must stay source-specific. Radiation, chaos, and cybernetic pressure may
interact later, but each keeps its own component so packages can be enabled independently.

## 9.3 Wasteland scavenging and salvage

### Mechanics

```text
scavenging site
ruin cache
locked crate
hazardous salvage
junk item
scrap metal
electronics
chemicals
pre-war artifact
salvage rights
faction claim
```

### Components

```python
ScavengeSiteComponent
LootTableComponent
SalvageClaimComponent
JunkComponent
PreWarArtifactComponent
ContaminationComponent
```

### Systems

```text
ScavengeRefreshSystem
LootTableSystem
HazardousSalvageSystem
SalvageClaimSystem
ArtifactDiscoverySystem
```

### Actions

```text
scavenge
claim salvage
sort junk
scrap item
appraise artifact
```

### Events

```text
SiteScavengedEvent
LootFoundEvent
HazardTriggeredEvent
SalvageClaimedEvent
ItemScrappedEvent
ArtifactAppraisedEvent
```

## 9.4 Jury-rigged crafting, repair, and chems

### Mechanics

```text
workbench
chem bench
camp stove
schematic
weapon mod
armor patch
rad medicine
dirty water
purified water
scrap recipe
field repair
```

### Components

```python
JuryRiggedComponent
SchematicComponent
WeaponModComponent
ArmorPatchComponent
ChemComponent
WaterPurificationComponent
RecipeComponent
WorkstationComponent
ResourceStackComponent
DurabilityComponent
```

### Systems

```text
JuryRigCraftingSystem
FieldRepairSystem
WaterPurificationSystem
ChemCraftingSystem
ModInstallationSystem
```

### Actions

```text
craft wasteland item
install mod
field repair
purify water
brew chem
strip for parts
```

### Events

```text
WastelandItemCraftedEvent
ModInstalledEvent
FieldRepairCompletedEvent
WaterPurifiedEvent
ChemBrewedEvent
ItemStrippedEvent
```

`nukesim` should reuse `colonysim` resource stacks, recipes, and workstations where they
fit. It should add new components only when the wasteland rule is distinct: radiation,
mutation, hazardous salvage, chems, and jury-rigged instability.

## 9.5 Settlements, factions, and old-world tech

### Mechanics

```text
settlement claim
scrap barricade
water purifier
generator
radio beacon
trader route
raider pressure
faction reputation
old-world terminal
reactor core
vault door
```

### Components

```python
SettlementComponent
WaterPurifierComponent
GeneratorComponent
RadioBeaconComponent
TraderRouteComponent
RaiderThreatComponent
WastelandFactionComponent
OldWorldTechComponent
VaultComponent
```

### Systems

```text
SettlementUpkeepSystem
WaterProductionSystem
GeneratorFuelSystem
TraderRouteSystem
RaiderPressureSystem
FactionReputationSystem
OldWorldTechSystem
```

### Actions

```text
claim settlement
build purifier
power generator
activate beacon
open trader route
negotiate faction salvage
boot old-world terminal
```

### Events

```text
SettlementClaimedEvent
PurifierBuiltEvent
GeneratorPoweredEvent
BeaconActivatedEvent
TraderRouteOpenedEvent
FactionSalvageNegotiatedEvent
OldWorldTerminalBootedEvent
```

## 9.6 How `nukesim` uses worldgen

`nukesim` should expand worlds through hazardous sites and salvage leads:

```text
geiger spike -> generated ruin -> radiation source -> scavenging cache -> mutation pressure
radio beacon -> generated settlement -> purifier job -> trader route -> raider incident
pre-war keycard -> generated vault door -> old-world terminal -> faction salvage dispute
```

The first playable slice should be small and deterministic: radiation accumulation,
shielding, decontamination, mutation manifestation, one scavenging action, and recipes that
use found scrap.

---

# 10. `neonsim` package — Deus Ex / Watch Dogs / Cyberpunk 2077 / Blade Runner / Shadows of Doubt-inspired cyberpunk mechanics

`neon-sim` is the cyberpunk city package. The internal package name should be `neonsim`,
matching the existing no-hyphen Python package style while the public-facing package label
uses the same hyphenated style as `void-sim`, `dragon-sim`, and `dagger-sim`.

Its main inspirations are **Deus Ex**, **Watch Dogs**, **Cyberpunk 2077**, **Blade Runner**,
and **Shadows of Doubt**: corporate
systems, surveillance, street-level access, hacking as actionable interaction with devices,
consequences for being seen, cybernetics, fixers, gangs, and missions that can be solved
through infiltration, social pressure, economics, violence, or data. The tone should land
near Blade Runner and Cyberpunk 2077: rain, debt, neon, corporate shadow power, dangerous
bargains, and people trying to survive inside systems that record everything.

The first implementation should keep hacking in ECS actions and consequences. Minigames
can exist later as client-side or script-driven presentation over the same actions.

## 10.1 Cyberpunk districts, sites, and access

### Mechanics

```text
district
street market
corp campus
arcology
nightclub
clinic
data center
transit hub
checkpoint
safehouse
back alley
restricted area
public/private access
security clearance
```

### Components

```python
DistrictComponent
CyberpunkSiteComponent
SecurityZoneComponent
AccessLevelComponent
CheckpointComponent
SafehouseComponent
PublicAccessComponent
RestrictedAreaComponent
```

### Systems

```text
DistrictStatusSystem
AccessControlSystem
CheckpointSystem
SafehouseSystem
SecurityZoneSystem
TrespassDetectionSystem
```

### Actions

```text
enter district
show credentials
bribe guard
sneak through checkpoint
claim safehouse
case location
```

### Events

```text
DistrictEnteredEvent
AccessGrantedEvent
AccessDeniedEvent
CheckpointPassedEvent
TrespassDetectedEvent
SafehouseClaimedEvent
```

---

## 10.2 Devices, networks, and surveillance

### Mechanics

```text
camera
door lock
terminal
server
sensor
drone
traffic system
comms tower
mesh network
device ownership
surveillance coverage
recorded evidence
blind spot
```

### Components

```python
DeviceComponent
NetworkNodeComponent
CameraComponent
SensorComponent
DroneComponent
TerminalComponent
ServerComponent
LockComponent
SurveillanceCoverageComponent
RecordedEvidenceComponent
BlindSpotComponent
```

### Systems

```text
DeviceStateSystem
NetworkTopologySystem
SurveillanceSystem
EvidenceRecordingSystem
CameraCoverageSystem
DronePatrolSystem
BlindSpotSystem
```

### Actions

```text
inspect device
disable camera
loop camera
unlock door
access terminal
trace network
jam sensor
deploy drone
wipe evidence
```

### Events

```text
DeviceInspectedEvent
CameraDisabledEvent
CameraLoopedEvent
DoorUnlockedEvent
TerminalAccessedEvent
NetworkTracedEvent
SensorJammedEvent
EvidenceRecordedEvent
EvidenceWipedEvent
```

---

## 10.3 Hacking, credentials, and intrusion

### Mechanics

```text
credential
access token
exploit
backdoor
privilege escalation
trace timer
alarm
data theft
system sabotage
remote control
counter-intrusion
```

### Components

```python
CredentialComponent
AccessTokenComponent
ExploitComponent
BackdoorComponent
IntrusionComponent
TraceTimerComponent
AlarmComponent
DataPayloadComponent
SabotageComponent
CounterIntrusionComponent
```

### Systems

```text
CredentialValidationSystem
HackAttemptSystem
PrivilegeEscalationSystem
TraceTimerSystem
AlarmSystem
DataExfiltrationSystem
SystemSabotageSystem
CounterIntrusionSystem
```

### Actions

```text
scan network
use credential
run exploit
install backdoor
escalate privileges
exfiltrate data
sabotage system
spoof identity
evade trace
```

### Events

```text
NetworkScannedEvent
CredentialUsedEvent
HackSucceededEvent
HackFailedEvent
BackdoorInstalledEvent
PrivilegesEscalatedEvent
TraceStartedEvent
TraceEvadedEvent
DataExfiltratedEvent
SystemSabotagedEvent
AlarmRaisedEvent
```

---

## 10.4 Fixers, missions, and corporate intrigue

### Mechanics

```text
fixer
handler
corporation
shell company
blackmail file
dead drop
runner contract
data broker
asset extraction
corporate sabotage
whistleblower
double cross
```

### Components

```python
FixerComponent
HandlerComponent
CorporationComponent
ShellCompanyComponent
BlackmailFileComponent
DeadDropComponent
RunnerContractComponent
DataBrokerComponent
AssetExtractionComponent
CorporateSabotageComponent
WhistleblowerComponent
DoubleCrossComponent
```

### Systems

```text
FixerJobSystem
CorporateIntrigueSystem
DeadDropSystem
BlackmailSystem
DataBrokerSystem
AssetExtractionSystem
DoubleCrossSystem
ContractPayoutSystem
```

### Actions

```text
take fixer job
meet handler
deliver data
plant evidence
extract asset
leak file
blackmail target
collect payout
burn contact
```

### Events

```text
FixerJobAcceptedEvent
HandlerMetEvent
DataDeliveredEvent
EvidencePlantedEvent
AssetExtractedEvent
FileLeakedEvent
BlackmailAppliedEvent
PayoutCollectedEvent
ContactBurnedEvent
DoubleCrossRevealedEvent
```

---

## 10.5 Street economy, reputation, and wanted levels

### Mechanics

```text
street vendor
black market
contraband
corporate scrip
debt
favor
reputation
heat
wanted level
warrant
bounty
informant
law response
```

### Components

```python
StreetVendorComponent
BlackMarketComponent
ContrabandComponent
CorporateScripComponent
DebtComponent
FavorComponent
ReputationComponent
HeatComponent
WantedLevelComponent
WarrantComponent
BountyComponent
InformantComponent
```

### Systems

```text
StreetEconomySystem
ContrabandSystem
DebtSystem
FavorSystem
ReputationSystem
HeatSystem
WantedLevelSystem
WarrantSystem
BountySystem
InformantSystem
LawResponseSystem
```

### Actions

```text
buy contraband
sell data
call in favor
pay debt
hide from law
clear warrant
post bounty
turn informant
```

### Events

```text
ContrabandBoughtEvent
DataSoldEvent
FavorCalledEvent
DebtPaidEvent
ReputationChangedEvent
HeatChangedEvent
WantedLevelChangedEvent
WarrantIssuedEvent
BountyPostedEvent
InformantTurnedEvent
```

---

## 10.6 Cybernetics, implants, and tradeoffs

### Mechanics

```text
implant
augmentation slot
clinic
street surgeon
license
maintenance
power draw
side effect
medical risk
identity risk
illegal implant
implant exploit
```

### Components

```python
ImplantComponent
AugmentationSlotComponent
ClinicComponent
StreetSurgeonComponent
ImplantLicenseComponent
MaintenanceNeedComponent
PowerDrawComponent
SideEffectComponent
MedicalRiskComponent
IdentityRiskComponent
IllegalImplantComponent
ImplantExploitComponent
```

### Systems

```text
ImplantInstallationSystem
ImplantMaintenanceSystem
PowerDrawSystem
SideEffectSystem
MedicalRiskSystem
IdentityRiskSystem
ImplantLegalitySystem
ImplantExploitSystem
```

### Actions

```text
install implant
remove implant
service implant
license implant
overclock implant
disable implant
scan implant
exploit implant
```

### Events

```text
ImplantInstalledEvent
ImplantRemovedEvent
ImplantServicedEvent
ImplantLicensedEvent
ImplantOverclockedEvent
ImplantDisabledEvent
ImplantScannedEvent
ImplantExploitedEvent
SideEffectTriggeredEvent
```

Cybernetics should not have one universal penalty. Each implant type should define its own
tradeoff: money, maintenance, heat, health, identity exposure, legality, power use, social
stigma, or vulnerability to hacking.

---

## 10.7 How `neonsim` uses other packages

`neonsim` should compose heavily with existing systems rather than becoming a second core.

```text
dagger-sim law/reputation -> cyberpunk warrants, institutions, services, debt, and bounties
void-sim tech/hacking -> terminals, drones, data salvage, and cybernetics
colonysim jobs/reservations -> crew work, safehouse logistics, crafting, and black-market production
lifesim relationships -> contacts, jealousy, blackmail, favors, household risk, and private memory
barbariansim combat/policy -> street fights, raids, PvP boundaries, and weapon legality
```

`neonsim` should use worldgen for city expansion:

```text
fixer rumor -> generated corp site -> access challenge -> surveillance/evidence -> payout/reputation/heat
black-market lead -> generated vendor -> contraband offer -> warrant risk -> debt/favor consequences
data leak -> generated shell company -> terminal network -> blackmail file -> faction reaction
```

---

# 11. `dinosim` package — Jurassic Park / ARK / Dino Crisis / Pacific Rim-inspired creature lifecycle

`dino-sim` is the prehistoric creature lifecycle package. The internal package name should
be `dinosim`, matching the existing no-hyphen Python package style while the public-facing
package label uses the same hyphenated style as `garden-sim`, `void-sim`, and `neon-sim`.

Its main inspirations are **Jurassic Park**, **ARK**, **Dino Crisis**, and **Pacific Rim**: fossils, eggs,
taming, training, caretaking, tracking, dangerous escapes, and the moment when a managed
animal becomes an emergency. This is not a park-management package and should not introduce
Zoo Tycoon-style guest attraction, ticketing, visitor happiness, shops, tours, or exhibit
rating loops. It is survival farming with pulp battles: Jurassic Park with a hint of
Pacific Rim, where creatures can be livestock, companions, or enemies depending on species,
training, hunger, enclosure state, and recent events.

The initial package should focus on three primary mechanics:

```text
egg handling and reptile procreation
kaiju attack storyteller incidents
fossil and species identification and cloning
```

Everything else in this section supports those loops. Taming, training, containment,
feeding, and creature products are useful once creatures exist, but they should not
displace the lifecycle, discovery, or incident systems as the package's core. Full genetic
engineering should be left out at first. The initial cloning surface should be constrained
to recovering viable ancient material, identifying it, preparing a clone candidate, and
producing an egg or embryo that the normal incubation and lifesim ageing surface can take
over.

## 11.1 Fossils, identification, and cloning

### Mechanics

```text
fossil deposit
excavation site
fossil fragment
bone set
amber sample
ancient tissue
sample contamination
species identification
genome completeness
clone viability
clone candidate
embryo
lab incubator
surrogate egg
failed clone
```

### Components

```python
FossilDepositComponent
ExcavationSiteComponent
FossilFragmentComponent
BoneSetComponent
AmberSampleComponent
AncientTissueComponent
ContaminationComponent
SpeciesIdentificationComponent
GenomeCompletenessComponent
CloneViabilityComponent
CloneCandidateComponent
EmbryoComponent
LabIncubatorComponent
SurrogateEggComponent
FailedCloneComponent
```

### Systems

```text
FossilDiscoverySystem
ExcavationSystem
FossilIdentificationSystem
AncientSampleSystem
ContaminationSystem
GenomeCompletenessSystem
CloneViabilitySystem
ClonePreparationSystem
LabIncubationSystem
SurrogateEggSystem
```

### Actions

```text
survey fossil site
excavate fossil
clean fossil
identify fossil
extract ancient sample
stabilize sample
prepare clone
implant embryo
monitor lab incubator
discard failed clone
```

### Events

```text
FossilSiteSurveyedEvent
FossilExcavatedEvent
FossilCleanedEvent
SpeciesIdentifiedEvent
AncientSampleExtractedEvent
SampleContaminatedEvent
SampleStabilizedEvent
ClonePreparedEvent
EmbryoImplantedEvent
LabIncubationChangedEvent
CloneFailedEvent
SurrogateEggCreatedEvent
```

Cloning should feed into the egg and hatching systems instead of bypassing them. A
successful clone preparation creates either an `EmbryoComponent` in a `LabIncubatorComponent`
or a `SurrogateEggComponent` that also carries the normal egg/incubation components. Once a
clone hatches, `dinosim` should hand the creature to the lifesim-compatible ageing surface
as a hatchling or juvenile rather than owning a separate adulthood timeline.

## 11.2 Species, ecology, and creature needs

### Mechanics

```text
dinosaur species
size class
diet
territory
herd
pack
nest
threat posture
temperament
stress
hunger
injury
sleep
migration
predator/prey pressure
```

### Components

```python
DinosaurComponent
SpeciesComponent
SizeClassComponent
DietComponent
TerritoryComponent
HerdComponent
PackComponent
NestComponent
ThreatPostureComponent
TemperamentComponent
CreatureStressComponent
CreatureNeedComponent
PredatorComponent
PreyComponent
MigrationComponent
```

### Systems

```text
CreatureNeedSystem
TerritorySystem
HerdBehaviorSystem
PackBehaviorSystem
PredatorPreySystem
MigrationSystem
ThreatPostureSystem
CreatureStressSystem
```

### Actions

```text
observe creature
feed creature
water creature
calm creature
mark territory
track herd
study tracks
```

### Events

```text
CreatureObservedEvent
CreatureFedEvent
CreatureCalmedEvent
TerritoryMarkedEvent
HerdTrackedEvent
TracksStudiedEvent
CreatureNeedChangedEvent
CreatureStressChangedEvent
```

---

## 11.3 Egg handling, reptile procreation, hatching, and raising

### Mechanics

```text
egg
clutch
nesting site
fertility
fertilization
reptile procreation
parent species
offspring species
incubation
temperature
brooding
hatching
juvenile
growth stage
imprinting
parent bond
caretaking
```

### Components

```python
EggComponent
ClutchComponent
NestingSiteComponent
FertilityComponent
FertilizationComponent
ReptileProcreationComponent
ParentSpeciesComponent
OffspringSpeciesComponent
IncubationComponent
BroodingComponent
HatchingComponent
JuvenileComponent
GrowthStageComponent
ImprintComponent
ParentBondComponent
CaretakingComponent
```

### Systems

```text
BreedingSystem
NestSelectionSystem
FertilizationSystem
ReptileProcreationSystem
OffspringSpeciesSystem
IncubationSystem
TemperatureIncubationSystem
HatchingSystem
GrowthStageSystem
ImprintingSystem
ParentBondSystem
CaretakingSystem
```

### Actions

```text
pair creatures
prepare nest
fertilize egg
collect egg
inspect egg
incubate egg
warm egg
cool egg
hatch egg
imprint hatchling
care for juvenile
```

### Events

```text
CreaturesPairedEvent
NestPreparedEvent
EggLaidEvent
EggFertilizedEvent
EggCollectedEvent
EggInspectedEvent
IncubationChangedEvent
EggHatchedEvent
HatchlingImprintedEvent
JuvenileCaredForEvent
GrowthStageChangedEvent
```

Eggs are the common output of natural procreation and cloning. A naturally laid egg should
carry parentage and species data; a cloned egg should carry source sample and species
identification data. After hatching, the creature should become a normal character or
critter entity with lifesim-compatible age state so ageing, care, relationships, injury,
and death do not need a separate dinosaur-only timeline.

---

## 11.4 Tracking, taming, training, and companions

### Mechanics

```text
tracks
scent
call
bait
tranquilizer
taming progress
trust
fear
command
mount
companion role
guard behavior
hunt behavior
recall
```

### Components

```python
TrackComponent
ScentComponent
BaitComponent
TranquilizerComponent
TamingComponent
TrustComponent
FearComponent
TrainingComponent
CommandComponent
MountComponent
CompanionComponent
GuardBehaviorComponent
HuntBehaviorComponent
RecallComponent
```

### Systems

```text
TrackingSystem
BaitSystem
TranquilizerSystem
TamingSystem
TrustSystem
FearSystem
TrainingSystem
CompanionCommandSystem
MountSystem
RecallSystem
```

### Actions

```text
track creature
set bait
tranquilize creature
approach creature
tame creature
train command
mount creature
command companion
recall creature
```

### Events

```text
CreatureTrackedEvent
BaitSetEvent
CreatureTranquilizedEvent
TamingProgressedEvent
CreatureTamedEvent
CommandTrainedEvent
CreatureMountedEvent
CompanionCommandedEvent
CreatureRecalledEvent
```

---

## 11.5 Enclosures, containment, and escapes

### Mechanics

```text
enclosure
fence
gate
lock
reinforcement
feeding pen
quarantine pen
escape risk
breach
stampede
panic
containment protocol
```

### Components

```python
EnclosureComponent
FenceComponent
GateComponent
ReinforcementComponent
FeedingPenComponent
QuarantinePenComponent
EscapeRiskComponent
BreachComponent
StampedeComponent
ContainmentProtocolComponent
```

### Systems

```text
EnclosureIntegritySystem
GateControlSystem
ReinforcementSystem
EscapeRiskSystem
BreachSystem
StampedeSystem
PanicSystem
ContainmentProtocolSystem
```

### Actions

```text
build enclosure
repair fence
reinforce gate
lock pen
open pen
trigger containment
recapture creature
hide from creature
evacuate room
```

### Events

```text
EnclosureBuiltEvent
FenceRepairedEvent
GateReinforcedEvent
PenLockedEvent
ContainmentTriggeredEvent
CreatureEscapedEvent
CreatureRecapturedEvent
StampedeStartedEvent
RoomEvacuatedEvent
```

---

## 11.6 Dangerous encounters, battles, and kaiju incidents

### Mechanics

```text
territorial attack
ambush
roar
charge
grapple
tail swipe
trample
armor plates
weak point
pack hunt
apex predator
kaiju arrival
army response
settlement damage
```

### Components

```python
CreatureAttackComponent
RoarComponent
ChargeComponent
GrappleComponent
TrampleComponent
ArmorPlateComponent
WeakPointComponent
PackHuntComponent
ApexPredatorComponent
KaijuComponent
ArmyResponseComponent
SettlementDamageComponent
```

### Systems

```text
CreatureCombatSystem
RoarFearSystem
ChargeAttackSystem
GrappleSystem
TrampleSystem
WeakPointSystem
PackHuntSystem
ApexPredatorSystem
KaijuIncidentSystem
ArmyResponseSystem
SettlementDamageSystem
```

### Actions

```text
dodge creature
hide from creature
fight creature
target weak point
drive off predator
call for help
signal army
repair damage
```

### Events

```text
CreatureAttackedEvent
CreatureRoaredEvent
CreatureChargedEvent
CreatureTrampledEvent
WeakPointHitEvent
ApexPredatorAppearedEvent
KaijuArrivedEvent
ArmyCalledEvent
SettlementDamagedEvent
PredatorDrivenOffEvent
```

Kaiju attacks should be storyteller incidents when both `dinosim` and `colonysim` are
enabled. Colony-sim provides the settlement target, jobs, reservations, hauling, repair
work, and post-attack recovery pressure; without colony-sim, kaiju can remain local
encounters or generated threats, but should not assume settlement damage or colony job
queues exist.

The incident should be selected by the storyteller budget, create an active incident entity,
and then spawn or reference a kaiju threat. Resolution should use normal incident handling
plus concrete colony work when available: evacuate, fight or drive off the kaiju, repair
damaged settlement objects, haul debris, and treat casualties. This makes kaiju attacks a
colony-scale emergency without adding park guests or attraction management.

---

## 11.7 Creature products and survival farming

### Mechanics

```text
feed stores
meat
eggs
hide
bone
toxin
milk
fertilizer
labor animal
mount work
guard animal
```

### Components

```python
FeedStoreComponent
CreatureProductComponent
HideComponent
BoneComponent
ToxinComponent
CreatureMilkComponent
RanchLaborComponent
GuardAnimalComponent
```

### Systems

```text
FeedStoreSystem
CreatureProductSystem
RanchLaborSystem
GuardAnimalSystem
```

### Actions

```text
stock feed
collect egg
harvest product
assign ranch work
assign guard
```

### Events

```text
FeedStockedEvent
CreatureProductCollectedEvent
RanchWorkAssignedEvent
GuardAssignedEvent
```

---

## 11.8 How `dinosim` uses other packages

`dinosim` should build on the existing survival and farming surface without turning into a
theme-park management game.

```text
garden-sim crops/seasons -> feed production, nesting conditions, forage, and ranch labor
barbarian-sim combat/survival -> dangerous encounters, armor, weapons, downing, and recovery
colonysim jobs/reservations -> enclosure repair, feeding jobs, resource hauling, and assigned handlers
lifesim relationships -> trust, imprinting, companion bonds, household risk, and grief after losses
storyteller incidents -> escapes, apex predators, kaiju arrivals, stampedes, and containment failures
void-sim emergency logic -> alarms, evacuation, quarantine pens, and system-like containment protocols
```

`dinosim` should use worldgen for dangerous creature discovery:

```text
strange tracks -> generated nesting site -> egg/clutch -> parent threat -> ranching or survival consequence
fossil rumor -> generated dig site -> identified fossil -> viable clone sample -> lab egg/embryo
storm damage -> enclosure breach -> escaped creature -> tracking/taming/battle -> settlement aftermath
legend rumor + colonysim settlement -> generated apex lair -> kaiju incident -> army response -> repair jobs
```

---

# 12. `fortresssim` package — Dwarf Fortress-inspired mechanics

Dwarf Fortress is the “mother of all sims” here, but it is intentionally not practical to exhaustively scope. The useful direction for bunnyland is to borrow *depth patterns*: generated world history, materials, geology, settlement logistics, strange moods, artifacts, nobles, justice, taverns, libraries, hospitals, sieges, migrants, trade caravans, tantrum spirals, and the fact that the world continues beyond any one character. Dwarf Fortress includes generated worlds and histories, fortress mode, adventure mode, legends mode, geology, z-level digging, farming, migrants, caravans, nobles, mandates, justice, vampires, strange moods, artifacts, and fortress-ending spirals. ([Wikipedia][8])

## 12.1 World generation and history

### Mechanics

```text
procedural world
regions
biomes
civilizations
historical figures
wars
beast legends
ruined sites
old fortresses
generated artifacts
lineages
```

### Components

```python
WorldHistoryComponent
CivilizationComponent
HistoricalFigureComponent
HistoricalEventComponent
SiteComponent
LegendComponent
ArtifactHistoryComponent
```

### Systems and generators

```text
WorldHistoryGenerator
CivilizationGenerator
HistoricalEventGenerator
LegendIndexProjection
WorldStateAgingSystem
```

### Events

```text
HistoricalEventCreatedEvent
CivilizationFoundedEvent
WarStartedEvent
SiteRuinedEvent
LegendRecordedEvent
```

---

## 12.2 Materials and item specificity

### Mechanics

```text
material types
quality
craftsmanship
creator signature
decoration
engraving
artifact status
wear
damage
ownership history
```

### Components

```python
MaterialComponent
ItemQualityComponent
CraftsmanshipComponent
CreatorComponent
DecorationComponent
EngravingComponent
ArtifactComponent
WearComponent
```

### Systems

```text
MaterialPropertySystem
CraftQualitySystem
ArtifactNamingSystem
DecorationSystem
ItemHistorySystem
WearSystem
```

### Events

```text
ItemCraftedEvent
ArtifactCreatedEvent
ItemDecoratedEvent
ItemNamedEvent
ItemDamagedEvent
```

---

## 12.3 Workshops and production chains

### Mechanics

```text
workshops
labor assignments
materials
intermediate goods
production orders
manager role
bookkeeper role
quality
stock monitoring
```

### Components

```python
WorkshopComponent
ProductionOrderComponent
ManagerComponent
BookkeeperComponent
MaterialRequirementComponent
OutputComponent
```

### Systems

```text
WorkshopJobSystem
ProductionOrderSystem
ManagerSystem
BookkeeperSystem
MaterialReservationSystem
QualityOutputSystem
```

### Events

```text
ProductionOrderCreatedEvent
WorkshopJobStartedEvent
WorkshopJobCompletedEvent
StockCountUpdatedEvent
```

---

## 12.4 Strange moods and artifacts

### Mechanics

```text
inspired character seizes workshop
demands materials
creates artifact
fails if materials unavailable
mood consequences
legend entry
```

### Components

```python
StrangeMoodComponent
ArtifactDemandComponent
ClaimedWorkshopComponent
ArtifactProjectComponent
```

### Systems

```text
StrangeMoodTriggerSystem
ArtifactDemandSystem
WorkshopClaimSystem
ArtifactConstructionSystem
MoodFailureSystem
ArtifactLegendSystem
```

### Events

```text
StrangeMoodStartedEvent
WorkshopClaimedEvent
ArtifactMaterialsDemandedEvent
ArtifactCompletedEvent
StrangeMoodFailedEvent
```

---

## 12.5 Nobles, mandates, justice

### Mechanics

```text
noble positions
room demands
mandates
export bans
criminal accusations
jail
punishment
social unrest
```

### Components

```python
NobleComponent
MandateComponent
DemandComponent
JusticeComponent
CrimeRecordComponent
PunishmentComponent
```

### Systems

```text
NobleAppointmentSystem
MandateSystem
DemandSatisfactionSystem
JusticeSystem
CrimeInvestigationSystem
PunishmentSystem
```

### Events

```text
NobleAppointedEvent
MandateIssuedEvent
MandateViolatedEvent
DemandUnmetEvent
CrimeAccusedEvent
PunishmentAppliedEvent
```

---

## 12.6 Tantrum spirals and social collapse

### Mechanics

```text
negative events create thoughts
thoughts create stress
stress creates violence/destruction
violence creates more grief
colony spirals
```

### Components

```python
TantrumRiskComponent
SocialCollapseComponent
GriefComponent
ViolenceImpulseComponent
```

### Systems

```text
TantrumRiskSystem
TantrumTriggerSystem
GriefPropagationSystem
SocialCollapseSystem
RecoverySystem
```

### Events

```text
TantrumStartedEvent
ItemDestroyedInTantrumEvent
SocialFightStartedEvent
GriefPropagatedEvent
ColonyCollapseWarningEvent
```

---

## 12.7 Taverns, temples, libraries, hospitals

### Mechanics

```text
public spaces
visitors
performers
scholars
doctors
patients
religion
books
social rooms
special storage
institution reputation
```

### Components

```python
TavernComponent
TempleComponent
LibraryComponent
HospitalComponent
VisitorComponent
PerformerComponent
ScholarComponent
PatientComponent
InstitutionReputationComponent
```

### Systems

```text
InstitutionRoleSystem
VisitorSystem
PerformanceSystem
ScholarshipSystem
HospitalCareSystem
ReligiousServiceSystem
LibraryUseSystem
```

### Events

```text
VisitorArrivedEvent
PerformanceStartedEvent
BookWrittenEvent
PatientAdmittedEvent
ReligiousServiceHeldEvent
InstitutionReputationChangedEvent
```

---

# 13. Cross-package mechanics we should expect

These are not one game’s feature. They are the glue that makes all packages interact.

## 13.1 Perception, attention, and stimuli

### Mechanics

```text
visible entities
audible events
attention shifts
stimuli
overhearing, later
stealth, later
sleeping perception
downed perception
```

### Components

```python
PerceptionComponent
HearingComponent
VisibilityComponent
StimulusComponent
AttentionComponent
NoiseComponent
SneakingComponent
```

### Systems

```text
PerceptionSystem
VisibilitySystem
HearingSystem
StimulusSystem
AttentionSystem
NoisePropagationSystem
StealthDetectionSystem
```

### Events

```text
EntitySeenEvent
NoiseHeardEvent
StimulusCreatedEvent
AttentionShiftedEvent
CharacterDetectedEvent
```

---

## 13.2 Notes, memory, and reflection

### Mechanics

```text
private notes
remember/search notes
vector memory
recent context
reflections
summaries
shared notes, v2
physical writing
```

### Components

```python
NoteEntryComponent
MemoryProfileComponent
RecentContextComponent
ReflectionComponent
WritableComponent
ReadableComponent
```

### Services and projections

```text
NoteService
MemoryWriteService
VectorSearchService
ReflectionTriggerSystem
ReflectionResultHandler
RecentContextProjection
PhysicalWritingSystem
```

### Actions

```text
take note
remember/search notes
write on item
read item
share note, v2
reflect, maybe LLM-only
```

### Events

```text
NoteTakenEvent
NotesSearchedEvent
MemoryWrittenEvent
ReflectionRequestedEvent
ReflectionCreatedEvent
PhysicalTextWrittenEvent
ReadableTextReadEvent
```

Physical writing is persisted on the readable object and is also eligible for shared
world history and physical mark projection, so later prompts can cite authored marks
after save/reload.

---

## 13.3 Economy, value, trade, and ownership

### Mechanics

```text
money
barter
prices
supply/demand
shops
markets
ownership
theft policy
gifts
taxes/bills
wages
rent
```

### Components

```python
CurrencyComponent
ValueComponent
PriceComponent
MarketComponent
ShopComponent
TradeOfferComponent
OwnershipComponent
BillComponent
WageComponent
```

### Systems

```text
ValuationSystem
TradeSystem
ShopInventorySystem
MarketPriceSystem
OwnershipSystem
TheftPolicySystem
GiftSystem
BillsSystem
WageSystem
```

### Actions

```text
buy
sell
trade
gift
pay
charge rent
set price
steal, if policy allows
```

### Events

```text
TradeOfferedEvent
TradeCompletedEvent
ItemBoughtEvent
ItemSoldEvent
GiftGivenEvent
OwnershipChangedEvent
TheftDetectedEvent
BillPaidEvent
```

---

## 13.4 Crafting, recipes, tools, and workstations

### Components

```python
RecipeComponent
IngredientComponent
ToolComponent
WorkstationComponent
CraftingTaskComponent
CraftingSkillRequirementComponent
OutputQualityComponent
```

### Systems

```text
RecipeDiscoverySystem
CraftingValidationSystem
CraftingProgressSystem
ToolRequirementSystem
QualityRollSystem
CraftingXPSystem
```

### Actions

```text
craft
cook
brew
forge
sew
carve
repair
upgrade
dismantle
```

---

## 13.5 Construction and world modification

### Components

```python
BuildSiteComponent
BlueprintComponent
ConstructionMaterialComponent
StructureComponent
TerrainModificationComponent
RoomExpansionComponent
```

### Systems

```text
BlueprintPlacementSystem
ConstructionJobSystem
MaterialDeliverySystem
BuildCompletionSystem
TerrainModificationSystem
RoomExpansionSystem
```

### Actions

```text
place blueprint
build
dig
expand room
repair structure
demolish
decorate
```

---

## 13.6 Resource nodes and regeneration

### Components

```python
ResourceNodeComponent
HarvestableComponent
RegenerationComponent
DepletionComponent
SeasonalResourceComponent
```

### Systems

```text
ResourceSpawnSystem
HarvestSystem
ResourceDepletionSystem
ResourceRegenerationSystem
SeasonalResourceSystem
```

---

## 13.7 Animals, wildlife, monsters

### Components

```python
AnimalComponent
WildlifeComponent
MonsterComponent
TameableComponent
AggressionComponent
HabitatComponent
PredatorComponent
PreyComponent
```

### Systems

```text
WildlifeSpawnSystem
AnimalBehaviorSystem
TamingSystem
PredatorPreySystem
MonsterEncounterSystem
AnimalNeedsSystem
```

---

## 13.8 Fire, fluids, weather hazards

**Implemented foundation:** `WeatherComponent`, `ShelterComponent`,
`MoistureComponent`, fire spread/damage, and canonical shelter resolution. Flooding,
drainage, smoke, structural weather damage, and weather-driven temperatures remain in
the backlog.

### Components

```python
FireComponent
FlammableComponent
MoistureComponent
ShelterComponent
SmokeComponent
WaterComponent
FloodComponent
WeatherHazardComponent
```

### Systems

```text
FireSpreadSystem
SmokeSystem
ExtinguishSystem
FloodSystem
WeatherHazardSystem
StructuralDamageSystem
```

---

## 13.9 Policies and boundaries

### Components

```python
WorldPolicyComponent
CharacterBoundaryComponent
PvPPolicyComponent
ContentPolicyComponent
PluginPolicyComponent
```

### Systems

```text
BoundaryPolicySystem
PvPPolicySystem
AdultContentPolicySystem
TheftPolicySystem
AdminPolicySystem
```

### Rules already decided

```text
Denied boundary tags always win.
Admins cannot override character boundaries.
PvP means player-player combat, not dialogue.
Pickpocketing is disabled for now.
Pregnancy cannot begin while suspended.
Suspended characters cannot die.
```

---

# 14. Package-level class inventory

This is the class taxonomy I would expect in the codebase.

## 14.1 Component classes

```text
IdentityComponent
DescriptionComponent
TagComponent
PhysicalComponent
ContainerComponent
InventoryComponent
WeightComponent
InteractiveComponent
PortableComponent
ActionPointsComponent
FocusPointsComponent
InitiativeComponent
CharacterComponent
SuspendedComponent
DownedComponent
DeadComponent
HungerComponent
ThirstComponent
SleepNeedComponent
AffectComponent
ThoughtComponent
TraitSetComponent
PreferenceComponent
SkillSetComponent
SocialBond
RoomComponent
RoomSummaryComponent
WeatherComponent
TemperatureComponent
CropComponent
JobComponent
QuestComponent
FactionComponent
MemoryProfileComponent
NoteEntryComponent
DeviceComponent
NetworkNodeComponent
SurveillanceCoverageComponent
CredentialComponent
HeatComponent
WantedLevelComponent
ImplantComponent
DinosaurComponent
EggComponent
TamingComponent
EnclosureComponent
KaijuComponent
```

## 14.2 Edge classes

```text
Contains
ExitTo
Holding
Wearing
Owns
ReservedBy
ControlledBy
DefaultController
SocialBond
RecordedBy
HasThought
HasGoal
MemberOfFaction
ParentOf
PartnerOf
AssignedTo
Targets
ParticipatingIn
```

## 14.3 System classes

```text
WorldClockSystem
ActionPointRegenSystem
FocusPointRegenSystem
HungerSystem
ThirstSystem
SleepNeedSystem
AffectAggregationSystem
ThoughtCreationSystem
JobDiscoverySystem
JobAssignmentSystem
TaskProgressSystem
CropGrowthSystem
WeatherSystem
TemperatureExposureSystem
CombatSystem
DownedSystem
DeathSystem
RoomQualitySystem
SkillXPSystem
RelationshipUpdateSystem
StorytellerIncidentSystem
MemoryWriteSystem
SurveillanceSystem
HackingSystem
HeatSystem
WantedLevelSystem
ImplantInstallationSystem
CreatureNeedSystem
TamingSystem
EnclosureIntegritySystem
KaijuIncidentSystem
```

## 14.4 Action handlers

```text
MoveActionHandler
TakeActionHandler
DropActionHandler
PutActionHandler
UseActionHandler
EatActionHandler
DrinkActionHandler
SayActionHandler
TellActionHandler
TakeNoteActionHandler
RememberActionHandler
WriteActionHandler
SleepActionHandler
WakeActionHandler
CraftActionHandler
BuildActionHandler
HarvestActionHandler
FightActionHandler
TradeActionHandler
GiftActionHandler
ResearchActionHandler
HackActionHandler
AccessTerminalActionHandler
InstallImplantActionHandler
TrackCreatureActionHandler
TameCreatureActionHandler
TrainCreatureActionHandler
TriggerContainmentActionHandler
```

## 14.5 Services

```text
CommandQueueService
CommandValidationService
ControllerRegistry
PromptContextBuilder
RoomSummaryProjection
MemorySearchService
VectorStoreService
WorldGenerationService
PathfindingService
ReachabilityService
ContainmentQueryService
PolicyService
SkillCheckService
RandomnessService
PersistenceService
PluginLoader
```

## 14.6 Generators

```text
WorldBuilderGenerator
RoomGraphGenerator
NPCGenerator
ItemGenerator
QuestGenerator
IncidentGenerator
FactionGenerator
CropGenerator
DungeonGenerator
LoreGenerator
DistrictGenerator
CyberpunkMissionGenerator
CreatureHabitatGenerator
ApexIncidentGenerator
```

## 14.7 Projections

```text
RoomSummaryProjection
RecentContextProjection
CharacterStatusProjection
InventoryProjection
NotesIndexProjection
EventTimelineProjection
WorldMapProjection
FactionRelationProjection
QuestLogProjection
SecurityMapProjection
WantedStatusProjection
HerdStatusProjection
ContainmentProjection
```

## 14.8 Typed events

```text
ActorMovedEvent
ItemTakenEvent
ItemDroppedEvent
FoodEatenEvent
DrinkConsumedEvent
SpeechSaidEvent
SpeechToldEvent
NoteTakenEvent
NotesSearchedEvent
ThoughtCreatedEvent
AffectChangedEvent
NeedChangedEvent
JobAssignedEvent
TaskCompletedEvent
CropGrewEvent
CropHarvestedEvent
RaidStartedEvent
QuestCompletedEvent
CharacterDownedEvent
CharacterDiedEvent
CharacterRevivedEvent
ControllerChangedEvent
PluginLoadedEvent
HackSucceededEvent
HeatChangedEvent
WantedLevelChangedEvent
ImplantInstalledEvent
CreatureTamedEvent
EggHatchedEvent
CreatureEscapedEvent
KaijuArrivedEvent
```

---

# 15. Suggested build ordering

Even though this is an exhaustive catalogue, the implementation order should be ruthless.

## Phase 1 — foundation

Status: implemented for the current parity pass.

```text
Relics ECS world
plugin loader
typed events
command queue
Action/Focus points
controllers
Contains model
room graph
prompt builder
notes/remember
say/tell with intent
threaded conversation micro-loop
hunger/thirst
sleep/suspended
basic LLM/Discord handoff
```

## Phase 2 — life sim

Status: implemented for the current parity pass.

```text
affect/thoughts
traits/preferences
social bonds
needs expansion
skills
households
private memory
romance boundaries
pregnancy gates
```

## Phase 3 — colony sim

Status: implemented for the current parity pass.

```text
jobs
reservations
stockpiles
hauling
crafting
rooms/quality
research
storyteller incidents
downed/revival
medicine
```

## Phase 4 — garden sim

Status: implemented for the current parity pass.

```text
seasons
crops
watering
fertilizer
animals
foraging
villagers/gifts
festivals
processing machines
```

## Phase 5 — barbarian sim

Status: implemented for the current parity pass.

```text
temperature exposure
combat
weapons/armor
base building
building decay/upgrade/demolish
thralls/followers
survival gaps
purges/siege readiness
rituals/shrines/blessings/curses
dungeons/danger zones/bosses/treasure/key loops
corruption/sorcery hooks
climbing as a skill or traversal gate only
PvP policies
```

## Phase 6 — dragon sim

Status: implemented for the current parity pass. Fast travel and richer world-map
features remain v2.

```text
quest tracking/branching/decline
factions
skills-by-use
perks
persuasion/surrender/report-crime
magic regen/cooldowns
fixed magic
dungeons
dragons/ancient beasts encounter options
voice powers
radiant quests
crime/bounty
artifact identification
```

## Phase 7 — dagger sim

Status: implemented for the current parity pass.

```text
expandable world frontiers
regions/settlements
rumors as expansion seeds
procedural quests/refusal/abandon/extension/lie
institutions/services/directories/promotion/dues
regional/institutional reputation
banks/loans/debt/property/letters of credit/safe storage/debt collectors
civic law/courts/sentences
travel logistics/lodging/camping/supplies/interruptions
procedural dungeons
custom classes
custom spells/enchantments/potions/recharge/ingredient identification
etiquette/streetwise
language skills/pacification
supernatural afflictions/incubation/stigma/cure quest hooks
```

## Phase 8 — void sim

Status: implemented for the current parity pass.

```text
ships/stations/habitats
life support/pressure/airlocks
space travel/orbits/navigation
crew roles/duty shifts/away teams/morale/mutiny
technology/research/fabrication/drones/ship AI/data salvage
alien contact/xenobiology/contamination/protocol/trade
space hazards/damage control/decompression/reactor/gravity/boarding
contracts/salvage/cargo/passengers/survey/mining
frontier economy/customs/smuggling/insurance/mortgages
```

## Phase 9 — nuke sim

Status: implemented for the current parity pass.

```text
radiation sources/exposure
rad shielding/decontamination
mutation pressure/outcomes
hotspot marking/suppressants
sample harvesting/study
wasteland scavenging
junk/scrap resource chains
locked crates/artifacts/faction salvage
jury-rigged crafting/schematics/mods/field repair
rad medicine/dirty water
chem brewing
settlement salvage hooks
old-world tech leads/beacons/trader routes/raider pressure/terminal boot
```

## Phase 10 — neon sim

Status: implemented for the current parity pass.

```text
districts/security zones/public and restricted access
credentials/access levels/checkpoints/safehouses
devices/networks/surveillance/cameras/evidence
hacking actions/traces/data extraction/backdoors
black markets/data brokers/contraband/informants
heat/wanted levels/bounties
cybernetics/implant slots/clinics/maintenance
fixers/handlers/runner contracts/blackmail/extractions
city expansion worldgen/demo world
```

## Phase 11 — dino sim

Status: implemented for the current parity pass.

```text
species/ecology/creature needs
fossil survey/excavation/cleaning/stabilization
eggs/inspection/breeding/brooding/incubation/hatching
lab incubation
imprinting/juvenile care
water creature study tracks
tracking/taming/training
companions/mounts/guards
enclosures/containment panic
escapes/stampedes
apex predators/kaiju incidents
ranch production
```

## Phase 12 — fortress sim

Status: planned and out of scope for the current parity pass.

```text
deep materials
world history
civilizations
artifacts
nobles/justice
institutions
tantrum spirals
multi-site worlds
legends mode
```

---

# 16. The most important design warning

The dangerous failure mode is trying to make one giant `SimulationSystem`.

Do not.

The design should stay plugin-shaped:

```text
hunger owns hunger
thirst owns thirst
sleep owns sleep
crops own crop growth
weather owns weather
combat owns combat
jobs own jobs
memory owns memory
```

Systems may read broadly but write narrowly.

Good:

```text
ThirstSystem reads TemperatureComponent.
ThirstSystem writes ThirstComponent.
ThirstSystem emits ThirstChangedEvent.
```

Bad:

```text
TemperatureSystem directly mutates thirst, mood, health, prompts, death, and social behavior.
```

Emergence comes from event-driven interaction among small systems, not from one giant god-object.

This mechanics catalogue is enough to start turning bunnyland into a real backlog: every package can now be split into `components.py`, `systems.py`, `actions.py`, `events.py`, `prompt.py`, `policy.py`, and `tests/`.

[1]: https://www.ea.com/games/the-sims/the-sims-4/news/whims-aspirations-goals?utm_source=chatgpt.com "Whims, Aspirations, and Goals: A Quick Guide"
[2]: https://store.steampowered.com/app/294100/RimWorld/?utm_source=chatgpt.com "RimWorld on Steam"
[3]: https://rimworldwiki.com/wiki/Mental_break?utm_source=chatgpt.com "Mental break"
[4]: https://www.conanexiles.com/?utm_source=chatgpt.com "Conan Exiles"
[5]: https://conanexiles.fandom.com/wiki/Thrall?utm_source=chatgpt.com "Thrall - Official Conan Exiles Wiki - Fandom"
[6]: https://stardewvalleywiki.com/Crops?utm_source=chatgpt.com "Crops"
[7]: https://en.wikipedia.org/wiki/The_Elder_Scrolls_V%3A_Skyrim?utm_source=chatgpt.com "The Elder Scrolls V: Skyrim"
[8]: https://en.wikipedia.org/wiki/Dwarf_Fortress?utm_source=chatgpt.com "Dwarf Fortress"
