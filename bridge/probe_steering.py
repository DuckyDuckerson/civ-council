"""Second probe: the steering levers the first probe couldn't settle. THROWAWAY SAVE ONLY.

    python3 bridge/probe_steering.py           # gives orders (see STEER below)
    (end one turn in the game)
    python3 bridge/probe_steering.py --after   # what stuck

Players: A = Portugal-like first AI, B = second, C = third (as picked by probe.py).
Saves reports to bridge/probe_steering_report.txt.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402
from tuner import Tuner  # noqa: E402

HERE = Path(__file__).parent
STATE_FILE = HERE / "probe_steering_state.json"
REPORT = HERE / "probe_steering_report.txt"

# InGame: valid tiles for a Campus in A's capital (read-only), so GameCore can place it.
PLOTS_INGAME = r"""
local A = __A__
chk("ui.campus_plots(A)", function()
  local city = Players[A]:GetCities():GetCapitalCity()
  local p = {}
  p[CityOperationTypes.PARAM_DISTRICT_TYPE] = GameInfo.Districts["DISTRICT_CAMPUS"].Hash
  local res = CityManager.GetOperationTargets(city, CityOperationTypes.BUILD, p)
  local plots = res and res[CityOperationResults.PLOTS]
  if not plots or #plots == 0 then return "none" end
  print("PLOT|" .. plots[1])
  return #plots .. " valid tiles, first " .. plots[1]
end)
"""

STEER_HEADER = r"""
local A, B, C, PLOT = __A__, __B__, __C__, __PLOT__
CivCouncilProbe = CivCouncilProbe or {log = {}}
"""

STEER = r"""
--@@
chk("district.place_incomplete(A)", function()
  if PLOT == nil then return "no valid tile from InGame" end
  local city = Players[A]:GetCities():GetCapitalCity()
  local q = city:GetBuildQueue()
  local row = GameInfo.Districts["DISTRICT_CAMPUS"]
  local plot = Map.GetPlotByIndex(PLOT)
  -- Signature from the game's own Debug/City.ltp + TunerCityPanel.lua: (district INDEX, plot index, % built).
  -- Passing the hash as the first argument crashed Civ 6 (segfault in GameCore_XP2).
  q:CreateIncompleteDistrict(row.Index, PLOT, 0)
  print("EXPECT|district_A|placed")
  return "placed: " .. tostring(q:HasDistrictBeenPlaced(row.Index)) .. "; plot district now " ..
    tostring(plot:GetDistrictType()) .. "; building " .. tostring(q:CurrentlyBuilding())
end)

--@@
chk("units.disable_settlers(C)", function()
  local u = Players[C]:GetUnits()
  local row = GameInfo.Units["UNIT_SETTLER"]
  local before = tostring(Players[C]:GetCities():GetCapitalCity():GetBuildQueue():CurrentlyBuilding())
  u:SetBuildDisabled(row.Index, true)   -- as in the game's own scenario scripts
  print("EXPECT|settlers_disabled_C|true")
  return "capital was building " .. before .. "; disabled now: " .. tostring(u:IsBuildDisabled(row.Index))
end)

--@@
chk("alliance.break(A,C)", function()
  local d = Players[A]:GetDiplomacy()
  d:SetHasAllied(C, false)
  print("EXPECT|allied_AC|false")
  return "allied " .. tostring(d:HasAllied(C)) .. "/" .. tostring(Players[C]:GetDiplomacy():HasAllied(A)) ..
    ", still friends " .. tostring(d:HasDeclaredFriendship(C)) .. ", state " .. tostring(Players[A]:GetAi_Diplomacy():GetDiplomaticState(C))
end)
--@@
chk("friendship.break(A,C)", function()
  local d = Players[A]:GetDiplomacy()
  d:SetHasDeclaredFriendship(C, false)
  print("EXPECT|friends_AC|false")
  return "friends " .. tostring(d:HasDeclaredFriendship(C)) .. "/" .. tostring(Players[C]:GetDiplomacy():HasDeclaredFriendship(A)) ..
    ", state " .. tostring(Players[A]:GetAi_Diplomacy():GetDiplomaticState(C))
end)

