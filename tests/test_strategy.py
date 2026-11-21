"""Grand strategies: written by a stronger model, rarely; followed turn by turn through a checklist."""
from __future__ import annotations

import json

from civ_council.agent import LeaderAgent
from civ_council.council import Council
from civ_council.llm import ScriptedClient
from civ_council.personas import persona_for

from tests.test_civ6_game import make_game

PLAN = json.dumps({"assessment": "Rome is strong, England is far.", "grand_strategy": "Grow, then take Rome's coast.",
                   "objectives": [{"id": "o1", "goal": "Take Ostia", "by_turn": 120, "why": "coast"}],
                   "checklist": [{"id": "c1", "task": "Make friends with Elizabeth", "for": "o1", "when": "now"},
                                 {"id": "c2", "task": "Build an army near Ostia", "for": "o1", "when": "soon"}],
                   "replan_if": ["Rome makes peace with everyone"]})


def plans_made(plans):
    return [c for c in plans.calls if "Why now:" in c[1][0]["content"]]


def setup(routine='{"checklist": [{"id": "c1", "status": "done", "note": "she agreed"}]}'):
    g, link = make_game()
    plans = ScriptedClient(lambda s, m: PLAN, name="opus")
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"),
                    ScriptedClient(lambda s, m: routine, name="haiku"))
    a.strategy_client = plans
    c = Council(g, {1: a}, human_id=0, summarize_after=0)
    return g, link, a, plans, c


def test_a_leader_gets_a_grand_strategy_and_works_through_its_checklist():
    g, link, a, plans, c = setup()
    c.on_new_turn()
    assert len(plans_made(plans)) == 1 and "you have no grand strategy yet" in plans_made(plans)[0][1][0]["content"]
    st = a.memory.strategy
    assert st["grand_strategy"] == "Grow, then take Rome's coast." and st["model"] == "opus"
    [mind] = [e for e in c.entries if e["kind"] == "strategy"]
    assert mind["channel"] == "minds" and mind["plan"] == "Take Ostia"
    assert next(x for x in st["checklist"] if x["id"] == "c1")["status"] == "done"     # the routine turn ticked it
    ctx = a.build_context(g, "turn", [])["your_grand_strategy"]
    assert [x["id"] for x in ctx["checklist"]] == ["c2"] and ctx["done_so_far"] == ["Make friends with Elizabeth"]
    # the next turns don't rewrite it ...
    link.world[0] = "TURN|61|0|-1757092276"
    c.on_new_turn()
    assert len(plans_made(plans)) == 1
    # ... but every 25 turns, they do
    link.world[0] = "TURN|86|0|-1757092276"
    c.on_new_turn()
    assert len(plans_made(plans)) == 2 and "26 turns since" in plans_made(plans)[1][1][0]["content"]


def test_big_news_or_a_request_brings_a_new_strategy_but_not_too_often():
    g, link, a, plans, c = setup(routine='{"new_strategy_needed": "Rome just allied everyone"}')
    c.on_new_turn()                                       # first plan on turn 60
    link.world[0] = "TURN|62|0|-1757092276"
    c.on_new_turn()
    assert len(plans_made(plans)) == 1                    # a request needs 8 turns since the last plan
    assert c._strategy_due(1, ["T64: Pedro II of Brazil declared war on Eleanor!"]) == ""   # too fresh (2 turns)
    link.world[0] = "TURN|64|0|-1757092276"
    assert c._strategy_due(1, ["T64: Elizabeth I of England and Pedro II of Brazil are at war!"]).startswith("big news")
    link.world[0] = "TURN|69|0|-1757092276"
    assert c._strategy_due(1, []).startswith("you asked for it: Rome just allied everyone")


def test_calls_are_recorded_for_the_usage_meter():
    g, link, a, plans, c = setup()
    plans.last_usage = None
    c.on_new_turn()
    purposes = [(u["leader"], u["model"], u["purpose"]) for u in c.usage]
    assert ("Pedro II", "opus", "grand strategy") in purposes and ("Pedro II", "haiku", "turn") in purposes
    assert all("seconds" in u and u["turn"] == 60 for u in c.usage)


