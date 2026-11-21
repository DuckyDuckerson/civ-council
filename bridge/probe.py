"""Civ Council feasibility probe for Mac (or any OS with the tuner enabled).

Answers one question: what can a script control for an AI civ, and does it stick?

    1. python3 bridge/probe.py               # reads only, safe on any save
    2. python3 bridge/probe.py --writes      # THROWAWAY SAVE: gives AI civs orders
    3. end one turn in the game
    4. python3 bridge/probe.py --after       # checks which orders the AI kept

--after can also run right after --writes (before ending the turn) to see which orders took
effect at once. Each step prints a report and saves it to bridge/probe_report.txt.
Setup: in AppOptions.txt set EnableTuner 1 (see README), start or load a game, then run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tuner import Tuner  # noqa: E402

HERE = Path(__file__).parent
STATE_FILE = HERE / "probe_state.json"
REPORT = HERE / "probe_report.txt"

HELPERS = r"""
local function chk(name, fn)
  local ok, r = pcall(fn)
  print("CHK|" .. name .. "|" .. (ok and "OK" or "FAIL") .. "|" .. tostring(r))
end
local function methods(o)
  local t, seen = {}, {}
  local mt = getmetatable(o)
  local function add(tbl) if type(tbl) == "table" then for k in pairs(tbl) do if not seen[k] then seen[k] = true; t[#t+1] = tostring(k) end end end end
  if mt then add(mt.__index); add(mt) end
  if type(o) == "table" then add(o) end
  table.sort(t)
  return table.concat(t, ",")
end
local function civic_ready(cul, row)   -- Culture has no CanProgress: check prerequisites by hand
  if cul:HasCivic(row.Index) then return false end
  for p in GameInfo.CivicPrereqs() do
    if p.Civic == row.CivicType and not cul:HasCivic(GameInfo.Civics[p.PrereqCivic].Index) then return false end
  end
  return true
end
local function tech_name(i) return i >= 0 and GameInfo.Technologies[i].TechnologyType or "nothing" end
local function civic_name(i) return i >= 0 and GameInfo.Civics[i].CivicType or "nothing" end
"""

PICK_PLAYERS = r"""
local ais, human = {}, nil
for _, id in ipairs(PlayerManager.GetAliveMajorIDs()) do
  local cfg = PlayerConfigurations[id]
  local isHuman = Players[id]:IsHuman()
  print("PLAYER|" .. id .. "|" .. tostring(cfg:GetLeaderTypeName()) .. "|" .. tostring(cfg:GetCivilizationTypeName()) .. "|" .. (isHuman and "HUMAN" or "AI"))
  if isHuman then human = id else ais[#ais+1] = id end
end
local A, B = ais[1], ais[2]
for i = 1, #ais do for j = i + 1, #ais do
  local ok, met = pcall(function() return Players[ais[i]]:GetDiplomacy():HasMet(ais[j]) end)
  if ok and met then A, B = ais[i], ais[j]; break end
end end
local C = nil
for _, id in ipairs(ais) do
  if id ~= A and id ~= B then
    local ok, met = pcall(function() return Players[A]:GetDiplomacy():HasMet(id) end)
    if C == nil or (ok and met) then C = id end
  end
end
print("PAIR|" .. tostring(A) .. "|" .. tostring(B) .. "|" .. tostring(C))
print("TURN|" .. tostring(Game.GetCurrentGameTurn()))
"""

READS_GAMECORE = r"""
local A, B = __A__, __B__
chk("gc.at_war", function() return Players[A]:GetDiplomacy():IsAtWarWith(B) end)
chk("gc.has_met", function() return Players[A]:GetDiplomacy():HasMet(B) end)
chk("gc.diplo_state", function() local i = Players[A]:GetDiplomaticAI():GetDiplomaticStateIndex(B); return GameInfo.DiplomaticStates[i].StateType end)
chk("gc.researching", function() local i = Players[A]:GetTechs():GetResearchingTech(); return i >= 0 and GameInfo.Technologies[i].TechnologyType or "nothing" end)
chk("gc.civic", function() local i = Players[A]:GetCulture():GetProgressingCivic(); return i >= 0 and GameInfo.Civics[i].CivicType or "nothing" end)
chk("gc.capital_builds", function() local c = Players[A]:GetCities():GetCapitalCity(); return c:GetName() .. " -> " .. tostring(c:GetBuildQueue():CurrentlyBuilding()) end)
chk("gc.methods.player", function() return methods(Players[A]) end)
chk("gc.methods.diplomacy", function() return methods(Players[A]:GetDiplomacy()) end)
chk("gc.methods.techs", function() return methods(Players[A]:GetTechs()) end)
chk("gc.methods.culture", function() return methods(Players[A]:GetCulture()) end)
chk("gc.methods.cities", function() return methods(Players[A]:GetCities()) end)
chk("gc.methods.units", function() return methods(Players[A]:GetUnits()) end)
"""

READS_INGAME = r"""
local A, B = __A__, __B__
chk("ui.diplo_state", function() local i = Players[A]:GetDiplomaticAI():GetDiplomaticStateIndex(B); return GameInfo.DiplomaticStates[i].StateType end)
chk("ui.PlayerOperations", function() return methods(PlayerOperations) end)
chk("ui.CityOperationTypes", function() return methods(CityOperationTypes) end)
chk("ui.methods.UI", function() return methods(UI) end)
chk("ui.methods.DiplomacyManager", function() return methods(DiplomacyManager) end)
"""

# Two ways to give orders: GameCore (direct state changes) and InGame (the same requests the UI makes).
WRITES_GAMECORE = r"""
local A, B, C = __A__, __B__, __C__
chk("gc.set_research(A)", function()
  local techs = Players[A]:GetTechs()
  for row in GameInfo.Technologies() do
    if not techs:HasTech(row.Index) and techs:CanResearch(row.Index) and row.Index ~= techs:GetResearchingTech() then
      techs:SetResearchingTech(row.Index)
      print("EXPECT|research_A|" .. row.TechnologyType)
      return "asked for " .. row.TechnologyType .. ", now " .. tech_name(techs:GetResearchingTech())
    end
  end
  return "no other tech available"
end)
chk("gc.set_civic(A)", function()
  local cul = Players[A]:GetCulture()
  for row in GameInfo.Civics() do
    if row.Index ~= cul:GetProgressingCivic() and civic_ready(cul, row) then
      cul:SetProgressingCivic(row.Index)
      print("EXPECT|civic_A|" .. row.CivicType)
      return "asked for " .. row.CivicType .. ", now " .. civic_name(cul:GetProgressingCivic())
    end
  end
  return "no other civic available"
end)
chk("gc.methods.city", function() return methods(Players[A]:GetCities():GetCapitalCity()) end)
chk("gc.methods.build_queue", function() return methods(Players[A]:GetCities():GetCapitalCity():GetBuildQueue()) end)
chk("gc.declare_war(A->B)", function()
  local d = Players[A]:GetDiplomacy()
  local can = tostring(d:CanDeclareWarOn(B))
  local tries = {
    function() d:DeclareWarOn(B, WarTypes.FORMAL_WAR, true) end,
    function() d:DeclareWarOn(B, WarTypes.FORMAL_WAR) end,
    function() d:DeclareWarOn(B) end,
  }
  local errs = {}
  for i, f in ipairs(tries) do
    local ok, e = pcall(f)
    if ok then return "CanDeclareWarOn " .. can .. "; variant " .. i .. " ran; at war now: " .. tostring(d:IsAtWarWith(B)) end
    errs[#errs+1] = tostring(e)
  end
  error(table.concat(errs, " / "))
end)
chk("gc.make_peace(A,B)", function()
  local d = Players[A]:GetDiplomacy()
  local can = tostring(d:CanMakePeaceWith(B))
  local ok = pcall(function() d:MakePeaceWith(B, true) end) or pcall(function() d:MakePeaceWith(B) end)
  return "CanMakePeaceWith " .. can .. "; ran: " .. tostring(ok) .. "; still at war: " .. tostring(d:IsAtWarWith(B))
end)
chk("gc.friendship(A,C)", function()
  if C == nil then return "no third AI" end
  local d = Players[A]:GetDiplomacy()
  d:SetHasDeclaredFriendship(C, true)
  print("EXPECT|friends_AC|true")
  return "A->C " .. tostring(d:HasDeclaredFriendship(C)) .. ", C->A " .. tostring(Players[C]:GetDiplomacy():HasDeclaredFriendship(A))
end)
chk("gc.alliance(A,C)", function()
  if C == nil then return "no third AI" end
  local d = Players[A]:GetDiplomacy()
  local errs = {}
  for i, f in ipairs({ function() d:SetHasAllied(C, true) end, function() d:SetHasAllied(C) end }) do
    local ok, e = pcall(f)
    if ok then
      print("EXPECT|allied_AC|true")
      return "variant " .. i .. " ran; A->C " .. tostring(d:HasAllied(C)) .. ", C->A " .. tostring(Players[C]:GetDiplomacy():HasAllied(A))
    end
    errs[#errs+1] = tostring(e)
  end
  error(table.concat(errs, " / "))
end)
chk("gc.found_city_at_settler", function()
  for _, pid in ipairs({A, B, C}) do
    for _, u in Players[pid]:GetUnits():Members() do
      if GameInfo.Units[u:GetType()].UnitType == "UNIT_SETTLER" then
        local x, y = u:GetX(), u:GetY()
        local cities = Players[pid]:GetCities()
        local valid = tostring(cities:IsValidFoundLocation(x, y))
        local before = cities:GetCount()
        local city = cities:Create(x, y)
        local after = cities:GetCount()
        if after > before then UnitManager.Kill(u, false) end
        print("EXPECT|cities_" .. pid .. "|" .. after)
        return "player " .. pid .. " settler at " .. x .. "," .. y .. " (valid site: " .. valid .. "): cities " .. before .. " -> " .. after
      end
    end
  end
  return "none of these AIs has a settler right now; try again later in the game"
end)
"""

WRITES_INGAME = r"""
local A, B = __A__, __B__
print("LOCAL|" .. tostring(Game.GetLocalPlayer()))
chk("ui.city_production(A)", function()
  local city = Players[A]:GetCities():GetCapitalCity()
  local p = {}
  p[CityOperationTypes.PARAM_UNIT_TYPE] = GameInfo.Units["UNIT_WARRIOR"].Hash
  p[CityOperationTypes.PARAM_INSERT_MODE] = CityOperationTypes.VALUE_EXCLUSIVE
  local can = tostring(CityManager.CanStartOperation(city, CityOperationTypes.BUILD, p, true))
  CityManager.RequestOperation(city, CityOperationTypes.BUILD, p)
  print("EXPECT|builds_A|UNIT_WARRIOR")
  return "CanStartOperation " .. can .. "; requested Warrior in " .. city:GetName()
end)
chk("ui.district_placement(B)", function()
  local city = Players[B]:GetCities():GetCapitalCity()
  for _, dt in ipairs({"DISTRICT_CAMPUS", "DISTRICT_HOLY_SITE", "DISTRICT_ENCAMPMENT", "DISTRICT_COMMERCIAL_HUB", "DISTRICT_THEATER", "DISTRICT_INDUSTRIAL_ZONE"}) do
    local p = {}
    p[CityOperationTypes.PARAM_DISTRICT_TYPE] = GameInfo.Districts[dt].Hash
    local res = CityManager.GetOperationTargets(city, CityOperationTypes.BUILD, p)
    local plots = res and res[CityOperationResults.PLOTS]
    if plots and #plots > 0 then
      local plot = Map.GetPlotByIndex(plots[1])
      p[CityOperationTypes.PARAM_X] = plot:GetX()
      p[CityOperationTypes.PARAM_Y] = plot:GetY()
      p[CityOperationTypes.PARAM_INSERT_MODE] = CityOperationTypes.VALUE_EXCLUSIVE
      CityManager.RequestOperation(city, CityOperationTypes.BUILD, p)
      print("EXPECT|builds_B|" .. dt)
      return "requested " .. dt .. " at " .. plot:GetX() .. "," .. plot:GetY() .. " in " .. city:GetName() .. " (" .. #plots .. " valid tiles)"
    end
  end
  return "no district available to that AI yet; try later in the game"
end)
chk("ui.research(B)", function()
  local techs = Players[B]:GetTechs()
  for row in GameInfo.Technologies() do
    if not techs:HasTech(row.Index) and techs:CanResearch(row.Index) and row.Index ~= techs:GetResearchingTech() then
      local p = {}
      p[PlayerOperations.PARAM_TECH_TYPE] = row.Hash
      p[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE
      UI.RequestPlayerOperation(B, PlayerOperations.RESEARCH, p)
      print("EXPECT|research_B|" .. row.TechnologyType)
      return "requested " .. row.TechnologyType
    end
  end
  return "no other tech available"
end)
chk("ui.civic(B)", function()
  local cul = Players[B]:GetCulture()
  for row in GameInfo.Civics() do
    if row.Index ~= cul:GetProgressingCivic() and civic_ready(cul, row) then
      local p = {}
      p[PlayerOperations.PARAM_CIVIC_TYPE] = row.Hash
      p[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE
      UI.RequestPlayerOperation(B, PlayerOperations.PROGRESS_CIVIC, p)
      print("EXPECT|civic_B|" .. row.CivicType)
      return "requested " .. row.CivicType
    end
  end
  return "no other civic available"
end)
chk("ui.declare_war_session(A->B)", function()
  DiplomacyManager.RequestSession(A, B, "DECLARE_SURPRISE_WAR")
  print("EXPECT|war_AB|true")
  return "requested"
end)
"""

AFTER = r"""
local A, B, C = __A__, __B__, __C__
local function builds(pid) return tostring(Players[pid]:GetCities():GetCapitalCity():GetBuildQueue():CurrentlyBuilding()) end
local function now(key, fn) local ok, v = pcall(fn); print("NOW|" .. key .. "|" .. (ok and tostring(v) or ("error: " .. tostring(v)))) end
now("turn", function() return Game.GetCurrentGameTurn() end)
now("research_A", function() return tech_name(Players[A]:GetTechs():GetResearchingTech()) end)
now("civic_A", function() return civic_name(Players[A]:GetCulture():GetProgressingCivic()) end)
now("builds_A", function() return builds(A) end)
now("builds_B", function() return builds(B) end)
now("research_B", function() return tech_name(Players[B]:GetTechs():GetResearchingTech()) end)
now("civic_B", function() return civic_name(Players[B]:GetCulture():GetProgressingCivic()) end)
now("war_AB", function() return Players[A]:GetDiplomacy():IsAtWarWith(B) end)
if C ~= nil then
  now("friends_AC", function() return Players[A]:GetDiplomacy():HasDeclaredFriendship(C) end)
  now("allied_AC", function() return Players[A]:GetDiplomacy():HasAllied(C) end)
end
for _, pid in ipairs({A, B, C}) do now("cities_" .. pid, function() return Players[pid]:GetCities():GetCount() end) end
"""


def run(t: Tuner, idx: int, body: str, a="nil", b="nil", c="nil") -> str:
    body = body.replace("__A__", str(a)).replace("__B__", str(b)).replace("__C__", str(c))
    return t.run(idx, HELPERS + body)


def lines(out: str, prefix: str) -> list[list[str]]:
    return [l.split("|") for l in out.splitlines() if l.startswith(prefix + "|")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--writes", action="store_true", help="give AI civs orders (use a throwaway save)")
    ap.add_argument("--after", action="store_true", help="after ending a turn: check which orders stuck")
    ap.add_argument("--gamecore", type=int, help="Lua state index for GameCore, if auto-detect fails")
    ap.add_argument("--ingame", type=int, help="Lua state index for InGame, if auto-detect fails")
    ap.add_argument("--port", type=int, default=None, help="tuner port (default: try 4318-4321)")
    args = ap.parse_args()

    report: list[str] = []
    say = lambda s="": (print(s), report.append(s))  # noqa: E731

    try:
        t = Tuner(port=args.port)
    except OSError as e:
        print(f"Couldn't connect to the game's tuner port: {e}")
        print("Is Civ 6 running with EnableTuner 1 in AppOptions.txt, and a game loaded?")
        sys.exit(1)
    states = t.handshake()
    say(f"Connected on port {t.port} to {t.app or '?'}")
    say(f"Lua states: {dict((i, s) for i, s in enumerate(states) if s)}")
    say(f"Raw handshake: {[r[:300] for r in t.raw_handshake]}")
    gc = args.gamecore if args.gamecore is not None else t.index_of("GameCore_Tuner")
    if gc is None:
        gc = t.index_of("GameCore")
    ui = args.ingame if args.ingame is not None else t.index_of("InGame")
    say(f"Using GameCore index {gc}, InGame index {ui}")
    if gc is None or ui is None:
        say("Couldn't find the Lua states. Is a game loaded (not the main menu)? "
            "Paste this report to Claude; we can pass --gamecore/--ingame by hand.")
        REPORT.write_text("\n".join(report))
        return

    out = run(t, gc, PICK_PLAYERS)
    for p in lines(out, "PLAYER"):
        say(f"  player {p[1]}: {p[2]} / {p[3]} ({p[4]})")
    pair = lines(out, "PAIR")
    if not pair or pair[0][1] == "nil":
        say("Need at least 2 AI civs in the game. Raw output:\n" + out)
        REPORT.write_text("\n".join(report))
        return
    a, b, c = pair[0][1], pair[0][2], pair[0][3] if len(pair[0]) > 3 else "nil"
    turn = lines(out, "TURN")
    say(f"Turn {turn[0][1] if turn else '?'}. Testing with AI players A={a}, B={b}, C={c}\n")

    def show(out: str) -> None:
        for c in lines(out, "CHK"):
            name, status, value = c[1], c[2], "|".join(c[3:])
            say(f"[{status:4}] {name}: {value[:1500]}")
        if not lines(out, "CHK"):
            say("  (no results; raw output follows)\n" + out[:2000])

    if args.after:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        a, b, c = state.get("A", a), state.get("B", b), state.get("C", c)   # same players as --writes
        expected = state.get("expected", {})
        out = run(t, gc, AFTER, a, b, c)
        current = {n[1]: "|".join(n[2:]) for n in lines(out, "NOW")}
        say(f"Orders were given on turn {state.get('turn', '?')}; it is now turn {current.get('turn', '?')}.")
        for key, want in expected.items():
            got = current.get(key, "(not checked)")
            verdict = "KEPT" if got.lower() == want.lower() else "CHANGED"
            say(f"[{verdict:7}] {key}: ordered {want}, now {got}")
        others = {k: v for k, v in current.items() if k not in expected and k != "turn"}
        say(f"Other values: {others}")
        if not current:
            say("  (no results; raw output follows)\n" + out[:2000])
    elif args.writes:
        expected = {}
        for label, idx, body in (("GameCore", gc, WRITES_GAMECORE), ("InGame", ui, WRITES_INGAME)):
            say(f"--- orders via {label} ---")
            out = run(t, idx, body, a, b, c)
            for loc in lines(out, "LOCAL"):
                say(f"(local player is {loc[1]})")
            show(out)
            expected.update({e[1]: e[2] for e in lines(out, "EXPECT")})
        STATE_FILE.write_text(json.dumps({"A": a, "B": b, "C": c, "turn": turn[0][1] if turn else "?",
                                          "expected": expected}, indent=1))
        say("\nNow end ONE turn in the game, then run:  python3 bridge/probe.py --after")
    else:
        say("--- reads via GameCore ---")
        show(run(t, gc, READS_GAMECORE, a, b, c))
        say("--- reads via InGame ---")
        show(run(t, ui, READS_INGAME, a, b, c))
        say("\nNext, on a THROWAWAY save:  python3 bridge/probe.py --writes")
    t.close()
    REPORT.write_text("\n".join(report))
    print(f"\nSaved to {REPORT}. Paste it to Claude.")


if __name__ == "__main__":
    main()
