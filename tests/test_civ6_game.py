"""Civ6Game against a fake tuner link that answers in the formats recorded from a real Civ 6 (2026-11-17)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "bridge"))
import civ6_game as cg  # noqa: E402
from civ6_game import Civ6Game  # noqa: E402

from civ_council.agent import LeaderAgent  # noqa: E402
from civ_council.council import WORLD, Council  # noqa: E402
from civ_council.llm import ScriptedClient  # noqa: E402
from civ_council.personas import persona_for  # noqa: E402

WORLD_LINES = """TURN|60|0|-1757092276
CIV|0|LEADER_ELIZABETH|CIVILIZATION_ENGLAND|Elizabeth I|England|true|true|1|45|5|2|432|40
CIV|1|LEADER_PEDRO|CIVILIZATION_BRAZIL|Pedro II|Brazil|false|true|2|85|4|6|167|32
CIV|2|LEADER_ELEANOR_FRANCE|CIVILIZATION_FRANCE|Eleanor of Aquitaine (France)|France|false|true|2|30|4|7|494|34
CIV|3|LEADER_GILGAMESH|CIVILIZATION_SUMERIA|Gilgamesh|Sumeria|false|true|1|20|3|3|50|20
REL|1|2|true|false|false
MET|0|1|
MET|1|0|DIPLO_STATE_NEUTRAL
MET|1|2|DIPLO_STATE_WAR
MET|2|1|DIPLO_STATE_WAR
CS|5|Venice|0|140|0,1|2
CS|6|Geneva|1|100|1,2|
CS|7|Kumasi|-1|60|3|""".splitlines()

OPTIONS_LINES = """RESEARCHING|TECH_ASTROLOGY|Astrology
TECH|TECH_MINING|Mining
TECH|TECH_ASTROLOGY|Astrology
CIVIC_NOW|CIVIC_EARLY_EMPIRE|Early Empire
CIVIC|CIVIC_MYSTICISM|Mysticism
SETTLERS|true
GOVOK|GOVERNMENT_CHIEFDOM|Chiefdom
GOVOK|GOVERNMENT_AUTOCRACY|Autocracy
BANNED|UNIT_SLINGER
ENVOYS|1
DELEG|2
CITY|131073|Beja|DISTRICT_CAMPUS""".splitlines()

TRADE_LINES = ["TR|0|you_can_give|gold|167", "TR|0|you_can_give|gold_per_turn|5", "TR|0|you_can_give|RESOURCE_IRON|12",
               "TR|0|you_can_give|open_borders|0", "TR|0|they_can_give|RESOURCE_MARBLE|1", "TR|0|they_can_give|favor|40",
               "TR|0|they_can_give|RESOURCE_SILK|0", "TRC|0|they_can_give|Newcastle"]

AWARE_LINES = """CT|0|65536|London|10|10
CT|1|65536|Rio|40|10
CT|1|131073|Sao Paulo|14|10
CT|2|65536|Paris|80|40
CD|1|65536|0|30
CD|1|131073|0|4
CD|0|65536|1|4
NEAR|0|65536|1|131073|4
TROOPS|0|1|3|90
WONDER|1|BUILDING_PYRAMIDS|Pyramids|Rio
RELIGION|2|RELIGION_CATHOLICISM|Catholicism
PROGRESS|0|10|5
PROGRESS|1|12|6
PROGRESS|2|9|8
DISTRICTS|0|4
DISTRICTS|1|2
HAS|1|RESOURCE_IRON|strategic|5
HAS|1|RESOURCE_MARBLE|luxury|2
HAS|0|RESOURCE_SILK|luxury|1
MOD|1|0|4|Likes civs who have not captured capitals
MOD|1|0|-6|Unknown Reason
MOD|1|0|2|Unknown Reason""".splitlines()

AGES_LINES = ["ERANEXT|0", "AGE|0|normal|40|24|36", "AGE|1|golden|12|30|42", "AGE|2|dark|5|20|30",
              "LOYAL|1|65536|Rio|100|2|-1|", "LOYAL|1|131073|Sao Paulo|31|-6|5|the Free Cities",
              "VIC|VICTORY_DEFAULT", "TAKEN|1|131073|Sao Paulo|0|false"]

SITE_LINES = ["SITE|6291|37|59|8|5|Beja|7|RESOURCE_WHALES,RESOURCE_WHALES||1|false|true|false|32|59|Zealandia",
              "SITE|5446|40|51|8|5|Beja|4|RESOURCE_WINE|RESOURCE_IRON|3|true|true|false"]

TILE_LINES = ["TILE|4740|76|44|Plains||false|0|1|3", "TILE|4632|74|43|Grassland|Woods|true|1|0|6"]


class FakeLink:
    def __init__(self):
        self.world = list(WORLD_LINES)
        self.calls: list[str] = []
        self.replies: dict[str, str] = {}   # action kind / steering key -> RES line
        self.government = "GOVOK|"   # unused placeholder
        self.gov_now = "GOVNOW|GOVERNMENT_CHIEFDOM|false"
        self.deals: list[str] = []          # DL| lines: ongoing deals
        self.aware = list(AWARE_LINES)      # what AWARE_LUA prints
        self.congress: list[str] = []       # what CONGRESS_LUA prints
        self.ages = list(AGES_LINES)        # what AGES_LUA prints
        self.combat: list[str] = []         # CMB| lines COMBAT_LUA prints (then the log starts over)
        self.pin_calls: list[str] = []
        self.fresh_peace = False            # FRESH_PEACE_LUA's answer
        self.disasters: list[str] = []      # DIS| lines DISASTER_LUA prints
        self.notes: list[str] = []          # Civ notifications sent to the player
        self.quieted: list[str] = []        # QD| lines: Civ AI pop-ups the quiet hook closed
        self.quiet_calls: list[str] = []
        self.next_pin = 0
        self.moments: list[str] = []        # MOM| lines MOMENTS_LUA prints (Civ's Historic Moments)

    def run(self, lua: str, ui: bool = False) -> list[str]:
        if "TURN|" in lua and "GetWasEverAliveMajorIDs" in lua:
            return list(self.world)
        if "HistoricalAgendas" in lua:
            return ["AGENDA|2|Angevin Empire|Builds populous cities."]
        self.calls.append(lua)
        for kind, body in cg.ACTION_LUA.items():
            if body in lua:
                return [self.replies.get(kind, "RES|ok|done")]
        if "RESEARCHING|" in lua:
            return OPTIONS_LINES
        if ui and "GetOperationTargets" in lua:
            return TILE_LINES
        if ui and "GetAllMomentsData" in lua:
            return list(self.moments)
        if ui and "HasDarkAge" in lua:
            return list(self.ages)
        if ui and "GetWorldCongress" in lua:
            return list(self.congress)
        if ui and 'print("FRESH|"' in lua:
            self.calls.append(lua)
            return [f"FRESH|{'true' if self.fresh_peace else 'false'}"]
        if ui and "GetMapPin(" in lua:
            self.pin_calls.append(lua)
            self.next_pin += 1
            return [self.replies.get("pin", f"RES|ok|{self.next_pin}|{6000 + self.next_pin}")]
        if ui and "GetCurrentGovernment" in lua:
            return [self.gov_now]
        if 'print(table.concat({"SITE"' in lua:
            return SITE_LINES
        if "NotificationManager.SendNotification" in lua:
            self.notes.append(lua)
            return ["RES|ok|sent"]
        if "SetFavoredYield" in lua:
            self.calls.append(lua)
            return [self.replies.get("focus", "RES|ok|Rio focuses on production")]
        if "gp:RecruitPerson(P" in lua:
            self.calls.append(lua)
            return [self.replies.get("recruit", "RES|ok|recruited Ada Lovelace")]
        if "GameEvents.GiftUnit.Call" in lua:
            self.calls.append(lua)
            return [self.replies.get("gift", "RES|ok|2|Tank")]
        for key, marker in (("attack", '"Attack Enemy City"'), ("settle", "UnitManager.Kill"),
                            ("embassy", "SetHasEmbassyAt")):
            if marker in lua:
                self.calls.append(lua)
                return [self.replies.get(key, "RES|ok|done")]
        if "PROGRESS|" in lua:
            return list(self.aware)
        if ui and "CC_QUIET_ON" in lua:
            self.quiet_calls.append(lua)
            out, self.quieted = ["QUIET|true"] + self.quieted, []
            return out
        if ui and "CC_DISASTERS" in lua:
            out, self.disasters = ["HOOK|true"] + self.disasters, []
            return out
        if "CC_FIGHTS" in lua:
            out, self.combat = ["HOOK|true"] + self.combat, []
            return out
        if 'print("TR|"' in lua:
            return TRADE_LINES
        if "GetPlayerDeals" in lua:
            return list(self.deals)
        for key, marker in (("research", "SetResearchingTech"), ("civic", "SetProgressingCivic"),
                            ("ban", "SetBuildDisabled(row.Index, BAN)"),
                            ("expand", "SetBuildDisabled"), ("district", "CreateIncompleteDistrict"),
                            ("government", "SetCurrentGovernment"), ("envoy", "GiveFreeTokenToPlayer"),
                            ("delegation", "SetHasDelegationAt"), ("deal", "EnactWorkingDeal")):
            if marker in lua:
                return [self.replies.get(key, "RES|ok|done")]
        raise AssertionError("unexpected Lua: " + lua[:200])


def make_game():
    link = FakeLink()
    return Civ6Game(link=link, cache_seconds=0), link


# ---- reads ------------------------------------------------------------------------------
def test_reads_names_stances_and_only_met_civs():
    g, _ = make_game()
    assert g.turn == 60 and g.local_player == 0
    assert g.civ(2).leader == "Eleanor of Aquitaine"          # "(France)" suffix dropped
    assert g.stance(1, 2) == "war" and g.stance(0, 1) == "peace"
    me = g.snapshot(1)
    assert [o["id"] for o in me["others"]] == [0, 2]            # Pedro has met England and France only
    assert me["world_wars"] == ["Pedro II vs Eleanor of Aquitaine"]
    assert [o["id"] for o in g.snapshot(0)["others"]] == [1]    # the human only sees who they've met
    assert g.snapshot(3)["others"] == [] and g.snapshot(3)["world_wars"] == []


def test_game_id_is_stable_and_names_the_leaders():
    g, link = make_game()
    import zlib
    assert g.game_id == f"{zlib.crc32(b'-1757092276'):08x}-elizabeth-pedro-eleanor_france-gilgamesh"
    link.world[0] = "TURN|99|0|-1757092276"          # later turn, same starting tiles: same game
    g2 = Civ6Game(link=link, cache_seconds=0)
    assert g2.game_id == g.game_id


# ---- diplomacy --------------------------------------------------------------------------
def test_war_respects_the_games_rules():
    g, link = make_game()
    link.replies["declare_war"] = "RES|no|Civ 6 doesn't allow that war right now"
    res = g.apply(1, {"type": "declare_war", "target": 0})
    assert not res.ok and "doesn't allow" in res.message
    link.replies["declare_war"] = "RES|ok|war declared"
    assert g.apply(1, {"type": "declare_war", "target": 0}).ok
    assert "local A, B = 1, 0" in link.calls[-1]


def test_cannot_act_on_civs_you_have_not_met():
    g, link = make_game()
    res = g.apply(1, {"type": "declare_war", "target": 3})
    assert not res.ok and "hasn't met" in res.message and link.calls == []


def test_friendship_is_a_proposal_until_accepted_then_real():
    g, link = make_game()
    assert g.apply(1, {"type": "propose_friendship", "target": 0}).ok
    assert link.calls == []                                      # nothing in the game yet
    [p] = g.pending_proposals(0)
    res = g.resolve_proposal(p.id, 0, accept=True)
    assert res.ok and "declared friends" in res.message
    assert "SetHasDeclaredFriendship(B, true)" in link.calls[-1] and "local A, B = 1, 0" in link.calls[-1]


def test_alliance_needs_friendship_and_agreed_peace_is_signed_even_early():
    g, link = make_game()
    assert "friendship first" in g.apply(1, {"type": "propose_alliance", "target": 0}).message
    assert g.apply(1, {"type": "make_peace", "target": 2}).ok
    link.replies["agreed_peace"] = "RES|ok|early"          # inside Civ's minimum war length
    res = g.resolve_proposal(g.pending_proposals(2)[0].id, 2, accept=True)
    assert res.ok and "by agreement, before Civ's usual minimum length of a war" in res.message
    assert cg.ACTION_LUA["agreed_peace"] in link.calls[-1] and "CanMakePeaceWith" in link.calls[-1]
    link.replies["agreed_peace"] = "RES|no|still at war"
    assert g.apply(1, {"type": "make_peace", "target": 2}).ok
    res = g.resolve_proposal(g.pending_proposals(2)[0].id, 2, accept=True)
    assert not res.ok and "still at war" in res.message


def test_denounce_is_remembered_and_saved(tmp_path):
    g, _ = make_game()
    assert g.apply(1, {"type": "denounce", "target": 2}).ok
    assert g.is_denouncing(1, 2) and g.snapshot(2)["others"][0]["they_denounced_you"]
    g.save_state(tmp_path / "s.json")
    g2, _ = make_game()
    g2.load_state(tmp_path / "s.json")
    assert g2.is_denouncing(1, 2)


# ---- turns and events -------------------------------------------------------------------
def test_end_turn_reports_what_changed_in_the_game():
    g, link = make_game()
    assert g.end_turn() == []                                    # first call is the baseline
    link.world = [l.replace("REL|1|2|true", "REL|1|2|false") for l in link.world]
    link.world = [l.replace("|France|false|true|2|", "|France|false|true|3|") for l in link.world]
    link.world += ["MET|3|1|", "MET|1|3|"]
    events = g.end_turn()
    assert any("made peace" in e for e in events)
    assert any("France now has 3 cities" in e for e in events)
    assert any("met" in e and "Gilgamesh" in e for e in events)
    assert any("made peace" in e for e in g.snapshot(1)["recent_events"])
    assert not any("made peace" in e for e in g.snapshot(0)["recent_events"])   # England hasn't met France


# ---- steering ---------------------------------------------------------------------------
def test_steering_options_and_orders():
    g, link = make_game()
    o = g.steering_options(1)
    assert o["researching"] == "TECH_ASTROLOGY" and o["can_research"]["TECH_MINING"] == "Mining"
    assert o["building_settlers_allowed"] is True
    sites = o["cities"][0]["district_sites"]["DISTRICT_CAMPUS"]
    assert sites[0]["tile"] == 4632 and sites[0]["river"] and sites[0]["terrain"] == "Grassland, Woods"   # best first
    notes = g.set_orders(1, {"research": "tech_mining", "expand": False,
                             "district": {"city": 131073, "type": "DISTRICT_CAMPUS", "tile": 4740}})
    assert len(notes) == 3 and g.orders[1] == {"research": "TECH_MINING", "expand": False}
    assert any('"TECH_MINING"' in c for c in link.calls)
    assert any("CreateIncompleteDistrict(row.Index, PLOT, 0)" in c and "4740" in c for c in link.calls)


def test_district_on_an_invalid_tile_never_reaches_the_game():
    g, link = make_game()
    notes = g.set_orders(1, {"district": {"city": 131073, "type": "DISTRICT_CAMPUS", "tile": 999}})
    assert "isn't a valid" in notes[0]
    assert not any("CreateIncompleteDistrict" in c for c in link.calls)


def test_orders_reject_odd_names_and_ignore_the_human():
    g, link = make_game()
    assert g.set_orders(1, {"research": 'x"); os.exit()'}) == ["research: unknown 'X\"); OS.EXIT()'"]
    assert g.set_orders(0, {"research": "TECH_MINING"}) == []
    assert link.calls == []


def test_finished_goals_are_dropped_and_kept_goals_reapplied():
    g, link = make_game()
    g.orders = {1: {"research": "TECH_MINING", "civic": "CIVIC_MYSTICISM"}}
    link.replies["research"] = "RES|no|can't research that now"
    notes = g.reapply_orders()
    assert g.orders[1] == {"civic": "CIVIC_MYSTICISM"} and len(notes) == 2


# ---- the council on a real game ----------------------------------------------------------
def test_council_turn_on_the_real_game(tmp_path):
    g, link = make_game()
    reply = {"messages": [{"to": 0, "text": "Greetings, Queen."}, {"to": 3, "text": "Hello stranger"}],
             "orders": {"civic": "CIVIC_MYSTICISM"}, "actions": [], "memory": [], "journal": "watch France"}
    pedro = ScriptedClient(lambda s, m: json.dumps(reply))
    eleanor = ScriptedClient(lambda s, m: "{}")
    gil = ScriptedClient(lambda s, m: "{}")
    agents = {i: LeaderAgent(i, persona_for(g.leader_type(i), g.civ(i).leader, g.civ(i).name), c)
              for i, c in ((1, pedro), (2, eleanor), (3, gil))}
    c = Council(g, agents, human_id=0, spy=True, memory_dir=tmp_path)
    g.end_turn()
    link.world = [l.replace("TURN|60", "TURN|61") for l in link.world]
    c.on_new_turn()
    texts = [(e["channel"], e["text"]) for e in c.entries]
    assert (WORLD, "Turn 61 begins") in texts
    assert (1, "Greetings, Queen.") in texts                     # Pedro wrote to the human
    assert not any(t == "Hello stranger" for _, t in texts)      # ...but not to Gilgamesh, whom he hasn't met
    assert any(ch == "minds" and "Pedro II's orders" in t for ch, t in texts)
    assert "your_empire" in pedro.calls[0][1][0]["content"] and "ORDERS" not in pedro.calls[0][0]
    assert (tmp_path / "pedro.json").exists() and (tmp_path / "game_state.json").exists()
    assert g.orders[1] == {"civic": "CIVIC_MYSTICISM"}


def test_human_cannot_message_unmet_leaders():
    g, _ = make_game()
    agents = {3: LeaderAgent(3, persona_for("LEADER_GILGAMESH", "Gilgamesh", "Sumeria"),
                             ScriptedClient(lambda s, m: "{}"))}
    c = Council(g, agents, human_id=0)
    c.say(3, "hello?")
    assert c.entries[-1]["text"] == "You haven't met that leader yet."
    assert agents[3].client.calls == []


def test_personas_for_any_leader():
    known = persona_for("LEADER_GILGAMESH", "Gilgamesh", "Sumeria")
    assert known.key == "gilgamesh" and "Enkidu" in known.agenda
    made = persona_for("LEADER_JOAO_III", "João III", "Portugal", "Navigator's Legacy", "Likes explorers.")
    assert made.key == "joao_iii" and made.agenda == "Navigator's Legacy: Likes explorers."
    assert "João III" in made.voice


def test_web_server_runs_one_catch_up_turn_when_civ_moves_on():
    import threading
    from civ_council.web import Server
    g, link = make_game()
    gate, started = threading.Event(), threading.Event()
    calls = []

    def slow(system, messages):
        calls.append(1)
        started.set()
        gate.wait(5)
        return "{}"
    agents = {1: LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(slow))}
    srv = Server(Council(g, agents, human_id=0, summarize_after=0))
    assert srv.live and srv.handle("/api/end_turn", {})["ok"] is False
    srv.new_game_turn(61)
    assert started.wait(5)
    srv.new_game_turn(62)           # Civ moves on twice while Pedro is still thinking
    srv.new_game_turn(63)
    gate.set()
    for _ in range(200):
        if not srv.turn_queued and srv.pending == 0:
            break
        threading.Event().wait(0.02)
    assert len(calls) == 2           # the turn in progress, plus exactly one catch-up turn


def test_parallel_leaders_act_in_a_fixed_order():
    import time
    g, _ = make_game()

    def say_after(delay, text):
        def reply(system, messages):
            time.sleep(delay)
            return json.dumps({"messages": [{"to": 0, "text": text}]})
        return reply
    agents = {1: LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(say_after(0.3, "Pedro"))),
              2: LeaderAgent(2, persona_for("LEADER_ELEANOR_FRANCE", "Eleanor", "France"), ScriptedClient(say_after(0.0, "Eleanor")))}
    g.link.world.append("MET|2|0|")
    c = Council(g, agents, human_id=0, parallel=2, summarize_after=0)
    start = time.time()
    c.on_new_turn()
    assert time.time() - start < 0.55                             # both thought at the same time
    said = [e["text"] for e in c.entries if e["kind"] == "message"]
    assert said == ["Pedro", "Eleanor"]                           # but act in roster order


def test_leaders_chat_casually_by_default_but_keep_their_personality():
    from civ_council.personas import PERSONAS
    casual = LeaderAgent(1, PERSONAS["gilgamesh"], ScriptedClient(lambda s, m: "{}")).system_prompt()
    assert "playing Civ 6 online" in casual and "Enkidu" in casual and "No old-fashioned" in casual
    old = LeaderAgent(1, PERSONAS["gilgamesh"], ScriptedClient(lambda s, m: "{}"), voice="roleplay").system_prompt()
    assert "How you speak: Epic and heroic" in old and "online" not in old


def test_your_message_shows_at_once_even_while_leaders_are_busy():
    import threading
    from civ_council.web import Server
    g, _ = make_game()
    gate, started = threading.Event(), threading.Event()

    def slow(system, messages):
        started.set()
        gate.wait(5)
        return json.dumps({"messages": [{"to": 0, "text": "sup"}]})
    agents = {1: LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(slow))}
    srv = Server(Council(g, agents, human_id=0, summarize_after=0))
    srv.new_game_turn(61)                       # Pedro starts a slow turn
    assert started.wait(5)
    assert srv.handle("/api/say", {"to": 1, "text": "hey pedro"})["ok"]
    assert [e["text"] for e in srv.c.entries if e["sender"] == 0] == ["hey pedro"]   # posted before the turn ends
    gate.set()
    for _ in range(200):
        if srv.pending == 0:
            break
        threading.Event().wait(0.02)
    assert srv.state(0)["boot"] == srv.boot


def test_unread_messages_are_not_listed_twice_and_journal_has_one_turn_tag():
    a = LeaderAgent(2, persona_for("LEADER_PHILIP_II", "Philip II", "Spain"), ScriptedClient(lambda s, m: "{}"))
    convo = [{"turn": 1, "from": "Philip II", "text": "religion?"}, {"turn": 1, "from": "Elizabeth I", "text": "none"}]
    assert a._unread_removed(convo, [{"from": "Elizabeth I", "text": "none"}]) == convo[:1]
    a.memory.add_journal(81, "T81: watching for missionaries")
    assert a.memory.journal == ["T81: watching for missionaries"]


def test_memory_summaries_come_when_a_leaders_memory_has_grown():
    g, link = make_game()
    calls = []
    agents = {1: LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"),
                             ScriptedClient(lambda s, m: calls.append(m[0]["content"]) or "{}"))}
    c = Council(g, agents, human_id=0, summarize_after=6)
    mem = agents[1].memory
    def at(turn, chatter=0):
        for i in range(chatter):
            mem.log(0, turn, "Elizabeth", f"hello {i}")
        link.world[0] = f"TURN|{turn}|0|-1757092276"
        calls.clear()
        c.on_new_turn()
        return any("tidy your memory" in x for x in calls)
    assert at(60) is False                        # counting starts
    assert [at(t) for t in (61, 70, 100)] == [False, False, False]   # a quiet leader: nothing to condense
    assert at(101, chatter=6) is True             # a lot was said: condensed
    assert mem.fresh == 0 and mem.summarized == 101
    assert at(108, chatter=10) is False           # not again within 10 turns, however busy
    assert at(111) is True
    assert at(130, chatter=5) is False and at(191) is True     # a few things new, 80 turns on: condensed anyway


def test_leaders_switch_to_the_talk_model_when_you_are_in_the_conversation():
    g, _ = make_game()
    cheap, smart = ScriptedClient(lambda s, m: "{}", name="haiku"), ScriptedClient(lambda s, m: "{}", name="sonnet")
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), cheap, talk_client=smart)
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    c.on_new_turn()                                   # routine turn: cheap model
    assert len(cheap.calls) == 1 and smart.calls == []
    c.say(1, "hey pedro")                             # you talk: smart model
    assert len(smart.calls) == 1 and a.last_model == "sonnet"
    a.consolidate(g)                                  # memory summaries use the smart model too
    assert len(smart.calls) == 2 and len(cheap.calls) == 1


def test_talk_model_defaults():
    from civ_council.cli import _talk_client, make_parser
    args = make_parser().parse_args(["--provider", "claude-code", "--claude-bin", "/bin/echo"])
    import civ_council.llm as llm
    llm.check_claude_login.cache_clear()
    orig = llm.check_claude_login
    llm.check_claude_login = lambda b: None
    try:
        assert _talk_client(args, "claude-code", "haiku").name == "claude-code:sonnet"
        assert _talk_client(args, "claude-code", "sonnet") is None
        args.talk_model = "same"
        assert _talk_client(args, "claude-code", "haiku") is None
        assert _talk_client(make_parser().parse_args([]), "fake", None) is None
    finally:
        llm.check_claude_login = orig


def test_minds_channel_shows_each_leaders_thinking_and_model():
    g, _ = make_game()
    cheap = ScriptedClient(lambda s, m: json.dumps({"thoughts": "France is weak, push now.", "journal": "war soon",
                                                     "orders": {"civic": "CIVIC_MYSTICISM"}}), name="claude-code:haiku")
    smart = ScriptedClient(lambda s, m: json.dumps({"thoughts": "She seems honest.", "messages": [{"to": 0, "text": "ok"}]}),
                           name="claude-code:sonnet")
    c = Council(g, {1: LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), cheap, talk_client=smart)},
                human_id=0, summarize_after=0)
    c.on_new_turn()
    c.say(1, "hi")
    minds = [e for e in c.entries if e["channel"] == "minds"]
    thoughts = [(e["text"], e["model"], e["why"]) for e in minds if e["kind"] == "thought"]
    assert thoughts == [("France is weak, push now.", "haiku", "taking its turn (empire review)"),
                        ("She seems honest.", "sonnet", "reading your message")]
    assert any(e["kind"] == "orders" and e["text"].startswith("Pedro II's orders: civic") for e in minds)
    assert [e for e in minds if e["kind"] == "thought"][0]["plan"] == "war soon"


# ---- city-states and strength ------------------------------------------------------------
def test_briefing_shows_city_state_backers_and_strength():
    g, _ = make_game()
    pedro = g.snapshot(1)
    assert {c["name"]: c["suzerain"] for c in pedro["city_states"]} == {"Venice": "Elizabeth I", "Geneva": "Pedro II"}
    eliz = pedro["others"][0]
    assert eliz["city_states_backing_them"] == ["Venice"] and eliz["military_with_city_states"] == 45 + 140
    assert pedro["you"]["city_states_backing_you"] == ["Geneva"]
    assert pedro["you"]["military_with_city_states"] == 85 + 100
    assert [c["name"] for c in g.snapshot(3)["city_states"]] == ["Kumasi"]       # only the ones you've met
    assert g.snapshot(3)["city_states"][0]["suzerain"] is None
    assert g.snapshot(2)["city_states"][0]["at_war_with_you"] is False
    assert {c["name"]: c["at_war_with_you"] for c in g.snapshot(1)["city_states"]}["Venice"] is False


# ---- decisions Civ's own AI made -----------------------------------------------------------
def test_civ_ai_peace_is_flagged_and_can_be_overruled():
    g, link = make_game()
    g.end_turn()                                                     # baseline
    link.world = [l.replace("REL|1|2|true", "REL|1|2|false") for l in link.world]
    g.end_turn()                                                     # nobody ordered this peace
    [n] = g.steering_options(1)["decided_without_you"]
    assert "made peace with Eleanor" in n["what"] and n["to_overrule"] == "go back to war"
    assert len(g.steering_options(2)["decided_without_you"]) == 1   # Eleanor's leader is told too
    notes = g.set_orders(1, {"overrule": [n["id"]]})
    assert notes == ["overrule: Pedro II of Brazil overruled its advisors and chose to go back to war "
                     "with Eleanor of Aquitaine of France."]
    assert "DeclareWarOn(B" in link.calls[-1] and "local A, B = 1, 2" in link.calls[-1]
    assert g.notices == {}                                           # both sides' notices are settled
    link.world = [l.replace("REL|1|2|false", "REL|1|2|true") for l in link.world]
    g.end_turn()
    assert g.notices == {}                                           # the leader's own war isn't flagged


def test_changes_a_leader_ordered_are_not_flagged():
    g, link = make_game()
    g.end_turn()
    assert g.apply(1, {"type": "declare_war", "target": 0}).ok
    link.world.append("REL|0|1|true|false|false")
    g.end_turn()
    assert g.notices == {}


def test_war_with_the_human_cannot_be_undone_from_the_ai_side():
    g, link = make_game()
    g.end_turn()
    link.world.append("REL|0|1|true|false|false")                   # the human may have declared it
    g.end_turn()
    [n] = g.steering_options(1)["decided_without_you"]
    assert n["to_overrule"] == "can't be undone" and "Elizabeth I declared it" in n["what"]
    assert g.set_orders(1, {"overrule": [n["id"]]}) == ["overrule: that one can't be undone"]
    assert g.set_orders(2, {"overrule": [n["id"]]}) == [f"overrule: no decision #{n['id']} to overrule"]


def test_notices_expire_and_research_says_who_chose_it():
    g, link = make_game()
    assert g.steering_options(1)["research_chosen_by"] == "Civ's built-in AI"
    g.set_orders(1, {"research": "TECH_ASTROLOGY"})
    assert g.steering_options(1)["research_chosen_by"] == "you"
    g.end_turn()
    link.world = [l.replace("REL|1|2|true", "REL|1|2|false") for l in link.world]
    g.end_turn()
    assert g.notices
    link.world[0] = "TURN|64|0|-1757092276"
    g.end_turn()
    assert g.notices == {}


# ---- deals and the rest of the empire controls (probe 3) -----------------------------------
def test_gold_gift_goes_through_at_once_and_deals_wait_for_a_yes():
    g, link = make_game()
    res = g.apply(1, {"type": "propose_deal", "target": 0, "give_gold": 50})
    assert res.ok and res.proposal_id is None and "sent Elizabeth I of England 50 gold" in res.message
    deal_lua = link.calls[-2]   # then the bridge re-reads ongoing deals
    assert "local A, B = 1, 0" in deal_lua and "EnactWorkingDeal" in deal_lua
    assert '{from=A, kind="GOLD", amount=50, label="50 gold from Pedro II"}' in deal_lua
    n = len(link.calls)
    res = g.apply(1, {"type": "propose_deal", "target": 0,
                      "give": {"gold_per_turn": 3, "resources": {"resource_iron": 5}, "open_borders": True},
                      "ask": {"resources": ["RESOURCE_MARBLE"], "favor": 10}})
    assert res.ok and res.proposal_id and len(link.calls) == n          # nothing happens until they agree
    [p] = g.pending_proposals(0)
    from civ_council.game import proposal_label
    assert proposal_label(p) == ("a deal: you get 3 gold per turn, Iron x5, open borders "
                                 "and you give Marble, 10 diplomatic favor")
    assert g.resolve_proposal(p.id, 0, accept=True).ok
    lua = link.calls[-2]   # the deal (the last call re-reads ongoing deals)
    for part in ('{from=A, kind="GOLD", per_turn=true, amount=3', '{from=A, kind="RESOURCES", res="RESOURCE_IRON", amount=5',
                 '{from=A, kind="AGREEMENTS", sub="OPEN_BORDERS"', '{from=B, kind="RESOURCES", res="RESOURCE_MARBLE", amount=1',
                 '{from=B, kind="FAVOR", amount=10'):
        assert part in lua


def test_deals_are_checked_before_reaching_the_game():
    g, link = make_game()
    assert "only has 167 gold" in g.apply(1, {"type": "propose_deal", "target": 0, "give_gold": 500}).message
    assert "peace, on these terms" in g.apply(1, {"type": "propose_deal", "target": 2, "give_gold": 5}).message
    assert "needs something" in g.apply(1, {"type": "propose_deal", "target": 0}).message
    assert "between 0 and" in g.apply(1, {"type": "propose_deal", "target": 0, "ask_gold": -3}).message
    assert "unknown resource" in g.apply(1, {"type": "propose_deal", "target": 0,
                                             "give": {"resources": {"IRON\"); os.exit()": 1}}}).message
    assert link.calls == []
    link.replies["deal"] = "RES|no|Iron from Pedro II: only 2 available"
    res = g.apply(1, {"type": "propose_deal", "target": 0, "give": {"resources": {"RESOURCE_IRON": 5}}})
    assert not res.ok and "only 2 available" in res.message


def test_trade_options_and_deals_civ_ai_made_on_its_own():
    g, link = make_game()
    opts = g.steering_options(1)["trade_options"]
    assert opts == {"Elizabeth I": {"civ_id": 0,
                                    "you_can_give": {"gold": 167, "gold_per_turn": 5, "resources": {"RESOURCE_IRON": 12},
                                                     "open_borders": True},
                                    "they_can_give": {"resources": {"RESOURCE_MARBLE": 1}, "favor": 40,
                                                      "cities": ["Newcastle"]}}}
    link.deals = ["DL|0|1|1|AGREEMENTS|OPEN_BORDERS|0|120"]
    g.end_turn()                                                 # baseline: existing deals aren't news
    assert g.notices == {}
    link.deals.append("DL|0|1|0|RESOURCES|RESOURCE_MARBLE|1|90")
    link.deals.append("DL|0|1|1|GOLD|PER_TURN|4|90")
    g.end_turn()
    [n] = g.notices.values()
    assert n["civ"] == 1 and n["undo"] is None
    assert "deal with Elizabeth I for you: you give 4 gold per turn; you get Marble" in n["what"]
    assert g.steering_options(1)["decided_without_you"][0]["to_overrule"] == "can't be undone"


def test_deals_the_bridge_made_are_not_flagged():
    g, link = make_game()
    g.end_turn()
    link.deals = ["DL|0|1|1|GOLD|PER_TURN|2|90"]                # what Civ shows after our deal
    assert g.apply(1, {"type": "propose_deal", "target": 0, "give": {"gold_per_turn": 2}}).ok
    g.end_turn()
    assert g.notices == {}


def test_government_orders_respect_unlocks_cooldown_and_anarchy():
    g, link = make_game()
    gov = g.steering_options(1)["government"]
    assert gov["current"] == "GOVERNMENT_CHIEFDOM" and gov["chosen_by"] == "Civ's built-in AI"
    assert gov["can_switch_to"] == ["GOVERNMENT_AUTOCRACY"]
    assert g.set_orders(1, {"government": "GOVERNMENT_AUTOCRACY"}) == ["government: done"]
    assert "SetCurrentGovernment" in link.calls[-1] and 'NAME = 1, "GOVERNMENT_AUTOCRACY"' in link.calls[-1]
    link.gov_now = "GOVNOW|GOVERNMENT_AUTOCRACY|false"
    gov = g.steering_options(1)["government"]
    assert gov["chosen_by"] == "you" and gov["can_switch_to"] == [] and "switch again in 10 turns" in gov["note"]
    assert "wait 10 more turns" in g.set_orders(1, {"government": "GOVERNMENT_CHIEFDOM"})[0]
    g2, link2 = make_game()
    link2.gov_now = "GOVNOW||true"
    assert "anarchy" in g2.set_orders(1, {"government": "GOVERNMENT_AUTOCRACY"})[0]


def test_unit_bans_envoys_and_delegations():
    g, link = make_game()
    opts = g.steering_options(1)
    assert opts["banned_units"] == ["UNIT_SLINGER"] and opts["envoys_to_send"] == 1
    assert opts["city_states_you_know"] == ["Geneva", "Venice"] and opts["delegations_at"] == [2]
    notes = g.set_orders(1, {"ban_units": ["UNIT_WARRIOR"], "allow_units": ["UNIT_SLINGER"], "envoy": "venice",
                             "delegation": 0})
    assert notes == ["ban_units: done", "allow_units: done", "envoy: done", "delegation: done"]
    calls = "\n".join(link.calls[-4:])
    assert '"UNIT_WARRIOR", true' in calls and '"UNIT_SLINGER", false' in calls
    assert "local P, M = 1, 5" in calls and "ChangeTokensToGive(-1)" in calls and "local P, B = 1, 0" in calls
    assert g.set_orders(1, {"envoy": "Kumasi"}) == ["envoy: you don't know a city-state called 'Kumasi'"]
    assert g.set_orders(1, {"ban_units": ["unit warrior!"]}) == ["ban_units: unknown 'UNIT WARRIOR!'"]


def test_council_keeps_ai_deals_private_and_shows_you_your_offers():
    g, link = make_game()
    p1 = persona_for("LEADER_PEDRO", "Pedro II", "Brazil", "", "")
    reply = json.dumps({"thoughts": "buy her off", "messages": [], "journal": "",
                        "actions": [{"type": "propose_deal", "target": 0, "give_gold": 30,
                                     "ask": {"gold": 10}}]})
    agents = {1: LeaderAgent(1, p1, ScriptedClient([reply]))}
    c = Council(g, agents, human_id=0, summarize_after=0)
    c._carry_out(1, agents[1].decide(g, "turn", []))
    offer = [e for e in c.entries if e.get("kind") == "proposal"]
    assert offer and offer[-1]["text"] == "Pedro II offers you a deal: you get 30 gold and you give 10 gold."


def test_city_trades():
    g, link = make_game()
    res = g.apply(1, {"type": "propose_deal", "target": 0, "give": {"gold": 100}, "ask": {"cities": ["Newcastle"]}})
    assert res.ok
    [p] = g.pending_proposals(0)
    from civ_council.game import proposal_label
    assert proposal_label(p) == "a deal: you get 100 gold and you give the city of Newcastle"
    assert g.resolve_proposal(p.id, 0, accept=True).ok
    assert '{from=B, kind="CITIES", city="Newcastle", label="the city of Newcastle from Elizabeth I"}' in link.calls[-2]
    bad = g.apply(1, {"type": "propose_deal", "target": 0, "ask": {"cities": ['Newcastle"); Players[0]:Kill() --']}})
    assert not bad.ok and "odd city name" in bad.message


# ---- awareness: what a real player would notice -----------------------------------------
def test_briefing_shows_neighbors_troops_wonders_and_civs_own_reasons():
    g, link = make_game()
    pedro = g.snapshot(1)
    eliz = pedro["others"][0]
    assert eliz["cities_close_to_yours"] == ["their London is 4 tiles from your Sao Paulo"]
    assert "their_troops_near_your_cities" not in eliz and eliz["your_troops_near_their_cities"] == 3
    assert eliz["your_peoples_feelings"] == ["+4 Likes civs who have not captured capitals", "-4 other reasons"]
    assert eliz["techs_known"] == 10 and eliz["districts"] == 4 and pedro["you"]["districts"] == 2
    assert pedro["you"]["wonders"] == ["Pyramids (Rio)"] and pedro["you"]["age"] == "golden"
    assert "heading for a dark age" in pedro["you"]["era_score"]
    assert pedro["you"]["cities_losing_loyalty"] == ["Sao Paulo: loyalty 31, -6 a turn"]
    assert pedro["you"]["cities_about_to_flip"] == ["Sao Paulo joins the Free Cities in 5 turns"]
    assert g.snapshot(2)["you"]["age"] == "dark" and g.snapshot(0)["you"]["age"] == "normal"
    assert pedro["you"]["your_cities"] == ["Rio", "Sao Paulo"]
    assert pedro["you"]["your_resources"] == {"luxuries": {"RESOURCE_MARBLE": 2}, "strategic": {"RESOURCE_IRON": 5}}
    assert "your_resources" not in eliz                    # you know your own, not theirs
    eleanor = pedro["others"][1]
    assert eleanor["religion"] == "Catholicism" and "no cities within" in eleanor["neighbors"]
    me = g.snapshot(0)["others"][0]                       # the human sees no "feelings"
    assert me["their_troops_near_your_cities"] == 3 and "your_peoples_feelings" not in me


def test_awareness_events_settling_close_cities_changing_hands_and_wonders():
    g, link = make_game()
    g.end_turn()                                           # baseline
    link.world[0] = "TURN|61|0|-1757092276"
    link.aware = [l for l in AWARE_LINES if not l.startswith(("TROOPS", "CT|1|131073"))] + [
        "CT|0|131073|Sao Paulo|14|10",                     # Pedro's city is now England's
        "CT|1|196610|Brasilia|13|13", "NEAR|1|196610|0|65536|3",
        "WONDER|2|BUILDING_COLOSSUS|Colossus|Paris", "TROOPS|1|0|2|60"]
    texts = g.end_turn()
    assert "T61: Pedro II of Brazil founded Brasilia just 3 tiles from Elizabeth I of England's city London." in texts
    assert "T61: Sao Paulo changed hands: Pedro II of Brazil lost it to Elizabeth I of England." in texts
    assert "T61: Eleanor of Aquitaine of France completed the wonder Colossus in Paris." in texts
    assert "T61: Elizabeth I of England has 2 military units in or near Pedro II of Brazil's territory." in texts
    assert any("Brasilia" in e for e in g.snapshot(0)["recent_events"])   # England sees it (met Pedro)
    assert not any("Colossus" in e for e in g.snapshot(0)["recent_events"])   # England never met France


def test_two_leaders_offering_the_same_thing_are_only_friends_once():
    g, link = make_game()
    assert g.apply(1, {"type": "propose_friendship", "target": 0}).ok
    assert g.apply(0, {"type": "propose_friendship", "target": 1}).ok
    [p] = g.pending_proposals(0)
    assert g.resolve_proposal(p.id, 0, accept=True).ok
    assert g.pending_proposals(1) == []


# ---- empire reviews ---------------------------------------------------------------------
def test_empire_reviews_happen_when_civs_ai_is_choosing_or_every_few_turns():
    g, link = make_game()
    calls = []

    def brain(system, messages):
        calls.append(messages[0]["content"])
        return json.dumps({"thoughts": "ok", "orders": {"research": "TECH_ASTROLOGY", "civic": "CIVIC_MYSTICISM"}})

    c = Council(g, {1: LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(brain))},
                human_id=0, summarize_after=0, review_every=5)
    reviewed = []
    for turn in range(60, 70):
        link.world[0] = f"TURN|{turn}|0|-1757092276"
        c.on_new_turn()
        reviewed.append("EMPIRE REVIEW" in calls[-1])
    # turn 60: first look; then research+civic are the leader's own, so the next is 5 turns later
    assert reviewed == [True, False, False, False, False, True, False, False, False, False]
    assert g.orders[1] == {"research": "TECH_ASTROLOGY", "civic": "CIVIC_MYSTICISM"}


def test_a_decision_civs_ai_made_triggers_a_review_and_unset_research_retries_soon():
    g, link = make_game()
    calls = []
    c = Council(g, {1: LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"),
                                   ScriptedClient(lambda s, m: (calls.append(m[0]["content"]), "{}")[1]))},
                human_id=0, summarize_after=0, review_every=5)
    c.on_new_turn()                                       # turn 60: review, but no orders given
    assert "EMPIRE REVIEW" in calls[-1]
    link.world[0] = "TURN|61|0|-1757092276"
    c.on_new_turn()
    assert "EMPIRE REVIEW" not in calls[-1]               # not two in a row
    link.world[0] = "TURN|62|0|-1757092276"
    c.on_new_turn()
    assert "EMPIRE REVIEW" in calls[-1]                   # Civ's AI is still choosing its research
    link.world[0] = "TURN|63|0|-1757092276"
    link.world = [l.replace("REL|1|2|true", "REL|1|2|false") for l in link.world]   # peace nobody ordered
    c.on_new_turn()
    assert "EMPIRE REVIEW" in calls[-1] and "decided_without_you" in calls[-1]


def test_a_leader_sees_its_waiting_offers_and_cannot_send_the_same_deal_twice():
    g, link = make_game()
    assert g.apply(1, {"type": "propose_deal", "target": 0, "give_gold": 150, "ask": {"resources": {"RESOURCE_IRON": 10}}}).ok
    again = g.apply(1, {"type": "propose_deal", "target": 0, "give_gold": 150, "ask": {"resources": {"RESOURCE_IRON": 10}}})
    assert not again.ok and "already offered Elizabeth I a deal" in again.message
    agent = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(["{}"]))
    ctx = agent.build_context(g, "turn", [])
    assert ctx["your_offers_waiting_for_an_answer"] == [
        {"id": 1, "to": "Elizabeth I", "offer": "you give 150 gold; Elizabeth I gives Iron x10", "since_turn": 60}]


def test_offers_still_waiting_survive_a_restart(tmp_path):
    g, _ = make_game()
    assert g.apply(1, {"type": "propose_deal", "target": 0, "give_gold": 150, "ask": {"resources": {"RESOURCE_IRON": 10}}}).ok
    assert g.apply(1, {"type": "propose_friendship", "target": 0}).ok
    g.save_state(tmp_path / "s.json")
    g2, _ = make_game()
    g2.load_state(tmp_path / "s.json")
    from civ_council.game import proposal_label
    assert [proposal_label(p) for p in g2.pending_proposals(0)] == [
        "a deal: you get 150 gold and you give Iron x10", "friendship"]
    assert g2.apply(1, {"type": "make_peace", "target": 2}).proposal_id == 3     # ids carry on


def test_a_deal_that_falls_through_is_reported_to_both_sides():
    g, link = make_game()
    assert g.apply(1, {"type": "propose_deal", "target": 0, "give": {"resources": {"RESOURCE_IRON": 10}},
                       "ask": {"gold": 150}}).ok
    [p] = g.pending_proposals(0)
    link.replies["deal"] = "RES|no|Iron from Pedro II: only 5 available"
    res = g.resolve_proposal(p.id, 0, accept=True)
    assert not res.ok and "fell through: Iron from Pedro II: only 5 available" in res.message
    assert any("fell through" in e for e in g.snapshot(1)["recent_events"])     # Pedro learns it failed
    assert any("fell through" in e for e in g.snapshot(0)["recent_events"])


# ---- war targets, city sites, embassies -------------------------------------------------
def test_declaring_war_can_aim_the_army_at_a_city():
    g, link = make_game()
    pedro = g.snapshot(1)["others"][0]
    assert pedro["their_cities"] == ["London (4 tiles from your nearest city)"]
    link.replies["attack"] = "RES|ok|3 units ordered to attack London, rallying at Sao Paulo"
    res = g.apply(1, {"type": "declare_war", "target": 0, "target_city": "london"})
    assert res.ok and "declared war" in res.message
    assert "local A, B, CITY = 1, 0, 65536" in link.calls[-1]
    assert g.orders[1]["attack"]["city"] == "london"
    assert g.set_orders(1, {"attack": {"enemy": 0, "city": "Camelot"}}) == [
        "attack: Elizabeth I has no city called 'Camelot'"]


def test_city_sites_and_settling_waits_for_a_settler():
    g, link = make_game()
    sites = g.steering_options(1)["city_sites"]
    assert sites[0] == {"tile": 6291, "where": "5 tiles east of Beja", "continent": "Zealandia",
                        "resources_nearby": ["Whales", "Whales"], "features": ["coast"]}
    assert sites[1]["where"] == "5 tiles from Beja"          # an older reply without positions still reads
    assert "Iron" in sites[1]["resources_nearby"] and "fresh water" in sites[1]["features"]
    assert g.set_orders(1, {"settle": 999}) == ["settle: tile 999 isn't one of your city_sites"]
    link.replies["settle"] = "RES|wait|your nearest settler is 12 tiles away"
    assert g.set_orders(1, {"settle": 6291}) == ["settle: site saved; your nearest settler is 12 tiles away"]
    assert g.orders[1]["settle"] == 6291
    link.replies["settle"] = "RES|ok|founded Recife"
    assert any("settle: founded Recife" in n for n in g.reapply_orders())
    assert "settle" not in g.orders[1]
    assert any("founded Recife" in e for e in g.snapshot(0)["recent_events"])


def test_embassies_follow_civs_rules():
    g, link = make_game()
    link.replies["embassy"] = "RES|no|embassies need the Diplomatic Service civic"
    assert g.set_orders(1, {"embassy": 0}) == ["embassy: embassies need the Diplomatic Service civic"]
    link.replies["embassy"] = "RES|ok|embassy opened (50 gold)"
    assert g.set_orders(1, {"embassy": 0}) == ["embassy: embassy opened (50 gold)"]
    assert any("opened an embassy" in e for e in g.snapshot(0)["recent_events"])


# ---- World Congress --------------------------------------------------------------------
CONGRESS_LINES = [
    "WCNOW|Mercenary Companies|Producing military units with the chosen yield is +100% [ICON_Production] cost.|"
    "Producing military units with the chosen yield is -50% cost.|YIELD|Production;Gold;Faith",
    "WCNOW|Public Relations|The chosen player generates 100% more Grievances.|The chosen player generates 50% fewer "
    "Grievances.|PLAYER|Elizabeth I",
    "WCPAST|0|Mercenary Companies|Producing military units is -50% of the cost.|Food",
    "WCVOTE|0|0|0|-|", "WCVOTE|0|1|4|B|Production", "WCVOTE|0|2|6|B|Production",
    "WCPAST|1|Public Relations|The chosen player generates 50% fewer Grievances.|Elizabeth I",
    "WCVOTE|1|1|1|A|Pedro II",
]


def test_leaders_know_whats_before_the_world_congress_and_how_everyone_voted():
    g, link = make_game()
    assert "world_congress" not in g.snapshot(1)          # nothing to report: no key at all
    link.congress = list(CONGRESS_LINES)
    link.world[0] = "TURN|61|0|-1757092276"                 # read once a turn
    wc = g.snapshot(1)["world_congress"]
    now = wc["up_for_a_vote_now"]
    assert now[0]["resolution"] == "Mercenary Companies" and now[0]["targets"] == ["Production", "Gold", "Faith"]
    assert "[ICON" not in now[0]["option_a"] and "+100%" in now[0]["option_a"]
    assert now[1]["targets"] == ["Elizabeth I"]
    last = wc["last_session"]
    assert last[0]["votes"] == ["Pedro II: 4 votes for B (Production)",
                                "Eleanor of Aquitaine: 6 votes for B (Production)"]
    assert last[1]["passed"].endswith("(target: Elizabeth I)") and last[1]["votes"] == ["Pedro II: 1 vote for A (Pedro II)"]
    assert "can't change them" in wc["note"]


# ---- public chat is a stage ----------------------------------------------------------------
def test_public_messages_only_reach_leaders_who_met_you_and_get_public_and_private_replies():
    g, link = make_game()
    seen = {}

    def brain_for(aid):
        def brain(system, messages):
            seen[aid] = messages[0]["content"]
            return json.dumps({"thoughts": "posture in public, deal in private",
                               "messages": [{"to": "all", "text": "england is welcome here"},
                                            {"to": 0, "text": "between us: what do you want for your iron?"}]})
        return brain

    agents = {1: LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(brain_for(1))),
              3: LeaderAgent(3, persona_for("LEADER_GILGAMESH", "Gilgamesh", "Sumeria"), ScriptedClient(brain_for(3)))}
    c = Council(g, agents, human_id=0, summarize_after=0)
    c.say("all", "england comes in peace")
    assert 1 in seen and 3 not in seen                     # Gilgamesh never met England: he didn't hear it
    assert "everyone is watching" in seen[1] and "is a stage" in agents[1].system_prompt()
    assert "Most public replies need nothing more" in seen[1]
    world = [e["text"] for e in c.entries if e["channel"] == WORLD and e.get("kind") == "message"]
    private = [e["text"] for e in c.entries if e["channel"] == 1 and e.get("kind") == "message"]
    assert "england is welcome here" in world
    assert "between us: what do you want for your iron?" in private
    assert agents[3].memory.conversations.get("public", []) == []



def test_leaders_are_invited_to_speak_publicly_when_something_happens():
    g, _ = make_game()
    prompts = []
    agent = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"),
                        ScriptedClient(lambda s, m: (prompts.append(m[0]["content"]), "{}")[1]))
    agent.decide(g, "turn", [])
    assert "speak to everyone" in prompts[-1] and "not every turn" in prompts[-1]



def test_a_city_lost_to_loyalty_says_so_and_ages_are_tracked():
    g, link = make_game()
    g.end_turn()                                           # baseline
    link.world[0] = "TURN|61|0|-1757092276"
    link.aware = [l for l in AWARE_LINES if not l.startswith("CT|1|131073")] + ["CTX|30|the Free Cities|5|Sao Paulo"]
    link.ages = ["ERANEXT|0", "AGE|0|dark|10|24|36", "AGE|1|normal|12|30|42", "AGE|2|dark|5|20|30"]
    texts = g.end_turn()
    assert ("T61: Pedro II of Brazil's city Sao Paulo broke away and joined the Free Cities "
            "(its loyalty ran out).") in texts
    assert "T61: Elizabeth I of England entered a dark age." in texts
    assert "T61: Pedro II of Brazil's golden age is over." in texts


def test_leaders_are_told_how_to_win_and_who_conquered_what():
    g, link = make_game()
    pedro = g.snapshot(1)
    assert "ONLY way to win" in pedro["how_to_win"] and "hundreds of turns" in pedro["how_to_win"]
    assert pedro["civs_still_in_the_game"] == 4 and pedro["civs_you_havent_met"] == 1
    assert pedro["you"]["cities_you_conquered"] == ["Sao Paulo (from Elizabeth I)"]
    eliz = g.snapshot(0)
    assert eliz["you"]["your_cities_others_hold"] == ["Sao Paulo (held by Pedro II)"]
    assert eliz["others"][0]["cities_they_conquered"] == ["Sao Paulo (from Elizabeth I)"]


def test_victory_text_for_other_setups():
    assert "Domination" in cg.victory_text(["VICTORY_DEFAULT", "VICTORY_CONQUEST"])
    assert "ONLY way" not in cg.victory_text(["VICTORY_DEFAULT", "VICTORY_TECHNOLOGY"])
    assert "ONLY way" in cg.victory_text(["VICTORY_DEFAULT"])


def test_a_stalled_talk_model_falls_back_to_the_regular_one():
    g, _ = make_game()
    def stall(s, m):
        raise RuntimeError("timed out after 90 seconds")
    cheap = ScriptedClient(lambda s, m: '{"messages": [{"to": 0, "text": "here"}]}', name="haiku")
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), cheap,
                    talk_client=ScriptedClient(stall, name="sonnet"))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    c.say(1, "hey pedro")
    assert a.last_model == "haiku" and not a.last_decision.error
    assert any(e["kind"] == "message" and e["text"] == "here" for e in c.entries)


def test_an_offer_civ_refuses_is_reported_to_the_leader_and_to_you():
    g, _ = make_game()
    reply = '{"messages": [{"to": 0, "text": "alliance?"}], "actions": [{"type": "propose_alliance", "target": 0}]}'
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: reply))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    c.on_new_turn()
    assert g.pending_proposals(0) == []
    [err] = [e for e in c.entries if e["kind"] == "error" and e["channel"] == 1]
    assert "didn't go through" in err["text"] and "friendship first" in err["text"]
    ctx = a.build_context(g, "turn", [])
    assert "friendship first" in ctx["your_offers_that_failed_or_expired"][0]


def test_an_offer_that_expires_unanswered_is_reported():
    g, link = make_game()
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: "{}"))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    assert g.apply(1, {"type": "propose_friendship", "target": 0}).ok
    link.world[0] = "TURN|63|0|-1757092276"
    c.on_new_turn()
    assert g.pending_proposals(0) == []
    assert any(e["channel"] == 1 and "expired unanswered" in e["text"] for e in c.entries)
    assert "expired unanswered" in a.build_context(g, "turn", [])["your_offers_that_failed_or_expired"][0]


def test_your_chats_survive_a_restart(tmp_path):
    g, _ = make_game()
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"),
                    ScriptedClient(lambda s, m: '{"messages": [{"to": 0, "text": "coal for gold?"}]}'))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    c.on_new_turn()
    c.create_group("trade", [1])
    c.save_memories(tmp_path)
    g2, _ = make_game()
    c2 = Council(g2, {1: LeaderAgent(1, a.persona, ScriptedClient(lambda s, m: "{}"))}, human_id=0, summarize_after=0)
    c2.load_memories(tmp_path)
    assert any(e["channel"] == 1 and e["text"] == "coal for gold?" for e in c2.entries)
    assert "g1" in c2.groups
    c2._record(1, "message", "new", sender=1)
    assert c2.entries[-1]["id"] > max(e["id"] for e in c2.entries[:-1])


def test_old_chats_are_rebuilt_from_leader_memories_once(tmp_path):
    g, _ = make_game()
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: "{}"))
    a.memory.log("0", 50, "Pedro II", "coal for gold?")
    a.memory.log("0", 50, "Elizabeth I", "sure")
    a.memory.log("public", 51, "Pedro II", "hello world")
    a.memory.save(tmp_path / f"{a.persona.key}.json")
    c = Council(g, {1: LeaderAgent(1, a.persona, ScriptedClient(lambda s, m: "{}"))}, human_id=0, summarize_after=0)
    c.load_memories(tmp_path)
    msgs = [(e["channel"], e["sender"], e["text"]) for e in c.entries if e["kind"] == "message"]
    assert msgs == [(1, 1, "coal for gold?"), (1, 0, "sure"), ("world", 1, "hello world")]
    c.save_memories(tmp_path)
    c2 = Council(g, {1: LeaderAgent(1, a.persona, ScriptedClient(lambda s, m: "{}"))}, human_id=0, summarize_after=0)
    c2.load_memories(tmp_path)                       # chat.json exists now: loaded, not rebuilt twice
    assert [e["text"] for e in c2.entries] == [e["text"] for e in c.entries]


def test_leaders_see_fights_spies_treaty_timers_emergencies_and_unit_types():
    g, link = make_game()
    link.aware = [l if not l.startswith("TROOPS|1|0|") else "TROOPS|1|0|3|180|Battleship:2,Submarine:1"
                  for l in AWARE_LINES]
    if not any(l.startswith("TROOPS|1|0|") for l in link.aware):
        link.aware.append("TROOPS|1|0|3|180|Battleship:2,Submarine:1")
    link.ages = list(AGES_LINES) + [
        "FRLIMIT|30", "TREATY|0|1|DIPLO_STATE_DECLARED_FRIEND|50|-1", "TREATY|1|2|DIPLO_STATE_ALLIED|48|7",
        "SPYCAP|1|0|16|1|Leofflaed",
        "EMER|1|Military Emergency|2|12|true|0;1|Take back the city", "EMER|0|Military Emergency|2|12|true|0;1|Take back the city"]
    link.combat = ["CMB|60|-1|barbarians|Horseman|false||1|||false|Rio",
                   "CMB|60|-1|barbarians|Horseman|false||1|||false|Rio",
                   "CMB|60|1||Archer|false||-1|barbarians|Scout|true|",
                   "CMB|60|-1|||||1||Archer|false|"]                # no attacker info: left out
    pedro = g.snapshot(1)
    eliz = next(o for o in pedro["others"] if o["id"] == 0)
    assert eliz["their_troops_near_your_cities"] == 3 and eliz["which_of_their_units"] == "2 Battleship, 1 Submarine"
    assert eliz["friendship_ends_in_turns"] == 20                     # since 50, lasts 30, now turn 60
    assert next(o for o in pedro["others"] if o["id"] == 2)["alliance_ends_in_turns"] == 7
    assert pedro["you"]["spies_you_caught"] == ["Leofflaed (rank 1), sent by Elizabeth I"]
    assert g.snapshot(0)["you"]["your_spies_caught_by_others"] == ["Pedro II holds your spy Leofflaed"]
    assert pedro["you"]["fighting_recently"] == [
        "T60: barbarians' Horseman attacked your city Rio (x2)",
        "T60: your Archer attacked barbarians' Scout and destroyed it"]
    [em] = pedro["you"]["emergencies"]
    assert em.startswith("Military Emergency against Eleanor of Aquitaine; you're a member; 12 turns left")


def test_new_spy_captures_emergencies_and_returning_cities_become_events():
    g, link = make_game()
    link.aware = list(AWARE_LINES) + ["CTX|30|the Free Cities|5|Ur"]
    g.end_turn()                                           # baseline
    link.world[0] = "TURN|61|0|-1757092276"
    link.aware = list(AWARE_LINES) + ["CT|1|999|Ur|10|10"]
    link.ages = list(AGES_LINES) + ["SPYCAP|1|0|16|1|Leofflaed", "EMER|1|Military Emergency|2|12|true|1|Retake it"]
    texts = g.end_turn()
    assert "T61: Pedro II of Brazil caught a spy sent by Elizabeth I of England." in texts
    assert "T61: Ur left the Free Cities and joined Pedro II of Brazil." in texts
    assert "T61: Emergency declared: Military Emergency against Eleanor of Aquitaine of France." in texts


def test_hex_directions_match_civs_grid():
    # probe 7, from London at 51,14: NE is 51,15, NW 50,15, E 52,14, SE 51,13, SW 50,13
    for x, y, way in ((51, 15, "northeast"), (50, 15, "northwest"), (52, 14, "east"), (51, 13, "southeast"),
                      (50, 13, "southwest"), (50, 14, "west")):
        assert cg.hex_step(51, 14, x, y) == (1, way)
    assert cg.hex_step(51, 14, 51, 20) == (6, "north")
    assert cg.hex_step(1, 10, 104, 10, width=106) == (3, "west")      # the short way round a wrapping map


def test_leaders_know_which_continent_everyone_is_on():
    g, link = make_game()
    link.aware = [l + ("|Africa" if l.startswith("CT|0|") else "|Lemuria" if l.startswith("CT|1|") else "|Zealandia")
                  if l.startswith("CT|") else l for l in AWARE_LINES] + ["GRID|106|66|true"]
    pedro = g.snapshot(1)
    assert pedro["you"]["your_cities_by_continent"] == {"Lemuria": ["Rio", "Sao Paulo"]}
    eliz = next(o for o in pedro["others"] if o["id"] == 0)
    assert eliz["their_continents"] == {"Africa": 1}
    assert eliz["their_cities"][0].startswith("London (Africa, ")


def test_a_leader_can_pin_a_place_on_your_map_and_keeps_only_a_few(tmp_path):
    g, link = make_game()
    n = iter(range(10))
    reply = lambda s_, m: ('{"messages": [{"to": 0, "text": "I am settling here, stay off it", '  # noqa: E731
                           f'"pin": {6291 + next(n)}, "pin_label": "my next city"}}]}}')
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(reply))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    c.on_new_turn()
    assert any(e["channel"] == 1 and e["text"] == "📍 Pedro II marked a spot on your map: my next city"
               for e in c.entries)
    assert 'local NAME = "Pedro II: my next city"' in link.pin_calls[0] and "local DROP = {}" in link.pin_calls[0]
    for _ in range(3):
        g.pin(1, 7000, "another")
    assert len(g.pins[1]) == cg.MAX_PINS
    assert '{1, "Pedro II: my next city"}' in link.pin_calls[-1]       # the oldest one goes, by id and name
    assert g.pin(1, "London", 'stay "out"]]').ok and 'Pedro II: stay out' in link.pin_calls[-1]
    assert not g.pin(1, "Atlantis", "x").ok
    link.replies["pin"] = "RES|no|you already have a pin of your own there"
    assert g.pin(1, 7000, "x").message == "you already have a pin of your own there"
    g.save_state(tmp_path / "s.json")
    g2, _ = make_game()
    g2.load_state(tmp_path / "s.json")
    assert g2.pins == g.pins


def test_a_city_civs_ai_founded_is_flagged_but_one_the_leader_chose_is_not():
    g, link = make_game()
    link.aware = [l + ("|Africa" if l.startswith("CT|0|") else "|Lemuria") if l.startswith("CT|") else l
                  for l in AWARE_LINES]
    g.end_turn()                                           # baseline
    link.replies["settle"] = "RES|ok|founded Recife"
    g.orders[1] = {"settle": 6291}
    g.reapply_orders()                                     # the leader's own pick
    link.world[0] = "TURN|61|0|-1757092276"
    link.aware = link.aware + ["CT|1|200|Recife|30|10|Lemuria", "CT|1|201|Manaus|11|11|Africa",
                               "NEAR|1|201|0|65536|2"]
    g.end_turn()
    flagged = g.steering_options(1)["decided_without_you"]
    assert len(flagged) == 1 and flagged[0]["to_overrule"] == "can't be undone"
    assert flagged[0]["what"].startswith("Civ's built-in AI founded your new city Manaus "
                                         "(on Africa; 2 tiles from Elizabeth I's London; a continent you share with Elizabeth I)")


def test_war_can_be_declared_right_after_a_peace_and_says_so():
    g, link = make_game()
    link.fresh_peace = True                               # InGame says the peace is still inside Civ's lock
    link.replies["declare_war"] = "RES|ok|early"         # ...which the house rule lifts
    res = g.apply(0, {"type": "declare_war", "target": 1})
    assert res.ok and "breaking the peace they had only just made" in res.message
    ask, lua = link.calls[-2], link.calls[-1]
    assert "GetAtWarChangeTurn(B)" in ask and "GetMinPeaceDuration()" in ask      # read on the UI side (probe 8)
    assert "local A, B = 0, 1\nlocal FRESH = true" in lua and "HasDeclaredFriendship(B)" in lua
    link.fresh_peace = False
    g.apply(0, {"type": "declare_war", "target": 1})
    assert "local FRESH = false" in link.calls[-1]
    link.replies["declare_war"] = "RES|no|Civ 6 doesn't allow that war right now"   # any other block stands
    assert not g.apply(1, {"type": "declare_war", "target": 0}).ok


def test_peace_with_terms_signs_peace_and_terms_together_or_the_war_goes_on():
    g, link = make_game()                                  # Pedro (1) and Eleanor (2) are at war
    res = g.apply(2, {"type": "propose_deal", "target": 1, "ask": {"cities": ["Sao Paulo"]}})
    assert res.ok and "peace, on these terms" in res.message
    [p] = g.pending_proposals(1)
    from civ_council.game import proposal_label
    assert proposal_label(p) == "peace, if you give the city of Sao Paulo"
    link.replies["agreed_peace"] = "RES|ok|early"
    done = g.resolve_proposal(p.id, 1, accept=True)
    assert done.ok and "made peace: Pedro II gives the city of Sao Paulo" in done.message
    kinds = [c for c in link.calls[-3:]]
    assert any(cg.ACTION_LUA["agreed_peace"] in c for c in kinds) and any("EnactWorkingDeal" in c for c in kinds)

    g, link = make_game()
    assert g.apply(1, {"type": "propose_deal", "target": 2, "give": {"gold": 50}}).ok   # a "gift" at war waits: it's peace terms
    [p] = g.pending_proposals(2)
    link.replies["agreed_peace"] = "RES|ok|peace made"
    link.replies["deal"] = "RES|no|they can't trade that"
    failed = g.resolve_proposal(p.id, 2, accept=True)
    assert not failed.ok and "the terms couldn't be carried out" in failed.message and "the war goes on" in failed.message
    assert cg.ACTION_LUA["force_war"] in link.calls[-1]


def test_trade_options_at_war_are_peace_terms():
    g, link = make_game()
    link.aware = [l + "|Lemuria|" + ("true" if l.startswith("CT|1|65536|") else "false") if l.startswith("CT|") else l
                  for l in AWARE_LINES]
    real = link.run
    link.run = lambda lua, ui=False: (["TRW|2"] if 'print("TR|"' in lua else real(lua, ui))
    opts = g.trade_options(1)["Eleanor of Aquitaine"]
    assert opts["at_war"] and "peace offer" in opts["note"]
    assert opts["you_can_give"]["cities"] == ["Sao Paulo"]             # never the capital (Rio)
    assert opts["you_can_give"]["gold"] == g.civ(1).gold


def test_every_briefing_field_reaches_the_leader():
    g, link = make_game()                                  # AGES_LINES has VIC|VICTORY_DEFAULT
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: "{}"))
    snap = g.snapshot(1)
    ctx = a.build_context(g, "turn", [])
    regrouped = {"world_wars": "wars", "world_alliances": "alliances", "recent_events": "recent_events"}
    for key in snap:
        if key in regrouped:
            assert regrouped[key] in ctx["world"], key
        else:
            assert key in ctx, f"{key} never reaches the leader"
    assert "ONLY way to win" in ctx["how_to_win"] and "city_states" in ctx and "civs_still_in_the_game" in ctx
    assert list(ctx).index("how_to_win") < list(ctx).index("you")    # up front, where it's read first


def test_a_refused_routine_call_is_retried_with_the_other_model():
    g, _ = make_game()
    def refuse(s, m):
        raise RuntimeError("claude -p error: API Error: Haiku's safeguards flagged this message.")
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(refuse, name="haiku"),
                    talk_client=ScriptedClient(lambda s, m: '{"thoughts": "fine"}', name="sonnet"))
    d = a.decide(g, "turn", [], with_human=False)
    assert not d.error and d.thoughts == "fine" and a.last_model == "sonnet"


def test_a_flagged_request_is_saved_for_a_look(tmp_path, monkeypatch):
    import json as _json
    import subprocess
    from civ_council import llm
    monkeypatch.setattr(llm, "FLAGGED_DIR", tmp_path)
    flagged = {"is_error": True, "result": "API Error: Haiku 5.5's safeguards flagged this message. Details: x"}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, _json.dumps(flagged), ""))
    client = llm.ClaudeCodeClient(model="haiku", binary="/bin/true")
    try:
        client.complete("SYSTEM TEXT", [{"role": "user", "content": "THE PROMPT"}])
        raise AssertionError("should have failed")
    except RuntimeError as e:
        assert "safeguards flagged" in str(e) and str(tmp_path) in str(e)
    [saved] = list(tmp_path.iterdir())
    text = saved.read_text()
    assert "SYSTEM TEXT" in text and "THE PROMPT" in text and "Details: x" in text


def test_briefings_show_economy_army_cities_grievances_era_and_barbarians():
    g, link = make_game()
    link.aware = list(AWARE_LINES) + [
        "ECON|1|900|270|360|500|60|8", "ECON|0|1200|300|400|300|20|7",
        "ARMY|1|Tank:3,Infantry:2|1:4,0:1", "ARMY|0|Battleship:4|-1:4",
        "CITY2|1|65536|14|98|50|200|120|400", "CITY2|0|65536|20|120|0|200|0|400",
        "CAMP|1|131073|4", "BARB|1|131073", "BARB|1|131073", "MODON|gift_it_to_me|true"]
    link.ages = list(AGES_LINES) + ["ERANAME|7|Modern Era", "ERANAME|8|Atomic Era", "FAVOR|1|9", "GRIEV|1|0|114",
                                    "CITYX|1|65536|2|5|1|Tank", "CITYX|1|131073|6|3|0|"]
    pedro = g.snapshot(1)
    you = pedro["you"]
    assert you["era"] == "Atomic Era"
    assert you["economy_per_turn"] == {"gold": 630, "gold_before_upkeep": 900, "upkeep": 270, "science": 360,
                                       "culture": 500, "faith": 60, "diplomatic_favor": 9}
    assert you["your_army"] == "3 Tank, 2 Infantry"
    assert you["where_your_army_is"] == {"in your own land": 4, "in Elizabeth I's land": 1}
    rio = next(d for d in you["your_city_details"] if d.startswith("Rio:"))
    assert "UNDER ATTACK: garrison damaged 50/200, walls damaged 120/400" in rio and "-3 amenities (unhappy)" in rio
    assert "war weariness costs 1 amenities" in rio and "building Tank" in rio
    assert you["barbarians_near_you"] == ["a barbarian camp 4 tiles from Sao Paulo", "2 barbarian units next to Sao Paulo"]
    eliz = next(o for o in pedro["others"] if o["id"] == 0)
    assert eliz["their_army"] == "4 Battleship" and eliz["era"] == "Modern Era" and eliz["science_per_turn"] == 400
    assert eliz["grievances"] == "you hold 114 grievances against them"
    assert next(o for o in g.snapshot(0)["others"] if o["id"] == 1)["grievances"] == "they hold 114 grievances against you"
    assert any(c.startswith("London (") and "size 20" in c for c in eliz["their_cities"])
    assert "economy_per_turn" not in eliz and "where_your_army_is" not in eliz        # you don't see their books
    assert g.steering_options(1)["gift_units"]["you_can_gift"] == {"Tank": 3, "Infantry": 2}


def test_leaders_gift_units_through_the_mod():
    g, link = make_game()
    res = g.apply(1, {"type": "gift_units", "target": 0, "unit": "Tank", "count": 2})
    assert res.ok and res.message == "Pedro II of Brazil gifted 2 Tank to Elizabeth I of England."
    lua = link.calls[-1]
    assert 'local A, B, NAME, N = 1, 0, "Tank", 2' in lua and "GameEvents.PopulateUnit.Call" in lua
    assert not g.apply(1, {"type": "gift_units", "target": 0, "unit": 'Tank"); os.exit() --'}).ok
    link.replies["gift"] = "RES|no|the Gift It To Me mod isn't active in this game"
    assert "isn't active" in g.apply(1, {"type": "gift_units", "target": 0, "unit": "Tank"}).message
    from civ_council.game import MockGame
    assert not MockGame(["a", "b"]).apply(1, {"type": "gift_units", "target": 0, "unit": "Tank"}).ok


def test_nukes_policies_governors_spies_great_people_routes_deals_city_states_and_headlines():
    g, link = make_game()
    link.aware = [l + ("|Lemuria|true" if l.startswith("CT|1|65536|") else "|Lemuria|false") if l.startswith("CT|") else l
                  for l in AWARE_LINES] + ["CITY2|1|65536|14|98|50|200|0|400"]
    link.ages = list(AGES_LINES) + [
        "WMD|0|Nuclear Device|2", "GOV|0|Fascism", "GOV|1|Democracy", "POLICY|1|Liberalism", "POLICY|1|Ecommerce",
        "ROUTE|1|1", "ROUTE|1|0", "ROUTE|1|0", "ROUTE|0|1", "GOVERNOR|1|Pingala|Rio", "GOVERNOR|1|Amani|",
        "SPY|1|London|0|Steal Tech Boost", "SPY|1|Rio|1|", "GPCLASS|5|Great Scientist", "GPP|1|5|1110",
        "GPNOW|5|Ada Lovelace|1320", "GPNOW|6|Somebody|900", "CSTYPE|5|scientific", "ENVOY|5|0|6", "ENVOY|5|1|2",
        "FRLIMIT|30", "TREATY|0|1|DIPLO_STATE_DECLARED_FRIEND|32|-1"]
    pedro = g.snapshot(1)
    you = pedro["you"]
    assert you["your_policy_cards"] == ["Liberalism", "Ecommerce"]
    assert you["your_governors"] == ["Pingala in Rio", "Amani (not assigned to a city)"]
    assert you["your_spies"] == ["in Elizabeth I's city London: Steal Tech Boost", "in your city Rio: no mission"]
    assert you["great_people_up_for_grabs"] == ["Great Scientist Ada Lovelace: costs 1320, you have 1110"]
    assert you["your_trade_routes"] == {"within your empire": 1, "to Elizabeth I": 2}
    assert you["foreign_trade_routes_into_your_cities"] == {"from Elizabeth I": 1}
    eliz = next(o for o in pedro["others"] if o["id"] == 0)
    assert eliz["nuclear_weapons"] == {"Nuclear Device": 2} and eliz["government"] == "Fascism"
    assert eliz["trade_routes_between_you"] == 3
    venice = next(cs for cs in pedro["city_states"] if cs["name"] == "Venice")
    assert venice["type"] == "scientific" and venice["envoys"] == {"Elizabeth I": 6, "Pedro II": 2}
    h = pedro["headlines"]
    assert "Your city Rio is under attack." in h
    assert "Elizabeth I has nuclear weapons: 2 Nuclear Device." in h
    assert any(x.startswith("Your friendship with Elizabeth I runs out in 2 turns") for x in h)
    # routine turns leave the slow books out, reviews and talks with you keep them
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: "{}"))
    quick, full = a.build_context(g, "turn", [], detail=False), a.build_context(g, "turn", [], detail=True)
    assert "your_policy_cards" not in quick["you"] and "your_policy_cards" in full["you"]
    assert "envoys" not in next(cs for cs in quick["city_states"] if cs["name"] == "Venice")
    assert list(quick).index("headlines") < list(quick).index("you")


def test_new_nukes_government_changes_and_disasters_become_events():
    g, link = make_game()
    g.end_turn()                                           # baseline
    link.world[0] = "TURN|61|0|-1757092276"
    link.ages = list(AGES_LINES) + ["WMD|0|Nuclear Device|1", "GOV|1|Fascism"]
    g.end_turn()
    link.world[0] = "TURN|62|0|-1757092276"
    link.ages = list(AGES_LINES) + ["WMD|0|Nuclear Device|1", "GOV|1|Democracy"]
    link.disasters = ["DIS|62|Flood|40|11"]
    texts = g.end_turn()
    assert "T62: Pedro II of Brazil switched government to Democracy." in texts
    assert "T62: Flood struck near Pedro II of Brazil's city Rio." in texts


def test_an_early_friendship_renewal_is_queued_and_sent_when_it_lapses(tmp_path):
    g, link = make_game()
    link.world = [l for l in link.world if not l.startswith("REL|0|1")] + ["REL|0|1|false|true|false"]   # friends
    g.end_turn()                                           # baseline
    res = g.apply(1, {"type": "propose_friendship", "target": 0})
    assert res.ok and "as soon as the current one lapses" in res.message and g.pending_proposals(0) == []
    again = g.apply(1, {"type": "propose_friendship", "target": 0})      # leaders kept re-queuing it every turn
    assert not again.ok and "already pending" in again.message and g.renewals == {(1, 0): 60}
    [you] = [o for o in g.snapshot(1)["others"] if o["id"] == 0]
    assert you["your_friendship_renewal"].startswith("queued")      # so the leader knows it's sent already
    g.save_state(tmp_path / "s.json")
    g2, link2 = make_game()
    g2.load_state(tmp_path / "s.json")
    assert g2.renewals == {(1, 0): 60}
    link.world = [l for l in link.world if not l.startswith("REL|0|1")]        # it lapsed: back to peace
    link.world[0] = "TURN|61|0|-1757092276"
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: "{}"))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    c.on_new_turn()
    [p] = g.pending_proposals(0)
    assert p.kind == "propose_friendship" and p.sender == 1 and not g.renewals
    assert any(e["kind"] == "proposal" and e["channel"] == 1 and "renewing your friendship" in e["text"] for e in c.entries)


def test_a_queued_renewal_to_you_is_one_quiet_line(tmp_path):
    g, link = make_game()
    link.world = [l for l in link.world if not l.startswith("REL|0|1")] + ["REL|0|1|false|true|false"]   # friends
    g.end_turn()
    turn = {"n": 0}
    def leader(system, msg):
        turn["n"] += 1
        return json.dumps({"actions": [{"type": "propose_friendship", "target": 0}]}) if turn["n"] <= 2 else "{}"
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(leader))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    c._decide(1, "turn")
    c._decide(1, "turn")
    mine = [e for e in c.entries if e["channel"] == 1]
    assert [e["kind"] for e in mine] == ["info"] and "as soon as the current one lapses" in mine[0]["text"]
    assert g.pending_proposals(0) == [] and g.renewals == {(1, 0): 60}


def test_briefings_use_the_leader_ribbons_numbers():
    g, link = make_game()
    link.aware = list(AWARE_LINES) + ["ECON|1|900|270|360|500|60|8|590", "ECON|0|1200|300|400|300|20|7|3932"]
    link.ages = list(AGES_LINES) + ["RIBBON|0|1479", "RIBBON|1|327", "RIBBON|5|40"]
    pedro = g.snapshot(1)
    eliz = next(o for o in pedro["others"] if o["id"] == 0)
    assert eliz["military"] == 1479 and eliz["gold"] == g.civ(0).gold and eliz["faith"] == 3932
    assert eliz["military_with_city_states"] == 1479 + 40                 # Venice (5) backs Elizabeth, ribbon scale too
    assert pedro["you"]["military"] == 327 and pedro["you"]["faith"] == 590
    assert next(cs for cs in pedro["city_states"] if cs["name"] == "Venice")["military"] == 40


def test_leader_messages_and_offers_to_you_also_show_in_civs_notifications(tmp_path):
    g, link = make_game()
    reply = ('{"messages": [{"to": 0, "text": "hello liz"}, {"to": 2, "text": "psst eleanor"}], '
             '"actions": [{"type": "propose_friendship", "target": 0}]}')
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: reply))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    c.on_new_turn()
    heads = [n for n in link.notes]
    assert any('local HEAD = "Pedro II writes to you (Civ Council)"' in n and '"hello liz"' in n for n in heads)
    assert any('"Offer from Pedro II (Civ Council)"' in n for n in heads)
    assert not any("psst" in n for n in heads)                 # to another leader: not yours to see
    c.civ_notify = False
    c.save_memories(tmp_path)
    c2 = Council(g, {1: a}, human_id=0, summarize_after=0)
    c2.load_memories(tmp_path)
    assert c2.civ_notify is False
    link.notes.clear()
    c2._record(1, "message", "quiet now", sender=1)
    assert link.notes == []
    assert cg._lua_quote('a "q"\nb\\c') == '"a \\"q\\" b\\\\c"'   # one line, quotes and backslashes escaped


def test_civs_own_ai_popups_are_closed_and_reported(tmp_path):
    g, link = make_game()
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: "{}"))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    g.end_turn()                                           # baseline (installs the hook)
    assert "local QUIET, BLOCK = true, true" in link.quiet_calls[-1]
    assert 'DiplomacyManager.CloseSession(sid)' in link.quiet_calls[-1] and 't:find("FIRST_MEET")' in link.quiet_calls[-1]
    link.quieted = ["QD|61|1|MAKE_DEAL", "QD|61|1|MAKE_DEAL", "QD|61|1|KUDO", "QD|61|1|YOU_OPENED"]
    link.world[0] = "TURN|61|0|-1757092276"
    c.on_new_turn()
    [note] = [e for e in c.entries if e["kind"] == "info" and e["channel"] == 1 and "Civ's own AI" in e["text"]]
    assert note["text"].startswith("Civ's own AI sent you a trade offer, praise for Pedro II; Civ Council closed it")
    g.set_quiet_ai(False)
    assert "local QUIET, BLOCK = false, true" in link.quiet_calls[-1]
    g.set_block_screen(False)
    assert "local QUIET, BLOCK = false, false" in link.quiet_calls[-1]
    c.save_memories(tmp_path)
    g2, _ = make_game()
    c2 = Council(g2, {1: a}, human_id=0, summarize_after=0)
    c2.load_memories(tmp_path)
    assert g2.quiet_ai is False and g2.block_screen is False


def test_the_browser_gets_what_civs_leader_screen_shows():
    from civ_council.web import Server
    g, link = make_game()
    link.aware = list(AWARE_LINES) + ["ECON|1|900|270|360|500|60|8|590"]
    link.ages = list(AGES_LINES) + ["GOV|1|Democracy", "WMD|1|Nuclear Device|3", "FRLIMIT|30"]
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: "{}"))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    c.offer_pact(0, 1, "Border", "Nobody settles past the river.", 20)
    c.answer_pact(1, "pact-1", True)
    st = Server(c).state(0)
    d = next(l for l in st["leaders"] if l["id"] == 1)["diplomacy"]
    assert d["agenda"] and d["government"] == "Democracy" and d["nuclear_weapons"] == {"Nuclear Device": 3}
    assert d["relationships"] == {}          # his war with Eleanor stays hidden: you have not met her
    assert d["pacts_with_you"] == ["Border: Nobody settles past the river. (20 turns left)"]
    assert d["science_per_turn"] == 360
    assert st["block_civ_screen"] is True and st["quiet_civ_ai"] is True


def test_leaders_set_city_focus_and_recruit_great_people():
    g, link = make_game()
    notes = g.set_orders(1, {"city_focus": {"city": "Rio", "focus": "production"}})
    assert notes == ["city focus: Rio focuses on production"]
    assert 'local P, CITY, FAVOR = 1, 65536, "YIELD_PRODUCTION"' in link.calls[-1]
    assert g.orders[1]["focus"] == {"65536": "PRODUCTION"}
    assert "must be one of" in g.set_orders(1, {"city_focus": {"city": "Rio", "focus": "vibes"}})[0]
    g.reapply_orders()                                     # Civ's AI may reset it: put it back each turn
    assert any('FAVOR = 1, 65536, "YIELD_PRODUCTION"' in c for c in link.calls[-3:])
    link.replies["recruit"] = "RES|wait|not enough great person points for Ada Lovelace yet"
    assert g.set_orders(1, {"recruit": "Ada Lovelace"}) == ["recruit: not enough great person points for Ada Lovelace yet"]
    assert g.orders[1]["recruit"] == "Ada Lovelace"
    link.replies["recruit"] = "RES|ok|recruited Ada Lovelace"
    assert any("recruit: recruited Ada Lovelace" in n for n in g.reapply_orders())
    assert "recruit" not in g.orders[1]
    assert "Pedro II of Brazil recruited a great person: Ada Lovelace." in [t.split(": ", 1)[1] for t, _ in g.events]


def test_an_aid_request_is_a_race_to_help_not_a_plot():
    g, link = make_game()
    g.end_turn()                                           # baseline
    link.world[0] = "TURN|61|0|-1757092276"
    link.ages = list(AGES_LINES) + ["EMER|1|Aid Request|1|20|true|0;2|", "EMER|0|Aid Request|1|20|true|0;2|"]
    texts = g.end_turn()
    assert "T61: Aid Request for Pedro II of Brazil: civs race to help them." in texts
    [mine] = g.snapshot(1)["you"]["emergencies"]
    assert mine.startswith("Aid Request for you") and "in your favor" in mine and "against" not in mine
    [yours] = g.snapshot(0)["you"]["emergencies"]
    assert yours.startswith("Aid Request for Pedro II") and "race the others to help them" in yours


def test_civs_historic_moments_fill_the_story_back_to_turn_1():
    g, link = make_game()
    link.moments = ["MOM|1|18|937298822|2|The rulers of Fez bow to Brazil, who became their first Suzerain.",
                    "MOM|1|11|-1672407476|1|Villagers bring gifts as they join our civilization.",   # too common
                    "MOM|0|41|815813506|1|Wary welcomes mark our meeting with the people of Brazil."]
    assert g.historic_moments() == [(18, 1, 2, "The rulers of Fez bow to Brazil, who became their first Suzerain."),
                                    (41, 0, 1, "Wary welcomes mark our meeting with the people of Brazil.")]
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: "{}"))
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    c.history = [{"turn": 30, "kind": "war", "text": "x", "civs": []}]
    assert c.sync_moments() == 2 and c.sync_moments() == 0                  # once each
    assert [(h["turn"], h["kind"]) for h in c.history] == [(18, "moment"), (30, "war"), (41, "moment")]
    first = c.history[0]
    assert first["by"] == 1 and first["civs"] == [1] and first["score"] == 2