--@@
chk("opinion.adjust(B about C)", function()
  local ai = Players[B]:GetAi_Diplomacy()
  local before = ai:GetDiplomaticScore(C)
  ai:AdjustBaseDiplomacy(C, -30)   -- as in the game's own Debug/Diplomacy.ltp (which uses +-5)
  print("EXPECT|score_BC_before|" .. before)
  return "score " .. before .. " -> " .. ai:GetDiplomaticScore(C) .. ", state " .. tostring(ai:GetDiplomaticState(C))
end)

--@@
chk("war.with_army(B->A)", function()
  local d = Players[B]:GetDiplomacy()
  d:DeclareWarOn(A, WarTypes.FORMAL_WAR, true)
  local m = Players[B]:GetAi_Military()
  m:SetRival(A)   -- as in the game's own scenario scripts; PrepareForWarWith has no known caller, so not tried
  print("EXPECT|war_BA|true")
  return "at war " .. tostring(d:IsAtWarWith(A)) .. "; SetRival ran; operation against A now: " .. tostring(m:HasOperationAgainst(A))
end)

--@@
chk("government(A)", function()
  local cul = Players[A]:GetCulture()
  for row in GameInfo.Governments() do
    if cul:IsGovernmentUnlocked(row.Index) and row.GovernmentType ~= "GOVERNMENT_CHIEFDOM" then
      cul:SetCurrentGovernment(row.Index)
      print("EXPECT|government_A|" .. row.GovernmentType)
      return "SetCurrentGovernment(" .. row.GovernmentType .. ") ran; anarchy turns " .. tostring(cul:GetAnarchyTurns())
    end
  end
  return "no government besides Chiefdom unlocked"
end)