def test_the_usage_meter_sums_calls_per_turn_and_model(tmp_path):
    from civ_council.web import Server
    g, link, a, plans, c = setup()
    c.usage += [{"turn": 60, "leader": "Pedro II", "model": "opus", "purpose": "grand strategy", "seconds": 26.2,
                 "input_tokens": 15541, "output_tokens": 2260, "cost_usd": 0.17},
                {"turn": 60, "leader": "Pedro II", "model": "haiku", "purpose": "turn", "seconds": 4.0,
                 "input_tokens": 9000, "output_tokens": 300, "cost_usd": 0.01}]
    c.turn_seconds[60] = 31.0
    u = Server(c).state(0)["usage"]
    [t] = u["turns"]
    assert t["calls"] == 2 and t["wall_seconds"] == 31.0 and t["models"] == {"opus": 1, "haiku": 1}
    assert t["tokens_in"] == 24541 and abs(t["cost"] - 0.18) < 1e-9
    assert u["rows"][0] == {"for": "grand strategy", "model": "opus", "calls": 1, "cost": 0.17}
    assert u["game"]["calls"] == 2 and u["game"]["cost"] == 0.18 and u["game"]["first_turn"] == 60
    c.save_memories(tmp_path)
    c2 = Council(g, {1: a}, human_id=0, summarize_after=0)
    c2.load_memories(tmp_path)
    assert len(c2.usage) == 2 and c2.turn_seconds == {60: 31.0}


def test_the_game_total_survives_old_calls_being_dropped(tmp_path, monkeypatch):
    import civ_council.council as council_mod
    monkeypatch.setattr(council_mod, "USAGE_KEEP", 3)
    g, link, a, plans, c = setup()
    log = a.usage_log
    for t in range(1, 6):   # 5 turns, one call each: more than we keep one by one
        c.usage.append({"turn": t, "leader": "Pedro II", "model": "haiku", "purpose": "turn", "seconds": 2.0,
                        "input_tokens": 100, "output_tokens": 10, "cost_usd": 0.01})
    c.save_memories(tmp_path)
    assert len(c.usage) == 3 and c.usage is log            # trimmed in place: the leader still logs here
    assert c.usage_archived["calls"] == 2
    c2 = Council(g, {1: a}, human_id=0, summarize_after=0)
    c2.load_memories(tmp_path)
    total = c2.usage_totals()
    assert total["calls"] == 5 and abs(total["cost"] - 0.05) < 1e-9 and total["first_turn"] == 1
    assert total["rows"]["turn|haiku"]["calls"] == 5


def test_world_news_groups_what_you_would_know_and_public_talk():
    g, link, a, plans, c = setup()
    c.on_new_turn()                                       # baseline turn
    c._record("world", "message", "Rome will rise", sender=1)
    link.world[0] = "TURN|61|0|-1757092276"
    link.aware = [l for l in link.aware if not l.startswith("CT|1|131073")] + ["CT|0|131073|Sao Paulo|14|10"]
    c.on_new_turn()
    [news] = [e for e in c.entries if e["kind"] == "news" and e["turn"] == 61]
    titles = {s["title"]: s["items"] for s in news["sections"]}
    assert "Sao Paulo changed hands: Pedro II of Brazil lost it to Elizabeth I of England." in titles["Cities"]
    assert titles["Said in public"] == ["Pedro II: Rome will rise"]
    assert not any("Eleanor" in x for items in titles.values() for x in items)   # you haven't met her


def test_rumors_are_labelled_for_the_listener_and_shown_to_you():
    g, link, a, plans, c = setup(routine='{"messages": [{"to": 0, "text": "Eleanor is massing troops on your border", '
                                        '"rumor_about": 2}]}')
    a.strategy_client = None
    c.on_new_turn()
    [m] = [e for e in c.entries if e["kind"] == "message" and e["channel"] == 1]
    assert m["text"] == "Eleanor is massing troops on your border" and m["rumor_about"] == "Eleanor of Aquitaine"
    heard = a.memory.conversations["0"][-1]["text"]
    assert heard.startswith("(rumor about Eleanor of Aquitaine, unverified)")


def test_trust_history_feeds_the_spy_timeline():
    from civ_council.web import Server
    g, link, a, plans, c = setup()
    a.memory.rel(0).adjust(10, "helped me", turn=60)
    a.memory.rel(0).adjust(-25, "betrayed me", turn=64)
    assert a.memory.rel(0).history == [[59, 0], [60, 10], [64, -15]]
    assert "trust" not in Server(c).state(0)                  # only with Spy on
    c.spy = True
    assert Server(c).state(0)["trust"] == {"Pedro II": {"you": [[59, 0], [60, 10], [64, -15]]}}


