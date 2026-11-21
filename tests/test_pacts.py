"""Pacts: agreements in words (vassals, borders, joint and proxy wars), with an optional payment."""
from __future__ import annotations

from civ_council.agent import LeaderAgent
from civ_council.council import Council
from civ_council.llm import ScriptedClient
from civ_council.pacts import PactBook
from civ_council.personas import persona_for

from tests.test_civ6_game import make_game


def leader(cid, reply="{}"):
    names = {1: ("LEADER_PEDRO", "Pedro II", "Brazil"), 2: ("LEADER_ELEANOR_FRANCE", "Eleanor of Aquitaine", "France")}
    return LeaderAgent(cid, persona_for(*names[cid]), ScriptedClient(reply if callable(reply) else (lambda s, m: reply)))


def test_the_book_offers_answers_ends_and_breaks():
    book = PactBook()
    p = book.offer(1, 0, "Brazil under England's wing", "Brazil follows England's lead.", 20, turn=60)
    assert p.key == "pact-1" and p.length() == "for 20 turns"
    assert book.offer(1, 0, "again", "x", None, 60) == "you already have a pact offer waiting for their answer"
    assert isinstance(book.offer(1, 0, "", "", None, 60), str)            # terms are required
    assert book.answer("pact-1", 1, True, 61) == "no pact offer pact-1 waiting for you"   # only the other side answers
    assert book.answer("pact-1", 0, True, 61).status == "active" and p.ends == 81
    assert book.end("pact-1", 0, 70).status == "broken"                   # before turn 81: broken
    q = book.offer(0, 1, "Open-ended", "No settling on Africa.", None, 70)
    book.answer(q.key, 1, True, 70)
    assert book.end(q.key, 1, 90).status == "ended"                       # open-ended: ending it is fair
    r = book.offer(0, 1, "Late", "x", 5, 90)
    assert book.tick(93) == [r] and r.status == "expired"                 # unanswered for 3 turns


def test_a_leader_offers_you_a_pact_you_accept_and_both_briefings_show_it(tmp_path):
    g, _ = make_game()
    pedro = leader(1, '{"actions": [{"type": "propose_pact", "target": 0, "title": "Brazil under England", '
                      '"terms": "Brazil joins England\'s wars and stays off Africa; England protects Brazil.", "turns": 30}]}')
    c = Council(g, {1: pedro}, human_id=0, summarize_after=0)
    c.on_new_turn()
    [card] = [e for e in c.entries if e["kind"] == "pact"]
    assert card["channel"] == 1 and "Brazil joins England's wars" in card["text"]
    ctx_offer = leader(1).build_context(g, "turn", [])   # (a fresh agent has no book until the council sets one)
    assert "your_pacts" not in ctx_offer
    assert c.answer_pact(0, "pact-1", True).ok
    ctx = pedro.build_context(g, "turn", [])
    [mine] = ctx["your_pacts"]
    assert mine["with"] == "Elizabeth I" and mine["turns_left"] == 30 and mine["you_proposed_it"]
    c.save_memories(tmp_path)
    c2 = Council(g, {1: leader(1)}, human_id=0, summarize_after=0)
    c2.load_memories(tmp_path)
    assert c2.pacts.active(1)[0].title == "Brazil under England"
    assert c2.agents[1].pact_book is c2.pacts


def test_you_offer_a_pact_with_a_payment_a_proxy_war_and_it_pays_on_acceptance():
    g, link = make_game()                      # Pedro (1) is at war with Eleanor (2), at peace with you (0)
    pedro = leader(1, lambda s, m: ('{"proposal_responses": [{"id": "pact-1", "accept": true}]}'
                                    if "pact-1" in m[-1]["content"] else "{}"))
    c = Council(g, {1: pedro}, human_id=0, summarize_after=0)
    res, responders = c.human_offer_pact(1, "Brazil fights France", "Brazil keeps fighting France for 20 turns.",
                                         20, give={"gold": 300})
    assert res.ok and responders == [1]
    assert any(e["sender"] == 0 and e["text"].startswith("📜 Pact offer pact-1: Brazil fights France.")
               and "I gives 300 gold" not in e["text"] for e in c.entries)
    c.answer(responders)
    assert c.pacts.active(1) and any("EnactWorkingDeal" in x for x in link.calls)     # the 300 gold went through
    assert any(e["kind"] == "treaty" and "Pact agreed" in e["text"] for e in c.entries)


def test_a_pact_whose_payment_falls_through_never_starts():
    g, link = make_game()
    c = Council(g, {1: leader(1)}, human_id=0, summarize_after=0)
    assert c.offer_pact(0, 1, "Tribute", "Brazil pays tribute.", 10, ask={"gold": 50}).ok
    link.replies["deal"] = "RES|no|they can't trade that"
    res = c.answer_pact(1, "pact-1", True)
    assert not res.ok and c.pacts.pacts[1].status == "failed" and not c.pacts.active(1)
    assert "payment fell through" in c.agents[1].build_context(g, "turn", [])["pacts_that_ended_recently"][0]


def test_a_leader_breaks_a_pact_and_you_see_it():
    g, _ = make_game()
    pedro = leader(1)
    c = Council(g, {1: pedro}, human_id=0, summarize_after=0)
    c.offer_pact(0, 1, "No settling on Africa", "Brazil never settles on Africa.", 50)
    c.answer_pact(1, "pact-1", True)
    pedro.client = ScriptedClient(lambda s, m: '{"actions": [{"type": "end_pact", "pact": "pact-1", "note": "times change"}]}')
    c.on_new_turn()
    assert any(e["channel"] == 1 and "Pedro II broke the pact with Elizabeth I" in e["text"] for e in c.entries)
    assert "broken by you" in pedro.build_context(g, "turn", [])["pacts_that_ended_recently"][0]
