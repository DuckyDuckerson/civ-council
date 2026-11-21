from __future__ import annotations

import json

from civ_council.agent import LeaderAgent, parse_decision
from civ_council.council import Council
from civ_council.game import ALLIED, FRIENDS, PEACE, WAR, Civ, MockGame
from civ_council.llm import ScriptedClient
from civ_council.memory import LeaderMemory
from civ_council.personas import PERSONAS


def make_world(brains: dict[int, object] | None = None, **kw):
    players = [Civ(0, "Mine", "Me", is_human=True), Civ(1, "Sumeria", "Gilgamesh"), Civ(2, "Egypt", "Cleopatra")]
    game = MockGame(players, seed=1)
    brains = brains or {}
    agents = {
        1: LeaderAgent(1, PERSONAS["gilgamesh"], brains.get(1) or ScriptedClient(lambda s, m: "{}")),
        2: LeaderAgent(2, PERSONAS["cleopatra"], brains.get(2) or ScriptedClient(lambda s, m: "{}")),
    }
    return Council(game, agents, human_id=0, **kw)


def reply(**fields) -> str:
    return json.dumps(fields)


# ---- game --------------------------------------------------------------------
def test_war_peace_and_proposals():
    g = make_world().game
    assert g.apply(0, {"type": "declare_war", "target": 1}).ok
    assert g.stance(0, 1) == WAR
    assert not g.apply(0, {"type": "declare_war", "target": 1}).ok
    assert g.apply(0, {"type": "make_peace", "target": 1}).ok
    pid = g.pending_proposals(1)[0].id
    assert not g.resolve_proposal(pid, 2, True).ok          # wrong responder
    assert g.resolve_proposal(pid, 1, True).ok
    assert g.stance(0, 1) == PEACE


def test_friendship_alliance_denounce_break():
    g = make_world().game
    g.apply(1, {"type": "propose_friendship", "target": 2})
    g.resolve_proposal(g.pending_proposals(2)[0].id, 2, True)
    assert g.stance(1, 2) == FRIENDS
    g.apply(2, {"type": "propose_alliance", "target": 1})
    g.resolve_proposal(g.pending_proposals(1)[0].id, 1, True)
    assert g.stance(1, 2) == ALLIED
    assert not g.apply(1, {"type": "declare_war", "target": 2}).ok   # must break alliance first
    assert g.apply(1, {"type": "denounce", "target": 2}).ok
    assert g.stance(1, 2) == PEACE and g.is_denouncing(1, 2)


def test_bad_actions_rejected_and_proposals_expire():
    g = make_world().game
    assert not g.apply(0, {"type": "nuke", "target": 1}).ok
    assert not g.apply(0, {"type": "declare_war", "target": 0}).ok
    assert not g.apply(0, {"type": "declare_war", "target": "zebra"}).ok
    g.apply(0, {"type": "propose_friendship", "target": 2})
    for _ in range(3):
        g.end_turn()
    assert g.pending_proposals(2)            # still open for three full turns
    g.end_turn()
    assert g.pending_proposals(2) == []


# ---- parsing -------------------------------------------------------------------
def test_parse_handles_fences_garbage_and_invalid_actions():
    d = parse_decision('```json\n{"messages":[{"to":1,"text":"hi"}],"actions":[{"type":"nuke"},'
                       '{"type":"denounce","target":1}]}\n```')
    assert d.messages == [{"to": 1, "text": "hi"}]
    assert d.actions == [{"type": "denounce", "target": 1}]
    assert parse_decision("I refuse to answer in JSON.").error
    assert parse_decision('{"messages": "oops"}').messages == []


# ---- agents & memory -------------------------------------------------------------
def test_memory_updates_are_clamped_and_shown_in_context():
    brain = ScriptedClient([reply(memory=[{"about": 0, "trust_change": 999, "note": "kind to me",
                                           "promise": "will not settle near Uruk"}], journal="trust Me")])
    c = make_world({1: brain})
    c.say("gilgamesh", "hello friend")
    r = c.agents[1].memory.rel(0)
    assert r.trust == 25 and "kind to me" in r.notes[-1] and r.promises
    # Next call's context carries what he remembers.
    c.agents[1].decide(c.game, "turn", [])
    ctx_text = brain.calls[-1][1][0]["content"]
    assert "will not settle near Uruk" in ctx_text and "trust Me" in ctx_text


def test_memory_save_load_roundtrip(tmp_path):
    m = LeaderMemory()
    m.rel(3).adjust(-40, "burned my city", turn=5)
    m.log(3, 5, "Montezuma", "Your gods are weak.")
    m.save(tmp_path / "g.json")
    m2 = LeaderMemory.load(tmp_path / "g.json")
    assert m2.rel(3).trust == -25 and m2.conversations["3"][0]["text"] == "Your gods are weak."


# ---- council routing ---------------------------------------------------------------
def test_private_reply_reaches_only_the_human():
    c = make_world({1: ScriptedClient([reply(messages=[{"to": 0, "text": "Welcome, brother."}])])})
    lines = c.say("Gilgamesh", "Shall we be friends?")
    assert any("Welcome, brother." in l for l in lines)
    assert c.agents[2].memory.conversations == {}       # Cleopatra heard nothing


def test_ai_to_ai_messages_get_answered_and_spy_mode_shows_them():
    gil = ScriptedClient([reply(messages=[{"to": 2, "text": "Join me against them?"}])])
    cleo = ScriptedClient([reply(messages=[{"to": 1, "text": "For a price."}])])
    c = make_world({1: gil, 2: cleo}, spy=True)
    lines = c.end_turn()
    assert any("overheard] Gilgamesh → Cleopatra" in l for l in lines)
    assert any("For a price." in l for l in lines)
    assert "Join me against them?" in json.dumps(cleo.calls[0][1])   # she saw his message
    convo = c.agents[1].memory.conversations["2"]
    assert [m["text"] for m in convo] == ["Join me against them?", "For a price."]


def test_pair_cap_stops_endless_chatter():
    chatty = lambda s, m: reply(messages=[{"to": 2 if '"id": 1,' in m[0]["content"] else 1, "text": "And another thing?"}])
    c = make_world({1: ScriptedClient(chatty), 2: ScriptedClient(chatty)}, spy=True, reply_rounds=10)
    c.end_turn()
    assert all(n <= c.max_pair_msgs for n in c._pair_count.values()) or c._pair_count == {}
    sent = sum(1 for l in c.feed if "And another thing?" in l)
    assert sent <= 2 * c.max_pair_msgs


def test_ai_proposal_to_human_and_acceptance():
    c = make_world({2: ScriptedClient([reply(actions=[{"type": "propose_alliance", "target": 0}])])})
    lines = c.end_turn()
    assert any("Cleopatra offers you an alliance" in l for l in lines)
    pid = c.game.pending_proposals(0)[0].id
    c.respond(pid, True)
    assert c.game.stance(0, 2) == ALLIED


def test_ai_war_declaration_and_broken_llm_dont_crash():
    def boom(s, m):
        raise ConnectionError("no internet")
    c = make_world({1: ScriptedClient([reply(actions=[{"type": "declare_war", "target": 0}])]),
                    2: ScriptedClient(boom)})
    lines = c.end_turn()
    assert c.game.stance(0, 1) == WAR
    assert any("garbled" in l for l in lines)


def test_resolve_names():
    c = make_world()
    assert c.resolve("cleo") == 2 and c.resolve("Sumeria") == 1
    assert c.resolve("everyone") == "all" and c.resolve("nobody") is None