--@@
chk("hook.turn_start(B research goal)", function()
  local techs = Players[B]:GetTechs()
  local goal = nil
  for row in GameInfo.Technologies() do
    if not techs:HasTech(row.Index) and techs:CanResearch(row.Index) and row.Index ~= techs:GetResearchingTech() then goal = row break end
  end
  if goal == nil then return "no other tech" end
  CivCouncilProbe.goal = goal.Index
  if not CivCouncilProbe.hooked then
    GameEvents.PlayerTurnStarted.Add(function(pid)
      local log = CivCouncilProbe.log
      local entry = Game.GetCurrentGameTurn() .. ":" .. pid
      if pid == B then
        local t = Players[B]:GetTechs()
        entry = entry .. ":was " .. tostring(t:GetResearchingTech())
        if t:GetResearchingTech() ~= CivCouncilProbe.goal then t:SetResearchingTech(CivCouncilProbe.goal) end
      end
      log[#log+1] = entry
    end)
    CivCouncilProbe.hooked = true
  end
  print("EXPECT|research_B|" .. goal.TechnologyType)
  return "hook installed; goal " .. goal.TechnologyType .. " (not set now; only the hook sets it); now " .. tostring(techs:GetResearchingTech())
end)

--@@
chk("found_city", function()
  for _, pid in ipairs(PlayerManager.GetAliveMajorIDs()) do
    if not Players[pid]:IsHuman() then
      for _, u in Players[pid]:GetUnits():Members() do
        if GameInfo.Units[u:GetType()].UnitType == "UNIT_SETTLER" then return "player " .. pid .. " has a settler at " .. u:GetX() .. "," .. u:GetY() .. " (not founding; just noting)" end
      end
    end
  end
  return "no AI settler"
end)
"""

AFTER = r"""
local A, B, C, PLOT = __A__, __B__, __C__, __PLOT__
local function now(key, fn) local ok, v = pcall(fn); print("NOW|" .. key .. "|" .. (ok and tostring(v) or ("error: " .. tostring(v)))) end
now("turn", function() return Game.GetCurrentGameTurn() end)
now("district_A", function()
  local q = Players[A]:GetCities():GetCapitalCity():GetBuildQueue()
  local row = GameInfo.Districts["DISTRICT_CAMPUS"]
  return (q:HasDistrictBeenPlaced(row.Index) and "placed" or "not placed") .. "; building " .. tostring(q:CurrentlyBuilding()) ..
    "; campus progress " .. tostring(q:HasDistrictProductionProgress(row.Index))
end)
now("settlers_disabled_C", function()
  local row = GameInfo.Units["UNIT_SETTLER"]
  return tostring(Players[C]:GetUnits():IsBuildDisabled(row.Index)) .. "; capital building " .. tostring(Players[C]:GetCities():GetCapitalCity():GetBuildQueue():CurrentlyBuilding())
end)
now("allied_AC", function() return Players[A]:GetDiplomacy():HasAllied(C) end)
now("friends_AC", function() return Players[A]:GetDiplomacy():HasDeclaredFriendship(C) end)
now("score_BC_before", function() local ai = Players[B]:GetAi_Diplomacy(); return ai:GetDiplomaticScore(C) .. " (" .. tostring(ai:GetDiplomaticState(C)) .. ")" end)
now("war_BA", function() return tostring(Players[B]:GetDiplomacy():IsAtWarWith(A)) .. "; operation against A: " .. tostring(Players[B]:GetAi_Military():HasOperationAgainst(A)) end)
now("research_B", function() local i = Players[B]:GetTechs():GetResearchingTech(); return i >= 0 and GameInfo.Technologies[i].TechnologyType or "nothing" end)
now("hook_log", function() return CivCouncilProbe and table.concat(CivCouncilProbe.log, " ") or "no CivCouncilProbe global (state was reset)" end)
"""

GOV_INGAME = r"""
local A = __A__
local ok, v = pcall(function() local i = Players[A]:GetCulture():GetCurrentGovernment(); return i >= 0 and GameInfo.Governments[i].GovernmentType or "none" end)
print("NOW|government_A|" .. (ok and tostring(v) or ("error: " .. tostring(v))))
"""


def fill(body: str, a, b, c, plot) -> str:
    return (probe.HELPERS + body).replace("__A__", str(a)).replace("__B__", str(b)) \
        .replace("__C__", str(c)).replace("__PLOT__", str(plot))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", action="store_true")
    args = ap.parse_args()
    report: list[str] = []
    say = lambda s="": (print(s), report.append(s))  # noqa: E731

    t = Tuner()
    t.handshake()
    gc, ui = t.index_of("GameCore_Tuner"), t.index_of("InGame")
    out = t.run(gc, probe.HELPERS + probe.PICK_PLAYERS)
    a, b, c = probe.lines(out, "PAIR")[0][1:4]
    turn = probe.lines(out, "TURN")[0][1]
    say(f"Turn {turn}. A={a}, B={b}, C={c}")

    if args.after:
        state = json.loads(STATE_FILE.read_text())
        a, b, c, plot = state["A"], state["B"], state["C"], state["plot"]
        out = t.run(gc, fill(AFTER, a, b, c, plot)) + "\n" + t.run(ui, fill(GOV_INGAME, a, b, c, plot))
        current = {n[1]: "|".join(n[2:]) for n in probe.lines(out, "NOW")}
        say(f"Orders given on turn {state['turn']}; now turn {current.get('turn')}.")
        for key, want in state["expected"].items():
            say(f"{key}: ordered {want} -> now {current.get(key, '(not checked)')}")
        say(f"hook_log: {current.get('hook_log')}")
        if not current:
            say(out[:2000])
    else:
        pout = t.run(ui, fill(PLOTS_INGAME, a, b, c, "nil"))
        plots = probe.lines(pout, "PLOT")
        plot = plots[0][1] if plots else "nil"
        for ch in probe.lines(pout, "CHK"):
            say(f"[{ch[2]:4}] {ch[1]}: {'|'.join(ch[3:])}")
        expected = {}
        for test in [x for x in STEER.split("--@@") if x.strip()]:
            name = test.split('chk("', 1)[1].split('"', 1)[0]
            print(f"   running {name} ...", flush=True)
            REPORT.write_text("\n".join(report + [f"(running {name})"]))   # survives a crash
            out = t.run(gc, fill(STEER_HEADER + test, a, b, c, plot))
            for ch in probe.lines(out, "CHK"):
                say(f"[{ch[2]:4}] {ch[1]}: {'|'.join(ch[3:])[:1200]}")
            if not probe.lines(out, "CHK"):
                say(f"[????] {name}: {out[:800]}")
            expected.update({e[1]: e[2] for e in probe.lines(out, "EXPECT")})
        STATE_FILE.write_text(json.dumps({"A": a, "B": b, "C": c, "plot": plot, "turn": turn,
                                          "expected": expected}, indent=1))
        say("\nEnd ONE turn in the game, then: python3 bridge/probe_steering.py --after")
    t.close()
    REPORT.write_text("\n".join(report))


if __name__ == "__main__":
    main()
