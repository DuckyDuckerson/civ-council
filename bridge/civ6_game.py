"""The real Civ 6 connection: the same `Game` interface as MockGame, backed by the running game.

    game = Civ6Game()                 # connects through the tuner port (EnableTuner 1)
    game.civs(), game.snapshot(0)     # reads
    game.apply(4, {"type": "declare_war", "target": 5})

What it does, and why (see bridge/PROBE_RESULTS.md for the tests behind each call):
- Everything goes through the GameCore_Tuner Lua state. GameCore changes work for AI civs;
  InGame "requests" only work for the local player, so they're used only for read-only queries.
- Only function signatures that the game's own scripts use (Debug/*.ltp, UI/Tuner/*.lua) or that
  the probes ran safely. A wrong argument type can crash Civ 6, and pcall can't catch that.
- Civ's rules are respected: war only when CanDeclareWarOn, alliances need declared friendship and Civil
  Service. One house rule: a peace both sides accepted is signed at once, even inside
  Civ's minimum war length (CanMakePeaceWith is only checked to say so).
- Peace, friendship and alliances are proposals kept here (Civ's own deal screen is UI-only);
  when accepted they become real treaties in the game.
- Civ moves the turn, not us: end_turn() reports what changed since last time, and
  wait_for_turns() calls back when you end your turn in Civ.
- Leaders only see civs they have met.
"""
from __future__ import annotations

import itertools
import json
import math
import re
import sys
import zlib
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from civ_council.game import (ACTION_TYPES, CITY_NAME_RE, ALLIED, FRIENDS, PEACE, PROPOSAL_ACTIONS,  # noqa: E402
                              PROPOSAL_LABELS, WAR, ActionResult, Civ, Proposal, deal_summary, deal_terms,
                              is_gift, side_text)
from tuner import Tuner  # noqa: E402


class GameNotReady(RuntimeError):
    """Civ 6 isn't reachable, or no game is loaded (main menu)."""


class LuaFailed(RuntimeError):
    pass


# ---- connection -----------------------------------------------------------------------
class Link:
    """One tuner connection shared by all threads. Reconnects and re-finds the Lua states as needed."""

    def __init__(self, host: str = "127.0.0.1", port: int | None = None, timeout: float = 20,
                 connect=Tuner):
        self.host, self.port, self.timeout = host, port, timeout
        self._connect = connect
        self._t = None
        self._gc: int | None = None
        self._ui: int | None = None
        self._lock = threading.Lock()

    def _open(self) -> None:
        try:
            t = self._connect(host=self.host, port=self.port, timeout=self.timeout)
        except OSError as e:
            raise GameNotReady(f"Can't reach Civ 6's tuner port ({e}). Is Civ 6 running with "
                               "EnableTuner 1 in AppOptions.txt?") from None
        t.handshake()
        gc = t.index_of("GameCore_Tuner")
        if gc is None:
            t.close()
            raise GameNotReady("Civ 6 answered, but without the game's Lua states: either no game is loaded "
                               "yet, or another program is already connected to the tuner (Civ only gives "
                               "them to the first client; close the other one, e.g. a running Civ Council).")
        self._t, self._gc, self._ui = t, gc, t.index_of("InGame")

    def close(self) -> None:
        with self._lock:
            if self._t:
                self._t.close()
            self._t = None

    def run(self, lua: str, ui: bool = False) -> list[str]:
        """Run Lua in GameCore (or InGame for read-only UI queries) and return its printed lines.

        Retries once on a dropped connection.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._t is None:
                        self._open()
                    if ui and self._ui is None:
                        raise GameNotReady("The game's InGame Lua state isn't available.")
                    out = self._t.run(self._ui if ui else self._gc, lua)
                    break
                except (ConnectionError, OSError):
                    self._t = None
                    if attempt == 2:
                        raise GameNotReady("Lost the connection to Civ 6.") from None
        lines = out.splitlines()
        for line in lines:
            if line.startswith("[Lua error]"):
                if "attempt to index" in line and "nil" in line and not lines[0].startswith("TURN"):
                    self._t = None   # a reload can renumber states; re-find them next time
                raise LuaFailed(line)
            if line.startswith("[timed out"):
                raise GameNotReady("Civ 6 didn't answer in time (is it busy loading?).")
        return lines


# ---- Lua --------------------------------------------------------------------------------
LUA_COMMON = r"""
local function esc(s) return (tostring(s):gsub("[|\n]", "/")) end
local function txt(key)
  if key == nil then return "" end
  local ok, v = pcall(function() return Locale.Lookup(key) end)
  return (ok and v and v ~= "") and v or key