def test_leaders_without_a_handwritten_persona_get_one_written_once(tmp_path):
    from civ_council.personas import persona_for
    g, link = make_game()
    writer = ScriptedClient(lambda s, m: (json.dumps({"temperament": "Loud and proud.", "voice": "Talks in slogans.",
                                                      "quirks": ["quotes his own laws"]})
                                          if "character sheets" in s else PLAN), name="opus")
    a = LeaderAgent(2, persona_for("LEADER_HAMMURABI", "Hammurabi", "Babylon", "Lawgiver", "Loves order."),
                    ScriptedClient(lambda s, m: "{}", name="haiku"))
    a.strategy_client = writer
    c = Council(g, {2: a}, human_id=0, summarize_after=0)
    c.on_new_turn()
    assert a.persona.temperament.startswith("Loud and proud.") and "quotes his own laws" in a.persona.temperament
    assert a.persona.voice == "Talks in slogans." and "Loud and proud" in a.system_prompt()
    c.save_memories(tmp_path)
    fresh = LeaderAgent(2, persona_for("LEADER_HAMMURABI", "Hammurabi", "Babylon"), ScriptedClient(lambda s, m: "{}"))
    fresh.strategy_client = writer
    c2 = Council(g, {2: fresh}, human_id=0, summarize_after=0)
    c2.load_memories(tmp_path)
    assert fresh.persona.voice == "Talks in slogans."
    calls = len(writer.calls)
    link.world[0] = "TURN|61|0|-1757092276"
    c2.on_new_turn()
    assert sum("character sheets" in s for s, _ in writer.calls[calls:]) == 0   # not written twice


def test_a_checklist_note_is_stamped_with_the_turn_once():
    g, link, a, plans, c = setup('{"checklist": [{"id": "c2", "status": "doing", "note": "T60: army gathering"}]}')
    c.on_new_turn()
    note = next(x for x in a.memory.strategy["checklist"] if x["id"] == "c2")["note"]
    assert note.endswith(": army gathering") and note.count("T") == 1


def test_the_strategist_is_told_what_leaders_cant_do():
    g, link, a, plans, c = setup()
    c.on_new_turn()
    prompt = plans_made(plans)[0][1][0]["content"]
    assert "city's focus" in prompt and "recruit a great person" in prompt
    assert "CAN'T choose what a city builds or buys" in prompt


def test_timed_strategy_rewrites_are_spaced_by_era():
    g, link, a, plans, c = setup()
    c.on_new_turn()                                         # the first plan, turn 60
    era = {"now": 0}
    g.era_of = lambda civ_id: era["now"]
    def due_after(turns):
        a.memory.strategy["turn"] = c.game.turn - turns
        return c._strategy_due(1, [])
    assert due_after(30) == "" and due_after(50).startswith("it's been 50 turns")   # Ancient: every 50
    era["now"] = 3
    assert due_after(30) == "" and due_after(35)                                   # Renaissance: every 35
    era["now"] = 7
    assert due_after(20)                                                           # Information: every 20
    g.era_of = lambda civ_id: None                                                 # era unknown: the default
    assert due_after(24) == "" and due_after(25)


def test_opus_writes_a_strategy_now_and_then_and_sonnet_the_rest():
    g, link = make_game()
    opus, sonnet = ScriptedClient(lambda s, m: PLAN, name="opus"), ScriptedClient(lambda s, m: PLAN, name="sonnet")
    a = LeaderAgent(1, persona_for("LEADER_PEDRO", "Pedro II", "Brazil"), ScriptedClient(lambda s, m: "{}", name="haiku"),
                    talk_client=sonnet)
    a.strategy_client = opus
    def who(turn):
        link.world[0] = f"TURN|{turn}|0|-1757092276"
        g.world(fresh=True)
        a.strategize(g, "test")
        return a.memory.strategy["model"]
    assert who(60) == "opus"                                    # the first plan
    assert who(70) == "sonnet" and a.memory.strategy["deep_turn"] == 60     # big news in between: Sonnet
    assert who(109) == "sonnet"
    assert who(110) == "opus"                                   # 50 turns since Opus last wrote one
    broken = ScriptedClient(lambda s, m: "not json", name="sonnet")
    a.talk_client = broken
    assert who(115) == "opus" and len(broken.calls) == 1        # Sonnet failed: Opus writes it after all
    a.memory.strategy = {"turn": 100, "model": "opus", "grand_strategy": "old"}   # written before this rule
    a.talk_client = sonnet
    assert a._strategy_writers(120)[0][0] is sonnet and a._strategy_writers(150)[0][0] is opus
