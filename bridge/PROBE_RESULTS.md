# What a script can do in Civ 6 (probe results)

Found by experiment through Civ 6's tuner port (macOS, Steam, Gathering Storm), in single-player test games.
Each "probe" below was one round of tests; the code cites them by number. Lua state indexes (`GameCore_Tuner`,
`InGame`) change between sessions: always look them up.

## Probe 1: what a script can do for an AI civ

| Area | Call | Result |
| --- | --- | --- |
| Research | GameCore `Techs:SetResearchingTech` | Applied at once, **kept** through AI turns |
| Civics | GameCore `Culture:SetProgressingCivic` | Applied at once, **kept** through AI turns |
| Research / civics | InGame `UI.RequestPlayerOperation(ai, RESEARCH / PROGRESS_CIVIC)` | **Ignored** (only works for the local player) |
| War | GameCore `Diplomacy:DeclareWarOn(B, WarTypes.FORMAL_WAR, true)` | Works (`CanDeclareWarOn` was true) |
| Peace | GameCore `Diplomacy:MakePeaceWith(B, true)` | Works even when `CanMakePeaceWith` is false (bypasses the 10-turn rule) |
| War | InGame `DiplomacyManager.RequestSession(A, B, "DECLARE_SURPRISE_WAR")` | **Ignored** |
| Friendship | GameCore `Diplomacy:SetHasDeclaredFriendship(C, true)` | Works both ways, **kept** |
| Alliance | GameCore `Diplomacy:SetHasAllied(C, true)` | Works both ways, **kept**; alliance type unknown |
| Production | InGame `CityManager.RequestOperation(aiCity, BUILD, ...)` | **Ignored** (`CanStartOperation` said true) |
| Production | GameCore `BuildQueue` | No "set current production". Has `CreateIncompleteDistrict`, `CreateIncompleteBuilding`, `CreateDistrict`, `CreateBuilding`, `AddProgress`, `FinishProgress`, `Remove*`. `CanProduce` = "Not Implemented" |
| Unit builds | GameCore `Units:SetBuildDisabled`, AI `Military:AllowUnitConstruction` | Exist (see probe 2) |
| Reads | `Ai_Diplomacy:GetDiplomaticState(p)` (string, e.g. `DIPLO_STATE_ALLIED`), `GetDiplomaticScore(p)` | Work in GameCore. (`GetDiplomaticAI` is InGame only) |
| AI military | `Ai_Military:SetRival`, `PrepareForWarWith`, `StartScriptedOperation*` | Exist; see probe 2 |
| Turn hooks | GameCore `GameEvents.PlayerTurnStarted`, `Events.PlayerTurnActivated` | Exist; see probe 2 |
| Local player | GameCore `PlayerManager.SetLocalPlayerAndObserver` | Exists; not used (would hijack the human's interface) |

Rule of thumb: **GameCore state changes work for AI civs; InGame "requests" only work for the local player.**

## Probe 2: steering

**Crash warning:** a call with a wrong argument type can segfault Civ 6 (pcall can't catch it).
`CreateIncompleteDistrict(row.Hash, plot)` crashed the game. Copy signatures from the game's own
scripts: `Civ6.app/Contents/Assets/Debug/*.ltp` (FireTuner panels, mostly GameCore_Tuner) and
`Assets/Base/Assets/UI/Tuner/*.lua`. Run risky tests one command at a time.

| Lever | Call (signature from the game's scripts) | Result |
| --- | --- | --- |
| District placement | `BuildQueue:CreateIncompleteDistrict(district.Index, plotIndex, 0)`; tiles from InGame `CityManager.GetOperationTargets` | Placed and **kept**; the AI decides when to build it (the tile is fixed) |
| Forbid a unit | `Units:SetBuildDisabled(unit.Index, true)` | **Kept** |
| Opinion | `Ai_Diplomacy:AdjustBaseDiplomacy(player, n)` | Score changes and is **kept**. Used as the stand-in for denounce |
| End alliance | `SetHasAllied(p, false)` | **Does nothing**; the alliance stays (also after removing friendship) |
| End friendship | `SetHasDeclaredFriendship(p, false)` | Works both ways |
| War + rival | `DeclareWarOn` + `Ai_Military:SetRival(p)` | War kept; allies of the target joined |
| War prep (no war) | `Ai_Military:PrepareForWarWith(p, true)` + `SetRival(p)` + `ScriptForceUpdateTargets()` (native sig `PrepareForWarWith(PlayerTypes, bool)`) | No crash, but **no visible effect** over 5-6 turns: no new units, no move toward the target, no war |
| Scripted attack | `DeclareWarOn` + `SetRival` + `Ai_Military:StartScriptedOperationWithTargetAndRally("Attack Enemy City", B, targetPlotIndex, rallyPlotIndex)` + `AddUnitToScriptedOperation(op, unitID)` (as in Firaxis's NubiaScenario.lua; the operation needs war) | No crash; returned an operation id and took the units, which closed on the target over several turns. Slow, and hard to tell from Civ's own war operation |
| Turn hook | `GameEvents.PlayerTurnStarted.Add(...)` from the tuner | **Never fired.** Lua globals persist between commands, handlers don't run |

Consequence: steering is applied by the bridge whenever it sees a new turn (it re-sets research and civics to
the leader's goal), not by in-game hooks.

## Probe 3: everything that isn't moving units (GameCore unless noted)

Signatures from the game's own scripts (DLC scenario `Scripts/*.lua` run in GameCore) or the native signatures in
`DLC/Expansion2/Binaries/Win64/GameCore_XP2_FinalRelease.map`.

| What | Call | Result |
|---|---|---|
| Government | `Culture:SetCurrentGovernment(GameInfo.Governments[X].Index)` (returns true) | **Works, with Civ's rules.** A second switch soon after puts the civ in anarchy (no government, 0 policy slots, ~3 turns) before the new one starts. Treat a change as costly. |
| Read government | `Culture:GetCurrentGovernment()` | **InGame only** (nil in GameCore). Also `IsInAnarchy()`, `GetAnarchyEndTurn()` there. Unlock check in GameCore: the government's `PrereqCivic` via `HasCivic(index)` (`IsGovernmentUnlocked` takes a hash in the UI, an index in GameCore: avoid). |
| Policies | `ClearPolicySlot`, `GetSlotPolicy`, `IsPolicyActive` | Only clearing is scriptable; `SlotPolicy` has no Lua binding. |
| Gold gift / tribute | `Treasury:ChangeGoldBalance(-n)` on A, `(+n)` on B | Works |
| Deal | `DealManager.GetWorkingDeal(DealDirection.OUTGOING, A, B)`, `deal:AddItemOfType(DealItemTypes.GOLD, A)`, `item:SetDuration(0)`, `item:SetAmount(n)`, `deal:Validate()` (1 = VALID), `DealManager.EnactWorkingDeal(A, B)` (returns 1) | **Works**, no crash. The same API carries resources, open borders and gold per turn (see probe 4). |
| Ban a unit type | `Units:SetBuildDisabled(GameInfo.Units[X].Index, true/false)` | Works both ways |
| Envoy | `Influence:GiveFreeTokenToPlayer(minorId)` | Works (can change who is suzerain), but it's a **free** envoy. Fair use: only when `GetTokensToGive() > 0`, then `ChangeTokensToGive(-1)`; Civ's AI usually spends its own tokens first. |
| Delegation | `Diplomacy:SetHasDelegationAt(B, true)` (Firaxis scenarios do this) | Works and kept. |
| Unfinished building | `BuildQueue:CreateIncompleteBuilding(building.Index, 0)` (wonders/placed: `(index, plotIndex, 0)`, per TunerCityPanel.lua) | Placed (`HasBuildingBeenPlaced` true) and kept, but like districts, the AI decides when to build it. |
| Warnings / kudos | `SendWarningTo(B, uint, bool)`, `SendKudoTo` | Not tested: the uint is an unknown enum, and a bad value could crash. |

## Probe 4: deal items

What two civs can trade comes from Civ itself: `DealManager.GetPossibleDealItems(from, to, DealItemTypes.X, workingDeal)`
returns entries with `ForType` (resource index), `SubType` (agreement), `Duration`, `MaxAmount`, `IsValid`.
`DealItemTypes` / `DealAgreementTypes` values are hashes. Loop deal items with `for item in deal:Items()` (one value).

| Item | How | Result |
|---|---|---|
| Gold per turn | GOLD item, `SetDuration(30)`, `SetAmount(n)` | Works: an ongoing deal that ends 30 turns later |
| Luxury resource | RESOURCES item from the owner, `SetValueType(resource.Index)`, `SetAmount(1)`, `SetDuration(30)` | Works (the receiver's `GetResourceAmount` doesn't count it) |
| Strategic resource | RESOURCES item, `SetDuration(0)`, `SetAmount(n)` (Gathering Storm stockpile) | Works |
| Open borders | AGREEMENTS item from each side, `SetSubType(DealAgreementTypes.OPEN_BORDERS)`, duration from the possible-items entry | Works both ways |
| Diplomatic favor | FAVOR item, `SetAmount(n)`; read favor with `Players[p]:GetFavor()` (InGame only) | Works |
| Cities | `deal:AddItemOfType(DealItemTypes.CITIES, from, to, entry.SubType, entry.ForType)`, then `SetSubType(entry.SubType)`, `SetValueType(entry.ForType)` (copied from DiplomacyDealView.lua; `ForType` is the city id, and ids repeat across players) | **Works**, no crash. Capitals are never offered. |
| Alliance, joint war, research agreement | AGREEMENTS subtypes exist | Not offered in tests (prerequisites) |
| Ongoing deals | `DealManager.GetPlayerDeals(a, b)` -> list of deals; lump sums vanish once paid | Read works; used to spot deals Civ's AI made on its own |

Tuner gotcha: a top-level `return` in a chunk also skipped the appended `print("---END---")`, so every early
refusal looked like a 20 s timeout. `Tuner.run` wraps chunks in `(function() ... end)()`.

## Probe 5: settling, embassies, World Congress

| What | Call | Result |
|---|---|---|
| Send a settler to a site | `Ai_Military:StartScriptedOperationWithTargetAndRally("Settle New City", -1 or P, plot, rally)` | **Refused** (returns -1 either way) |
| Found a city at a chosen site | `Players[P]:GetCities():IsValidFoundLocation(x, y)`, `:Create(x, y)`, then `UnitManager.Kill(settler, false)` (as in Firaxis scenario scripts) | **Works**. The bridge only does it when a settler is within 8 tiles. |
| Embassy | `Diplomacy:SetHasEmbassyAt(B, true/false)`; Civ's rule: `CIVIC_DIPLOMATIC_SERVICE`, 50 gold (`GameInfo.DiplomaticActions`) | Works both ways |
| World Congress, emergencies | InGame `Game.GetWorldCongress():GetMeetingStatus/GetResolutions/GetProposals/GetEmergencies`, `Game.GetEmergencyManager()` | Readable. Voting and joining emergencies only via `UI.RequestPlayerOperation` (local player only), so AI votes aren't scriptable. |

## Probe 6: fights, spies, treaties, emergencies
- `GameEvents.OnCombatOccurred(attPlayer, attUnit, defPlayer, defUnit, attDistrict, defDistrict)` fires in the
  GameCore_Tuner state; a handler added from the tuner logs real fights. Hook once per Lua state; keep the handler
  a global so it can be replaced without reloading.
- InGame: `GetDiplomacy():GetDeclaredFriendshipTurn(b)` + `Game.GetGameDiplomacy():GetDenounceTimeLimit()` (30)
  gives when a friendship expires; `GetAllianceTurnsUntilExpiration(b)` for alliances.
- InGame: `GetDiplomacy():GetNumSpiesCaptured()` / `GetNthCapturedSpy(captor, i)` -> {OwningPlayer, NameIndex,
  Level, Name}.
- InGame: `Game.GetEmergencyManager():GetEmergencyInfoTable(p)` -> {NameText, TargetID, MemberIDs, TurnsLeft,
  HasBegun, GoalDescription, ...}.
- `plot:GetOwner()` works in GameCore (units inside someone's borders).

## Probe 7: map directions, continents, map pins
- Grid: y grows north; odd rows sit half a tile east (from 51,14: NE 51,15, NW 50,15, SE 51,13, SW 50,13).
- `plot:GetContinentType()` -> `GameInfo.Continents[i].Description` (works in GameCore).
- Map pins (InGame, local player only): `PlayerConfigurations[me]:GetMapPin(x, y)` makes or returns the pin on
  that tile; `SetName`, `SetIconName("ICON_MAP_PIN_CIRCLE")`, `Network.BroadcastPlayerInfo()`; remove with
  `DeleteMapPin(id)`, look up with `GetMapPinID(id)`. Pins are saved with the game.

## Probe 8: the lock on war after a peace
- `GetDiplomacy():GetAtWarChangeTurn(b)` and `Game.GetGameDiplomacy():GetMinPeaceDuration()` (10) exist only in
  the InGame state; GameCore has neither. GameCore has `HasDeclaredFriendship`/`HasAllied`, InGame doesn't.
  So the fresh-peace check is read InGame and passed to the GameCore declaration as a flag.

## Probe 9: economy, cities, grievances, era, barbarians; Gift It To Me
- GameCore: `GetTreasury():GetGoldYield()` (before upkeep) / `GetTotalMaintenance()`, `GetTechs():GetScienceYield()`,
  `GetCulture():GetCultureYield()`, `GetReligion():GetFaithYield()`, `Player:GetEra()`, `city:GetPopulation()`,
  city center `GetDefenseStrength()`, `GetDamage/GetMaxDamage(DefenseTypes.DISTRICT_GARRISON / DISTRICT_OUTER)`,
  barbarian camps via `IMPROVEMENT_BARBARIAN_CAMP`.
- InGame only: `GetFavorPerTurn()`, `GetDiplomacy():GetGrievancesAgainst(b)` (one signed tally per pair: A's +N
  is B's -N), `GetGrowth():GetAmenities()/GetAmenitiesNeeded()/GetAmenitiesLostFromWarWeariness()`,
  `GetBuildQueue():GetCurrentProductionTypeHash()` (look it up in Units/Buildings/Districts/Projects).
- Gift It To Me (Steam Workshop 1683750352): `GameEvents.GiftUnit` / `PopulateUnit` are reachable from
  GameCore_Tuner; its script (Scripts/GiftItToMeNow.lua) kills the unit and makes the same one for the recipient
  where it stood. A ship gifted while standing in the giver's waters ended up off the map (x = -9999), where the
  recipient can't be; GIFT_LUA places the new unit on a free tile next to the recipient's city (water for ships).

## Probe 10: nukes, policies, city-states, trade, great people, governors, spies, disasters
- Both states: `GetWMDs():GetWeaponCount(GameInfo.WMDs[..].Index)`, `GetCulture():GetNumPolicySlots()/GetSlotPolicy(i)`,
  `GetInfluence():GetTokensReceived(civ)` / `GetSuzerain()`, city-state type from the leader's `InheritFrom`,
  `GetGreatPeoplePoints():GetPointsTotal(class)`, `Game.GetGreatPeople():GetTimeline()` (Claimant nil = up for grabs).
- InGame only: `GetCulture():GetCurrentGovernment()` for any civ, `city:GetTrade():GetOutgoingRoutes()`,
  `GetGovernors():GetGovernorList()` + `GetAssignedCity()`, spies (`GetSpyOperation`, `CityManager.GetCityAt`).
- Disasters: `Events.RandomEventOccurred(type, severity, x, y, ...)` hooked in InGame like the combat hook.

## Probe 11: the leader ribbon's numbers
- The ribbon (Base/Assets/UI/DiplomacyRibbon.lua) shows score, `GetStats():GetMilitaryStrengthWithoutTreasury()`,
  science and culture per turn, `GetTreasury():GetGoldBalance()` and `GetReligion():GetFaithBalance()`.
- The military figure is InGame only (GameCore has no GetStats); balances and score work in both. Briefings use
  these, so a number a player quotes from the ribbon matches what the leaders see.

## Probe 12: peace terms and pact payments
- At war, `GetPossibleDealItems` still lists gold, gold per turn, resources, favor and cities: peace terms use them.
- Peace with terms (one side pays gold): one acceptance made peace and moved the gold.
- Impossible terms (a resource the payer doesn't have): peace signed, the deal refused, war resumed.
- Breaking a fresh peace by declaring war again: allowed, and the event says so.
- A pact with a two-way payment went through Civ's DealManager on acceptance.

## Probe 13: notifications in the player's own Civ notification stack
- GameCore `NotificationManager.SendNotification(player, NotificationTypes.USER_DEFINED_1, headline, body, x, y)`
  works (a plain "!" icon). Used for leader messages and offers to the player.
- Civ's AI addresses the player through `Events.DiplomacyStatement(from, to, kVariants)` (InGame; kVariants has
  StatementType/StatementSubType, named with `DiplomacyManager.GetKeyName`, and SessionID) and closes a session
  with `DiplomacyManager.CloseSession(id)`, as DiplomacyActionView.lua does.
- Statement names (from Civ's text files and a live recording): MAKE_DEAL, MAKE_DEMAND, DECLARE_FRIEND,
  MAKE_ALLIANCE, OPEN_BORDERS, DELEGATION, RESIDENT_EMBASSY, KUDO, WARNING_*, DENOUNCE, MAKE_PEACE, DECLARE_WAR,
  DEFEAT, GREETING, FIRST_MEET*; subtypes like HUMAN_ACCEPT_DEAL mark replies to the player. QUIET_LUA closes
  AI-opened sessions except first meetings, war declarations and defeats.

## Probe 14: blocking Civ's own leader screen
- Clicking a leader portrait opens DiplomacyActionView in overview mode with no diplomacy session, so the
  DiplomacyStatement hook never sees it.
- The screen force-closes itself on LuaEvents.NaturalWonderPopup_Shown (OnBlockingPopupShown -> OnForceClose,
  its clean exit); raising NaturalWonderPopup_Closed right after undoes InGame's bulk-hide.
- Its own OnLeaderLoaded (Events.LeaderScreenFinishedLoading) un-hides the screen, and runs after our handler; a
  hidden screen ignores the force-close. So our handler un-hides it first
  (ContextPtr:LookUpControl("/InGame/DiplomacyActionView"):SetHide(false)). Opening it from the map and from
  inside the screen both close, with the interface intact.

## Probe 15: more decisions for the leaders?
- City focus: GameCore `city:GetCitizens():SetFavoredYield(YieldTypes.X, bool)` / `IsYieldFavored` work for AI
  civs. Reapplied each turn as a standing order in case Civ's AI resets it.
- Great people: GameCore `Game.GetGreatPeople()` has CanRecruitPerson(player, individual), GetRecruitCost,
  RecruitPerson, GrantPerson, CanPatronizePerson / GetPatronizeCost, but no patronize call (gold/faith buying is
  UI-only). Unclaimed people have Claimant -1 in GameCore. Recruiting is only tried when CanRecruitPerson says yes.
- Not controllable: governors (GameCore only has points), policy cards (ClearPolicySlot only, no way to set one),
  spy missions (GameCore UnitManager has MoveUnit/PlaceUnit/Kill/InitUnit but no operations).

## Probe 16: taking envoys back? (reads only)
- No. The live Influence object has no remove call in either state. GameCore_Tuner: CanGiveInfluence,
  CanReceiveInfluence, ChangeTokensToGive, GetMostTokensReceived, GetPoints*, GetSuzerain, GetTokensPerThreshold,
  GetTokensReceived, GetTokensToGive, GiveFreeTokenToPlayer. InGame adds the levy and Can/Is/SetGivingTokens
  calls. `ChangeTokensToGive` is the unspent pool, not placed envoys. The engine's `ChangeTokensReceivedModifier`
  isn't exposed to Lua, and no modifier effect removes envoys.
- The only way Civ takes envoys away (besides conquering the city-state): the Rise and Fall spy mission Fabricate
  Scandal (UNITOPERATION_SPY_FABRICATE_SCANDAL, 16 turns, offensive) in a city-state. The civs with the most envoys
  there lose 2 + 1 per spy level (ESPIONAGE_FABRICATE_SCANDAL_*_ENVOYS_REMOVED). Spy missions can't be scripted
  for AI civs (probe 15).

## Probe 17: Civ's own history (reads only)
- Historic Moments: `Game.GetHistoryManager():GetAllMomentsData(player, 1)` (arguments as in Civ's
  HistoricMoments.lua) works in **InGame only** (GameCore has no history manager) and returns any civ's moments,
  not just the local player's, back to turn 1. Fields: Turn, Type (a hash; -1672407476 is the goody-hut
  "Villagers bring gifts", skipped as noise), ActingPlayer, EraScore, GameEra, InstanceDescription (already
  localized text). Feeds the History page (`MOMENTS_LUA`, `Civ6Game.historic_moments`).
- GameSummary (InGame): 24 data sets with per-turn values for every player from turn 1, e.g.
  REPLAYDATASET_SCOREPERTURN, TOTALCITIESBUILT, SCIENCEPERTURN, CULTURE, TOTALGOLD, TOTALWARSDECLARED,
  TOTALCITIESCAPTURED. `GameSummary.CoalesceDataSet(i, GameConfiguration.GetStartTurn(), Game.GetCurrentGameTurn())`
  returns {player id: {value per turn}}. Not used yet (standings charts would use it).