end
"""

SNAPSHOT_LUA = LUA_COMMON + r"""
local majors = PlayerManager.GetWasEverAliveMajorIDs()
-- A game's fingerprint: where every civ started. Unlike Game.GetRandomSeed (which moves as the game
-- plays), it never changes, so a game keeps its memories across saves, reloads and restarts.
local starts = {}
for _, id in ipairs(majors) do
  local ok, plot = pcall(function() return Players[id]:GetStartingPlot() end)
  starts[#starts+1] = (ok and plot) and tostring(plot:GetIndex()) or "x"
end
print("TURN|" .. Game.GetCurrentGameTurn() .. "|" .. tostring(Game.GetLocalPlayer()) .. "|" .. table.concat(starts, "."))
for _, id in ipairs(majors) do
  local p, cfg = Players[id], PlayerConfigurations[id]
  local lt, ct = cfg:GetLeaderTypeName(), cfg:GetCivilizationTypeName()
  local alive = p:IsAlive()
  local mil, sci, cul, gold, cities = 0, 0, 0, 0, 0
  if alive then
    for _, u in p:GetUnits():Members() do
      local r = GameInfo.Units[u:GetType()]
      mil = mil + math.max(r.Combat or 0, r.RangedCombat or 0, r.Bombard or 0)
    end
    sci = math.floor(p:GetTechs():GetScienceYield())
    cul = math.floor(p:GetCulture():GetCultureYield())
    gold = math.floor(p:GetTreasury():GetGoldBalance())
    cities = p:GetCities():GetCount()
  end
  local leaderRow, civRow = GameInfo.Leaders[lt], GameInfo.Civilizations[ct]
  print(table.concat({"CIV", id, lt, ct, esc(txt(leaderRow and leaderRow.Name)), esc(txt(civRow and civRow.Name)),
    tostring(p:IsHuman()), tostring(alive), cities, mil, sci, cul, gold, p:GetScore()}, "|"))
end
for i, a in ipairs(majors) do
  if Players[a]:IsAlive() then
    local d = Players[a]:GetDiplomacy()
    for j, b in ipairs(majors) do
      if b ~= a and Players[b]:IsAlive() and d:HasMet(b) then
        local view = ""
        if not Players[a]:IsHuman() then
          local ok, s = pcall(function() return Players[a]:GetAi_Diplomacy():GetDiplomaticState(b) end)
          view = ok and tostring(s) or ""
        end
        if a < b then
          print(table.concat({"REL", a, b, tostring(d:IsAtWarWith(b)), tostring(d:HasDeclaredFriendship(b)), tostring(d:HasAllied(b))}, "|"))
        end
        print(table.concat({"MET", a, b, view}, "|"))
      end
    end
  end
end
-- City-states: who their suzerain is (they fight on the suzerain's side), their strength, who has met them.
for _, id in ipairs(PlayerManager.GetAliveMinorIDs()) do
  local p, cfg = Players[id], PlayerConfigurations[id]
  local row = GameInfo.Civilizations[cfg:GetCivilizationTypeName()]
  local ok, sz = pcall(function() return p:GetInfluence():GetSuzerain() end)
  local mil = 0
  for _, u in p:GetUnits():Members() do
    local r = GameInfo.Units[u:GetType()]
    mil = mil + math.max(r.Combat or 0, r.RangedCombat or 0, r.Bombard or 0)
  end
  local met, war = {}, {}
  for _, m in ipairs(majors) do
    if Players[m]:IsAlive() then
      local d = Players[m]:GetDiplomacy()
      if d:HasMet(id) then met[#met+1] = m end
      if d:IsAtWarWith(id) then war[#war+1] = m end
    end
  end
  print(table.concat({"CS", id, esc(txt(row and row.Name)), tostring(ok and sz or -1), mil,
                      table.concat(met, ","), table.concat(war, ",")}, "|"))
end
"""

LEADER_INFO_LUA = LUA_COMMON + r"""
for _, id in ipairs(PlayerManager.GetWasEverAliveMajorIDs()) do
  local lt = PlayerConfigurations[id]:GetLeaderTypeName()
  local names, descs = {}, {}
  for row in GameInfo.HistoricalAgendas() do
    if row.LeaderType == lt then
      local ag = GameInfo.Agendas[row.AgendaType]
      if ag then names[#names+1] = esc(txt(ag.Name)); descs[#descs+1] = esc(txt(ag.Description)) end
    end
  end
  print("AGENDA|" .. id .. "|" .. table.concat(names, "; ") .. "|" .. table.concat(descs, " "))
end
"""

TURN_LUA = 'print("NOW|" .. Game.GetCurrentGameTurn() .. "|" .. tostring(Players[Game.GetLocalPlayer()]:IsTurnActive()))'

# One snippet per action. A and B are filled in as integers; the snippet prints RES|ok|message or RES|no|reason.
ACTION_LUA = {
    "declare_war": r"""
local d = Players[A]:GetDiplomacy()
local early = false
if not d:CanDeclareWarOn(B) then
  -- House rule: a fresh peace can be broken at once. Only that block is lifted: Civ's other
  -- reasons (friends, allies, ...) still stand. FRESH comes from FRESH_PEACE_LUA (the InGame state: GameCore
  -- has no GetAtWarChangeTurn / GetMinPeaceDuration, probe 8).
  local okf, close = pcall(function() return d:HasDeclaredFriendship(B) or d:HasAllied(B) end)
  if not FRESH or d:IsAtWarWith(B) or not okf or close then print("RES|no|Civ 6 doesn't allow that war right now") return end
  early = true
end
d:DeclareWarOn(B, WarTypes.FORMAL_WAR, true)
if not Players[A]:IsHuman() then pcall(function() Players[A]:GetAi_Military():SetRival(B) end) end
print("RES|" .. (d:IsAtWarWith(B) and (early and "ok|early" or "ok|war declared") or "no|the declaration didn't take"))
""",
    "make_peace": r"""
local d = Players[A]:GetDiplomacy()
if not d:IsAtWarWith(B) then print("RES|no|not at war") return end
if not d:CanMakePeaceWith(B) then print("RES|no|Civ 6 doesn't allow peace yet (wars must last a while first)") return end
d:MakePeaceWith(B, true)
print("RES|" .. (d:IsAtWarWith(B) and "no|still at war" or "ok|peace made"))
""",
    "propose_friendship": r"""
local d = Players[A]:GetDiplomacy()
d:SetHasDeclaredFriendship(B, true)
print("RES|" .. (d:HasDeclaredFriendship(B) and "ok|friendship declared" or "no|the friendship didn't take"))
""",
    "propose_alliance": r"""
local function civil(p) return Players[p]:GetCulture():HasCivic(GameInfo.Civics["CIVIC_CIVIL_SERVICE"].Index) end
local d = Players[A]:GetDiplomacy()
if not d:HasDeclaredFriendship(B) then print("RES|no|alliances need declared friendship first") return end
if not (civil(A) and civil(B)) then print("RES|no|both sides need the Civil Service civic for an alliance") return end
d:SetHasAllied(B, true)
print("RES|" .. (d:HasAllied(B) and "ok|alliance formed" or "no|the alliance didn't take"))
""",
    # Civ 6 has no scriptable denounce; the target's AI (if any) takes it as a serious insult.
    "denounce": r"""
if not Players[B]:IsHuman() then Players[B]:GetAi_Diplomacy():AdjustBaseDiplomacy(A, -15) end
if Players[A]:GetDiplomacy():HasDeclaredFriendship(B) then Players[A]:GetDiplomacy():SetHasDeclaredFriendship(B, false) end
print("RES|ok|denounced")
""",
    # A peace both sides accepted (a house rule): signed even inside Civ's minimum war length.
    "agreed_peace": r"""
local d = Players[A]:GetDiplomacy()
if not d:IsAtWarWith(B) then print("RES|no|not at war") return end
local early = not d:CanMakePeaceWith(B)
d:MakePeaceWith(B, true)
print("RES|" .. (d:IsAtWarWith(B) and "no|still at war" or (early and "ok|early" or "ok|peace made")))
""",
    # Overruling Civ's own AI (not offered as normal actions): undo a war or peace it made for a leader.
    "force_peace": r"""
local d = Players[A]:GetDiplomacy()
if not d:IsAtWarWith(B) then print("RES|no|not at war") return end
d:MakePeaceWith(B, true)
print("RES|" .. (d:IsAtWarWith(B) and "no|still at war" or "ok|peace made"))
""",
    "force_war": r"""
local d = Players[A]:GetDiplomacy()
if d:IsAtWarWith(B) then print("RES|no|already at war") return end
d:DeclareWarOn(B, WarTypes.FORMAL_WAR, true)
if not Players[A]:IsHuman() then pcall(function() Players[A]:GetAi_Military():SetRival(B) end) end
print("RES|" .. (d:IsAtWarWith(B) and "ok|back at war" or "no|the declaration didn't take"))
""",
    # SetHasAllied(B, false) doesn't end an alliance (probe 2), so only friendships can be broken.
    "break_alliance": r"""
local d = Players[A]:GetDiplomacy()
if d:HasAllied(B) then print("RES|no|Civ 6 alliances can't be broken early; they run until they expire") return end
if not d:HasDeclaredFriendship(B) then print("RES|no|no friendship to break") return end
d:SetHasDeclaredFriendship(B, false)
print("RES|" .. (d:HasDeclaredFriendship(B) and "no|still friends" or "ok|friendship ended"))
""",
}


# A deal, enacted through Civ's own deal system (probe 3/4). A, B and ITEMS (built by _deal_items) filled in.
# Each item is checked against what Civ itself says is tradeable (GetPossibleDealItems), which also gives
# the right duration (luxuries and gold per turn last 30 turns, open borders longer, lump sums 0).
DEAL_LUA = r"""
DealManager.ClearWorkingDeal(DealDirection.OUTGOING, A, B)
local deal = DealManager.GetWorkingDeal(DealDirection.OUTGOING, A, B)
if deal == nil then print("RES|no|the game has no deal channel between them") return end
local function possible(from, to, typ)
  local ok, l = pcall(function() return DealManager.GetPossibleDealItems(from, to, typ, deal) end)
  return (ok and l) or {}
end
local function add(s)
  local to = (s.from == A) and B or A
  local typ = DealItemTypes[s.kind]
  for _, e in ipairs(possible(s.from, to, typ)) do
    local match = true
    if s.kind == "GOLD" then match = (s.per_turn and (e.Duration or 0) > 0) or ((not s.per_turn) and (e.Duration or 0) == 0)
    elseif s.kind == "RESOURCES" then match = GameInfo.Resources[s.res] ~= nil and e.ForType == GameInfo.Resources[s.res].Index
    elseif s.kind == "AGREEMENTS" then match = e.SubType == DealAgreementTypes[s.sub]
    elseif s.kind == "CITIES" then match = string.lower(Locale.Lookup(tostring(e.ForTypeName))) == string.lower(s.city) end
    if match then
      if not e.IsValid then return s.label .. " can't be traded right now" end
      if s.amount and (e.MaxAmount or 0) < s.amount then return s.label .. ": only " .. tostring(e.MaxAmount) .. " available" end
      local it
      if s.kind == "CITIES" then it = deal:AddItemOfType(typ, s.from, to, e.SubType, e.ForType)
      else it = deal:AddItemOfType(typ, s.from) end
      if it == nil then return "couldn't add " .. s.label end
      if s.kind == "CITIES" then
        it:SetSubType(e.SubType)
        it:SetValueType(e.ForType)
        return nil
      end
      if s.kind == "RESOURCES" then it:SetValueType(e.ForType) end
      if s.kind == "AGREEMENTS" then it:SetSubType(e.SubType) end
      it:SetDuration(e.Duration or 0)
      if s.amount then it:SetAmount(s.amount) end
      return nil
    end
  end
  if s.kind == "CITIES" then return s.label .. " can't be traded (capitals never can)" end
  return s.label .. " isn't available"
end
for _, s in ipairs(ITEMS) do
  local err = add(s)
  if err then DealManager.ClearWorkingDeal(DealDirection.OUTGOING, A, B) print("RES|no|" .. err) return end
end
if deal:Validate() ~= DealValidationResult.VALID then
  DealManager.ClearWorkingDeal(DealDirection.OUTGOING, A, B)
  print("RES|no|the game rejected the deal") return
end
local r = DealManager.EnactWorkingDeal(A, B)
print("RES|" .. ((r == DealValidationResult.VALID) and "ok|deal done" or ("no|the game refused the deal (" .. tostring(r) .. ")")))
"""

# What P could trade with each civ it has met and isn't at war with (read-only).
TRADE_LUA = LUA_COMMON + r"""
local d = Players[P]:GetDiplomacy()
for _, b in ipairs(PlayerManager.GetAliveMajorIDs()) do
  if b ~= P and d:HasMet(b) then
    if d:IsAtWarWith(b) then print("TRW|" .. b) end   -- at war: a deal is peace terms
    local wd = DealManager.GetWorkingDeal(DealDirection.OUTGOING, P, b)
    for _, dir in ipairs({{P, b, "you_can_give"}, {b, P, "they_can_give"}}) do
      for _, kind in ipairs({"GOLD", "RESOURCES", "FAVOR", "AGREEMENTS", "CITIES"}) do
        local ok, list = pcall(function() return DealManager.GetPossibleDealItems(dir[1], dir[2], DealItemTypes[kind], wd) end)
        if ok and list then
          for _, e in ipairs(list) do
            if e.IsValid and kind == "CITIES" then
              print("TRC|" .. b .. "|" .. dir[3] .. "|" .. esc(Locale.Lookup(tostring(e.ForTypeName))))
            elseif e.IsValid then
              local name = ""
              if kind == "GOLD" then name = ((e.Duration or 0) > 0) and "gold_per_turn" or "gold"
              elseif kind == "RESOURCES" then local r = GameInfo.Resources[e.ForType]; name = r and r.ResourceType or ""
              elseif kind == "FAVOR" then name = "favor"
              elseif kind == "AGREEMENTS" and e.SubType == DealAgreementTypes.OPEN_BORDERS then name = "open_borders" end
              if name ~= "" then print("TR|" .. b .. "|" .. dir[3] .. "|" .. name .. "|" .. tostring(e.MaxAmount or 0)) end
            end
          end
        end
      end
    end
  end
end
"""

# Ongoing deals between every pair of civs (lump sums don't show up here once paid).
DEALS_LUA = r"""
local ids = PlayerManager.GetAliveMajorIDs()
local rev = {}
for k, v in pairs(DealItemTypes) do rev[v] = k end
for _, a in ipairs(ids) do for _, b in ipairs(ids) do if a < b then
  local ok, deals = pcall(function() return DealManager.GetPlayerDeals(a, b) end)
  if ok and type(deals) == "table" then
    for _, dl in ipairs(deals) do for it in dl:Items() do
      local kind = rev[it:GetType()] or tostring(it:GetType())
      local what = ""
      if kind == "RESOURCES" then
        local okv, v = pcall(function() return it:GetValueType() end)
        local r = okv and GameInfo.Resources[v] or nil
        what = r and r.ResourceType or ""
      elseif kind == "AGREEMENTS" then what = (it:GetSubType() == DealAgreementTypes.OPEN_BORDERS) and "OPEN_BORDERS" or tostring(it:GetSubType())
      elseif kind == "GOLD" then what = "PER_TURN" end
      print(table.concat({"DL", a, b, it:GetFromPlayerID(), kind, what, tostring(it:GetAmount()), tostring(it:GetEndTurn())}, "|"))
    end end
  end
end end end
"""

# Once a turn (read-only): cities and who settled near whom, troops near other civs' cities, wonders,
# religions, tech/civic counts, golden/dark ages, and Civ's own reasons each AI likes or dislikes someone
# (GetDiplomaticModifiers; Civ shows "Unknown Reason" for ones it keeps hidden).
AWARE_LUA = LUA_COMMON + r"""
local majors = PlayerManager.GetAliveMajorIDs()
local NEAR, BORDER = 8, 3
local cities, armies = {}, {}
print("MODON|gift_it_to_me|" .. tostring(GameEvents ~= nil and GameEvents.GiftUnit ~= nil and GameEvents.PopulateUnit ~= nil))
local gw, gh = Map.GetGridSize()
local okw, wrap = pcall(function() return Map.IsWrapX() end)
print("GRID|" .. gw .. "|" .. gh .. "|" .. tostring(okw and wrap ~= false))
local function continent(x, y)
  local ok, ct = pcall(function() return Map.GetPlot(x, y):GetContinentType() end)
  local row = ok and ct and ct >= 0 and GameInfo.Continents[ct] or nil
  return row and esc(txt(row.Description)) or ""
end
for _, p in ipairs(majors) do
  local okc, cap = pcall(function() return Players[p]:GetCities():GetCapitalCity() end)
  local capID = (okc and cap) and cap:GetID() or nil   -- GameCore cities have no IsCapital
  for _, c in Players[p]:GetCities():Members() do
    cities[#cities+1] = {p, c:GetID(), c:GetX(), c:GetY()}
    print("CT|" .. p .. "|" .. c:GetID() .. "|" .. esc(Locale.Lookup(c:GetName())) .. "|" .. c:GetX() .. "|" .. c:GetY()
      .. "|" .. continent(c:GetX(), c:GetY()) .. "|" .. tostring(capID ~= nil and c:GetID() == capID))
    for b in GameInfo.Buildings() do
      if b.IsWonder and c:GetBuildings():HasBuilding(b.Index) then
        print("WONDER|" .. p .. "|" .. b.BuildingType .. "|" .. esc(txt(b.Name)) .. "|" .. esc(Locale.Lookup(c:GetName())))
      end
    end
  end
  local a = {}
  for _, u in Players[p]:GetUnits():Members() do
    local r = GameInfo.Units[u:GetType()]
    if r and ((r.Combat or 0) > 0 or (r.RangedCombat or 0) > 0 or (r.Bombard or 0) > 0) then
      a[#a+1] = {u:GetX(), u:GetY(), math.max(r.Combat or 0, r.RangedCombat or 0, r.Bombard or 0), txt(r.Name)}
    end
  end
  armies[p] = a
  local okr, rel = pcall(function() return Players[p]:GetReligion():GetReligionTypeCreated() end)
  if okr and rel and rel >= 0 and GameInfo.Religions[rel] then
    print("RELIGION|" .. p .. "|" .. GameInfo.Religions[rel].ReligionType .. "|" .. esc(txt(GameInfo.Religions[rel].Name)))
  end
  local techs, n = Players[p]:GetTechs(), 0
  for t in GameInfo.Technologies() do if techs:HasTech(t.Index) then n = n + 1 end end
  local civics, m = Players[p]:GetCulture(), 0
  for t in GameInfo.Civics() do if civics:HasCivic(t.Index) then m = m + 1 end end
  print("PROGRESS|" .. p .. "|" .. n .. "|" .. m)
  local nd = 0
  pcall(function()
    for _, dist in Players[p]:GetDistricts():Members() do
      local row = GameInfo.Districts[dist:GetType()]
      local done = (dist.IsComplete == nil) or dist:IsComplete()
      if row and done and row.DistrictType ~= "DISTRICT_CITY_CENTER" and row.DistrictType ~= "DISTRICT_WONDER" then nd = nd + 1 end
    end
  end)
  print("DISTRICTS|" .. p .. "|" .. nd)
  -- Economy per turn and era (probe 9: these work in GameCore)
  local function num(f) local ok, v = pcall(f) return (ok and type(v) == "number") and math.floor(v + 0.5) or 0 end
  local P = Players[p]
  print(table.concat({"ECON", p, num(function() return P:GetTreasury():GetGoldYield() end),
    num(function() return P:GetTreasury():GetTotalMaintenance() end), num(function() return P:GetTechs():GetScienceYield() end),
    num(function() return P:GetCulture():GetCultureYield() end), num(function() return P:GetReligion():GetFaithYield() end),
    num(function() return P:GetEra() end), num(function() return P:GetReligion():GetFaithBalance() end)}, "|"))
  -- What the army is made of, and whose land it stands in
  local kinds, where = {}, {}
  for _, u in ipairs(a) do
    kinds[u[4]] = (kinds[u[4]] or 0) + 1
    local pl = Map.GetPlot(u[1], u[2])
    local owner = pl and pl:GetOwner() or -1
    where[owner] = (where[owner] or 0) + 1
  end
  local ks, ws = {}, {}
  for k, v in pairs(kinds) do ks[#ks+1] = esc(k):gsub("[,:]", " ") .. ":" .. v end
  for k, v in pairs(where) do ws[#ws+1] = k .. ":" .. v end
  print("ARMY|" .. p .. "|" .. table.concat(ks, ",") .. "|" .. table.concat(ws, ","))
  -- Cities: size, defense, damage (the city center's garrison and walls)
  local cc = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
  for _, c in P:GetCities():Members() do
    local d = c:GetDistricts():GetDistrict(cc)
    local dmg = function(kind) return num(function() return d:GetDamage(kind) end) end
    local cap = function(kind) return num(function() return d:GetMaxDamage(kind) end) end
    print(table.concat({"CITY2", p, c:GetID(), num(function() return c:GetPopulation() end),
      num(function() return d:GetDefenseStrength() end), dmg(DefenseTypes.DISTRICT_GARRISON), cap(DefenseTypes.DISTRICT_GARRISON),
      dmg(DefenseTypes.DISTRICT_OUTER), cap(DefenseTypes.DISTRICT_OUTER)}, "|"))
  end
  local pres = Players[p]:GetResources()
  for r in GameInfo.Resources() do
    if r.ResourceClassType == "RESOURCECLASS_LUXURY" or r.ResourceClassType == "RESOURCECLASS_STRATEGIC" then
      local okr, amt = pcall(function() return pres:GetResourceAmount(r.Index) end)
      if okr and amt and amt > 0 then
        print("HAS|" .. p .. "|" .. r.ResourceType .. "|" .. (r.ResourceClassType == "RESOURCECLASS_LUXURY" and "luxury" or "strategic") .. "|" .. math.floor(amt))
      end
    end
  end
end
-- Barbarian camps (and barbarian units) near each civ's cities
local campRow = GameInfo.Improvements["IMPROVEMENT_BARBARIAN_CAMP"]
if campRow then
  for i = 0, Map.GetPlotCount() - 1 do
    local pl = Map.GetPlotByIndex(i)
    if pl:GetImprovementType() == campRow.Index then
      for _, c in ipairs(cities) do
        local d = Map.GetPlotDistance(pl:GetX(), pl:GetY(), c[3], c[4])
        if d <= 6 then print("CAMP|" .. c[1] .. "|" .. c[2] .. "|" .. d) end
      end
    end
  end
end
for _, id in ipairs(PlayerManager.GetAliveIDs()) do
  local okb, barb = pcall(function() return Players[id]:IsBarbarian() end)
  if okb and barb then
    for _, u in Players[id]:GetUnits():Members() do
      local r = GameInfo.Units[u:GetType()]
      if r and ((r.Combat or 0) > 0 or (r.RangedCombat or 0) > 0) then
        for _, c in ipairs(cities) do
          if Map.GetPlotDistance(u:GetX(), u:GetY(), c[3], c[4]) <= BORDER then print("BARB|" .. c[1] .. "|" .. c[2]) break end
        end
      end
    end
  end
end
local closest = {}
for i = 1, #cities do for j = i + 1, #cities do
  local a, b = cities[i], cities[j]
  if a[1] ~= b[1] then
    local d = Map.GetPlotDistance(a[3], a[4], b[3], b[4])
    if d <= NEAR then print("NEAR|" .. a[1] .. "|" .. a[2] .. "|" .. b[1] .. "|" .. b[2] .. "|" .. d) end
    for _, pair in ipairs({{a, b}, {b, a}}) do
      local k = pair[1][1] .. "|" .. pair[1][2] .. "|" .. pair[2][1]
      if closest[k] == nil or d < closest[k] then closest[k] = d end
    end
  end
end end
for k, d in pairs(closest) do print("CD|" .. k .. "|" .. d) end
for _, A in ipairs(majors) do for _, B in ipairs(majors) do
  if A ~= B then
    local n, s, kinds = 0, 0, {}
    for _, u in ipairs(armies[B]) do
      -- B's unit counts if it's inside A's borders or within BORDER tiles of one of A's cities
      local pl = Map.GetPlot(u[1], u[2])
      local near = pl ~= nil and pl:GetOwner() == A
      if not near then
        for _, c in ipairs(cities) do
          if c[1] == A and Map.GetPlotDistance(u[1], u[2], c[3], c[4]) <= BORDER then near = true; break end
        end
      end
      if near then n = n + 1; s = s + u[3]; kinds[u[4]] = (kinds[u[4]] or 0) + 1 end
    end
    if n > 0 then
      local ks = {}
      for k, v in pairs(kinds) do ks[#ks+1] = esc(k):gsub("[,:]", " ") .. ":" .. v end
      print("TROOPS|" .. A .. "|" .. B .. "|" .. n .. "|" .. s .. "|" .. table.concat(ks, ","))
    end
    if not Players[A]:IsHuman() and Players[A]:GetDiplomacy():HasMet(B) then
      local ok, mods = pcall(function() return Players[A]:GetAi_Diplomacy():GetDiplomaticModifiers(B) end)
      if ok and type(mods) == "table" then
        for _, m in ipairs(mods) do
          if m.Score and m.Score ~= 0 then print("MOD|" .. A .. "|" .. B .. "|" .. tostring(m.Score) .. "|" .. esc(txt(m.Text))) end
        end
      elseif not ok then print("MODERR|" .. A .. "|" .. B .. "|" .. esc(tostring(mods))) end
    end
  end
end end
local oke, era = pcall(function() return Game.GetEras():GetCurrentEra() end)
print("ERA|" .. tostring(oke and era))
-- Cities held by city-states and the Free Cities: where a city went when it flipped away (loyalty).
local isMajor = {}
for _, p in ipairs(majors) do isMajor[p] = true end
local minors = {}
for _, m in ipairs(PlayerManager.GetAliveMinorIDs()) do minors[m] = true end
for _, id in ipairs(PlayerManager.GetAliveIDs()) do
  if not isMajor[id] then
    local okf, free = pcall(function() return Players[id]:IsFreeCities() end)
    local okb, barb = pcall(function() return Players[id]:IsBarbarian() end)
    local kind = (okf and free) and "the Free Cities" or nil
    if kind == nil and minors[id] then
      local row = GameInfo.Civilizations[PlayerConfigurations[id]:GetCivilizationTypeName()]
      kind = "the city-state of " .. txt(row and row.Name)
    end
    if kind and not (okb and barb) then
      for _, c in Players[id]:GetCities():Members() do
        print("CTX|" .. id .. "|" .. esc(kind) .. "|" .. c:GetID() .. "|" .. esc(Locale.Lookup(c:GetName())))
      end
    end
  end
end
"""
NEAR_TILES, BORDER_TILES = 8, 3
PROPOSAL_TURNS = 3   # an offer waits this many turns for an answer
FIGHT_TURNS = 3      # leaders hear about fights from this many turns back


# InGame (read-only): ages, era score and city loyalty only exist on the UI side (GameCore has no HasDarkAge).
AGES_LUA = LUA_COMMON + r"""
local eras = Game.GetEras()
local function try(f) local ok, v = pcall(f) if ok then return v end return nil end
print("ERANEXT|" .. tostring(try(function() return eras:GetNextEraCountdown() end) or 0))
-- How this game can be won (the game setup's victory types; VICTORY_DEFAULT = last civ standing).
for row in GameInfo.Victories() do
  if try(function() return Game.IsVictoryEnabled(row.VictoryType) end) then print("VIC|" .. row.VictoryType) end
end
-- Treaty timers (Civ's own math in DiplomacyActionView: friendship lasts GetDenounceTimeLimit turns).
local alive = PlayerManager.GetAliveMajorIDs()
print("FRLIMIT|" .. tostring(try(function() return Game.GetGameDiplomacy():GetDenounceTimeLimit() end) or 30))
for _, a in ipairs(alive) do for _, b in ipairs(alive) do if a < b then
  local d = Players[a]:GetDiplomacy()
  local st = try(function() return GameInfo.DiplomaticStates[Players[a]:GetDiplomaticAI():GetDiplomaticStateIndex(b)].StateType end)
  if st == "DIPLO_STATE_DECLARED_FRIEND" or st == "DIPLO_STATE_ALLIED" then
    print(table.concat({"TREATY", a, b, st, tostring(try(function() return d:GetDeclaredFriendshipTurn(b) end) or -1),
      tostring(try(function() return d:GetAllianceTurnsUntilExpiration(b) end) or -1)}, "|"))
  end
end end end
-- Favor per turn, grievances, amenities, war weariness and production (probe 9: InGame only)
for row in GameInfo.Eras() do print("ERANAME|" .. row.Index .. "|" .. esc(txt(row.Name))) end
for _, a in ipairs(alive) do
  print("FAVOR|" .. a .. "|" .. tostring(try(function() return Players[a]:GetFavorPerTurn() end) or 0))
  for _, b in ipairs(alive) do
    if a ~= b then
      local g = try(function() return Players[a]:GetDiplomacy():GetGrievancesAgainst(b) end)
      if type(g) == "number" and g ~= 0 then print("GRIEV|" .. a .. "|" .. b .. "|" .. math.floor(g)) end
    end
  end
  for _, c in Players[a]:GetCities():Members() do
    local gr = try(function() return c:GetGrowth() end)
    local h = try(function() return c:GetBuildQueue():GetCurrentProductionTypeHash() end)
    local what = ""
    if h and h ~= 0 then
      for _, tbl in ipairs({"Units", "Buildings", "Districts", "Projects"}) do
        local row = GameInfo[tbl][h]
        if row then what = txt(row.Name) break end
      end
    end
    print(table.concat({"CITYX", a, c:GetID(), tostring(gr and try(function() return gr:GetAmenities() end) or ""),
      tostring(gr and try(function() return gr:GetAmenitiesNeeded() end) or ""),
      tostring(gr and try(function() return gr:GetAmenitiesLostFromWarWeariness() end) or 0), esc(what)}, "|"))
  end
end
-- The leader ribbon's military strength (DiplomacyRibbon.lua; InGame only, probe 11), for civs and city-states
for _, id in ipairs(PlayerManager.GetAliveIDs()) do
  local m = try(function() return Players[id]:GetStats():GetMilitaryStrengthWithoutTreasury() end)
  if type(m) == "number" then print("RIBBON|" .. id .. "|" .. math.floor(m + 0.5)) end
end
-- Probe 10 (InGame): nukes, government and policy cards, trade routes, governors, spies, great people points
local function nm(row) return row and esc(txt(row.Name)) or "" end
for _, a in ipairs(alive) do
  local P = Players[a]
  local w = try(function() return P:GetWMDs() end)
  if w then
    for row in GameInfo.WMDs() do
      local n = try(function() return w:GetWeaponCount(row.Index) end) or 0
      if n > 0 then print("WMD|" .. a .. "|" .. nm(row) .. "|" .. n) end
    end
  end
  local cul = P:GetCulture()
  local g = try(function() return cul:GetCurrentGovernment() end)
  if g and g >= 0 then print("GOV|" .. a .. "|" .. nm(GameInfo.Governments[g])) end
  local slots = try(function() return cul:GetNumPolicySlots() end) or 0
  for i = 0, slots - 1 do
    local id = try(function() return cul:GetSlotPolicy(i) end)
    if id and id >= 0 then print("POLICY|" .. a .. "|" .. nm(GameInfo.Policies[id])) end
  end
  for _, c in P:GetCities():Members() do
    for _, r in ipairs(try(function() return c:GetTrade():GetOutgoingRoutes() end) or {}) do
      print("ROUTE|" .. tostring(r.OriginCityPlayer) .. "|" .. tostring(r.DestinationCityPlayer))
    end
  end
  local okg, list = pcall(function() local _, l = P:GetGovernors():GetGovernorList() return l end)
  for _, gov in ipairs((okg and list) or {}) do
    local city = try(function() return gov:GetAssignedCity() end)
    print("GOVERNOR|" .. a .. "|" .. nm(GameInfo.Governors[gov:GetType()]) .. "|" .. (city and esc(Locale.Lookup(city:GetName())) or ""))
  end
  for _, u in P:GetUnits():Members() do
    local r = GameInfo.Units[u:GetType()]
    if r and r.UnitType == "UNIT_SPY" then
      local op = try(function() return u:GetSpyOperation() end) or -1
      local city = try(function() return CityManager.GetCityAt(u:GetX(), u:GetY()) end)
      local orow = op >= 0 and GameInfo.UnitOperations[op] or nil
      print("SPY|" .. a .. "|" .. (city and esc(Locale.Lookup(city:GetName())) or "on the move") .. "|"
        .. (city and tostring(city:GetOwner()) or "-1") .. "|" .. (orow and esc(txt(orow.Description)) or ""))
    end
  end
  local gp = try(function() return P:GetGreatPeoplePoints() end)
  if gp then
    for row in GameInfo.GreatPersonClasses() do
      local v = try(function() return gp:GetPointsTotal(row.Index) end) or 0
      if v > 0 then print("GPP|" .. a .. "|" .. row.Index .. "|" .. math.floor(v)) end
    end
  end
end
for row in GameInfo.GreatPersonClasses() do print("GPCLASS|" .. row.Index .. "|" .. nm(row)) end
local tl = try(function() return Game.GetGreatPeople():GetTimeline() end) or {}
for _, e in ipairs(tl) do
  if e.Claimant == nil and e.Individual ~= nil and e.Class ~= nil then
    print("GPNOW|" .. e.Class .. "|" .. nm(GameInfo.GreatPersonIndividuals[e.Individual]) .. "|" .. tostring(e.Cost or 0))
  end
end
-- City-states: type and envoys from each civ
for _, cs in ipairs(PlayerManager.GetAliveMinorIDs()) do
  local inf = try(function() return Players[cs]:GetInfluence() end)
  local li = GameInfo.Leaders[PlayerConfigurations[cs]:GetLeaderTypeName()]
  local kind = li and (li.InheritFrom or li.LeaderType) or ""
  print("CSTYPE|" .. cs .. "|" .. tostring(kind):gsub("LEADER_MINOR_CIV_", ""):lower())
  if inf then
    for _, a in ipairs(alive) do
      local n = try(function() return inf:GetTokensReceived(a) end) or 0
      if n > 0 then print("ENVOY|" .. cs .. "|" .. a .. "|" .. n) end
    end
  end
end
-- Spies each civ has caught (EspionageOverview: GetNthCapturedSpy(captor, i).OwningPlayer is whose spy it is).
for _, p in ipairs(PlayerManager.GetWasEverAliveMajorIDs()) do
  local d = Players[p]:GetDiplomacy()
  local n = try(function() return d:GetNumSpiesCaptured() end) or 0
  for i = 0, n - 1 do
    local spy = try(function() return d:GetNthCapturedSpy(p, i) end)
    if type(spy) == "table" and spy.OwningPlayer ~= nil then
      print(table.concat({"SPYCAP", p, spy.OwningPlayer, tostring(spy.NameIndex or i), tostring(spy.Level or 0),
        esc(txt(spy.Name or ""))}, "|"))
    end
  end
end
-- Emergencies (Gathering Storm): each civ's view of the ones it can join, leads or is the target of.
local em = try(function() return Game.GetEmergencyManager() end)
if em then
  for _, p in ipairs(alive) do
    local t = try(function() return em:GetEmergencyInfoTable(p) end)
    if type(t) == "table" then
      for _, c in pairs(t) do
        if type(c) == "table" then
          local members = {}
          for _, m in pairs(c.MemberIDs or {}) do members[#members+1] = tostring(m) end
          print(table.concat({"EMER", p, esc(txt(c.NameText or c.EmergencyType or "an emergency")), tostring(c.TargetID or -1),
            tostring(c.TurnsLeft or -1), tostring(c.HasBegun == true), table.concat(members, ";"),
            esc(txt(c.GoalDescription or ""))}, "|"))
        end
      end
    end
  end
end
for _, p in ipairs(PlayerManager.GetAliveMajorIDs()) do
  -- Cities this civ took from another major civ (what World Rankings counts for domination).
  for _, c in Players[p]:GetCities():Members() do
    local orig = try(function() return c:GetOriginalOwner() end)
    if orig ~= nil and orig ~= p and Players[orig] and try(function() return Players[orig]:IsMajor() end) then
      print(table.concat({"TAKEN", p, c:GetID(), esc(Locale.Lookup(c:GetName())), orig,
        tostring(try(function() return c:IsOriginalCapital() end) == true)}, "|"))
    end
  end
  local age = "normal"
  if try(function() return eras:HasHeroicGoldenAge(p) end) then age = "heroic"
  elseif try(function() return eras:HasGoldenAge(p) end) then age = "golden"
  elseif try(function() return eras:HasDarkAge(p) end) then age = "dark" end
  print(table.concat({"AGE", p, age, tostring(try(function() return eras:GetPlayerCurrentScore(p) end) or ""),
    tostring(try(function() return eras:GetPlayerDarkAgeThreshold(p) end) or ""),
    tostring(try(function() return eras:GetPlayerGoldenAgeThreshold(p) end) or "")}, "|"))
  for _, c in Players[p]:GetCities():Members() do
    local ci = try(function() return c:GetCulturalIdentity() end)
    if ci then
      local to = try(function() return ci:GetPotentialTransferPlayer() end)
      local toName = ""
      if to ~= nil and to >= 0 and PlayerConfigurations[to] then
        local okf, free = pcall(function() return Players[to]:IsFreeCities() end)
        local row = GameInfo.Civilizations[PlayerConfigurations[to]:GetCivilizationTypeName()]
        toName = (okf and free) and "the Free Cities" or txt(row and row.Name)
      end
      print(table.concat({"LOYAL", p, c:GetID(), esc(Locale.Lookup(c:GetName())),
        tostring(try(function() return ci:GetLoyalty() end) or ""), tostring(try(function() return ci:GetLoyaltyPerTurn() end) or ""),
        tostring(try(function() return ci:GetTurnsToConversion() end) or ""), esc(toName)}, "|"))
    end
  end
end
"""


# GameCore: who fought whom. GameEvents.OnCombatOccurred (signature from the Pirates/CivRoyale scenario
# scripts) feeds a log that lives in Civ's Lua state; each read prints it and starts a new one. The hook is
# added once per Lua state (a reloaded save gets a fresh state, so the next read adds it again).
COMBAT_LUA = LUA_COMMON + r"""
if CC_FIGHTS == nil then CC_FIGHTS = {} end
-- The handler is a global redefined on every read, so a newer Civ Council takes effect without a reload;
-- the hook itself is added once per Lua state and just calls it.
CC_ON_FIGHT = function(ap, au, dp, du, ad, dd)
    pcall(function()
      local function kind(p)
        local P = Players[p]
        if P == nil then return "?" end
        if P:IsBarbarian() then return "barbarians" end
        local ctype = PlayerConfigurations[p] and PlayerConfigurations[p]:GetCivilizationTypeName() or ""
        local okf, free = pcall(function() return P:IsFreeCities() end)
        if (okf and free) or ctype == "CIVILIZATION_FREE_CITIES" then return "the Free Cities" end
        if not P:IsMajor() then
          local row = GameInfo.Civilizations[ctype]
          return "the city-state of " .. txt(row and row.Name)
        end
        return ""
      end
      local function side(p, uid, did)
        if p == nil or p < 0 or Players[p] == nil then return {"-1", "", "", "", ""} end
        local what, dead, city = "", "", ""
        if uid ~= nil and uid >= 0 then
          local u = Players[p]:GetUnits():FindID(uid)
          if u then
            local r = GameInfo.Units[u:GetType()]
            what = r and txt(r.Name) or ""
            local okd, isd = pcall(function() return u:IsDead() or u:IsDelayedDeath() end)
            dead = tostring(okd and isd == true)
          end
        end
        if did ~= nil and did >= 0 then
          local d = Players[p]:GetDistricts():FindID(did)
          if d then
            local okc, c = pcall(function() return CityManager.GetCityAt(d:GetX(), d:GetY()) end)
            if okc and c then city = Locale.Lookup(c:GetName()) end
            if city == "" then local row = GameInfo.Districts[d:GetType()]; city = row and txt(row.Name) or "a district" end
          end
        end
        return {tostring(p), esc(kind(p)), esc(what), dead, esc(city)}
      end
      local A, D = side(ap, au, ad), side(dp, du, dd)
      CC_FIGHTS[#CC_FIGHTS+1] = Game.GetCurrentGameTurn() .. "|" .. table.concat(A, "|") .. "|" .. table.concat(D, "|")
      if #CC_FIGHTS > 500 then table.remove(CC_FIGHTS, 1) end
    end)
end
if not CC_FIGHTS_HOOKED and GameEvents and GameEvents.OnCombatOccurred then
  CC_FIGHTS_HOOKED = true
  GameEvents.OnCombatOccurred.Add(function(...) if CC_ON_FIGHT then CC_ON_FIGHT(...) end end)
end
CC_COMBAT = nil   -- an older hook's log: with it gone, that hook fails quietly inside its pcall
print("HOOK|" .. tostring(CC_FIGHTS_HOOKED == true))
for _, line in ipairs(CC_FIGHTS) do print("CMB|" .. line) end
CC_FIGHTS = {}
"""


# InGame: natural disasters (Events.RandomEventOccurred, signature from NaturalDisasterPopup.lua). A refreshable
# global handler logs them in the InGame state; each read prints the log and starts a new one.
# Civ's own timeline (Historic Moments, Gathering Storm), every civ's, back to turn 1. UI side only (probe 17);
# the arguments copy Civ's HistoricMoments.lua (player, minimum interest 1).
MOMENTS_LUA = LUA_COMMON + r"""
local hm = Game.GetHistoryManager and Game.GetHistoryManager()
if hm then
  for _, p in ipairs(PlayerManager.GetWasEverAliveMajorIDs()) do
    local ok, all = pcall(function() return hm:GetAllMomentsData(p, 1) end)
    if ok and type(all) == "table" then
      for _, m in ipairs(all) do
        print(table.concat({"MOM", p, tostring(m.Turn), tostring(m.Type), tostring(m.EraScore or 0),
                            esc(tostring(m.InstanceDescription or ""))}, "|"))
      end
    end
  end
end
"""
GOODY_HUT_MOMENT = -1672407476   # "Villagers bring gifts as they join our civilization": too common to tell a story

DISASTER_LUA = LUA_COMMON + r"""
if CC_DISASTERS == nil then CC_DISASTERS = {} end
CC_ON_DISASTER = function(kind, severity, x, y)
  pcall(function()
    local row = GameInfo.RandomEvents[kind]
    local name = row and txt(row.Name) or "a natural disaster"
    CC_DISASTERS[#CC_DISASTERS+1] = Game.GetCurrentGameTurn() .. "|" .. esc(name) .. "|" .. tostring(x) .. "|" .. tostring(y)
    if #CC_DISASTERS > 200 then table.remove(CC_DISASTERS, 1) end
  end)
end
if not CC_DISASTER_HOOKED and Events and Events.RandomEventOccurred then
  CC_DISASTER_HOOKED = true
  Events.RandomEventOccurred.Add(function(...) if CC_ON_DISASTER then CC_ON_DISASTER(...) end end)
end
for _, line in ipairs(CC_DISASTERS) do print("DIS|" .. line) end
CC_DISASTERS = {}
"""


# InGame: quiet Civ's own AI. Its trade offers, requests, praise and warnings to you come as diplomacy sessions
# (Events.DiplomacyStatement); a refreshable global handler closes the ones the AI opened, the way Civ's own
# leader screen closes sessions (DiplomacyManager.CloseSession). Kept: first meetings, war declarations and
# defeats, anything inside a session that began with a greeting (that's you opening a leader's screen), and
# replies to your own proposals. QUIET switches it on or off; each read prints and clears what was closed.
QUIET_LUA = r"""
if CC_QUIETED == nil then CC_QUIETED = {} end
if CC_SESSIONS == nil or #CC_QUIETED > 400 then CC_SESSIONS = {} end
CC_QUIET_ON, CC_BLOCK_SCREEN = QUIET, BLOCK
CC_ON_STATEMENT = function(fromPlayer, toPlayer, kVariants)
  pcall(function()
    local me = Game.GetLocalPlayer()
    if toPlayer ~= me and fromPlayer ~= me then return end
    local other = (fromPlayer == me) and toPlayer or fromPlayer
    if not (Players[other] and Players[other]:IsMajor()) then return end
    local sid = kVariants.SessionID
    local t = DiplomacyManager.GetKeyName(kVariants.StatementType) or ""
    local sub = DiplomacyManager.GetKeyName(kVariants.StatementSubType) or ""
    if CC_SESSIONS[sid] == nil then   -- who opened it: a first meeting, you (a greeting), or the AI
      CC_SESSIONS[sid] = t:find("FIRST_MEET") and "keep" or ((fromPlayer == me or t:find("GREETING")) and "you" or "ai")
    end
    local kind = CC_SESSIONS[sid]
    if kind == "keep" or t:find("DECLARE_WAR") or t:find("DEFEAT") then CC_ALLOW_SCREEN = true return end
    if kind == "ai" and (not CC_QUIET_ON or sub:find("HUMAN")) then CC_ALLOW_SCREEN = true return end
    if kind == "you" and not CC_BLOCK_SCREEN then return end
    DiplomacyManager.CloseSession(sid)
    CC_QUIETED[#CC_QUIETED+1] = Game.GetCurrentGameTurn() .. "|" .. other .. "|" .. (kind == "you" and "YOU_OPENED" or t)
  end)
end
if not CC_STATEMENT_HOOKED and Events and Events.DiplomacyStatement then
  CC_STATEMENT_HOOKED = true
  Events.DiplomacyStatement.Add(function(...) if CC_ON_STATEMENT then CC_ON_STATEMENT(...) end end)
end
-- Clicking a leader's portrait opens the leader screen in overview mode with no session at all, so the
-- statement hook never sees it (probe 14). Instead: once the leader has loaded (the screen is visible), raise
-- the "blocking popup" event the screen force-closes itself on (DiplomacyActionView.OnBlockingPopupShown ->
-- OnForceClose, its own clean exit), and at once the matching "closed" event so InGame's bulk-hide is undone.
-- First meetings, war declarations and defeats set CC_ALLOW_SCREEN and are left alone until the scene closes.
CC_ON_LEADER_LOADED = function()
  pcall(function()
    if not CC_BLOCK_SCREEN or CC_ALLOW_SCREEN then return end
    -- This hook runs just before the screen's own OnLeaderLoaded un-hides it, and a hidden screen ignores the
    -- force-close; so show it first (its OnLeaderLoaded then sees it closed and returns early).
    local dav = ContextPtr:LookUpControl("/InGame/DiplomacyActionView")
    if dav and dav:IsHidden() then dav:SetHide(false) end
    LuaEvents.NaturalWonderPopup_Shown()
    LuaEvents.NaturalWonderPopup_Closed()
    CC_QUIETED[#CC_QUIETED+1] = Game.GetCurrentGameTurn() .. "|-1|YOU_OPENED"
  end)
end
CC_ON_SCENE_CLOSED = function() CC_ALLOW_SCREEN = false end
if not CC_SCREEN_HOOKED and Events and Events.LeaderScreenFinishedLoading then
  CC_SCREEN_HOOKED = true
  Events.LeaderScreenFinishedLoading.Add(function(...) if CC_ON_LEADER_LOADED then CC_ON_LEADER_LOADED(...) end end)
  LuaEvents.DiploScene_SceneClosed.Add(function(...) if CC_ON_SCENE_CLOSED then CC_ON_SCENE_CLOSED(...) end end)
end
print("QUIET|" .. tostring(CC_QUIET_ON))
for _, line in ipairs(CC_QUIETED) do print("QD|" .. line) end
CC_QUIETED = {}
"""
QUIET_LABELS = {"YOU_OPENED": "YOU_OPENED", "MAKE_DEAL": "a trade offer", "MAKE_DEMAND": "a demand", "DECLARE_FRIEND": "a friendship request",
                "MAKE_ALLIANCE": "an alliance request", "OPEN_BORDERS": "an open borders request",
                "DELEGATION": "a delegation", "RESIDENT_EMBASSY": "an embassy request", "KUDO": "praise",
                "WARNING": "a warning", "DENOUNCE": "a denouncement speech", "MAKE_PEACE": "a peace offer"}


# InGame (read-only): the World Congress. What's up for a vote now, and how everyone voted last session
# (Civ's built-in AI casts each AI civ's votes; scripts can't vote, probe 5). A = Effect1, B = Effect2.
CONGRESS_LUA = LUA_COMMON + r"""
local wc = Game.GetWorldCongress and Game.GetWorldCongress()
if wc == nil then return end
local me = Game.GetLocalPlayer()
local function who(id)
  local n = tonumber(id)
  if n == nil or PlayerConfigurations[n] == nil then return nil end
  local row = GameInfo.Leaders[PlayerConfigurations[n]:GetLeaderTypeName()]
  return row and txt(row.Name) or tostring(n)
end
local function label(t) if t == nil or t == "" then return "" end return who(t) or txt(tostring(t)) end
local function resrow(t) return GameInfo.Resolutions[t] end
local okr, res = pcall(function() return wc:GetResolutions(me) end)
if okr and type(res) == "table" then
  for i, r in pairs(res) do
    if type(i) == "number" then
      local row = resrow(r.Type)
      local targets = {}
      for _, t in ipairs(r.PossibleTargets or {}) do targets[#targets+1] = esc(label(t)) end
      if row then print(table.concat({"WCNOW", esc(txt(row.Name)), esc(txt(row.Effect1Description)),
        esc(txt(row.Effect2Description)), tostring(row.TargetKind), table.concat(targets, ";")}, "|")) end
    end
  end
end
local okv, rev = pcall(function() return wc:GetReview(me) end)
if okv and type(rev) == "table" and type(rev.Resolutions) == "table" then
  for i, r in pairs(rev.Resolutions) do
    local row = resrow(r.Type)
    local name = row and txt(row.Name) or "a resolution"
    print(table.concat({"WCPAST", i, esc(name), esc(txt(tostring(r.ChosenOption or ""))), esc(label(r.ChosenThing))}, "|"))
    for _, sel in pairs(r.PlayerSelections or {}) do
      local opt = (sel.OptionChosen == 1 and "A") or (sel.OptionChosen == 2 and "B") or "-"
      print(table.concat({"WCVOTE", i, sel.PlayerID, sel.Votes or 0, opt, esc(label(sel.ResolutionTarget))}, "|"))
    end
  end
end
"""


# InGame, for YOUR map only: a leader marks a place for you with one of your own map pins (probe 7:
# GetMapPin(x, y) makes or returns the pin on that tile, as Civ's map-pin popup does). Pins are part of your
# save. Older pins of that leader's are removed first, but only if they still carry the name we gave them,
# and a tile with a pin of your own is left alone.
PIN_LUA = r"""
local cfg = PlayerConfigurations[Game.GetLocalPlayer()]
local plot = TILE >= 0 and Map.GetPlotByIndex(TILE) or Map.GetPlot(X, Y)
if plot == nil then print("RES|no|there's no such place on the map") return end
for _, old in ipairs(DROP) do
  local okp, pin = pcall(function() return cfg:GetMapPinID(old[1]) end)
  if okp and pin and pin:GetName() == old[2] then cfg:DeleteMapPin(old[1]) end
end
local pin = cfg:GetMapPin(plot:GetX(), plot:GetY())
if pin == nil then Network.BroadcastPlayerInfo() print("RES|no|Civ wouldn't place a pin there") return end
local had = pin:GetName()
if had ~= nil and had ~= "" and not OURS[had] then
  Network.BroadcastPlayerInfo()
  print("RES|no|you already have a pin of your own there") return
end
pin:SetName(NAME)
pin:SetIconName("ICON_MAP_PIN_CIRCLE")
Network.BroadcastPlayerInfo()
print("RES|ok|" .. pin:GetID() .. "|" .. plot:GetIndex())
"""
MAX_PINS = 3   # map pins each leader keeps on your map; the oldest goes when it adds another

# GameCore (probe 15): a city's focus, as a player sets it on the city screen (CitizenManager SetFavoredYield /
# SetDisfavoredYield, works for AI civs). FAVOR is a YIELD_ type or "" for no focus.
FOCUS_LUA = r"""
local city = Players[P]:GetCities():FindID(CITY)
if city == nil then print("RES|no|no such city") return end
local ci = city:GetCitizens()
for row in GameInfo.Yields() do
  if ci:IsYieldFavored(row.Index) and row.YieldType ~= FAVOR then ci:SetFavoredYield(row.Index, false) end
end
if FAVOR ~= "" then
  local row = GameInfo.Yields[FAVOR]
  if row == nil then print("RES|no|unknown yield") return end
  ci:SetFavoredYield(row.Index, true)
end
print("RES|ok|" .. Locale.Lookup(city:GetName()) .. (FAVOR ~= "" and (" focuses on " .. FAVOR:gsub("YIELD_", ""):lower()) or " has no focus"))
"""
FOCUS_YIELDS = ("FOOD", "PRODUCTION", "GOLD", "SCIENCE", "CULTURE", "FAITH")

# GameCore (probe 15): recruit a great person with points, if Civ says this civ can (CanRecruitPerson; same
# arguments as RecruitPerson). Unclaimed people have Claimant -1 in GameCore. Buying with gold/faith isn't exposed.
RECRUIT_LUA = r"""
local gp = Game.GetGreatPeople()
for _, e in ipairs(gp:GetTimeline()) do
  local row = e.Individual and e.Individual >= 0 and GameInfo.GreatPersonIndividuals[e.Individual] or nil
  if row and (e.Claimant == nil or e.Claimant == -1) and Locale.Lookup(row.Name):lower() == NAME:lower() then
    if not gp:CanRecruitPerson(P, e.Individual) then
      print("RES|wait|not enough great person points for " .. Locale.Lookup(row.Name) .. " yet") return
    end
    gp:RecruitPerson(P, e.Individual)
    print("RES|ok|recruited " .. Locale.Lookup(row.Name)) return
  end
end
print("RES|no|nobody called " .. NAME .. " is up for grabs")
"""

# GameCore: a notification in YOUR Civ notification stack (as Civ's scenario scripts send theirs). USER_DEFINED_1
# shows as a plain "!" and clicking it just looks at the plot (probe 13).
NOTIFY_LUA = r"""
local ok, err = pcall(function()
  NotificationManager.SendNotification(ME, NotificationTypes.USER_DEFINED_1, HEAD, BODY, X, Y)
end)
print("RES|" .. (ok and "ok|sent" or ("no|" .. tostring(err))))
"""

# GameCore, with the Gift It To Me mod: A hands N of its NAME units to B through the mod's own game events
# (GameEvents.GiftUnit / PopulateUnit, the ones its gift button fires; its script kills the unit and makes the
# same one for B where it stood, with promotions). Military units only, never to a civ A is at war with.
# The mod's own button needs the unit inside B's borders: elsewhere Civ won't place B's new unit and it ends up
# off the map (x = -9999, seen on the first live gift). So any such unit is put on the nearest free tile of
# B's own land that suits it: water for ships, open land for land units, the city itself for aircraft.
GIFT_LUA = r"""
if not (GameEvents and GameEvents.GiftUnit and GameEvents.PopulateUnit) then
  print("RES|no|the Gift It To Me mod isn't active in this game") return end
if Players[A]:GetDiplomacy():IsAtWarWith(B) then print("RES|no|you can't gift units to a civ you're at war with") return end
local ids, label, utype, domain, ox, oy = {}, "", nil, nil, nil, nil
for _, u in Players[A]:GetUnits():Members() do
  local r = GameInfo.Units[u:GetType()]
  if #ids < N and r and u:GetX() >= 0 and ((r.Combat or 0) > 0 or (r.RangedCombat or 0) > 0)
     and (r.UnitType == NAME or Locale.Lookup(r.Name):lower() == NAME:lower()) then
    ids[#ids+1] = u:GetID(); label = Locale.Lookup(r.Name); utype, domain = r.UnitType, r.Domain
    ox, oy = ox or u:GetX(), oy or u:GetY()
  end
end
if #ids == 0 then print("RES|no|you have no military units called " .. NAME) return end
local given = 0
for _, id in ipairs(ids) do
  local ok = pcall(function()
    GameEvents.GiftUnit.Call(A, {giftingPlayerID = A, recipientPlayerID = B, giftingUnitID = id})
    GameEvents.PopulateUnit.Call(A, {})
  end)
  if ok and Players[A]:GetUnits():FindID(id) == nil then given = given + 1 end
end
local function freeTile()
  local cs = {}
  for _, c in Players[B]:GetCities():Members() do cs[#cs+1] = {Map.GetPlotDistance(ox, oy, c:GetX(), c:GetY()), c} end
  table.sort(cs, function(x, y) return x[1] < y[1] end)
  for _, e in ipairs(cs) do
    local c = e[2]
    if domain == "DOMAIN_AIR" then return c:GetX(), c:GetY() end
    for r = 1, 3 do for dx = -r, r do for dy = -r, r do
      local p = Map.GetPlot(c:GetX() + dx, c:GetY() + dy)
      if p and Map.GetPlotDistance(c:GetX(), c:GetY(), p:GetX(), p:GetY()) == r and p:GetOwner() == B and p:GetUnitCount() == 0 then
        local water = p:IsWater()
        if (domain == "DOMAIN_SEA" and water) or (domain ~= "DOMAIN_SEA" and not water and not p:IsImpassable()) then
          return p:GetX(), p:GetY()
        end
      end
    end end end
  end
end
local moved = 0
for _, u in Players[B]:GetUnits():Members() do
  if u:GetX() < 0 and GameInfo.Units[u:GetType()].UnitType == utype then
    local x, y = freeTile()
    if x then UnitManager.PlaceUnit(u, x, y); moved = moved + 1 end
  end
end
print("RES|" .. (given > 0 and "ok|" or "no|") .. given .. "|" .. label .. "|" .. moved)
"""
MAX_GIFT_UNITS = 6   # per gift


# Aim an AI civ's army at an enemy city (probe 2): Civ's own "Attack Enemy City" operation, rallying at
# A's city closest to the target, with all of A's land combat units. Civ's AI still moves and fights.
ATTACK_LUA = r"""
local target = Players[B]:GetCities():FindID(CITY)
if target == nil then print("RES|no|that city is gone") return end
if not Players[A]:GetDiplomacy():IsAtWarWith(B) then print("RES|no|you're not at war with them") return end
local rally, best = nil, 999
for _, c in Players[A]:GetCities():Members() do
  local d = Map.GetPlotDistance(c:GetX(), c:GetY(), target:GetX(), target:GetY())
  if d < best then best, rally = d, c end
end
if rally == nil then print("RES|no|you have no cities to rally at") return end
local m = Players[A]:GetAi_Military()
pcall(function() m:SetRival(B) end)
local op = m:StartScriptedOperationWithTargetAndRally("Attack Enemy City", B,
  Map.GetPlot(target:GetX(), target:GetY()):GetIndex(), Map.GetPlot(rally:GetX(), rally:GetY()):GetIndex())
if op == nil or op < 0 then print("RES|no|your generals couldn't plan that attack") return end
local n = 0
for _, u in Players[A]:GetUnits():Members() do
  local r = GameInfo.Units[u:GetType()]
  if r and r.Domain == "DOMAIN_LAND" and ((r.Combat or 0) > 0 or (r.RangedCombat or 0) > 0) then
    m:AddUnitToScriptedOperation(op, u:GetID()); n = n + 1
  end
end
print("RES|ok|" .. n .. " units ordered to attack " .. Locale.Lookup(target:GetName()) .. ", rallying at " .. Locale.Lookup(rally:GetName()))
"""


# The best few places this civ could found its next city (read-only): valid sites it has revealed, 4-9
# tiles from its own cities, scored by luxuries/strategics it can see, fresh water, coast and hills.
SITES_LUA = LUA_COMMON + r"""
local function try(f, default) local ok, v = pcall(f) if ok and v ~= nil then return v end return default end
local me, cities = Players[P], Players[P]:GetCities()
local vis = try(function() return PlayersVisibility[P] end, nil)
local mine, others = {}, {}
for _, id in ipairs(PlayerManager.GetAliveIDs()) do
  for _, c in Players[id]:GetCities():Members() do
    if id == P then mine[#mine+1] = {c:GetX(), c:GetY(), c:GetName()} else others[#others+1] = {c:GetX(), c:GetY()} end
  end
end
local found = {}
for i = 0, Map.GetPlotCount() - 1 do
  local plot = Map.GetPlotByIndex(i)
  local x, y = plot:GetX(), plot:GetY()
  local near, nearName, nx, ny = 99, "", -1, -1
  for _, c in ipairs(mine) do local d = Map.GetPlotDistance(x, y, c[1], c[2]); if d < near then near, nearName, nx, ny = d, c[3], c[1], c[2] end end
  if near >= 4 and near <= 9 and (vis == nil or try(function() return vis:IsRevealed(i) end, false)) and cities:IsValidFoundLocation(x, y) then
    local foreign = 99
    for _, c in ipairs(others) do local d = Map.GetPlotDistance(x, y, c[1], c[2]); if d < foreign then foreign = d end end
    local lux, strat, bonus = {}, {}, 0
    for dx = -3, 3 do for dy = -3, 3 do
      local q = Map.GetPlot(x + dx, y + dy)
      if q and Map.GetPlotDistance(x, y, q:GetX(), q:GetY()) <= 2 then
        local r = q:GetResourceType()
        local row = (r >= 0) and GameInfo.Resources[r] or nil
        local seen = row ~= nil
        if seen and row.PrereqTech then seen = me:GetTechs():HasTech(GameInfo.Technologies[row.PrereqTech].Index) end
        if seen and row.PrereqCivic then seen = me:GetCulture():HasCivic(GameInfo.Civics[row.PrereqCivic].Index) end
        if seen then
          if row.ResourceClassType == "RESOURCECLASS_LUXURY" then lux[#lux+1] = row.ResourceType
          elseif row.ResourceClassType == "RESOURCECLASS_STRATEGIC" then strat[#strat+1] = row.ResourceType
          else bonus = bonus + 1 end
        end
      end
    end end
    local water = try(function() return plot:IsFreshWater() end, false)
    local coast = try(function() return plot:IsCoastalLand() end, false)
    local hills = try(function() return plot:IsHills() end, false)
    local score = 3 * #lux + 2 * #strat + bonus + (water and 3 or 0) + (coast and 1 or 0) + (hills and 1 or 0)
                  - math.abs(near - 5) - (foreign <= 5 and 4 or 0)
    local ok, ct = pcall(function() return plot:GetContinentType() end)
    local crow = ok and ct and ct >= 0 and GameInfo.Continents[ct] or nil
    found[#found+1] = {score, i, x, y, near, nearName, foreign, table.concat(lux, ","), table.concat(strat, ","), bonus, water, coast, hills,
                       nx, ny, crow and esc(txt(crow.Description)) or ""}
  end
end
table.sort(found, function(a, b) return a[1] > b[1] end)
for k = 1, math.min(#found, 4) do
  local f = found[k]
  print(table.concat({"SITE", f[2], f[3], f[4], f[1], f[5], esc(Locale.Lookup(f[6])), f[7], f[8], f[9], f[10], tostring(f[11]), tostring(f[12]), tostring(f[13]),
    f[14], f[15], f[16]}, "|"))
end
print("COUNT|" .. #found .. "|vis " .. tostring(vis ~= nil))
"""

# Found a city where the leader chose, with the civ's closest settler, if it's within RANGE tiles.
# (Civ won't let scripts send a settler somewhere, probe 5; RANGE keeps the shortcut to a few turns' walk.)
SETTLE_LUA = r"""
local cities = Players[P]:GetCities()
if not cities:IsValidFoundLocation(X, Y) then print("RES|no|that site can't be settled any more") return end
local best, bd = nil, 999
for _, u in Players[P]:GetUnits():Members() do
  if GameInfo.Units[u:GetType()].UnitType == "UNIT_SETTLER" then
    local d = Map.GetPlotDistance(u:GetX(), u:GetY(), X, Y)
    if d < bd then best, bd = u, d end
  end
end
if best == nil then print("RES|wait|waiting for a settler") return end
if bd > RANGE then print("RES|wait|your nearest settler is " .. bd .. " tiles away") return end
local before = cities:GetCount()
local city = cities:Create(X, Y)
if city == nil or cities:GetCount() <= before then print("RES|no|the city couldn't be founded") return end
UnitManager.Kill(best, false)
print("RES|ok|founded " .. Locale.Lookup(city:GetName()))
"""
SETTLE_RANGE = 8

# An embassy (probe 5): Civ's rules are the Diplomatic Service civic and 50 gold.
EMBASSY_LUA = r"""
local d = Players[P]:GetDiplomacy()
if not d:HasMet(B) then print("RES|no|you haven't met them") return end
if d:IsAtWarWith(B) then print("RES|no|not while at war") return end
if d:HasEmbassyAt(B) then print("RES|no|you already have an embassy there") return end
local civic = GameInfo.Civics["CIVIC_DIPLOMATIC_SERVICE"]
if civic and not Players[P]:GetCulture():HasCivic(civic.Index) then print("RES|no|embassies need the Diplomatic Service civic") return end
local t = Players[P]:GetTreasury()
if t:GetGoldBalance() < 50 then print("RES|no|an embassy costs 50 gold") return end
t:ChangeGoldBalance(-50)
d:SetHasEmbassyAt(B, true)
print("RES|ok|embassy opened (50 gold)")
"""


# ---- steering (research, civics, expansion, districts) ------------------------------------
STEER_COMMON = LUA_COMMON + r"""
local function civic_ready(cul, row)
  if cul:HasCivic(row.Index) then return false end
  local prereqs = 0
  for p in GameInfo.CivicPrereqs() do
    if p.Civic == row.CivicType then
      prereqs = prereqs + 1
      if not cul:HasCivic(GameInfo.Civics[p.PrereqCivic].Index) then return false end
    end
  end
  -- Future-era civics have no prerequisite rows (they're gated by era): only starting civics may have none.
  return prereqs > 0 or row.EraType == "ERA_ANCIENT"
end
local function district_unlocked(pid, row)
  if row.DistrictType == "DISTRICT_CITY_CENTER" or row.DistrictType == "DISTRICT_WONDER" or row.TraitType then return false end
  if row.PrereqTech and not Players[pid]:GetTechs():HasTech(GameInfo.Technologies[row.PrereqTech].Index) then return false end
  if row.PrereqCivic and not Players[pid]:GetCulture():HasCivic(GameInfo.Civics[row.PrereqCivic].Index) then return false end
  return true
end
"""

OPTIONS_LUA = STEER_COMMON + r"""
local techs, cul = Players[P]:GetTechs(), Players[P]:GetCulture()
local cur = techs:GetResearchingTech()
print("RESEARCHING|" .. (cur >= 0 and GameInfo.Technologies[cur].TechnologyType or "") .. "|" .. (cur >= 0 and esc(txt(GameInfo.Technologies[cur].Name)) or ""))
for row in GameInfo.Technologies() do
  if not techs:HasTech(row.Index) and techs:CanResearch(row.Index) then print("TECH|" .. row.TechnologyType .. "|" .. esc(txt(row.Name))) end
end
local cc = cul:GetProgressingCivic()
print("CIVIC_NOW|" .. (cc >= 0 and GameInfo.Civics[cc].CivicType or "") .. "|" .. (cc >= 0 and esc(txt(GameInfo.Civics[cc].Name)) or ""))
for row in GameInfo.Civics() do
  if civic_ready(cul, row) then print("CIVIC|" .. row.CivicType .. "|" .. esc(txt(row.Name))) end
end
print("SETTLERS|" .. tostring(not Players[P]:GetUnits():IsBuildDisabled(GameInfo.Units["UNIT_SETTLER"].Index)))
for g in GameInfo.Governments() do
  if g.PrereqCivic == nil or (GameInfo.Civics[g.PrereqCivic] ~= nil and cul:HasCivic(GameInfo.Civics[g.PrereqCivic].Index)) then
    print("GOVOK|" .. g.GovernmentType .. "|" .. esc(txt(g.Name)))
  end
end
for u in GameInfo.Units() do
  if u.UnitType ~= "UNIT_SETTLER" and Players[P]:GetUnits():IsBuildDisabled(u.Index) then print("BANNED|" .. u.UnitType) end
end
print("ENVOYS|" .. Players[P]:GetInfluence():GetTokensToGive())
for _, b in ipairs(PlayerManager.GetAliveMajorIDs()) do
  if b ~= P and Players[P]:GetDiplomacy():HasDelegationAt(b) then print("DELEG|" .. b) end
  if b ~= P and Players[P]:GetDiplomacy():HasEmbassyAt(b) then print("EMB|" .. b) end
end
local settlers = 0
for _, u in Players[P]:GetUnits():Members() do
  if GameInfo.Units[u:GetType()].UnitType == "UNIT_SETTLER" then settlers = settlers + 1 end
end
print("SETTLER_COUNT|" .. settlers)
for _, city in Players[P]:GetCities():Members() do
  local have = city:GetDistricts()
  local free = {}
  for row in GameInfo.Districts() do
    if district_unlocked(P, row) and not have:HasDistrict(row.Index) then free[#free+1] = row.DistrictType end
  end
  print("CITY|" .. city:GetID() .. "|" .. esc(txt(city:GetName())) .. "|" .. table.concat(free, ","))
end
"""

# InGame (read-only): valid tiles for one district in one city, best first, with what's around them.
TILES_LUA = LUA_COMMON + r"""
local city = Players[P]:GetCities():FindID(CITY)
if city == nil then return end
local params = {}
params[CityOperationTypes.PARAM_DISTRICT_TYPE] = GameInfo.Districts[DTYPE].Hash
local res = CityManager.GetOperationTargets(city, CityOperationTypes.BUILD, params)
local plots = res and res[CityOperationResults.PLOTS] or {}
for _, idx in ipairs(plots) do
  local plot = Map.GetPlotByIndex(idx)
  local mountains, districts, water = 0, 0, 0
  for dir = 0, DirectionTypes.NUM_DIRECTION_TYPES - 1 do
    local adj = Map.GetAdjacentPlot(plot:GetX(), plot:GetY(), dir)
    if adj then
      if adj:IsMountain() then mountains = mountains + 1 end
      if adj:IsWater() then water = water + 1 end
      if adj:GetDistrictType() >= 0 then districts = districts + 1 end
    end
  end
  local terrain = GameInfo.Terrains[plot:GetTerrainType()]
  local f = plot:GetFeatureType()
  local feature = f >= 0 and GameInfo.Features[f] or nil
  print(table.concat({"TILE", idx, plot:GetX(), plot:GetY(), esc(terrain and txt(terrain.Name) or "?"),
    esc(feature and txt(feature.Name) or ""), tostring(plot:IsRiver()), mountains, districts, water}, "|"))
end
"""

SET_RESEARCH_LUA = r"""
local techs, row = Players[P]:GetTechs(), GameInfo.Technologies[NAME]
if row == nil or techs:HasTech(row.Index) or not techs:CanResearch(row.Index) then print("RES|no|can't research that now") return end
if techs:GetResearchingTech() ~= row.Index then techs:SetResearchingTech(row.Index) end
print("RES|ok|researching " .. NAME)
"""

SET_CIVIC_LUA = r"""
local cul, row = Players[P]:GetCulture(), GameInfo.Civics[NAME]
if row == nil or not civic_ready(cul, row) then print("RES|no|can't study that civic now") return end
if cul:GetProgressingCivic() ~= row.Index then cul:SetProgressingCivic(row.Index) end
print("RES|ok|studying " .. NAME)
"""

# InGame (read-only): the current government only exists on the UI side (probe 3).
GOV_NOW_LUA = r"""
local cul = Players[P]:GetCulture()
local g = cul:GetCurrentGovernment()
local row = (g ~= nil and g >= 0) and GameInfo.Governments[g] or nil
print("GOVNOW|" .. (row and row.GovernmentType or "") .. "|" .. tostring(cul:IsInAnarchy()))
"""

SET_GOVERNMENT_LUA = r"""
local cul, row = Players[P]:GetCulture(), GameInfo.Governments[NAME]
if row == nil then print("RES|no|no such government") return end
if row.PrereqCivic ~= nil and not (GameInfo.Civics[row.PrereqCivic] and cul:HasCivic(GameInfo.Civics[row.PrereqCivic].Index)) then
  print("RES|no|that government isn't unlocked yet") return
end
cul:SetCurrentGovernment(row.Index)
print("RES|ok|switched to " .. NAME)
"""

BAN_UNIT_LUA = r"""
local row = GameInfo.Units[NAME]
if row == nil or NAME == "UNIT_SETTLER" then print("RES|no|unknown unit type") return end
Players[P]:GetUnits():SetBuildDisabled(row.Index, BAN)
print("RES|ok|" .. (BAN and "banned " or "allowed ") .. NAME)
"""

# Only envoys the civ has actually earned: spend one, then send it (GiveFreeTokenToPlayer alone is free).
ENVOY_LUA = r"""
local inf = Players[P]:GetInfluence()
if inf:GetTokensToGive() < 1 then print("RES|no|no envoys to send right now") return end
if not Players[P]:GetDiplomacy():HasMet(M) then print("RES|no|you haven't met that city-state") return end
inf:ChangeTokensToGive(-1)
inf:GiveFreeTokenToPlayer(M)
print("RES|ok|envoy sent")
"""

# A delegation costs 25 gold in Civ 6.
DELEGATION_LUA = r"""
local d = Players[P]:GetDiplomacy()
if not d:HasMet(B) then print("RES|no|you haven't met them") return end
if d:IsAtWarWith(B) then print("RES|no|not while at war") return end
if d:HasDelegationAt(B) then print("RES|no|you already have a delegation there") return end
local t = Players[P]:GetTreasury()
if t:GetGoldBalance() < 25 then print("RES|no|a delegation costs 25 gold") return end
t:ChangeGoldBalance(-25)
d:SetHasDelegationAt(B, true)
print("RES|ok|delegation sent (25 gold)")
"""

SET_EXPAND_LUA = r"""
Players[P]:GetUnits():SetBuildDisabled(GameInfo.Units["UNIT_SETTLER"].Index, not EXPAND)
print("RES|ok|" .. (EXPAND and "settlers allowed" or "no more settlers"))
"""

# Signature from the game's own Debug/City.ltp: (district INDEX, plot index, percent built).
# The hash instead of the index crashed Civ 6, so the tile is re-checked against the game's own list first.
PLACE_DISTRICT_LUA = STEER_COMMON + r"""
local city = Players[P]:GetCities():FindID(CITY)
local row = GameInfo.Districts[DTYPE]
if city == nil or row == nil then print("RES|no|no such city or district") return end
if not district_unlocked(P, row) or city:GetDistricts():HasDistrict(row.Index) then print("RES|no|that district isn't available there") return end
city:GetBuildQueue():CreateIncompleteDistrict(row.Index, PLOT, 0)
print("RES|" .. (city:GetBuildQueue():HasDistrictBeenPlaced(row.Index) and "ok|district site marked" or "no|the game didn't place it"))
"""

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,60}$")

GOVERNMENT_COOLDOWN = 10   # turns between a leader's government changes (Civ adds anarchy for quick switches)

# What each successful bridge action changes: (relation field: 0 war, 1 friends, 2 allied; new value).
ORDERED_CHANGE = {"declare_war": (0, True), "force_war": (0, True), "make_peace": (0, False), "agreed_peace": (0, False),
                  "force_peace": (0, False), "propose_friendship": (1, True), "propose_alliance": (2, True),
                  "denounce": (1, False), "break_alliance": (1, False)}
# A decision Civ's own AI made -> the bridge action that undoes it (None: can't be undone).
UNDO = {"war": "force_peace", "peace": "force_war", "friendship": "break_alliance", "alliance": None}
UNDO_LABEL = {"force_peace": "make peace again", "force_war": "go back to war", "break_alliance": "end the friendship"}


def _lua_quote(text: str) -> str:
    """A Lua string literal for any text (no newlines or control characters survive)."""
    clean = " ".join(re.sub(r"[\x00-\x1f]", " ", str(text)).split())
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lua_for(kind: str, a: int, b: int, fresh: bool = False) -> str:
    return f"local A, B = {int(a)}, {int(b)}\nlocal FRESH = {'true' if fresh else 'false'}\n" + ACTION_LUA[kind]


# InGame (read-only): is A's peace with B still inside Civ's minimum peace length? (CityStates.lua's math)
FRESH_PEACE_LUA = r"""
local d = Players[A]:GetDiplomacy()
local ok, fresh = pcall(function()
  return not d:IsAtWarWith(B)
    and Game.GetCurrentGameTurn() - d:GetAtWarChangeTurn(B) < Game.GetGameDiplomacy():GetMinPeaceDuration()
end)
print("FRESH|" .. tostring(ok and fresh == true))
"""


def _fields(lines: list[str], tag: str) -> list[list[str]]:
    return [line.split("|")[1:] for line in lines if line.startswith(tag + "|")]


def _title(type_name: str, prefix: str) -> str:
    """LEADER_JOAO_III -> 'Joao Iii' style fallback when the game has no localized name."""
    return " ".join(w.capitalize() for w in type_name.removeprefix(prefix).split("_")) or type_name


# ---- the game ---------------------------------------------------------------------------
@dataclass
class World:
    """One read of the game."""
    turn: int
    local_player: int
    seed: str
    civs: dict[int, Civ]
    types: dict[int, tuple[str, str]]               # id -> (LEADER_X, CIVILIZATION_Y)
    scores: dict[int, int]
    relations: dict[tuple[int, int], tuple[bool, bool, bool]]   # (a<b) -> (war, friends, allied)
    met: dict[int, set[int]]
    views: dict[tuple[int, int], str]               # (a, b) -> a's DIPLO_STATE_* toward b
    city_states: dict[int, "CityState"] = field(default_factory=dict)


@dataclass
class Awareness:
    """One read of AWARE_LUA."""
    turn: int
    cities: dict[tuple[int, int], dict]              # (owner, city id) -> {name, x, y}
    near: list[tuple[int, int, int, int, int]]       # (owner a, city a, owner b, city b, tiles)
    troops: dict[tuple[int, int], tuple[int, int]]   # (a, b) -> b's units within BORDER_TILES of a's cities (n, strength)
    wonders: dict[int, list[tuple[str, str, str]]]   # owner -> [(BUILDING_X, name, city)]
    religion: dict[int, str]
    progress: dict[int, tuple[int, int]]             # owner -> (techs, civics)
    reasons: dict[tuple[int, int], list[tuple[int, str]]]   # (ai, other) -> [(score, text)]
    ages: dict[int, str]                             # owner -> "golden" | "dark"
    districts: dict[int, int] = field(default_factory=dict)   # owner -> finished districts (not city centers)
    resources: dict[int, dict[str, tuple[str, int]]] = field(default_factory=dict)   # owner -> {RESOURCE_X: (class, n)}
    reach: dict[tuple[int, int, int], int] = field(default_factory=dict)   # (owner, city id, other civ) -> tiles to its closest city
    other_cities: dict[str, str] = field(default_factory=dict)   # city name -> "the Free Cities" / "the city-state of X"
    era_score: dict[int, tuple[int, int, int]] = field(default_factory=dict)   # owner -> (score, dark below, golden at)
    loyalty: dict[tuple[int, int], dict] = field(default_factory=dict)   # (owner, city id) -> {name, loyalty, per_turn, ...}
    era_countdown: int = 0
    victories: list[str] = field(default_factory=list)   # enabled VICTORY_ types
    taken: dict[tuple[int, int], tuple[str, int, bool]] = field(default_factory=dict)   # (owner, city id) -> (name, founder, was their capital)
    troop_kinds: dict[tuple[int, int], dict[str, int]] = field(default_factory=dict)   # (a, b) -> {unit name: n} of b's near a
    treaties: dict[frozenset, tuple[str, int, int]] = field(default_factory=dict)   # {a, b} -> (state, friendship since, alliance turns left)
    friend_turns: int = 30                                # how long a declared friendship lasts
    spies: set[tuple[int, int, str, int, str]] = field(default_factory=set)   # (captor, owner, spy id, level, name)
    emergencies: dict[tuple[str, int], dict] = field(default_factory=dict)   # (name, target) -> {turns_left, members, goal, seen_by}
    combat: list[dict] = field(default_factory=list)      # fights since the last read
    grid: tuple[int, int, bool] = (0, 0, True)             # map width, height, wraps east-west
    economy: dict[int, dict] = field(default_factory=dict)   # owner -> gold, upkeep, science, culture, faith, era, favor
    army: dict[int, dict] = field(default_factory=dict)      # owner -> {"kinds": {name: n}, "where": {land owner: n}}
    city_info: dict[tuple[int, int], dict] = field(default_factory=dict)   # (owner, city id) -> size, defense, damage...
    camps: dict[tuple[int, int], int] = field(default_factory=dict)        # (owner, city id) -> tiles to nearest camp
    barbs_near: dict[tuple[int, int], int] = field(default_factory=dict)   # (owner, city id) -> barbarian units within 3
    grievances: dict[tuple[int, int], int] = field(default_factory=dict)   # (a, b) -> grievances a holds against b
    era_names: dict[int, str] = field(default_factory=dict)
    mods: set[str] = field(default_factory=set)              # mods we can use (gift_it_to_me)
    wmds: dict[int, dict[str, int]] = field(default_factory=dict)   # owner -> {weapon name: n}
    governments: dict[int, str] = field(default_factory=dict)
    policies: dict[int, list[str]] = field(default_factory=dict)
    routes: dict[tuple[int, int], int] = field(default_factory=dict)   # (from civ, to civ) -> trade routes
    governors: dict[int, list[tuple[str, str]]] = field(default_factory=dict)   # owner -> [(governor, city)]
    spies_out: dict[int, list[tuple[str, int, str]]] = field(default_factory=dict)   # owner -> [(city, city owner, mission)]
    gp_points: dict[int, dict[int, int]] = field(default_factory=dict)   # owner -> {class: points}
    gp_classes: dict[int, str] = field(default_factory=dict)
    gp_now: list[tuple[int, str, int]] = field(default_factory=list)      # (class, person, cost) up for grabs
    cs_types: dict[int, str] = field(default_factory=dict)
    envoys: dict[int, dict[int, int]] = field(default_factory=dict)       # city-state -> {civ: envoys}
    disasters: list[tuple[int, str, int, int]] = field(default_factory=list)   # (turn, what, x, y) since last read
    ribbon_military: dict[int, int] = field(default_factory=dict)   # the leader ribbon's military strength, by player


def parse_awareness(lines: list[str], turn: int) -> Awareness:
    num = lambda x: int(float(x)) if x.replace(".", "", 1).lstrip("-").isdigit() else 0  # noqa: E731
    a = Awareness(turn, {}, [], {}, {}, {}, {}, {}, {})
    for f in _fields(lines, "CT"):
        o, cid, name, x, y = f[:5]
        a.cities[(int(o), int(cid))] = {"name": name, "x": int(x), "y": int(y), "continent": f[5] if len(f) > 5 else "",
                                         "capital": len(f) > 6 and f[6] == "true"}
    for f in _fields(lines, "GRID"):
        a.grid = (int(f[0]), int(f[1]), f[2] != "false")
    a.mods = {f[0] for f in _fields(lines, "MODON") if len(f) > 1 and f[1] == "true"}
    for f in _fields(lines, "ECON"):
        if len(f) >= 7:
            o = int(f[0])
            a.economy[o] = {"gold": num(f[1]), "upkeep": num(f[2]), "science": num(f[3]), "culture": num(f[4]),
                            "faith": num(f[5]), "era": num(f[6]), **({"faith_saved": num(f[7])} if len(f) > 7 else {})}
    for f in _fields(lines, "ARMY"):
        kinds = {k: int(v) for k, _, v in (x.rpartition(":") for x in (f[1] if len(f) > 1 else "").split(",")) if k and v.isdigit()}
        where = {int(k): int(v) for k, _, v in (x.rpartition(":") for x in (f[2] if len(f) > 2 else "").split(","))
                 if k.lstrip("-").isdigit() and v.isdigit()}
        a.army[int(f[0])] = {"kinds": kinds, "where": where}
    for f in _fields(lines, "CITY2"):
        if len(f) >= 8:
            o, cid = int(f[0]), int(f[1])
            a.city_info.setdefault((o, cid), {}).update({"size": num(f[2]), "defense": num(f[3]), "garrison": (num(f[4]), num(f[5])),
                                                         "walls": (num(f[6]), num(f[7]))})
    for o, cid, d in (f[:3] for f in _fields(lines, "CAMP") if len(f) >= 3):
        k = (int(o), int(cid))
        a.camps[k] = min(a.camps.get(k, 99), int(d))
    for o, cid in (f[:2] for f in _fields(lines, "BARB") if len(f) >= 2):
        k = (int(o), int(cid))
        a.barbs_near[k] = a.barbs_near.get(k, 0) + 1
    a.near = [tuple(int(v) for v in f) for f in _fields(lines, "NEAR")]
    for f in _fields(lines, "TROOPS"):
        A, B, n, st = (int(x) for x in f[:4])
        a.troops[(A, B)] = (n, st)
        if len(f) > 4 and f[4]:
            a.troop_kinds[(A, B)] = {k: int(v) for k, _, v in (x.rpartition(":") for x in f[4].split(",")) if k and v.isdigit()}
    for o, btype, name, city in _fields(lines, "WONDER"):
        a.wonders.setdefault(int(o), []).append((btype, name, city))
    for o, _rtype, name in _fields(lines, "RELIGION"):
        a.religion[int(o)] = name
    for o, techs, civics in _fields(lines, "PROGRESS"):
        a.progress[int(o)] = (int(techs), int(civics))
    for o, rtype, rclass, n in _fields(lines, "HAS"):
        a.resources.setdefault(int(o), {})[rtype] = (rclass, int(n))
    for _id, kind, _cid, name in (f[:4] for f in _fields(lines, "CTX") if len(f) >= 4):
        a.other_cities[name] = kind
    for o, cid, other, d in _fields(lines, "CD"):
        a.reach[(int(o), int(cid), int(other))] = int(d)
    for o, n in _fields(lines, "DISTRICTS"):
        a.districts[int(o)] = int(n)
    for A, B, score, text in _fields(lines, "MOD"):
        a.reasons.setdefault((int(A), int(B)), []).append((num(score), text))
    return a


def add_ages(a: Awareness, lines: list[str]) -> None:
    """Fold AGES_LUA (InGame) into an awareness read."""
    num = lambda x: int(float(x)) if x and x.replace(".", "", 1).lstrip("-").isdigit() else None  # noqa: E731
    for f in _fields(lines, "ERANEXT"):
        a.era_countdown = num(f[0]) or 0
    for f in _fields(lines, "AGE"):
        o, age = int(f[0]), f[1]
        if age in ("golden", "heroic", "dark"):
            a.ages[o] = {"heroic": "heroic golden"}.get(age, age)
        score, dark, golden = (num(x) for x in (f[2:5] + ["", "", ""])[:3])
        if score is not None and dark is not None and golden is not None:
            a.era_score[o] = (score, dark, golden)
    a.victories = [f[0] for f in _fields(lines, "VIC") if f and f[0]]
    for f in _fields(lines, "RIBBON"):
        if len(f) >= 2 and f[0].lstrip("-").isdigit() and num(f[1]) is not None:
            a.ribbon_military[int(f[0])] = num(f[1])
    for f in _fields(lines, "WMD"):
        if len(f) >= 3 and num(f[2]):
            a.wmds.setdefault(int(f[0]), {})[f[1]] = num(f[2])
    for f in _fields(lines, "GOV"):
        if len(f) >= 2 and f[1]:
            a.governments[int(f[0])] = f[1]
    for f in _fields(lines, "POLICY"):
        if len(f) >= 2 and f[1]:
            a.policies.setdefault(int(f[0]), []).append(f[1])
    for f in _fields(lines, "ROUTE"):
        if len(f) >= 2 and f[0].lstrip("-").isdigit() and f[1].lstrip("-").isdigit():
            k = (int(f[0]), int(f[1]))
            a.routes[k] = a.routes.get(k, 0) + 1
    for f in _fields(lines, "GOVERNOR"):
        if len(f) >= 2:
            a.governors.setdefault(int(f[0]), []).append((f[1], f[2] if len(f) > 2 else ""))
    for f in _fields(lines, "SPY"):
        if len(f) >= 3:
            a.spies_out.setdefault(int(f[0]), []).append((f[1], num(f[2]) if num(f[2]) is not None else -1,
                                                          f[3] if len(f) > 3 else ""))
    for f in _fields(lines, "GPP"):
        if len(f) >= 3 and num(f[2]):
            a.gp_points.setdefault(int(f[0]), {})[int(f[1])] = num(f[2])
    for f in _fields(lines, "GPCLASS"):
        if len(f) >= 2 and f[0].isdigit():
            a.gp_classes[int(f[0])] = f[1]
    for f in _fields(lines, "GPNOW"):
        if len(f) >= 3 and f[0].isdigit():
            a.gp_now.append((int(f[0]), f[1], num(f[2]) or 0))
    for f in _fields(lines, "CSTYPE"):
        if len(f) >= 2:
            a.cs_types[int(f[0])] = f[1]
    for f in _fields(lines, "ENVOY"):
        if len(f) >= 3 and num(f[2]):
            a.envoys.setdefault(int(f[0]), {})[int(f[1])] = num(f[2])
    for f in _fields(lines, "ERANAME"):
        if len(f) >= 2 and f[0].isdigit():
            a.era_names[int(f[0])] = f[1]
    for f in _fields(lines, "FAVOR"):
        a.economy.setdefault(int(f[0]), {})["favor"] = num(f[1]) or 0
    for f in _fields(lines, "GRIEV"):
        if len(f) >= 3 and num(f[2]) is not None:
            a.grievances[(int(f[0]), int(f[1]))] = num(f[2])
    for f in _fields(lines, "CITYX"):
        if len(f) >= 6:
            info = a.city_info.setdefault((int(f[0]), int(f[1])), {})
            info.update({"amenities": num(f[2]), "amenities_needed": num(f[3]), "war_weariness": num(f[4]) or 0,
                         "building": f[5]})
    for f in _fields(lines, "FRLIMIT"):
        a.friend_turns = num(f[0]) or 30
    for f in _fields(lines, "TREATY"):
        if len(f) >= 5:
            a.treaties[frozenset({int(f[0]), int(f[1])})] = (f[2], num(f[3]) if num(f[3]) is not None else -1,
                                                             num(f[4]) if num(f[4]) is not None else -1)
    for f in _fields(lines, "SPYCAP"):
        if len(f) >= 5:
            a.spies.add((int(f[0]), int(f[1]), f[2], num(f[3]) or 0, f[4]))
    for f in _fields(lines, "EMER"):
        if len(f) >= 7:
            k = (f[1], num(f[2]) if num(f[2]) is not None else -1)
            e = a.emergencies.setdefault(k, {"turns_left": num(f[3]), "begun": f[4] == "true", "goal": f[6] if len(f) > 6 else "",
                                             "members": {int(m) for m in f[5].split(";") if m.lstrip("-").isdigit()},
                                             "seen_by": set()})
            e["seen_by"].add(int(f[0]))
    for f in _fields(lines, "TAKEN"):
        if len(f) >= 5:
            a.taken[(int(f[0]), int(f[1]))] = (f[2], int(f[3]), f[4] == "true")
    for f in _fields(lines, "LOYAL"):
        if len(f) < 7:
            continue
        o, cid, name, loyal, per, turns = int(f[0]), int(f[1]), f[2], num(f[3]), num(f[4]), num(f[5])
        a.loyalty[(o, cid)] = {"name": name, "loyalty": loyal, "per_turn": per, "turns_to_flip": turns,
                               "flips_to": f[6] if len(f) > 6 else ""}


COMPASS = ["east", "northeast", "north", "northwest", "west", "southwest", "south", "southeast"]


def _axial(x: int, y: int) -> tuple[int, int]:
    """Civ 6's offset grid as axial hex coordinates: odd rows sit half a tile east, y grows north (probe 7)."""
    return x - (y - (y & 1)) // 2, y


def hex_step(x0: int, y0: int, x1: int, y1: int, width: int = 0) -> tuple[int, str]:
    """Tiles and compass direction from (x0, y0) to (x1, y1), the short way round a map that wraps east-west."""
    best = None
    for shift in ((-width, 0, width) if width else (0,)):
        q0, r0 = _axial(x0, y0)
        q1, r1 = _axial(x1 + shift, y1)
        dq, dr = q1 - q0, r1 - r0
        dist = (abs(dq) + abs(dr) + abs(dq + dr)) // 2
        if best is None or dist < best[0]:
            best = (dist, dq, dr)
    dist, dq, dr = best
    if dist == 0:
        return 0, "here"
    px, py = math.sqrt(3) * (dq + dr / 2), 1.5 * dr
    return dist, COMPASS[int(((math.degrees(math.atan2(py, px)) % 360) + 22.5) // 45) % 8]


def parse_combat(lines: list[str]) -> list[dict]:
    """COMBAT_LUA's log: one dict per fight (attacker/defender: player, who if not a major civ, unit, killed, city)."""
    out = []
    for f in _fields(lines, "CMB"):
        if len(f) < 11:
            continue
        side = lambda g: {"player": int(g[0]) if g[0].lstrip("-").isdigit() else -1, "kind": g[1],  # noqa: E731
                          "unit": g[2], "killed": g[3] == "true", "city": g[4]}
        att, dfn = side(f[1:6]), side(f[6:11])
        if any(x["player"] < 0 and not (x["kind"] or x["unit"] or x["city"]) for x in (att, dfn)):
            continue   # Civ reported a side it couldn't name: better unsaid than "someone"
        out.append({"turn": int(f[0]) if f[0].isdigit() else 0, "attacker": att, "defender": dfn})
    return out


def describe_awareness(before: Awareness, now: Awareness, civs: dict[int, Civ]) -> list[tuple[str, frozenset]]:
    """Events from two turns of awareness: settling close, cities changing hands, wonders, religions, troops."""
    out: list[tuple[str, frozenset]] = []
    label = lambda i: civs[i].label if i in civs else f"civ {i}"  # noqa: E731
    names_before = {c["name"]: o for (o, _), c in before.cities.items()}
    names_now = {c["name"]: o for (o, _), c in now.cities.items()}
    for (o, cid), c in now.cities.items():
        name = c["name"]
        if name not in names_before:
            close = [(d, ob, now.cities[(ob, idb)]["name"]) for oa, ida, ob, idb, d in now.near
                     if (oa, ida) == (o, cid) and (ob, idb) in now.cities] + \
                    [(d, oa, now.cities[(oa, ida)]["name"]) for oa, ida, ob, idb, d in now.near
                     if (ob, idb) == (o, cid) and (oa, ida) in now.cities]
            if close:
                d, other, their_city = min(close)
                out.append((f"{label(o)} founded {name} just {d} tiles from {label(other)}'s city {their_city}.",
                            frozenset({o, other})))
            elif name in before.other_cities:
                out.append((f"{name} left {before.other_cities[name]} and joined {label(o)}.", frozenset({o})))
            else:
                out.append((f"{label(o)} founded the city of {name}.", frozenset({o})))
        elif names_before[name] != o:
            out.append((f"{name} changed hands: {label(names_before[name])} lost it to {label(o)}.",
                        frozenset({o, names_before[name]})))
    for name, o in names_before.items():
        if name not in names_now:
            if name in now.other_cities:
                out.append((f"{label(o)}'s city {name} broke away and joined {now.other_cities[name]} "
                            "(its loyalty ran out).", frozenset({o})))
            else:
                out.append((f"{label(o)}'s city {name} is gone (razed or destroyed).", frozenset({o})))
    for o, ws in now.wonders.items():
        old = {w[0] for w in before.wonders.get(o, [])}
        for btype, wname, city in ws:
            if btype not in old:
                out.append((f"{label(o)} completed the wonder {wname} in {city}.", frozenset({o})))
    for o, rel in now.religion.items():
        if rel != before.religion.get(o):
            out.append((f"{label(o)} founded a religion: {rel}.", frozenset({o})))
    for (a, b), (n, _st) in now.troops.items():
        if n >= 2 and before.troops.get((a, b), (0, 0))[0] < 2:
            out.append((f"{label(b)} has {n} military units in or near {label(a)}'s territory.", frozenset({a, b})))
    for o, arms in now.wmds.items():
        if sum(arms.values()) > sum(before.wmds.get(o, {}).values()):
            out.append((f"{label(o)} now has nuclear weapons ({', '.join(f'{n} {k}' for k, n in arms.items())}).",
                        frozenset(civs)))
    for o in now.governments:
        if before.governments.get(o) and now.governments[o] != before.governments[o]:
            out.append((f"{label(o)} switched government to {now.governments[o]}.", frozenset({o})))
    for _turn, what, x, y in now.disasters:
        near = sorted((max(abs(c["x"] - x), abs(c["y"] - y)), o, c["name"]) for (o, _), c in now.cities.items())
        if near and near[0][0] <= 3:
            _, o, city = near[0]
            out.append((f"{what} struck near {label(o)}'s city {city}.", frozenset({o})))
    old_spies = {(c, o, sid) for c, o, sid, _, _ in before.spies}
    for captor, owner, sid, _lvl, _name in sorted(now.spies):
        if (captor, owner, sid) not in old_spies:
            out.append((f"{label(captor)} caught a spy sent by {label(owner)}.", frozenset({captor, owner})))
    for (name, target), e in now.emergencies.items():
        if (name, target) not in before.emergencies:
            if emergency_helps(name) and target in civs:
                text = f"{name} for {label(target)}: civs race to help them."
            else:
                text = f"Emergency declared: {name}" + (f" against {label(target)}" if target in civs else "") + "."
            out.append((text, frozenset(e["seen_by"] | ({target} if target in civs else set()))))
    for o in set(now.ages) | set(before.ages):
        age, old = now.ages.get(o), before.ages.get(o)
        if age and age != old:
            out.append((f"{label(o)} entered a {age} age.", frozenset({o})))
        elif old and not age:
            out.append((f"{label(o)}'s {old} age is over.", frozenset({o})))
    return out


@dataclass
class CityState:
    id: int
    name: str
    suzerain: int | None          # major civ id, or None
    military: int
    met_by: set[int]
    at_war_with: set[int]


def parse_world(lines: list[str]) -> World:
    t = _fields(lines, "TURN")
    if not t:
        raise LuaFailed("no TURN line in the game's reply: " + " / ".join(lines[:3]))
    turn, local, seed = int(t[0][0]), int(t[0][1]), t[0][2]
    civs, types, scores = {}, {}, {}
    for f in _fields(lines, "CIV"):
        cid, lt, ct, lname, cname, human, alive, cities, mil, sci, cul, gold, score = f[:13]
        cid = int(cid)
        types[cid] = (lt, ct)
        lname = re.sub(rf"\s*\({re.escape(cname)}\)$", "", lname)   # "Eleanor of Aquitaine (France)" -> "Eleanor of Aquitaine"
        civs[cid] = Civ(cid, cname if cname and cname != ct else _title(ct, "CIVILIZATION_"),
                        lname if lname and lname != lt else _title(lt, "LEADER_"),
                        is_human=human == "true", cities=int(cities), military=int(mil), science=int(sci),
                        culture=int(cul), gold=int(gold), alive=alive == "true")
        scores[cid] = int(score)
    relations = {(int(a), int(b)): (w == "true", fr == "true", al == "true")
                 for a, b, w, fr, al in _fields(lines, "REL")}
    met: dict[int, set[int]] = {cid: set() for cid in civs}
    views = {}
    for a, b, view in _fields(lines, "MET"):
        met.setdefault(int(a), set()).add(int(b))
        if view:
            views[(int(a), int(b))] = view
    ids = lambda t: {int(x) for x in t.split(",") if x.strip().lstrip("-").isdigit()}  # noqa: E731
    city_states = {}
    for f in _fields(lines, "CS"):
        if len(f) < 4:
            continue
        sid, sname, sz, mil = f[:4]
        suz = int(sz) if sz.lstrip("-").isdigit() and int(sz) >= 0 else None
        city_states[int(sid)] = CityState(int(sid), sname, suz, int(mil) if mil.isdigit() else 0,
                                          ids(f[4]) if len(f) > 4 else set(), ids(f[5]) if len(f) > 5 else set())
    return World(turn, local, seed, civs, types, scores, relations, met, views, city_states)


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def emergency_helps(name: str) -> bool:
    """Aid Request and Military Aid Request are races to help their target (Civ marks them not hostile).
    Every other emergency with a target is against it: leaders read "Aid Request against X" as a plot on X."""
    return "aid request" in str(name).lower()


@dataclass
class Civ6Game:
    """Game protocol (see civ_council/game.py) for a running Civ 6, through the tuner port."""
    proposal_turns = PROPOSAL_TURNS

    link: Link = field(default_factory=Link)
    cache_seconds: float = 1.5
    _world: World | None = None
    _read_at: float = 0.0
    _last_reported: World | None = None
    _proposals: dict[int, Proposal] = field(default_factory=dict)
    pins: dict[int, list[dict]] = field(default_factory=dict)   # leader -> [{id, tile, name}] pins on your map
    expired: list[Proposal] = field(default_factory=list)   # ran out unanswered, not yet reported
    fights: list[dict] = field(default_factory=list)       # combat from the last few turns (COMBAT_LUA)
    renewals: dict[tuple[int, int], int] = field(default_factory=dict)   # (from, to) -> turn asked: friendship to re-offer
    renewed: list[Proposal] = field(default_factory=list)  # re-offers made this turn, for the council to show
    quiet_ai: bool = True    # close Civ's own AI diplomacy pop-ups to you (QUIET_LUA); the leaders talk through us
    block_screen: bool = True   # close Civ's leader screen when you open it (first meetings and wars still show)
    quieted: list[tuple[int, int, str]] = field(default_factory=list)   # (turn, from, what) closed since last report
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _denounced: set[tuple[int, int]] = field(default_factory=set)
    events: list[tuple[str, frozenset]] = field(default_factory=list)   # (text, civs involved)
    orders: dict[int, dict] = field(default_factory=dict)   # civ id -> standing orders ({research, civic, expand})
    # Diplomacy changes the bridge made itself since the last turn report: (pair, field, new value).
    # Any other change involving an AI civ was decided by Civ's own AI, and its Claude leader is told.
    _ordered: list[tuple[tuple[int, int], int, bool]] = field(default_factory=list)
    notices: dict[int, dict] = field(default_factory=dict)   # id -> a decision Civ's AI made for a leader
    settled_by_order: set[str] = field(default_factory=set)   # cities a leader's settle order founded
    _known_deals: set[tuple] | None = None   # ongoing deal items already reported or made by the bridge
    _aware: "Awareness | None" = None        # this turn's read of AWARE_LUA
    _congress: tuple[int, dict] | None = None   # (turn, parsed CONGRESS_LUA)
    _nids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # ---- reads ---------------------------------------------------------------------
    def world(self, fresh: bool = False) -> World:
        with self._lock:
            if fresh or self._world is None or time.time() - self._read_at > self.cache_seconds:
                self._world = parse_world(self.link.run(SNAPSHOT_LUA))
                self._read_at = time.time()
                if self._last_reported is None:
                    self._last_reported = self._world
            return self._world

    @property
    def turn(self) -> int:
        return self.world().turn

    @property
    def local_player(self) -> int:
        return self.world().local_player

    @property
    def game_id(self) -> str:
        """Stays the same across saves and reloads of one game: used to keep each game's memories apart.

        Built from the civs' starting tiles (fixed for the whole game) plus the leaders' names.
        """
        w = self.world()
        fingerprint = zlib.crc32(w.seed.encode())
        leaders = "-".join(w.types[i][0].removeprefix("LEADER_").lower() for i in sorted(w.types))
        return f"{fingerprint:08x}-{leaders}"[:90]

    def civs(self) -> list[Civ]:
        return list(self.world().civs.values())

    def civ(self, civ_id: int) -> Civ:
        try:
            return self.world().civs[int(civ_id)]
        except KeyError:
            raise KeyError(f"no civ with id {civ_id}") from None

    def leader_type(self, civ_id: int) -> str:
        return self.world().types[int(civ_id)][0]

    def has_met(self, a: int, b: int) -> bool:
        return b in self.world().met.get(a, set())

    def stance(self, a: int, b: int) -> str:
        war, friends, allied = self.world().relations.get(_pair(a, b), (False, False, False))
        return WAR if war else ALLIED if allied else FRIENDS if friends else PEACE

    def is_denouncing(self, a: int, b: int) -> bool:
        return (a, b) in self._denounced or self.world().views.get((a, b)) == "DIPLO_STATE_DENOUNCED"

    def score(self, c: Civ) -> int:
        return self.world().scores.get(c.id, 0)

    def leader_agendas(self) -> dict[int, tuple[str, str]]:
        """id -> (agenda names, agenda descriptions), from the game's own text."""
        clean = lambda t: re.sub(r"\s*\[ICON_\w+\]\s*", " ", t).strip()  # noqa: E731
        return {int(f[0]): (clean(f[1]), clean(f[2]) if len(f) > 2 else "")
                for f in _fields(self.link.run(LEADER_INFO_LUA), "AGENDA")}

    def snapshot(self, viewer: int) -> dict:
        w = self.world()
        me = w.civs[viewer]
        known = w.met.get(viewer, set())
        visible = known | {viewer}
        # City-states this viewer has met. They fight on their suzerain's side.
        my_cs = [cs for cs in w.city_states.values() if viewer in cs.met_by]
        aw = self.awareness()
        # The same numbers the player sees on Civ's leader ribbon: its military strength and the faith saved up
        mil = lambda cid, fallback: (aw.ribbon_military.get(cid, fallback) if aw is not None else fallback)  # noqa: E731
        faith = lambda cid: (aw.economy.get(cid, {}).get("faith_saved") if aw is not None else None)  # noqa: E731
        backing = lambda cid: [cs for cs in my_cs if cs.suzerain == cid]  # noqa: E731
        others = []
        for cid in sorted(known):
            c = w.civs[cid]
            if not c.alive:
                continue
            others.append({
                "id": c.id, "leader": c.leader, "civ": c.name, "is_human": c.is_human,
                "stance_with_you": self.stance(viewer, c.id),
                "you_denounced_them": self.is_denouncing(viewer, c.id),
                "they_denounced_you": self.is_denouncing(c.id, viewer),
                "score": w.scores.get(c.id, 0), "cities": c.cities, "military": mil(c.id, c.military),
                "gold": c.gold, **({"faith": f} if (f := faith(c.id)) is not None else {}),
                "city_states_backing_them": [cs.name for cs in backing(c.id)],
                "military_with_city_states": mil(c.id, c.military) + sum(mil(cs.id, cs.military) for cs in backing(c.id)),
                **self._aware_about(viewer, c.id),
            })
        suzerain = lambda cs: (None if cs.suzerain is None else w.civs[cs.suzerain].leader  # noqa: E731
                               if cs.suzerain in visible else "a civ you haven't met")
        pairs = [(a, b, rel) for (a, b), rel in w.relations.items() if a in visible and b in visible]
        return {
            "turn": w.turn,
            "you": {"id": me.id, "leader": me.leader, "civ": me.name, "cities": me.cities,
                    "military": mil(me.id, me.military), "science": me.science, "culture": me.culture,
                    **({"faith": f} if (f := faith(me.id)) is not None else {}),
                    "gold": me.gold, "score": w.scores.get(viewer, 0),
                    "city_states_backing_you": [cs.name for cs in backing(viewer)],
                    "military_with_city_states": mil(me.id, me.military) + sum(mil(cs.id, cs.military) for cs in backing(viewer)),
                    **self._aware_self(viewer)},
            "others": others,
            "city_states": [{"name": cs.name, "suzerain": suzerain(cs), "military": mil(cs.id, cs.military),
                             "at_war_with_you": viewer in cs.at_war_with, **self._cs_extra(cs.id, visible)}
                            for cs in my_cs],
            "world_wars": sorted(f"{w.civs[a].leader} vs {w.civs[b].leader}" for a, b, r in pairs if r[0]),
            "world_alliances": sorted(f"{w.civs[a].leader} + {w.civs[b].leader}" for a, b, r in pairs if r[2]),
            "recent_events": [text for text, who in self.events if who <= visible][-10:],
            **self._victory(viewer),
            **({"headlines": h} if (h := self._headlines(viewer, visible)) else {}),
            **({"world_congress": wc} if (wc := self._safe_congress()) else {}),
        }

    # ---- actions -------------------------------------------------------------------
    def apply(self, actor: int, action: dict) -> ActionResult:
        kind = action.get("type")
        if kind not in ACTION_TYPES:
            return ActionResult(False, f"unknown action {kind!r}")
        try:
            target = int(action.get("target"))
            them = self.civ(target)
        except (TypeError, ValueError, KeyError):
            return ActionResult(False, f"bad target {action.get('target')!r}")
        me = self.civ(actor)
        if target == actor:
            return ActionResult(False, "cannot target yourself")
        if not them.alive:
            return ActionResult(False, f"{them.label} is gone")
        if not self.has_met(actor, target):
            return ActionResult(False, f"{me.leader} hasn't met {them.leader} yet")
        cur = self.stance(actor, target)

        if kind == "propose_deal":
            terms, why = deal_terms(action)
            if terms is None:
                return ActionResult(False, why)
            if cur == WAR:
                terms["peace"] = True   # at war, a deal is a peace offer: peace and terms go through together
            if me.gold < terms["give"].get("gold", 0):
                return ActionResult(False, f"{me.leader} only has {me.gold} gold")
            if not is_gift(terms):
                for q in self._proposals.values():
                    if q.sender == actor and q.target == target and q.kind == kind:
                        return ActionResult(False, f"you already offered {them.leader} a deal (proposal #{q.id}); "
                                                   "wait for their answer")
            if is_gift(terms) and not terms.get("peace"):   # a gift goes through at once
                res = self._deal(actor, target, terms)
                if not res.ok:
                    return res
                return self._log(f"{me.label} sent {them.label} {side_text(terms['give'])}.", actor, target)
            p = Proposal(next(self._ids), kind, actor, target, self.turn, str(action.get("note", ""))[:200], terms)
            self._proposals[p.id] = p
            what = "peace, on these terms" if terms.get("peace") else "a deal"
            return ActionResult(True, f"{me.leader} offered {them.leader} {what}: "
                                      f"{deal_summary(terms, me.leader, them.leader)} (proposal #{p.id}).", p.id)

        if kind in PROPOSAL_ACTIONS:
            if kind == "make_peace" and cur != WAR:
                return ActionResult(False, "not at war")
            if kind == "propose_friendship" and cur == FRIENDS:
                # Still friends: Civ won't take a new declaration until the old one lapses, so queue the renewal
                # and send it the turn it does (leaders kept offering a turn early and getting refused).
                if (actor, target) in self.renewals:   # leaders kept re-queuing it every turn
                    return ActionResult(False, "your friendship renewal is already pending: it goes out the turn "
                                               "the current friendship lapses")
                self.renewals[(actor, target)] = self.turn
                return ActionResult(True, f"{me.leader} will renew the friendship with {them.leader} as soon as "
                                          "the current one lapses.")
            if kind == "propose_friendship" and cur != PEACE:
                return ActionResult(False, f"current stance is {cur}")
            if kind == "propose_alliance" and cur != FRIENDS:
                return ActionResult(False, "an alliance needs declared friendship first" if cur == PEACE
                                    else f"current stance is {cur}")
            if (actor, target) in self._denounced or (target, actor) in self._denounced:
                if kind != "make_peace":
                    return ActionResult(False, "not after a denouncement")
            for p in self._proposals.values():
                if p.sender == actor and p.target == target and p.kind == kind:
                    return ActionResult(False, "that proposal is already pending")
            p = Proposal(next(self._ids), kind, actor, target, self.turn, str(action.get("note", ""))[:200])
            self._proposals[p.id] = p
            return ActionResult(True, f"{me.leader} offered {them.leader} {PROPOSAL_LABELS[kind]} (proposal #{p.id}).",
                                p.id)

        if kind == "gift_units":
            return self._gift_units(actor, target, action)
        if kind == "declare_war" and cur == WAR:
            return ActionResult(False, "already at war")
        if kind == "declare_war" and cur == ALLIED:
            return ActionResult(False, "can't attack an ally")
        if kind == "denounce" and (actor, target) in self._denounced:
            return ActionResult(False, "already denounced")
        res = self._run_action(kind, actor, target)
        if not res.ok:
            return res
        if kind == "declare_war":
            logged = self._log(f"{me.label} declared war on {them.label}"
                               + (", breaking the peace they had only just made!" if res.message == "early" else "!"),
                               actor, target)
            if action.get("target_city") and not me.is_human:
                aimed = self._attack(actor, target, str(action["target_city"]))
                return ActionResult(True, logged.message + ("" if aimed.ok else f" (no attack plan: {aimed.message})"))
            return logged
        if kind == "denounce":
            self._denounced.add((actor, target))
            return self._log(f"{me.label} denounced {them.label}.", actor, target)
        return self._log(f"{me.label} ended their friendship with {them.label}.", actor, target)

    def pending_proposals(self, target: int) -> list[Proposal]:
        self._expire()
        return [p for p in self._proposals.values() if p.target == target]

    def offers_from(self, sender: int) -> list[Proposal]:
        """Proposals this civ made that are still waiting for an answer."""
        self._expire()
        return [p for p in self._proposals.values() if p.sender == sender]

    def resolve_proposal(self, proposal_id: int, responder: int, accept: bool) -> ActionResult:
        p = self._proposals.get(proposal_id)
        if p is None or p.target != responder:
            return ActionResult(False, f"no pending proposal #{proposal_id} for you")
        del self._proposals[proposal_id]
        a, b = self.civ(p.sender), self.civ(p.target)
        if not accept:
            what = "peace on terms" if p.terms.get("peace") else PROPOSAL_LABELS[p.kind]
            return self._log(f"{b.label} rejected {a.label}'s offer of {what}.", a.id, b.id)
        if p.kind == "propose_deal" and p.terms.get("peace"):
            return self._peace_deal(p, a, b)
        if p.kind == "propose_deal":
            if self.stance(a.id, b.id) == WAR:
                return ActionResult(False, "they're at war now, so the deal is off")
            res = self._deal(a.id, b.id, p.terms)
            if not res.ok:
                self._log(f"{a.label} and {b.label} agreed to a deal ({deal_summary(p.terms, a.leader, b.leader)}), "
                          f"but it fell through: {res.message}.", a.id, b.id)
                return ActionResult(False, f"{a.leader} and {b.leader} agreed, but the deal fell through: {res.message}.")
            return self._log(f"{a.label} and {b.label} made a deal: {deal_summary(p.terms, a.leader, b.leader)}.",
                             a.id, b.id)
        # Peace both sides agreed to is signed at once, even before Civ's usual minimum war length.
        res = self._run_action("agreed_peace" if p.kind == "make_peace" else p.kind, p.sender, p.target)
        if not res.ok:
            return ActionResult(False, f"{a.leader} and {b.leader} agreed, but {res.message}.")
        if p.kind == "make_peace" and res.message == "early":
            self._denounced.discard((p.sender, p.target))
            self._denounced.discard((p.target, p.sender))
            self._drop_mirrors(p)
            return self._log(f"{a.label} and {b.label} are now at peace (by agreement, before Civ's usual "
                             "minimum length of a war).", a.id, b.id)
        self._denounced.discard((p.sender, p.target))
        self._denounced.discard((p.target, p.sender))
        self._drop_mirrors(p)
        new = {"make_peace": "at peace", "propose_friendship": "declared friends", "propose_alliance": "allied"}[p.kind]
        return self._log(f"{a.label} and {b.label} are now {new}.", a.id, b.id)

    def _gift_units(self, actor: int, target: int, action: dict) -> ActionResult:
        name = str(action.get("unit") or "").strip()
        try:
            n = max(1, min(MAX_GIFT_UNITS, int(action.get("count") or 1)))
        except (TypeError, ValueError):
            return ActionResult(False, f"count should be a number, not {action.get('count')!r}")
        if not name or not CITY_NAME_RE.match(name):
            return ActionResult(False, "say which unit to gift (e.g. \"unit\": \"Tank\")")
        quoted = name.replace("\\", "").replace('"', "")
        lua = f'local A, B, NAME, N = {int(actor)}, {int(target)}, "{quoted}", {n}\n' + GIFT_LUA
        try:
            f = _fields(self.link.run(lua), "RES")
        finally:
            self._read_at = 0.0
        if not f:
            return ActionResult(False, "no answer from the game")
        if f[0][0] != "ok":
            return ActionResult(False, f[0][1] if len(f[0]) > 1 else "the gift didn't go through")
        given, label = f[0][1], f[0][2] if len(f[0]) > 2 else name
        moved = int(f[0][3]) if len(f[0]) > 3 and f[0][3].isdigit() else 0
        where = " (delivered to their own land)" if moved else ""
        return self._log(f"{self.civ(actor).label} gifted {given} {label} to {self.civ(target).label}{where}.", actor, target)

    def _peace_deal(self, p: Proposal, a: Civ, b: Civ) -> ActionResult:
        """Peace with terms: sign the peace, then carry out the terms. If the terms can't be carried out,
        the war resumes, so nobody gets the peace without paying for it."""
        summary = deal_summary(p.terms, a.leader, b.leader)
        if self.stance(a.id, b.id) != WAR:
            return ActionResult(False, "they're not at war any more, so those peace terms are moot")
        peace = self._run_action("agreed_peace", a.id, b.id)
        if not peace.ok:
            return ActionResult(False, f"{a.leader} and {b.leader} agreed to peace, but {peace.message}.")
        res = self._deal(a.id, b.id, p.terms)
        if not res.ok:
            back = self._run_action("force_war", a.id, b.id)
            tail = "so the war goes on" if back.ok else f"and the war couldn't be resumed ({back.message})"
            self._log(f"{a.label} and {b.label} agreed to peace on terms ({summary}), but the terms couldn't be "
                      f"carried out ({res.message}), {tail}.", a.id, b.id)
            return ActionResult(False, f"{a.leader} and {b.leader} agreed to peace, but the terms couldn't be carried "
                                       f"out: {res.message}; {tail}.")
        self._denounced.discard((a.id, b.id))
        self._denounced.discard((b.id, a.id))
        for pid, q in list(self._proposals.items()):   # other peace offers between them are moot now
            if {q.sender, q.target} == {a.id, b.id} and (q.kind == "make_peace" or q.terms.get("peace")):
                del self._proposals[pid]
        return self._log(f"{a.label} and {b.label} made peace: {summary}.", a.id, b.id)

    def _fresh_peace(self, a: int, b: int) -> bool:
        try:
            f = _fields(self.link.run(f"local A, B = {int(a)}, {int(b)}\n" + FRESH_PEACE_LUA, ui=True), "FRESH")
        except (LuaFailed, GameNotReady):
            return False
        return bool(f) and f[0][0] == "true"

    def _run_action(self, kind: str, a: int, b: int) -> ActionResult:
        fresh = kind == "declare_war" and self._fresh_peace(a, b)
        try:
            lines = self.link.run(_lua_for(kind, a, b, fresh))
        except (LuaFailed, GameNotReady) as e:
            return ActionResult(False, f"the game refused ({e})")
        finally:
            self._read_at = 0.0   # next read is fresh
        res = _fields(lines, "RES")
        if not res:
            return ActionResult(False, "no answer from the game")
        ok = res[0][0] == "ok"
        if ok and kind in ORDERED_CHANGE:
            self._ordered.append((_pair(a, b), *ORDERED_CHANGE[kind]))
        return ActionResult(ok, res[0][1] if len(res[0]) > 1 else "")

    def _deal(self, a: int, b: int, terms: dict) -> ActionResult:
        lua = f"local A, B = {int(a)}, {int(b)}\nlocal ITEMS = {_deal_items(terms, self.civ(a).leader, self.civ(b).leader)}\n"
        res = self._steer(lua + DEAL_LUA)
        self._read_at = 0.0
        if res.ok:
            try:   # the bridge made these: don't report them as Civ's AI's deals
                self._known_deals = (self._known_deals or set()) | self._read_deals()
            except (LuaFailed, GameNotReady):
                pass
        return res

    def _read_deals(self) -> set[tuple]:
        out = set()
        for a, b, frm, kind, what, amount, end in _fields(self.link.run(DEALS_LUA), "DL"):
            out.add((int(a), int(b), int(frm), kind, what, amount, end))
        return out

    def trade_options(self, civ_id: int) -> dict:
        """What this civ could put in a deal with each civ it has met and isn't at war with."""
        out: dict = {}
        lines = self.link.run(f"local P = {int(civ_id)}\n" + TRADE_LUA)
        for b, direction, name, max_amount in _fields(lines, "TR"):
            try:
                other = self.civ(int(b))
            except (KeyError, ValueError):
                continue
            side = out.setdefault(other.leader, {"civ_id": other.id, "you_can_give": {}, "they_can_give": {}})[direction]
            n = int(max_amount) if max_amount.lstrip("-").isdigit() else 0
            if name == "open_borders":
                side["open_borders"] = True
            elif name.startswith("RESOURCE_"):
                if n > 0:
                    side.setdefault("resources", {})[name] = n
            elif n > 0:
                side[name] = n
        for b, direction, name in _fields(lines, "TRC"):
            try:
                other = self.civ(int(b))
            except (KeyError, ValueError):
                continue
            side = out.setdefault(other.leader, {"civ_id": other.id, "you_can_give": {}, "they_can_give": {}})[direction]
            side.setdefault("cities", []).append(name)
        for (b,) in _fields(lines, "TRW"):
            try:
                other = self.civ(int(b))
            except (KeyError, ValueError):
                continue
            entry = out.setdefault(other.leader, {"civ_id": other.id, "you_can_give": {}, "they_can_give": {}})
            entry["at_war"] = True
            entry["note"] = "you're at war: a deal with them is a peace offer, and its terms are carried out with the peace"
            for key, who in (("you_can_give", int(civ_id)), ("they_can_give", other.id)):
                if not entry[key]:
                    entry[key] = self._holdings(who)
        return out

    def _holdings(self, civ_id: int) -> dict:
        """What a civ has that could go into peace terms (when Civ itself lists nothing during a war)."""
        side: dict = {}
        c = self.civ(civ_id)
        if c.gold > 0:
            side["gold"] = c.gold
        a = self.awareness()
        if a is not None:
            res = {r: n for r, (_cls, n) in a.resources.get(civ_id, {}).items() if n > 0}
            if res:
                side["resources"] = res
            cities = sorted(x["name"] for (o, _), x in a.cities.items() if o == civ_id and not x.get("capital"))
            if cities:
                side["cities"] = cities
        return side

    def _notice_deals(self, now: World) -> None:
        """Ongoing deals nobody here ordered: Civ's built-in AI agreed to them (with you or another AI)."""
        try:
            current = self._read_deals()
        except (LuaFailed, GameNotReady):
            return
        if self._known_deals is None:
            self._known_deals = current
            return
        new = current - self._known_deals
        self._known_deals = current
        by_pair: dict = {}
        for a, b, frm, kind, what, amount, end in sorted(new):
            by_pair.setdefault((a, b), []).append((frm, _deal_item_text(kind, what, amount)))
        for (a, b), items in by_pair.items():
            for civ in (a, b):
                c = now.civs.get(civ)
                other = now.civs.get(b if civ == a else a)
                if c is None or other is None or c.is_human or not c.alive:
                    continue
                gives = [t for frm, t in items if frm == civ]
                gets = [t for frm, t in items if frm != civ]
                what = "; ".join(x for x in (f"you give {', '.join(gives)}" if gives else "",
                                             f"you get {', '.join(gets)}" if gets else "") if x)
                nid = next(self._nids)
                self.notices[nid] = {"id": nid, "civ": civ, "other": other.id, "change": "deal", "turn": now.turn,
                                     "what": f"Civ's built-in AI agreed to a deal with {other.leader} for you: {what}. "
                                             "Deals can't be cancelled, but you can tell them how you feel.",
                                     "undo": None}

    def _drop_mirrors(self, p: Proposal) -> None:
        """Both sides offered the same thing at once: once one is accepted, the other is moot."""
        for pid, q in list(self._proposals.items()):
            if q.kind == p.kind and {q.sender, q.target} == {p.sender, p.target}:
                del self._proposals[pid]

    def _expire(self) -> None:
        turn = self.turn
        for pid, p in list(self._proposals.items()):
            if turn - p.turn >= PROPOSAL_TURNS:
                self.expired.append(self._proposals.pop(pid))

    def take_expired(self) -> list[Proposal]:
        """Offers that ran out unanswered since the last call (the council tells their senders)."""
        self._expire()
        out, self.expired = self.expired, []
        return out

    def _log(self, msg: str, *who: int) -> ActionResult:
        self.events.append((f"T{self.turn}: {msg}", frozenset(who)))
        return ActionResult(True, msg)

    # ---- steering --------------------------------------------------------------------
    def steering_options(self, civ_id: int, tiles_per_district: int = 3) -> dict:
        """What this AI leader can steer right now, in a shape that fits in a prompt."""
        lines = self.link.run(f"local P = {int(civ_id)}\n" + OPTIONS_LUA)
        now = _fields(lines, "RESEARCHING")
        civic_now = _fields(lines, "CIVIC_NOW")
        settlers = _fields(lines, "SETTLERS")
        cities = []
        for cid, cname, free in _fields(lines, "CITY"):
            districts = {}
            for dtype in [d for d in free.split(",") if d][:6]:
                tiles = self._tiles(civ_id, int(cid), dtype)[:tiles_per_district]
                if tiles:
                    districts[dtype] = tiles
            if districts:
                cities.append({"city": int(cid), "name": cname, "district_sites": districts})
        return {
            "researching": now[0][0] if now and now[0][0] else None,
            "can_research": {t: n for t, n in _fields(lines, "TECH")},
            "studying": civic_now[0][0] if civic_now and civic_now[0][0] else None,
            "can_study": {c: n for c, n in _fields(lines, "CIVIC")},
            "building_settlers_allowed": bool(settlers) and settlers[0][0] == "true",
            "cities": cities,
            "standing_orders": self.orders.get(int(civ_id), {}),
            "government": self._government(int(civ_id), [g for g, _ in _fields(lines, "GOVOK")]),
            "banned_units": [u for (u,) in _fields(lines, "BANNED")],
            "envoys_to_send": int(_fields(lines, "ENVOYS")[0][0]) if _fields(lines, "ENVOYS") else 0,
            "city_states_you_know": sorted(cs.name for cs in self.world().city_states.values()
                                           if int(civ_id) in cs.met_by),
            "delegations_at": [int(b) for (b,) in _fields(lines, "DELEG") if b.lstrip("-").isdigit()],
            "trade_options": self._safe_trade_options(int(civ_id)),
            "embassies_at": [int(b) for (b,) in _fields(lines, "EMB") if b.lstrip("-").isdigit()],
            "settlers": int(_fields(lines, "SETTLER_COUNT")[0][0]) if _fields(lines, "SETTLER_COUNT") else 0,
            "city_sites": self._city_sites(int(civ_id)),
            **self._gift_option(int(civ_id)),
            "research_chosen_by": _chooser(now, self.orders.get(int(civ_id), {}).get("research")),
            "civic_chosen_by": _chooser(civic_now, self.orders.get(int(civ_id), {}).get("civic")),
            "decided_without_you": [
                {"id": n["id"], "turn": n["turn"], "what": n["what"],
                 "to_overrule": UNDO_LABEL[n["undo"]] if n["undo"] else "can't be undone"}
                for n in self.notices.values() if n["civ"] == int(civ_id)],
        }

    def _gift_option(self, civ_id: int) -> dict:
        a = self.awareness()
        if a is None or "gift_it_to_me" not in a.mods:
            return {}
        units = a.army.get(civ_id, {}).get("kinds", {})
        return {"gift_units": {"how": 'actions: {"type": "gift_units", "target": <civ id>, "unit": "<unit name>", "count": <n>}',
                               "you_can_gift": units, "max_per_gift": MAX_GIFT_UNITS}} if units else {}

    def _city_sites(self, civ_id: int) -> list[dict]:
        try:
            lines = self.link.run(f"local P = {int(civ_id)}\n" + SITES_LUA)
        except (LuaFailed, GameNotReady):
            return []
        out = []
        a = self.awareness()
        for f in _fields(lines, "SITE"):
            tile, x, y, _score, near, near_city, _foreign, lux, strat, _bonus, water, coast, hills = f[:13]
            good = [r.removeprefix("RESOURCE_").replace("_", " ").title() for r in (lux + "," + strat).split(",") if r]
            extras = [w for w, on in (("fresh water", water), ("coast", coast), ("hills", hills)) if on == "true"]
            site = {"tile": int(tile), "where": f"{near} tiles from {near_city}",
                    "resources_nearby": good, "features": extras}
            if len(f) >= 16 and f[13].lstrip("-").isdigit() and int(f[13]) >= 0:
                site["where"] = self._where(int(f[13]), int(f[14]), int(x), int(y), near_city)
            if len(f) >= 16 and f[15]:
                site["continent"] = f[15]
                if a is not None:
                    others = sorted({self.civ(o).leader for (o, _), c in a.cities.items()
                                     if c.get("continent") == f[15] and o != civ_id and o in self.world().civs})
                    if others:
                        site["other_civs_on_this_continent"] = others
            out.append(site)
        return out

    def _where(self, x0: int, y0: int, x1: int, y1: int, place: str) -> str:
        """'6 tiles southwest of Uruk': how a player would say where something is."""
        a = self.awareness()
        width = a.grid[0] if a is not None and a.grid[2] else 0
        d, way = hex_step(x0, y0, x1, y1, width)
        return place if d == 0 else f"{d} tile{'s' if d != 1 else ''} {way} of {place}"

    def _safe_trade_options(self, civ_id: int) -> dict:
        try:
            return self.trade_options(civ_id)
        except (LuaFailed, GameNotReady):
            return {}

    def _safe_congress(self) -> dict:
        try:
            return self.congress()
        except Exception:   # never let the Congress read cost a leader its turn
            return {}

    def congress(self) -> dict:
        """The World Congress as the leaders may know it (read once a turn; {} if there's nothing to say)."""
        turn = self.turn
        if self._congress and self._congress[0] == turn:
            return self._congress[1]
        try:
            lines = self.link.run(CONGRESS_LUA, ui=True)
        except (LuaFailed, GameNotReady):
            return self._congress[1] if self._congress else {}
        clean = lambda t: re.sub(r"\s*\[ICON_\w+\]\s*", " ", t).strip()  # noqa: E731
        out: dict = {}
        now = [{"resolution": n, "option_a": clean(a), "option_b": clean(b), "targets": [x for x in t.split(";") if x]}
               for n, a, b, _kind, t in (f[:5] for f in _fields(lines, "WCNOW") if len(f) >= 5)]
        if now:
            out["up_for_a_vote_now"] = now
        past: dict = {}
        for i, name, chosen, thing in (f[:4] for f in _fields(lines, "WCPAST") if len(f) >= 4):
            past[i] = {"resolution": name, "passed": clean(chosen) + (f" (target: {thing})" if thing else ""), "votes": []}
        names = {c.id: c.leader for c in self.civs()}
        for i, pid, votes, opt, target in (f[:5] for f in _fields(lines, "WCVOTE") if len(f) >= 5):
            if i in past and opt != "-" and votes.isdigit() and int(votes) > 0:
                who = names.get(int(pid), f"civ {pid}") if pid.lstrip("-").isdigit() else pid
                past[i]["votes"].append(f"{who}: {votes} vote{'s' if votes != '1' else ''} for {opt}"
                                        + (f" ({target})" if target else ""))
        if past:
            out["last_session"] = list(past.values())
        if out:
            out["note"] = ("Civ's built-in AI casts your votes; you can't change them, but you can lobby others "
                           "and react to how they voted.")
        self._congress = (turn, out)
        return out

    def _headlines(self, viewer: int, visible: set[int]) -> list[str]:
        """What needs this leader's attention now, in a few lines (details are further down the briefing)."""
        a, w, out = self.awareness(), self.world(), []
        out += [text for text, who in self.events if text.startswith(f"T{w.turn}:") and viewer in who][-6:]
        if a is None:
            return out
        for k, c in a.cities.items():
            if k[0] == viewer and _damaged(a.city_info.get(k, {})):
                out.append(f"Your city {c['name']} is under attack.")
        for k, n in a.barbs_near.items():
            if k[0] == viewer and k in a.cities:
                out.append(f"Barbarians next to {a.cities[k]['name']}.")
        for (o, _), v in a.loyalty.items():
            if o == viewer and v.get("turns_to_flip") and 0 < v["turns_to_flip"] <= 5 and v.get("flips_to"):
                out.append(f"{v['name']} will break away to {v['flips_to']} in {v['turns_to_flip']} turns unless its loyalty recovers.")
        for other in sorted(visible - {viewer}):
            if other not in w.civs or not w.civs[other].alive:
                continue
            t = a.treaties.get(frozenset({viewer, other}))
            if t:
                state, since, ally_left = t
                left = ally_left if state == "DIPLO_STATE_ALLIED" else (since + a.friend_turns - w.turn if since >= 0 else -1)
                if 0 <= left <= 3:
                    what = "alliance" if state == "DIPLO_STATE_ALLIED" else "friendship"
                    how = ("your renewal is queued and goes out the turn it lapses" if (viewer, other) in self.renewals
                           else "for a friendship: propose_friendship now and it's sent the turn it lapses")
                    out.append(f"Your {what} with {w.civs[other].leader} runs out in {left} turn{'s' if left != 1 else ''} "
                               f"({how}).")
            if a.wmds.get(other):
                out.append(f"{w.civs[other].leader} has nuclear weapons: "
                           + ", ".join(f"{n} {k}" for k, n in a.wmds[other].items()) + ".")
        for x, y, frm, kind, what, amount, end in sorted(self._known_deals or ()):
            if viewer in (x, y) and str(end).isdigit() and 0 <= int(end) - w.turn <= 3:
                other = y if x == viewer else x
                out.append(f"Your deal with {self._who(other)} ({_deal_item_text(kind, what, amount)}) ends in "
                           f"{int(end) - w.turn} turns.")
        return out[:14]

    def _cs_extra(self, cs: int, visible: set[int]) -> dict:
        a = self.awareness()
        if a is None:
            return {}
        out = {}
        if a.cs_types.get(cs):
            out["type"] = a.cs_types[cs]
        env = {self.civ(c).leader: n for c, n in a.envoys.get(cs, {}).items() if c in visible and c in self.world().civs}
        if env:
            out["envoys"] = env
        return out

    def _more_about_self(self, a: Awareness, me: int) -> dict:
        """Nukes, policy cards, governors, spies, great people, trade routes and ongoing deals: your own books."""
        out: dict = {}
        if a.wmds.get(me):
            out["your_nuclear_weapons"] = a.wmds[me]
        if a.policies.get(me):
            out["your_policy_cards"] = a.policies[me]
        if a.governors.get(me):
            out["your_governors"] = [f"{g} in {c}" if c else f"{g} (not assigned to a city)" for g, c in a.governors[me]]
        spies = []
        for city, owner, mission in a.spies_out.get(me, []):
            whose = "your" if owner == me else f"{self._who(owner)}'s" if owner >= 0 else ""
            spies.append(f"{'in ' + whose + ' city ' + city if whose else city}: {mission or 'no mission'}")
        if spies:
            out["your_spies"] = spies
        mine = a.gp_points.get(me, {})
        gp = [f"{a.gp_classes.get(cls, 'Great Person')} {person}: costs {cost}, you have {mine.get(cls, 0)}"
              for cls, person, cost in a.gp_now if mine.get(cls)]
        if gp:
            out["great_people_up_for_grabs"] = gp
        routes = {}
        for (src, dst), n in a.routes.items():
            if src == me:
                key = "within your empire" if dst == me else f"to {self._who(dst)}"
                routes[key] = routes.get(key, 0) + n
        if routes:
            out["your_trade_routes"] = routes
        incoming = {f"from {self._who(src)}": n for (src, dst), n in a.routes.items() if dst == me and src != me}
        if incoming:
            out["foreign_trade_routes_into_your_cities"] = incoming
        deals = []
        for x, y, frm, kind, what, amount, end in sorted(self._known_deals or ()):
            if me not in (x, y):
                continue
            other = y if x == me else x
            item = _deal_item_text(kind, what, amount)
            turns = int(end) - self.turn if str(end).lstrip("-").isdigit() and int(end) > 0 else None
            when = f" (ends in {turns} turns)" if turns is not None and turns >= 0 else ""
            deals.append(f"you give {self._who(other)} {item}{when}" if int(frm) == me
                         else f"{self._who(other)} gives you {item}{when}")
        if deals:
            out["your_ongoing_deals"] = deals
        return out

    def _economy_and_army(self, a: Awareness, civ_id: int, mine: bool) -> dict:
        """Per-turn economy, era, and what the army is made of (and, for your own, where it stands)."""
        out: dict = {}
        e = a.economy.get(civ_id, {})
        if e.get("era") is not None and a.era_names:
            out["era"] = a.era_names.get(e["era"], f"era {e['era']}")
        if mine and "gold" in e:
            out["economy_per_turn"] = {"gold": e["gold"] - e.get("upkeep", 0), "gold_before_upkeep": e["gold"],
                                       "upkeep": e.get("upkeep", 0), "science": e.get("science", 0),
                                       "culture": e.get("culture", 0), "faith": e.get("faith", 0),
                                       **({"diplomatic_favor": e["favor"]} if "favor" in e else {})}
        elif "science" in e:
            out["science_per_turn"], out["culture_per_turn"] = e["science"], e["culture"]
        army = a.army.get(civ_id)
        if army and army["kinds"]:
            text = ", ".join(f"{n} {k}" for k, n in sorted(army["kinds"].items(), key=lambda kv: -kv[1]))
            out["your_army" if mine else "their_army"] = text
            if mine:
                where = {}
                for owner, n in army["where"].items():
                    if owner == civ_id:
                        label = "in your own land"
                    elif owner < 0:
                        label = "outside anyone's borders"
                    elif owner in self.world().civs:
                        label = f"in {self.world().civs[owner].leader}'s land"
                    elif owner in self.world().city_states:
                        label = f"in the city-state {self.world().city_states[owner].name}"
                    else:
                        label = "in other land"
                    where[label] = where.get(label, 0) + n
                out["where_your_army_is"] = where
        elif army is not None:
            out["your_army" if mine else "their_army"] = "none"
        return out

    def _city_detail(self, a: Awareness, k: tuple[int, int], c: dict, mine: bool) -> str:
        info = a.city_info.get(k)
        if not info:
            return ""
        bits = [f"size {info['size']}" if info.get("size") else "", f"defense {info['defense']}" if info.get("defense") else ""]
        if _damaged(info):
            (g, gm), (w, wm) = info.get("garrison", (0, 0)), info.get("walls", (0, 0))
            bits.append(f"UNDER ATTACK: garrison damaged {g}/{gm}" + (f", walls damaged {w}/{wm}" if w else ""))
        if info.get("amenities") is not None and info.get("amenities_needed") is not None:
            spare = info["amenities"] - info["amenities_needed"]
            bits.append(f"{spare:+d} amenities" + (" (unhappy)" if spare < 0 else ""))
        if info.get("war_weariness"):
            bits.append(f"war weariness costs {info['war_weariness']} amenities")
        if mine and info.get("building"):
            bits.append(f"building {info['building']}")
        bits = [b for b in bits if b]
        return f"{c['name']}: {', '.join(bits)}" if bits else ""

    def _who(self, cid: int) -> str:
        w = self.world()
        if cid in w.civs:
            return w.civs[cid].leader
        if cid in w.city_states:
            return w.city_states[cid].name
        return f"civ {cid}"

    def _fights_for(self, civ_id: int) -> list[str]:
        """This civ's fights from the last few turns, from its side, grouped ("x3")."""
        def name(side: dict) -> str:
            if side["player"] == civ_id:
                return "your"
            if side["kind"]:
                return side["kind"] + "'"
            return self._who(side["player"]) + "'s"
        def what(side: dict) -> str:
            return f"city {side['city']}" if side["city"] and not side["unit"] else (side["unit"] or side["city"] or "unit")
        counts: dict[str, int] = {}
        for f in self.fights:
            att, dfn = f["attacker"], f["defender"]
            if civ_id not in (att["player"], dfn["player"]):
                continue
            end = " and was destroyed" if att["killed"] else ""
            end = " and destroyed it" if dfn["killed"] else end
            line = f"T{f['turn']}: {name(att)} {what(att)} attacked {name(dfn)} {what(dfn)}{end}"
            counts[line] = counts.get(line, 0) + 1
        return [f"{k} (x{n})" if n > 1 else k for k, n in list(counts.items())[-12:]]

    def _victory(self, viewer: int) -> dict:
        """How the game is won and who's still in it (from the game setup; {} until the read works)."""
        a = self.awareness()
        if a is None or not a.victories:
            return {}
        w = self.world()
        known = w.met.get(viewer, set())
        alive = [c for c in w.civs.values() if c.alive]
        return {"how_to_win": victory_text(a.victories),
                "civs_still_in_the_game": len(alive),
                "civs_you_havent_met": sum(1 for c in alive if c.id != viewer and c.id not in known),
                **({"eliminated": sorted(c.label for c in w.civs.values() if not c.alive and c.id in known)}
                   if any(not c.alive and c.id in known for c in w.civs.values()) else {})}

    def _aware_self(self, civ_id: int) -> dict:
        a = self.awareness()
        if a is None:
            return {}
        out = {"techs_known": a.progress.get(civ_id, (0, 0))[0], "civics_known": a.progress.get(civ_id, (0, 0))[1],
               "districts": a.districts.get(civ_id, 0),
               "your_cities": sorted(c["name"] for (o, _), c in a.cities.items() if o == civ_id),
               **({"your_cities_by_continent": by_continent} if (by_continent := _by_continent(a, civ_id)) else {}),
               "your_resources": _resource_summary(a.resources.get(civ_id, {}))}
        if a.wonders.get(civ_id):
            out["wonders"] = [f"{w} ({city})" for _, w, city in a.wonders[civ_id]]
        if civ_id in a.religion:
            out["religion"] = a.religion[civ_id]
        out["age"] = a.ages.get(civ_id, "normal")
        if civ_id in a.era_score:
            score, dark, golden = a.era_score[civ_id]
            nxt = "a dark age" if score < dark else "a golden age" if score >= golden else "a normal age"
            out["era_score"] = (f"{score}: below {dark} means a dark age next era, {golden}+ a golden age. "
                                f"Right now you're heading for {nxt}.")
        if a.era_countdown:
            out["era_ends_in_turns"] = a.era_countdown
        name = lambda i: self.civ(i).leader if i in self.world().civs else f"civ {i}"  # noqa: E731
        took = [f"{n} (from {name(f)}{', their original capital' if cap else ''})"
                for (o, _), (n, f, cap) in a.taken.items() if o == civ_id]
        if took:
            out["cities_you_conquered"] = took
        lost = [f"{n} (held by {name(o)}{', your original capital' if cap else ''})"
                for (o, _), (n, f, cap) in a.taken.items() if f == civ_id]
        if lost:
            out["your_cities_others_hold"] = lost
        fights = self._fights_for(civ_id)
        if fights:
            out["fighting_recently"] = fights
        out.update(self._economy_and_army(a, civ_id, mine=True))
        out.update(self._more_about_self(a, civ_id))
        details = [self._city_detail(a, k, c, mine=True) for k, c in sorted(a.cities.items(), key=lambda kv: kv[1]["name"])
                   if k[0] == civ_id]
        if any(details):
            out["your_city_details"] = [d for d in details if d]
        barbs = [f"a barbarian camp {d} tiles from {a.cities[k]['name']}" for k, d in sorted(a.camps.items(), key=lambda kv: kv[1])
                 if k[0] == civ_id and k in a.cities] + \
                [f"{n} barbarian unit{'s' if n > 1 else ''} next to {a.cities[k]['name']}" for k, n in a.barbs_near.items()
                 if k[0] == civ_id and k in a.cities]
        if barbs:
            out["barbarians_near_you"] = barbs[:8]
        caught = [f"{name or 'a spy'} (rank {lvl}), sent by {self._who(owner)}" for c, owner, _, lvl, name in sorted(a.spies)
                  if c == civ_id]
        if caught:
            out["spies_you_caught"] = caught
        held = [f"{self._who(c)} holds your spy {name or ''}".rstrip() for c, owner, _, _, name in sorted(a.spies)
                if owner == civ_id]
        if held:
            out["your_spies_caught_by_others"] = held
        ems = []
        for (name, target), e in a.emergencies.items():
            if civ_id not in e["seen_by"] and civ_id != target:
                continue
            if emergency_helps(name) and target >= 0:   # a race to help the target, not a move against it
                role = ("others race to send YOU aid (a scored contest in your favor; nothing hostile)"
                        if target == civ_id else "you're a member: you race the others to help them"
                        if civ_id in e["members"] else "you can join the race to help them")
                bits = [f"{name} for {'you' if target == civ_id else self._who(target)}", role]
            else:
                role = ("you are its target" if target == civ_id else "you're a member" if civ_id in e["members"]
                        else "you can join")
                bits = [name + (f" against {self._who(target)}" if target >= 0 else ""), role]
            if e["turns_left"] is not None and e["turns_left"] >= 0:
                bits.append(f"{e['turns_left']} turns left")
            members = [self._who(m) for m in sorted(e["members"]) if m != civ_id]
            if members:
                bits.append("members: " + ", ".join(members))
            if e["goal"]:
                bits.append("goal: " + e["goal"])
            ems.append("; ".join(bits))
        if ems:
            out["emergencies"] = ems
        shaky = [v for (o, _), v in a.loyalty.items() if o == civ_id and v["loyalty"] is not None
                 and ((v["per_turn"] or 0) < 0 or v["loyalty"] < 50)]
        if shaky:
            out["cities_losing_loyalty"] = [
                f"{v['name']}: loyalty {v['loyalty']}, {v['per_turn']:+d} a turn" if v["per_turn"] is not None
                else f"{v['name']}: loyalty {v['loyalty']}" for v in sorted(shaky, key=lambda v: v["loyalty"])]
            flips = [v for v in shaky if v["turns_to_flip"] and v["turns_to_flip"] > 0 and v["flips_to"]]
            if flips:
                out["cities_about_to_flip"] = [f"{v['name']} joins {v['flips_to']} in {v['turns_to_flip']} turns"
                                               for v in flips]
        return out

    def _aware_about(self, viewer: int, other: int) -> dict:
        """What the viewer can reasonably know about another civ it has met."""
        a = self.awareness()
        if a is None:
            return {}
        out = {"techs_known": a.progress.get(other, (0, 0))[0], "civics_known": a.progress.get(other, (0, 0))[1],
               "districts": a.districts.get(other, 0)}
        close = []
        for oa, ida, ob, idb, d in a.near:
            if {oa, ob} == {viewer, other} and (oa, ida) in a.cities and (ob, idb) in a.cities:
                mine, theirs = ((oa, ida), (ob, idb)) if oa == viewer else ((ob, idb), (oa, ida))
                close.append((d, a.cities[mine]["name"], a.cities[theirs]["name"]))
        if close:
            out["cities_close_to_yours"] = [f"their {t} is {d} tiles from your {m}" for d, m, t in sorted(close)[:4]]
        else:
            out["neighbors"] = f"no cities within {NEAR_TILES} tiles of each other"
        n, _ = a.troops.get((viewer, other), (0, 0))
        if n:
            out["their_troops_near_your_cities"] = n
            kinds = a.troop_kinds.get((viewer, other))
            if kinds:
                out["which_of_their_units"] = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items(), key=lambda x: -x[1]))
        n, _ = a.troops.get((other, viewer), (0, 0))
        if n:
            out["your_troops_near_their_cities"] = n
        theirs = sorted((a.reach.get((o, cid, viewer), 99), c["name"], c.get("continent", ""), (o, cid))
                        for (o, cid), c in a.cities.items() if o == other)
        if theirs:
            def city_text(d, name, cont, k):
                info = a.city_info.get(k, {})
                bits = [cont, f"{d} tiles from your nearest city" if d < 99 else "",
                        f"size {info['size']}" if info.get("size") else "",
                        f"defense {info['defense']}" if info.get("defense") else "",
                        "damaged" if _damaged(info) else ""]
                bits = [b for b in bits if b]
                return f"{name} ({', '.join(bits)})" if bits else name
            out["their_cities"] = [city_text(*t) for t in theirs[:10]]
        conts = _by_continent(a, other)
        if conts:
            out["their_continents"] = {k: len(v) for k, v in conts.items()}
        if a.wonders.get(other):
            out["wonders"] = [w for _, w, _ in a.wonders[other]]
        if other in a.religion:
            out["religion"] = a.religion[other]
        if other in a.ages:
            out["age"] = a.ages[other]
        out.update(self._economy_and_army(a, other, mine=False))
        if a.wmds.get(other):
            out["nuclear_weapons"] = a.wmds[other]
        if a.governments.get(other):
            out["government"] = a.governments[other]
        between = a.routes.get((viewer, other), 0) + a.routes.get((other, viewer), 0)
        if between:
            out["trade_routes_between_you"] = between
        # Civ keeps one signed tally per pair (A's +N against B is B's -N against A), probe 9
        net = a.grievances.get((viewer, other))
        if net is None and (other, viewer) in a.grievances:
            net = -a.grievances[(other, viewer)]
        if net:
            out["grievances"] = (f"you hold {net} grievances against them" if net > 0
                                 else f"they hold {-net} grievances against you")
        treaty = a.treaties.get(frozenset({viewer, other}))
        if treaty:
            state, since, ally_left = treaty
            if state == "DIPLO_STATE_ALLIED" and ally_left >= 0:
                out["alliance_ends_in_turns"] = ally_left
            elif state == "DIPLO_STATE_DECLARED_FRIEND" and since >= 0:
                out["friendship_ends_in_turns"] = max(0, since + a.friend_turns - self.turn)
        if (viewer, other) in self.renewals:
            out["your_friendship_renewal"] = "queued: sent the turn the friendship lapses"
        took = [f"{n} (from {self.civ(f).leader if f in self.world().civs else f'civ {f}'}"
                f"{', an original capital' if cap else ''})" for (o, _), (n, f, cap) in a.taken.items() if o == other]
        if took:
            out["cities_they_conquered"] = took
        if not self.civ(viewer).is_human:
            reasons = a.reasons.get((viewer, other), [])
            known = [f"{'+' if s > 0 else ''}{s} {t}" for s, t in reasons if t != "Unknown Reason"]
            hidden = sum(s for s, t in reasons if t == "Unknown Reason")
            if known or hidden:
                out["your_peoples_feelings"] = known + ([f"{'+' if hidden > 0 else ''}{hidden} other reasons"]
                                                        if hidden else [])
        else:   # what Civ's leader screen shows the player: why that civ's people like or dislike you
            reasons = a.reasons.get((other, viewer), [])
            if reasons:
                out["their_feelings_about_you"] = [{"score": sc, "reason": t if t != "Unknown Reason" else "Other reasons"}
                                                   for sc, t in sorted(reasons, key=lambda r: -abs(r[0]))]
                out["feelings_total"] = sum(sc for sc, _ in reasons)
        return out

    def awareness(self, fresh: bool = False) -> Awareness | None:
        """This turn's read of cities, troops, wonders and Civ's reasons (once a turn; None if the game won't say)."""
        turn = self.turn
        if fresh or self._aware is None or self._aware.turn != turn:
            try:
                self._aware = parse_awareness(self.link.run(AWARE_LUA), turn)
            except (LuaFailed, GameNotReady):
                return self._aware
            try:
                add_ages(self._aware, self.link.run(AGES_LUA, ui=True))
            except (LuaFailed, GameNotReady):
                pass   # ages and loyalty are extras; the rest of the read still counts
            try:
                self._aware.combat = parse_combat(self.link.run(COMBAT_LUA))
            except (LuaFailed, GameNotReady):
                pass
            self._quiet_hook()
            try:
                for f in _fields(self.link.run(DISASTER_LUA, ui=True), "DIS"):
                    if len(f) >= 4 and f[0].isdigit() and f[2].lstrip("-").isdigit() and f[3].lstrip("-").isdigit():
                        self._aware.disasters.append((int(f[0]), f[1], int(f[2]), int(f[3])))
            except (LuaFailed, GameNotReady):
                pass
            self.fights = [x for x in self.fights + self._aware.combat if turn - x["turn"] <= FIGHT_TURNS][-300:]
        return self._aware

    def _government_now(self, civ_id: int) -> tuple[str | None, bool]:
        try:
            f = _fields(self.link.run(f"local P = {int(civ_id)}\n" + GOV_NOW_LUA, ui=True), "GOVNOW")
        except (LuaFailed, GameNotReady):
            return None, False
        return ((f[0][0] or None), f[0][1] == "true") if f else (None, False)

    def _government(self, civ_id: int, unlocked: list[str]) -> dict:
        current, anarchy = self._government_now(civ_id)
        standing = self.orders.get(civ_id, {})
        wait = standing.get("government_turn", -GOVERNMENT_COOLDOWN) + GOVERNMENT_COOLDOWN - self.turn
        return {"current": current, "in_anarchy": anarchy,
                "chosen_by": None if not current else "you" if standing.get("government") == current
                else "Civ's built-in AI",
                "can_switch_to": [g for g in unlocked if g != current] if wait <= 0 and not anarchy else [],
                "note": (f"you can switch again in {wait} turns" if wait > 0 else
                         "switching can cost a few turns of anarchy (no government, no policies); "
                         f"at most once every {GOVERNMENT_COOLDOWN} turns")}

    def _tiles(self, civ_id: int, city_id: int, dtype: str) -> list[dict]:
        if not NAME_RE.match(dtype):
            return []
        lua = f'local P, CITY, DTYPE = {int(civ_id)}, {int(city_id)}, "{dtype}"\n' + TILES_LUA
        tiles = []
        a = self.awareness()
        center = a.cities.get((int(civ_id), int(city_id))) if a is not None else None
        for idx, x, y, terrain, feature, river, mountains, districts, water in _fields(self.link.run(lua, ui=True), "TILE"):
            where = (self._where(center["x"], center["y"], int(x), int(y), "the city center") if center
                     else "near the city")
            tiles.append({"tile": int(idx), "where": where, "terrain": terrain + (f", {feature}" if feature else ""),
                          "river": river == "true", "next_to": {"mountains": int(mountains), "districts": int(districts),
                                                                "water": int(water)}})
        tiles.sort(key=lambda t: (t["next_to"]["mountains"] + t["next_to"]["districts"] + t["river"]), reverse=True)
        return tiles

    def set_orders(self, civ_id: int, orders: dict) -> list[str]:
        """Apply a leader's orders. Returns one line per order saying what happened."""
        civ_id = int(civ_id)
        if self.civ(civ_id).is_human or not isinstance(orders, dict):
            return []
        done, standing = [], self.orders.setdefault(civ_id, {})
        for key, template, label in (("research", SET_RESEARCH_LUA, "research"), ("civic", SET_CIVIC_LUA, "civic")):
            name = str(orders.get(key) or "").strip().upper()
            if not name:
                continue
            if not NAME_RE.match(name):
                done.append(f"{label}: unknown {name!r}")
                continue
            res = self._steer(f'local P, NAME = {civ_id}, "{name}"\n' + STEER_COMMON + template)
            done.append(f"{label}: {res.message}")
            if res.ok:
                standing[key] = name
        if isinstance(orders.get("expand"), bool):
            res = self._steer(f"local P, EXPAND = {civ_id}, {'true' if orders['expand'] else 'false'}\n" + SET_EXPAND_LUA)
            done.append(f"expansion: {res.message}")
            if res.ok:
                standing["expand"] = orders["expand"]
        district = orders.get("district")
        if isinstance(district, dict) and district.get("type"):
            done.append("district: " + self._place_district(civ_id, district).message)
        if orders.get("government"):
            done.append("government: " + self._set_government(civ_id, str(orders["government"]).strip().upper()).message)
        for key, ban in (("ban_units", True), ("allow_units", False)):
            names = orders.get(key)
            for name in (names if isinstance(names, list) else [names] if names else [])[:5]:
                name = str(name).strip().upper()
                if not NAME_RE.match(name):
                    done.append(f"{key}: unknown {name!r}")
                    continue
                res = self._steer(f'local P, NAME, BAN = {civ_id}, "{name}", {"true" if ban else "false"}\n' + BAN_UNIT_LUA)
                done.append(f"{key}: {res.message}")
        if orders.get("envoy") not in (None, ""):
            done.append("envoy: " + self._envoy(civ_id, orders["envoy"]).message)
        focus = orders.get("city_focus")
        if isinstance(focus, dict) and focus.get("city") is not None:
            res = self._city_focus(civ_id, focus.get("city"), str(focus.get("focus") or ""))
            if res.ok:
                standing.setdefault("focus", {})[str(self._city_id(civ_id, focus.get("city")))] = str(focus.get("focus") or "").upper()
            done.append("city focus: " + res.message)
        if orders.get("recruit") not in (None, ""):
            name = str(orders["recruit"]).strip()
            res = self._recruit(civ_id, name)
            if res.message.startswith("not enough"):
                standing["recruit"] = name          # keep trying each turn
            else:
                standing.pop("recruit", None)
            done.append("recruit: " + res.message)
        if orders.get("settle") not in (None, ""):
            done.append("settle: " + self._order_settle(civ_id, orders["settle"]).message)
        if orders.get("embassy") not in (None, ""):
            try:
                to = self.civ(int(orders["embassy"])).id
                res = self._steer(f"local P, B = {civ_id}, {to}\n" + EMBASSY_LUA)
                if res.ok:
                    self._log(f"{self.civ(civ_id).label} opened an embassy with {self.civ(to).label}.", civ_id, to)
            except (TypeError, ValueError, KeyError):
                res = ActionResult(False, f"bad civ id {orders['embassy']!r}")
            done.append("embassy: " + res.message)
        if orders.get("delegation") not in (None, ""):
            try:
                to = self.civ(int(orders["delegation"])).id
                res = self._steer(f"local P, B = {civ_id}, {to}\n" + DELEGATION_LUA)
                if res.ok:
                    self._log(f"{self.civ(civ_id).label} sent a delegation to {self.civ(to).label}.", civ_id, to)
            except (TypeError, ValueError, KeyError):
                res = ActionResult(False, f"bad civ id {orders['delegation']!r}")
            done.append("delegation: " + res.message)
        attack = orders.get("attack")
        if isinstance(attack, dict) and attack.get("city"):
            try:
                enemy = int(attack.get("enemy"))
                done.append("attack: " + self._attack(civ_id, enemy, str(attack["city"])).message)
            except (TypeError, ValueError, KeyError):
                done.append("attack: needs the enemy's civ id and a city name")
        overrule = orders.get("overrule")
        for nid in (overrule if isinstance(overrule, list) else [overrule] if overrule is not None else [])[:5]:
            done.append("overrule: " + self._overrule(civ_id, nid).message)
        return done

    def _attack(self, civ_id: int, enemy: int, city_name: str) -> ActionResult:
        """Point civ_id's army at one of the enemy's cities (by name)."""
        a = self.awareness()
        want = city_name.strip().lower()
        hits = [cid for (o, cid), c in (a.cities.items() if a else []) if o == enemy and c["name"].lower() == want]
        if not hits:
            return ActionResult(False, f"{self.civ(enemy).leader} has no city called {city_name!r}")
        res = self._steer(f"local A, B, CITY = {int(civ_id)}, {int(enemy)}, {hits[0]}\n" + ATTACK_LUA)
        if res.ok:
            self.orders.setdefault(civ_id, {})["attack"] = {"enemy": enemy, "city": city_name, "turn": self.turn}
        return res

    def _set_government(self, civ_id: int, name: str) -> ActionResult:
        if not NAME_RE.match(name):
            return ActionResult(False, f"unknown government {name!r}")
        standing = self.orders.setdefault(civ_id, {})
        wait = standing.get("government_turn", -GOVERNMENT_COOLDOWN) + GOVERNMENT_COOLDOWN - self.turn
        if wait > 0:
            return ActionResult(False, f"you changed government recently; wait {wait} more turns")
        current, anarchy = self._government_now(civ_id)
        if anarchy:
            return ActionResult(False, "you're in anarchy; wait until it ends")
        if current == name:
            standing["government"] = name
            return ActionResult(True, f"already {name}")
        res = self._steer(f'local P, NAME = {civ_id}, "{name}"\n' + SET_GOVERNMENT_LUA)
        if res.ok:
            standing["government"], standing["government_turn"] = name, self.turn
            self._log(f"{self.civ(civ_id).label} changed government to {_title(name, 'GOVERNMENT_')}.", civ_id)
        return res

    def _order_settle(self, civ_id: int, tile) -> ActionResult:
        """Remember where the leader wants its next city; found it now if a settler is close enough."""
        try:
            tile = int(tile)
        except (TypeError, ValueError):
            return ActionResult(False, f"bad tile {tile!r}")
        if tile not in {s["tile"] for s in self._city_sites(civ_id)}:
            return ActionResult(False, f"tile {tile} isn't one of your city_sites")
        self.orders.setdefault(civ_id, {})["settle"] = tile
        return self._try_settle(civ_id, tile)

    def _try_settle(self, civ_id: int, tile: int) -> ActionResult:
        lua = (f"local P, X, Y, RANGE = {civ_id}, Map.GetPlotByIndex({int(tile)}):GetX(), "
               f"Map.GetPlotByIndex({int(tile)}):GetY(), {SETTLE_RANGE}\n" + SETTLE_LUA)
        try:
            res = _fields(self.link.run(lua), "RES")
        except (LuaFailed, GameNotReady) as e:
            return ActionResult(False, f"the game refused ({e})")
        status, msg = (res[0][0], res[0][1] if len(res[0]) > 1 else "") if res else ("no", "no answer from the game")
        if status == "ok":
            self.orders.get(civ_id, {}).pop("settle", None)
            self.settled_by_order.add(msg.removeprefix("founded ").strip())
            self._read_at = 0.0
            self._log(f"{self.civ(civ_id).label} {msg}.", civ_id)
            return ActionResult(True, msg)
        if status == "no":
            self.orders.get(civ_id, {}).pop("settle", None)
            return ActionResult(False, msg)
        return ActionResult(True, f"site saved; {msg}")   # waiting for a settler

    def _envoy(self, civ_id: int, which) -> ActionResult:
        known = [cs for cs in self.world().city_states.values() if civ_id in cs.met_by]
        pick = [cs for cs in known if str(which).strip().lower() in (cs.name.lower(), str(cs.id))]
        if not pick:
            return ActionResult(False, f"you don't know a city-state called {which!r}")
        return self._steer(f"local P, M = {civ_id}, {pick[0].id}\n" + ENVOY_LUA)

    def _place_district(self, civ_id: int, order: dict) -> ActionResult:
        dtype = str(order.get("type", "")).upper()
        try:
            city_id, tile = int(order.get("city")), int(order.get("tile"))
        except (TypeError, ValueError):
            return ActionResult(False, "needs a city id and a tile")
        if not NAME_RE.match(dtype):
            return ActionResult(False, f"unknown district {dtype!r}")
        valid = {t["tile"] for t in self._tiles(civ_id, city_id, dtype)}
        if tile not in valid:
            return ActionResult(False, f"tile {tile} isn't a valid {dtype} site for that city right now")
        return self._steer(f'local P, CITY, DTYPE, PLOT = {civ_id}, {city_id}, "{dtype}", {tile}\n' + PLACE_DISTRICT_LUA)

    def _city_id(self, civ_id: int, city) -> int | None:
        a = self.awareness()
        if isinstance(city, int) or str(city).strip().lstrip("-").isdigit():
            return int(city)
        name = str(city).strip().lower()
        for (o, cid), c in (a.cities.items() if a else []):
            if o == civ_id and c["name"].lower() == name:
                return cid
        return None

    def _city_focus(self, civ_id: int, city, focus: str) -> ActionResult:
        cid = self._city_id(civ_id, city)
        if cid is None:
            return ActionResult(False, f"you have no city {city!r}")
        f = focus.strip().upper().removeprefix("YIELD_")
        if f in ("", "NONE", "NO"):
            f = ""
        elif f not in FOCUS_YIELDS:
            return ActionResult(False, f"focus must be one of {', '.join(y.lower() for y in FOCUS_YIELDS)} or none")
        return self._steer(f'local P, CITY, FAVOR = {int(civ_id)}, {int(cid)}, "{"YIELD_" + f if f else ""}"\n' + FOCUS_LUA)

    def _recruit(self, civ_id: int, name: str) -> ActionResult:
        if not name or not CITY_NAME_RE.match(name):
            return ActionResult(False, "say which great person to recruit, by name")
        quoted = name.replace("\\", "").replace('"', "")
        res = self._steer(f'local P, NAME = {int(civ_id)}, "{quoted}"\n' + RECRUIT_LUA)
        if res.ok:
            self._log(f"{self.civ(civ_id).label} recruited a great person: {res.message.removeprefix('recruited ')}.", civ_id)
        return res

    def _steer(self, lua: str) -> ActionResult:
        try:
            res = _fields(self.link.run(lua), "RES")
        except (LuaFailed, GameNotReady) as e:
            return ActionResult(False, f"the game refused ({e})")
        if not res:
            return ActionResult(False, "no answer from the game")
        return ActionResult(res[0][0] == "ok", res[0][1] if len(res[0]) > 1 else "")

    def reapply_orders(self) -> list[str]:
        """The AI picks its own research and civic when one finishes: put the leader's goal back.

        In-game turn hooks don't run when added through the tuner (probe 2), so the bridge does
        this at the start of each of your turns. Finished or impossible goals are dropped.
        """
        notes = []
        for civ_id, standing in list(self.orders.items()):
            if not self.civ(civ_id).alive:
                continue
            if standing.get("settle") is not None:
                res = self._try_settle(civ_id, int(standing["settle"]))
                notes.append(f"{self.civ(civ_id).leader} settle: {res.message}")
            for cid, focus in list((standing.get("focus") or {}).items()):   # Civ's AI may have changed it
                res = self._city_focus(civ_id, int(cid), focus)
                if not res.ok:
                    standing["focus"].pop(cid, None)
            if standing.get("recruit"):
                res = self._recruit(civ_id, standing["recruit"])
                if not res.message.startswith("not enough"):
                    standing.pop("recruit", None)
                    notes.append(f"{self.civ(civ_id).leader} recruit: {res.message}")
            for key, template in (("research", SET_RESEARCH_LUA), ("civic", SET_CIVIC_LUA)):
                name = standing.get(key)
                if name:
                    res = self._steer(f'local P, NAME = {civ_id}, "{name}"\n' + STEER_COMMON + template)
                    if not res.ok:
                        standing.pop(key, None)   # learned it, or no longer possible: the leader picks again
                    notes.append(f"{self.civ(civ_id).leader} {key}: {res.message}")
        return notes

    # ---- turns ---------------------------------------------------------------------
    def end_turn(self) -> list[str]:
        """Civ moves the turn. This reports what changed in the game since the last call."""
        with self._lock:
            before = self._last_reported
            now = self.world(fresh=True)
            self._last_reported = now
        if before is None:
            self.awareness(fresh=True)   # baseline for next turn's comparison
            return []
        start = len(self.events)
        for text, who in describe_changes(before, now):
            self.events.append((f"T{now.turn}: {text}", who))
        self._notice_unordered(before, now)
        self._notice_deals(now)
        self._send_renewals()
        old_aware = self._aware
        aware = self.awareness(fresh=True)
        if old_aware is not None and aware is not None:
            for text, who in describe_awareness(old_aware, aware, now.civs):
                self.events.append((f"T{now.turn}: {text}", who))
            self._notice_settled(old_aware, aware, now)
        self._expire()
        return [text for text, who in self.events[start:]]

    def _notice_settled(self, before: Awareness, now: Awareness, world: World) -> None:
        """A new city an AI leader didn't choose the site for was Civ's own AI walking a settler: tell them,
        with where it is and who lives close, so they can own it, explain it or apologize."""
        old_names = {c["name"] for c in before.cities.values()} | set(before.other_cities)
        for (o, cid), c in now.cities.items():
            civ = world.civs.get(o)
            if civ is None or civ.is_human or c["name"] in old_names or c["name"] in self.settled_by_order:
                continue
            bits = [f"on {c['continent']}"] if c.get("continent") else []
            close = sorted((d, ob, idb) for oa, ida, ob, idb, d in now.near if (oa, ida) == (o, cid) and ob != o) + \
                sorted((d, oa, ida) for oa, ida, ob, idb, d in now.near if (ob, idb) == (o, cid) and oa != o)
            if close:
                d, other, ocid = min(close)
                if (other, ocid) in now.cities and other in world.civs:
                    bits.append(f"{d} tiles from {world.civs[other].leader}'s {now.cities[(other, ocid)]['name']}")
            others_there = sorted({world.civs[x].leader for (x, _), oc in now.cities.items()
                                   if x != o and x in world.civs and c.get("continent")
                                   and oc.get("continent") == c["continent"]})
            if others_there:
                bits.append("a continent you share with " + ", ".join(others_there))
            nid = next(self._nids)
            self.notices[nid] = {"id": nid, "civ": o, "other": o, "change": "settled", "turn": now.turn,
                                 "what": (f"Civ's built-in AI founded your new city {c['name']}"
                                          + (f" ({'; '.join(bits)})" if bits else "")
                                          + ": you didn't choose that site. If you promised anyone to stay out of "
                                            "there, they'll see it as your doing unless you explain."),
                                 "undo": None}

    def _quiet_hook(self) -> None:
        """(Re)install the hook that closes Civ's own AI diplomacy pop-ups, and collect what it closed."""
        try:
            lines = self.link.run(f"local QUIET, BLOCK = {'true' if self.quiet_ai else 'false'}, "
                                  f"{'true' if self.block_screen else 'false'}\n" + QUIET_LUA, ui=True)
        except (LuaFailed, GameNotReady):
            return
        for f in _fields(lines, "QD"):
            if len(f) >= 3 and f[0].isdigit() and f[1].lstrip("-").isdigit():
                kind = next((v for k, v in QUIET_LABELS.items() if f[2].startswith(k)), "a message")
                self.quieted.append((int(f[0]), int(f[1]), kind))

    def set_quiet_ai(self, on: bool) -> None:
        self.quiet_ai = bool(on)
        self._quiet_hook()

    def set_block_screen(self, on: bool) -> None:
        self.block_screen = bool(on)
        self._quiet_hook()

    def take_quieted(self) -> list[tuple[int, int, str]]:
        out, self.quieted = self.quieted, []
        return out

    def _send_renewals(self) -> None:
        """Friendships that lapsed: send the renewals that were queued while they still ran."""
        for (a, b), asked in list(self.renewals.items()):
            stance = self.stance(a, b)
            if stance == PEACE:
                del self.renewals[(a, b)]
                if any(q.sender == a and q.target == b and q.kind == "propose_friendship" for q in self._proposals.values()):
                    continue
                p = Proposal(next(self._ids), "propose_friendship", a, b, self.turn, "renewing our friendship")
                self._proposals[p.id] = p
                self.renewed.append(p)
                self._log(f"{self.civ(a).label} offered to renew their friendship with {self.civ(b).label}.", a, b)
            elif stance != FRIENDS or self.turn - asked > 10:
                del self.renewals[(a, b)]   # it turned into war or an alliance, or never lapsed: drop it

    def historic_moments(self) -> list[tuple[int, int, int, str]]:
        """Civ's own timeline for every civ: (turn, civ, era score, what happened). Empty if Civ can't say."""
        try:
            lines = self.link.run(MOMENTS_LUA, ui=True)
        except (LuaFailed, GameNotReady):
            return []
        out = []
        for f in _fields(lines, "MOM"):
            if len(f) < 5 or not f[1].lstrip("-").isdigit() or not f[0].lstrip("-").isdigit():
                continue
            if f[2].lstrip("-").isdigit() and int(f[2]) == GOODY_HUT_MOMENT:
                continue
            score = int(f[3]) if f[3].lstrip("-").isdigit() else 0
            if f[4].strip():
                out.append((int(f[1]), int(f[0]), score, f[4].strip()))
        return out

    def era_of(self, civ_id: int) -> int | None:
        """The civ's era as an index (0 = Ancient), from this turn's read; None if the game didn't say."""
        a = self.awareness()
        era = a.economy.get(int(civ_id), {}).get("era") if a else None
        return None if era is None else int(era)

    def event_civs(self, text: str) -> frozenset:
        """The civs an event involves (empty if it isn't one of ours)."""
        return next((who for t, who in reversed(self.events) if t == text), frozenset())

    def news_for(self, viewer: int, turn: int) -> list[str]:
        """This turn's events that the viewer would know about (it has met everyone involved)."""
        visible = self.world().met.get(viewer, set()) | {viewer}
        return [text for text, who in self.events if text.startswith(f"T{turn}:") and who <= visible]

    def take_renewed(self) -> list[Proposal]:
        out, self.renewed = self.renewed, []
        return out

    def _notice_unordered(self, before: World, now: World) -> None:
        """Diplomacy that changed without a Claude leader's order was Civ's own AI (or the other side).

        Each AI civ involved gets a notice it can keep or overrule on its turn. Friendships and alliances
        that simply ran out aren't decisions, so they're only reported as events.
        """
        with self._lock:
            ordered, self._ordered = list(self._ordered), []
        for pair in sorted(set(before.relations) | set(now.relations)):
            old = before.relations.get(pair, (False, False, False))
            new = now.relations.get(pair, (False, False, False))
            for fld in range(3):
                if old[fld] == new[fld]:
                    continue
                if (pair, fld, new[fld]) in ordered:
                    ordered.remove((pair, fld, new[fld]))
                    continue
                change = {(0, True): "war", (0, False): "peace", (1, True): "friendship",
                          (2, True): "alliance"}.get((fld, new[fld]))
                if change is None or (change == "friendship" and new[2]):
                    continue
                for civ in pair:
                    c = now.civs.get(civ)
                    if c is None or c.is_human or not c.alive:
                        continue
                    other = now.civs[pair[1] if civ == pair[0] else pair[0]]
                    undo = UNDO[change]
                    if change == "war" and other.is_human:
                        undo = None          # they may have declared it: offer peace like anyone else
                    nid = next(self._nids)
                    self.notices[nid] = {"id": nid, "civ": civ, "other": other.id, "change": change,
                                         "turn": now.turn, "what": _notice_text(change, other), "undo": undo}
        for nid, n in list(self.notices.items()):
            if now.turn - n["turn"] > 3:
                del self.notices[nid]

    def _overrule(self, civ_id: int, nid) -> ActionResult:
        try:
            n = self.notices.get(int(nid))
        except (TypeError, ValueError):
            n = None
        if n is None or n["civ"] != civ_id:
            return ActionResult(False, f"no decision #{nid} to overrule")
        if not n["undo"]:
            return ActionResult(False, "that one can't be undone")
        res = self._run_action(n["undo"], civ_id, n["other"])
        if not res.ok:
            return res
        for k, m in list(self.notices.items()):
            if {m["civ"], m["other"]} == {civ_id, n["other"]} and m["change"] == n["change"]:
                del self.notices[k]
        me, them = self.civ(civ_id), self.civ(n["other"])
        return self._log(f"{me.label} overruled its advisors and chose to {UNDO_LABEL[n['undo']]} with {them.label}.",
                         civ_id, them.id)

    def events_for(self, viewer: int, texts: list[str]) -> list[str]:
        """Only the events about civs this viewer has met (plus their own)."""
        visible = self.world().met.get(viewer, set()) | {viewer}
        return [t for t, who in self.events if t in texts and who <= visible]

    def wait_for_turns(self, on_new_turn, stop: threading.Event | None = None, poll: float = 3.0) -> None:
        """Blocking loop: calls on_new_turn(turn) each time a new turn starts and it's your move again."""
        stop = stop or threading.Event()
        seen = None
        while not stop.is_set():
            try:
                now = _fields(self.link.run(TURN_LUA), "NOW")
                turn, active = int(now[0][0]), now[0][1] == "true"
                if seen is None:
                    seen = turn
                elif turn > seen and active:
                    seen = turn
                    on_new_turn(turn)
            except (GameNotReady, LuaFailed, IndexError, ValueError):
                pass   # game loading, at the menu, or between turns: try again shortly
            stop.wait(poll)

    # ---- small persistence (denouncements live only here) ----------------------------
    def notify(self, sender: int, headline: str, body: str) -> bool:
        """Show something in your own Civ notification stack, pointing at the sender's capital if we know it."""
        x = y = -1
        a = self._aware
        if a is not None:
            cap = next((c for (o, _), c in a.cities.items() if o == sender and c.get("capital")), None)
            if cap:
                x, y = cap["x"], cap["y"]
        lua = (f"local ME, X, Y = {self.local_player}, {x}, {y}\nlocal HEAD = {_lua_quote(headline[:90])}\n"
               f"local BODY = {_lua_quote(body[:280])}\n" + NOTIFY_LUA)
        try:
            f = _fields(self.link.run(lua), "RES")
        except (LuaFailed, GameNotReady):
            return False
        return bool(f) and f[0][0] == "ok"

    def pin(self, leader: int, where, label: str) -> ActionResult:
        """Mark a place on the human player's map with a labelled pin: a tile id from the leader's briefing,
        or a city's name. Each leader keeps at most MAX_PINS."""
        tile, x, y = -1, -1, -1
        if isinstance(where, int) or (isinstance(where, str) and where.strip().isdigit()):
            tile = int(where)
        else:
            a = self.awareness()
            name = str(where or "").strip().lower()
            city = next((c for c in (a.cities.values() if a else []) if c["name"].lower() == name), None)
            if city is None:
                return ActionResult(False, f"no city called {where!r} to pin")
            x, y = city["x"], city["y"]
        clean = " ".join(re.sub(r"[\x00-\x1f\"\\\[\]]", " ", str(label or "")).split())[:40] or "this spot"
        name = f"{self.civ(leader).leader}: {clean}"
        mine = self.pins.setdefault(int(leader), [])
        drop = mine[:-(MAX_PINS - 1)] if len(mine) >= MAX_PINS else []
        ours = {p["name"] for pins in self.pins.values() for p in pins} | {name}
        lua_str = lambda t: '"' + t.replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
        lua = (f"local TILE, X, Y = {tile}, {x}, {y}\nlocal NAME = {lua_str(name)}\n"
               "local DROP = {" + ", ".join(f"{{{int(p['id'])}, {lua_str(p['name'])}}}" for p in drop) + "}\n"
               "local OURS = {" + ", ".join(f"[{lua_str(n)}] = true" for n in sorted(ours)) + "}\n" + PIN_LUA)
        try:
            f = _fields(self.link.run(lua, ui=True), "RES")
        except (LuaFailed, GameNotReady) as e:
            return ActionResult(False, f"couldn't place the pin ({e})")
        if not f or f[0][0] != "ok":
            return ActionResult(False, f[0][1] if f and len(f[0]) > 1 else "couldn't place the pin")
        pid, at = int(f[0][1]), int(f[0][2])
        kept = [p for p in mine if p not in drop and p["tile"] != at and p["id"] != pid]
        self.pins[int(leader)] = kept + [{"id": pid, "tile": at, "name": name}]
        return ActionResult(True, f"📍 {self.civ(leader).leader} marked a spot on your map: {clean}")

    def save_state(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"denounced": sorted(self._denounced),
                                    "orders": {str(k): v for k, v in self.orders.items()},
                                    "proposals": [asdict(p) for p in self._proposals.values()],
                                    "pins": {str(k): v for k, v in self.pins.items()},
                                    "renewals": [[a, b, t] for (a, b), t in self.renewals.items()]}))

    def load_state(self, path: Path) -> None:
        if path.exists():
            data = json.loads(path.read_text())
            self._denounced = {tuple(x) for x in data.get("denounced", [])}
            self.orders = {int(k): v for k, v in data.get("orders", {}).items()}
            self.pins = {int(k): [p for p in v if isinstance(p, dict) and {"id", "tile", "name"} <= p.keys()]
                         for k, v in data.get("pins", {}).items()}
            self.renewals = {(int(a), int(b)): int(t) for a, b, t in data.get("renewals", []) if str(a).lstrip("-").isdigit()}
            for raw in data.get("proposals", []):   # offers still waiting survive a restart
                try:
                    p = Proposal(**raw)
                except TypeError:
                    continue
                self._proposals[p.id] = p
            if self._proposals:
                self._ids = itertools.count(max(self._proposals) + 1)


VICTORY_TEXT = {
    "VICTORY_CONQUEST": "Domination: hold every other civ's original capital at once.",
    "VICTORY_TECHNOLOGY": "Science: be first to finish the space-race projects.",
    "VICTORY_CULTURE": "Culture: draw more visiting tourists than any other civ has domestic tourists.",
    "VICTORY_RELIGIOUS": "Religion: your religion is the main one in every civ.",
    "VICTORY_DIPLOMATIC": "Diplomacy: reach the diplomatic victory points through the World Congress.",
    "VICTORY_SCORE": "Score: have the highest score when the turn limit runs out.",
}


def victory_text(victories: list[str]) -> str:
    """How this game can be won, said plainly. VICTORY_DEFAULT (last civ standing) is always there."""
    ways = [VICTORY_TEXT[v] for v in victories if v in VICTORY_TEXT]
    last = ("Last civ standing: you win when every other civ, the human player included, has been "
            "eliminated (lost every city).")
    if not ways:
        return (f"{last} That is the ONLY way to win this game: no science, culture, religion, diplomacy or "
                "score win, and no turn limit. Peace with everyone forever means nobody wins; sooner or later "
                "every rival, friends included, has to fall, and the civ that wins is the one that picks its wars "
                "well, not the one that starts the most. There's no end date: the game goes on until one civ is left, "
                "however many hundreds of turns that takes, so a plan can take dozens of turns or more.")
    return "Ways to win this game: " + " ".join(ways + [last])


def _damaged(info: dict) -> bool:
    return bool(info.get("garrison", (0, 0))[0] or info.get("walls", (0, 0))[0])


def _by_continent(a: Awareness, civ_id: int) -> dict[str, list[str]]:
    """A civ's cities grouped by the continent they stand on (Civ's own continent names)."""
    out: dict[str, list[str]] = {}
    for (o, _), c in sorted(a.cities.items(), key=lambda kv: kv[1]["name"]):
        if o == civ_id and c.get("continent"):
            out.setdefault(c["continent"], []).append(c["name"])
    return out


def _resource_summary(have: dict[str, tuple[str, int]]) -> dict:
    """{"luxuries": {"RESOURCE_MARBLE": 1}, "strategic": {"RESOURCE_IRON": 5}}: keys usable in deals."""
    out: dict = {"luxuries": {}, "strategic": {}}
    for rtype, (rclass, n) in sorted(have.items()):
        out["luxuries" if rclass == "luxury" else "strategic"][rtype] = n
    return out


def _deal_items(terms: dict, a_name: str, b_name: str) -> str:
    """Lua table of deal items for DEAL_LUA. Names were checked by deal_terms (RESOURCE_[A-Z0-9_]+)."""
    items = []
    for side, frm, who in (("give", "A", a_name), ("ask", "B", b_name)):
        t = terms.get(side, {})
        if t.get("gold"):
            items.append(f'{{from={frm}, kind="GOLD", amount={int(t["gold"])}, label="{t["gold"]} gold from {who}"}}')
        if t.get("gold_per_turn"):
            items.append(f'{{from={frm}, kind="GOLD", per_turn=true, amount={int(t["gold_per_turn"])}, '
                         f'label="{t["gold_per_turn"]} gold per turn from {who}"}}')
        if t.get("favor"):
            items.append(f'{{from={frm}, kind="FAVOR", amount={int(t["favor"])}, label="{t["favor"]} favor from {who}"}}')
        for name in t.get("cities", []):
            if CITY_NAME_RE.match(name):
                items.append(f'{{from={frm}, kind="CITIES", city="{name}", label="the city of {name} from {who}"}}')
        for name, n in t.get("resources", {}).items():
            if re.fullmatch(r"RESOURCE_[A-Z0-9_]+", name):
                items.append(f'{{from={frm}, kind="RESOURCES", res="{name}", amount={int(n)}, '
                             f'label="{name.removeprefix("RESOURCE_").title()} from {who}"}}')
        if t.get("open_borders"):
            items.append(f'{{from={frm}, kind="AGREEMENTS", sub="OPEN_BORDERS", label="open borders from {who}"}}')
    return "{" + ", ".join(items) + "}"


def _deal_item_text(kind: str, what: str, amount: str) -> str:
    if kind == "GOLD":
        return f"{amount} gold per turn"
    if kind == "RESOURCES":
        return what.removeprefix("RESOURCE_").replace("_", " ").title() or "a resource"
    if kind == "AGREEMENTS":
        return "open borders" if what == "OPEN_BORDERS" else "an agreement"
    return kind.lower()


def _chooser(current: list[list[str]], ordered: str | None) -> str | None:
    if not current or not current[0][0]:
        return None
    return "you" if ordered and current[0][0] == ordered else "Civ's built-in AI"


def _notice_text(change: str, other: Civ) -> str:
    who = other.leader
    if change == "war":
        return (f"You're at war with {who}, and you didn't order it: {who} declared it, or Civ's built-in AI did."
                if other.is_human else f"War with {who} started without your order (Civ's built-in AI).")
    if change == "peace":
        return f"Civ's built-in AI made peace with {who} for you."
    if change == "friendship":
        return f"Civ's built-in AI declared friendship with {who} for you."
    return f"Civ's built-in AI formed an alliance with {who} for you (alliances can't be broken early)."


def describe_changes(before: World, now: World) -> list[tuple[str, frozenset]]:
    """Human-readable events from two reads of the game."""
    out: list[tuple[str, frozenset]] = []
    name = lambda i: now.civs[i].label  # noqa: E731
    for pair in sorted(set(before.relations) | set(now.relations)):
        old = before.relations.get(pair, (False, False, False))
        new = now.relations.get(pair, (False, False, False))
        a, b = pair
        who = frozenset(pair)
        if new[0] and not old[0]:
            out.append((f"{name(a)} and {name(b)} are at war!", who))
        if old[0] and not new[0]:
            out.append((f"{name(a)} and {name(b)} made peace.", who))
        if new[2] and not old[2]:
            out.append((f"{name(a)} and {name(b)} formed an alliance.", who))
        elif new[1] and not old[1] and not new[2]:
            out.append((f"{name(a)} and {name(b)} declared friendship.", who))
        if old[2] and not new[2]:
            out.append((f"The alliance between {name(a)} and {name(b)} ended.", who))
        elif old[1] and not new[1] and not new[2]:
            out.append((f"{name(a)} and {name(b)} are no longer friends.", who))
    for cid, c in now.civs.items():
        old = before.civs.get(cid)
        if old is None:
            continue
        if old.alive and not c.alive:
            out.append((f"{c.label} has been eliminated!", frozenset({cid})))
        elif c.cities > old.cities:
            n = c.cities - old.cities
            out.append((f"{c.label} now has {c.cities} cities (+{n}).", frozenset({cid})))
        elif c.cities < old.cities:
            out.append((f"{c.label} lost {old.cities - c.cities} city{'ies' if old.cities - c.cities > 1 else ''}.",
                        frozenset({cid})))
    for a, known in now.met.items():
        for b in sorted(known - before.met.get(a, set())):
            if a < b or b not in now.met or a not in now.met.get(b, set()):
                out.append((f"{name(a)} met {name(b)}.", frozenset({a, b})))
    return out
