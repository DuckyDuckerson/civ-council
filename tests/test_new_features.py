from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from civ_council.cli import build, make_parser
from civ_council.llm import ClaudeCodeClient, ScriptedClient
from tests.test_council import make_world, reply

sys.path.insert(0, str(Path(__file__).parents[1] / "bridge"))
from tuner import Tuner  # noqa: E402


# ---- group chats ----------------------------------------------------------------
def test_group_chat_only_reaches_members_and_they_answer_in_group():
    gil = ScriptedClient(lambda s, m: reply(messages=[{"to": "g1", "text": "Agreed, brother."}]))
    cleo = ScriptedClient(lambda s, m: "{}")
    c = make_world({1: gil, 2: cleo})
    # add a third AI who is NOT in the group
    from civ_council.agent import LeaderAgent
    from civ_council.game import Civ
    from civ_council.personas import PERSONAS
    c.game.players.append(Civ(3, "Aztec", "Montezuma"))
    outsider = ScriptedClient(lambda s, m: "{}")
    c.agents[3] = LeaderAgent(3, PERSONAS["montezuma"], outsider)
    c._inbox[3] = []

    g = c.create_group("Southern Pact", [1, 2])
    assert g["id"] == "g1" and g["members"] == [0, 1, 2]
    c.say("g1", "Shall we move against Montezuma?")
    texts = [e["text"] for e in c.entries if e["channel"] == "g1" and e["kind"] == "message"]
    assert texts[0] == "Shall we move against Montezuma?" and "Agreed, brother." in texts
    assert outsider.calls == []                                   # Montezuma never saw it
    assert "Shall we move against Montezuma?" not in json.dumps(c.agents[3].memory.to_dict())
    assert "Southern Pact" in cleo.calls[0][1][0]["content"]      # Cleopatra's context lists the group


def test_non_member_cannot_post_into_group():
    c = make_world({2: ScriptedClient([reply(messages=[{"to": "g1", "text": "Let me in!"}])])})
    c.create_group("Just us", [1])
    c.end_turn()
    assert not any(e["text"] == "Let me in!" for e in c.entries)


# ---- memory summaries -----------------------------------------------------------
def test_memory_summaries_condense_and_feed_back_into_context():
    def brain(system, messages):
        ctx = messages[0]["content"]
        if '"mode": "summarize"' in ctx:
            return json.dumps({"relationships": [{"about": 0, "summary": "Betrayed our alliance on turn 3. Never trust."}],
                               "story": "Sumeria stands alone."})
        return reply(memory=[{"about": 0, "trust_change": -5, "note": f"note {len(messages)}"}])
    gil = ScriptedClient(brain)
    c = make_world({1: gil}, summarize_after=2, summary_min_gap=0)
    for _ in range(2):
        c.end_turn()
    r = c.agents[1].memory.rel(0)
    assert r.summary.startswith("Betrayed") and len(r.notes) <= 3
    assert c.agents[1].memory.story == "Sumeria stands alone."
    assert any("reflected on recent history" in e["text"] for e in c.entries)
    c.end_turn()
    assert "Never trust." in gil.calls[-1][1][0]["content"]


def test_failed_summary_changes_nothing():
    c = make_world({1: ScriptedClient(lambda s, m: "not json")}, summarize_after=1)
    c.agents[1].memory.rel(0).adjust(-10, "rude", turn=1)
    c.end_turn()
    assert c.agents[1].memory.rel(0).notes == ["T1: rude"]


# ---- setup options ----------------------------------------------------------------
def fake_claude(tmp_path, logged_in=True):
    """A stand-in `claude` CLI: answers `auth status` and `-p` the way Claude Code 2.1 does."""
    script = tmp_path / "claude"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": {logged_in!r}}}))
elif "-p" in sys.argv:
    sys.stdin.read()
    print(json.dumps({{"type": "result", "is_error": False,
                      "result": json.dumps({{"api_key_seen": "ANTHROPIC_API_KEY" in os.environ}})}}))
""")
    script.chmod(0o755)
    return str(script)


def test_per_leader_models(tmp_path):
    args = make_parser().parse_args(["--provider", "claude-code", "--model", "haiku",
                                     "--leader-models", "cleopatra=sonnet",
                                     "--claude-bin", fake_claude(tmp_path)])
    c = build(args)
    names = {a.persona.key: a.client.name for a in c.agents.values()}
    assert names["cleopatra"] == "claude-code:sonnet" and names["gilgamesh"] == "claude-code:haiku"


def test_claude_code_command_uses_subscription_safe_flags():
    cmd = ClaudeCodeClient("haiku")._cmd("/tmp/s.txt")
    assert "--bare" not in cmd                      # bare would skip the subscription login
    assert cmd[cmd.index("--tools") + 1] == ""      # no file/shell tools
    assert "--safe-mode" in cmd and "--model" in cmd and "haiku" in cmd


def test_claude_code_finds_cli_off_path_and_never_bills_an_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CIV_COUNCIL_CLAUDE", fake_claude(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    client = ClaudeCodeClient("haiku")
    assert client.binary == str(tmp_path / "claude")
    assert json.loads(client.complete("system", [{"role": "user", "content": "hi"}])) == {"api_key_seen": False}


def test_claude_code_logged_out_or_missing_stops_with_instructions(tmp_path):
    from civ_council.llm import check_claude_login, make_client
    import pytest
    with pytest.raises(RuntimeError, match="auth login"):
        make_client("claude-code", "haiku", claude_bin=fake_claude(tmp_path, logged_in=False))
    with pytest.raises(RuntimeError, match="install.sh"):
        check_claude_login(None)


# ---- web server -------------------------------------------------------------------
def test_web_api_round_trip():
    from http.server import ThreadingHTTPServer
    from civ_council.web import Server, make_handler
    args = make_parser().parse_args(["--spy"])
    srv = Server(build(args))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(srv))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(), {"content-type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req).read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def state():
        return json.loads(urllib.request.urlopen(base + "/api/state?since=0").read())

    def settle():
        for _ in range(100):
            if not state()["busy"]:
                return
            time.sleep(0.05)

    assert b"Civ Council" in urllib.request.urlopen(base + "/").read()
    assert post("/api/say", {"to": "world", "text": "Hello all"})["ok"]
    assert post("/api/say", {"to": 1, "text": "Hello friend"})["ok"]
    g = post("/api/group", {"name": "Pact", "members": [1, 2]})
    assert g["ok"] and g["group"]["id"] == "g1"
    assert post("/api/say", {"to": "g1", "text": "Group hello?"})["ok"]
    assert post("/api/end_turn", {})["ok"]
    assert not post("/api/act", {"type": "nuke", "target": 1})["ok"]
    assert not post("/api/group", {"name": "x", "members": []})["ok"]
    settle()
    s = state()
    chans = {str(e["channel"]) for e in s["entries"]}
    assert {"world", "1", "g1"} <= chans and s["turn"] == 2 and s["groups"][0]["name"] == "Pact"
    assert post("/api/group_leave", {"id": "g1"})["ok"] and not post("/api/group_leave", {"id": "g1"})["ok"]
    left = state()["groups"][0]   # the two leaders carry on without you
    assert left["members"] == [1, 2] and not left.get("closed") and 0 in left["ever"]
    httpd.shutdown()


# ---- tuner framing (fake Civ 6 on a local socket) ---------------------------------
def _fake_game(sock):
    conn, _ = sock.accept()

    def send(tag, text):
        data = text.encode() + b"\0"
        conn.sendall(struct.pack("<Ii", len(data), tag) + data)

    def recv():
        length, tag = struct.unpack("<Ii", conn.recv(8, socket.MSG_WAITALL))
        return tag, conn.recv(length, socket.MSG_WAITALL).rstrip(b"\0").decode()

    while True:
        try:
            tag, text = recv()
        except Exception:
            return
        # Replies as a real Civ 6 sends them (macOS, 2026): see bridge/tuner.py.
        if text == "APP:":
            send(4, "Civ6\x00Sid Meier's Civilization 6\x00C:\\Emu\\Debug")
        elif text == "LSQ:":
            send(4, "0\x00FrontEnd\x002\x00GameCore_Tuner\x003\x00InGame")   # index 1 unused
        elif text.startswith("CMD:3:"):
            send(3, "")                                                    # empty ack
            send(3, "O\x00InGame: hello: from InGame")
            send(3, "O\x00InGame: ---END---")
        elif text.startswith("CMD:2:"):
            send(3, "ERR:[string \"...\"]:1: attempt to index a nil value")


def test_tuner_handshake_and_run():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    threading.Thread(target=_fake_game, args=(srv,), daemon=True).start()
    t = Tuner(port=srv.getsockname()[1], timeout=5)
    states = t.handshake()
    assert t.app == "Civ6"
    assert t.index_of("GameCore_Tuner") == 2 and t.index_of("InGame") == 3, states
    assert t.run(3, "print('x')") == "hello: from InGame"            # prefix stripped, ack skipped
    start = time.time()
    assert t.run(2, "Players[99]:GetCities()").startswith("[Lua error] ")
    assert time.time() - start < 2                                  # an error doesn't wait for the timeout
    t.close()


def test_tuner_parses_plain_name_lists():
    assert Tuner._parse_states([b"LSQ:\x00Main State\x00GameCore_Tuner\x00InGame\x00"]) == \
        ["Main State", "GameCore_Tuner", "InGame"]


# ---- your deal screen -------------------------------------------------------------------
def test_you_can_offer_a_leader_a_deal_and_they_answer_it():
    import re as _re
    from civ_council.web import Server

    def gilgamesh(system, messages):
        ctx = messages[0]["content"]
        ids = [int(x) for x in _re.findall(r'"id":\s*(\d+),\s*"kind":\s*"propose_deal"', ctx)]
        return reply(messages=[{"to": 0, "text": "deal."}], proposal_responses=[{"id": i, "accept": True} for i in ids])

    c = make_world({1: ScriptedClient(gilgamesh)})
    c.game.civ(0).gold, c.game.civ(1).gold = 100, 80
    server = Server(c)
    opts = server.trade(1)
    assert opts == {"ok": True, "you_can_give": {"gold": 100}, "they_can_give": {"gold": 80}, "at_war": False}
    assert server.handle("/api/deal", {"to": 1, "give": {"gold": 30}, "ask": {"gold": 50}, "note": "fair?"}) == {"ok": True}
    for _ in range(100):                       # the leader answers in the background worker
        if server.pending == 0:
            break
        time.sleep(0.02)
    mine = [e for e in c.entries if e["channel"] == 1]
    assert mine[0]["text"] == "📜 Deal offer #1: I give 30 gold; you give 50 gold. fair?"
    assert any(e["kind"] == "treaty" and "made a deal" in e["text"] for e in mine)
    assert (c.game.civ(0).gold, c.game.civ(1).gold) == (120, 60)


def test_deal_screen_refuses_bad_offers():
    from civ_council.web import Server
    c = make_world()
    server = Server(c)
    assert "only has" in server.handle("/api/deal", {"to": 1, "give": {"gold": 500}})["error"]
    assert "needs something" in server.handle("/api/deal", {"to": 1})["error"]
    assert "AI leaders" in server.handle("/api/deal", {"to": 0, "give": {"gold": 1}})["error"]
    c.game.apply(0, {"type": "declare_war", "target": 2})
    assert "can't trade" in server.trade(2)["error"]


# ---- leaders' own group chats ----------------------------------------------------
def three_leaders(gil, cleo=None, monty=None, **kw):
    from civ_council.agent import LeaderAgent
    from civ_council.game import Civ
    from civ_council.personas import PERSONAS
    c = make_world({1: gil, 2: cleo or ScriptedClient(lambda s, m: "{}")}, **kw)
    c.game.players.append(Civ(3, "Aztec", "Montezuma"))
    c.agents[3] = LeaderAgent(3, PERSONAS["montezuma"], monty or ScriptedClient(lambda s, m: "{}"))
    c._inbox[3] = []
    c._sync_groups()
    return c


def once(first: str, then: str = "{}"):
    """A scripted leader that says `first` on its first call and `then` afterwards."""
    calls = []
    return ScriptedClient(lambda s, m: (calls.append(1), first if len(calls) == 1 else then)[1])


def test_a_leader_starts_a_secret_group_and_the_others_answer_there():
    gil = once(reply(group_chats=[{"do": "start", "name": "Against Egypt", "members": [3, "Cleopatra"],
                                   "message": "Cleopatra grows too strong. Shall we?"}]))
    monty = ScriptedClient(lambda s, m: reply(messages=[{"to": "g1", "text": "My jaguars are ready."}])
                           if "Cleopatra grows too strong" in m[0]["content"] else "{}")
    c = three_leaders(gil, monty=monty, max_pair_msgs=1)
    notes = []
    c.game.notify = lambda *a: notes.append(a)   # Civ's own notification stack
    c.end_turn()
    g = c.groups["g1"]
    assert g["members"] == [1, 2, 3] and g["started_by"] == 1 and g["name"] == "Against Egypt"
    said = [e for e in c.entries if e["channel"] == "g1" and e["kind"] == "message"]
    assert [e["text"] for e in said][:2] == ["Cleopatra grows too strong. Shall we?", "My jaguars are ready."]
    assert all(e.get("secret") for e in c.entries if e["channel"] == "g1")   # you weren't in it
    assert notes == []                                                        # and Civ didn't tell you
    assert "Against Egypt" in json.dumps(c.agents[2].build_context(c.game, "turn", [])["your_group_chats"])


def test_a_leader_can_bring_you_in_and_you_only_see_what_came_after():
    gil = once(reply(group_chats=[{"do": "start", "name": "Trade bloc", "members": [2, 3],
                                   "message": "Before we ask the human: we fix prices."}]),
               reply(group_chats=[{"do": "invite", "group": "g1", "member": 0}],
                     messages=[{"to": "g1", "text": "Welcome, friend. Let's trade."}]))
    c = three_leaders(gil)
    c.end_turn()
    c.end_turn()
    g = c.groups["g1"]
    assert 0 in g["members"] and 0 in g["ever"]
    entries = [e for e in c.entries if e["channel"] == "g1"]
    before = next(e for e in entries if e["text"].startswith("Before we ask"))
    welcome = next(e for e in entries if e["text"] == "Welcome, friend. Let's trade.")
    added = next(e for e in entries if e["kind"] == "membership" and "added you" in e["text"])
    assert before.get("secret") and not welcome.get("secret") and not added.get("secret")
    # you can answer there, and leave: the rest go on without you, in secret again
    assert c.post("g1", "Deal.") == [1, 2, 3]
    assert c.leave_group(0, "g1").ok and 0 not in c.groups["g1"]["members"] and not c.groups["g1"].get("closed")
    c._deliver(1, "g1", "Now that they're gone...")
    assert c.entries[-1]["text"] == "Now that they're gone..." and c.entries[-1].get("secret")


def test_group_chat_limits_and_leaving():
    c = three_leaders(ScriptedClient(lambda s, m: "{}"))
    assert not c.leader_group(1, {"do": "start", "members": [2]}).ok                  # one other: a private message
    assert c.leader_group(1, {"do": "start", "name": "a", "members": [2, 3]}).ok
    assert not c.leader_group(1, {"do": "start", "name": "b", "members": [3, 2]}).ok  # the same people again
    assert not c.leader_group(2, {"do": "invite", "group": "g1", "member": 3}).ok     # already in
    assert not c.leader_group(3, {"do": "invite", "group": "g9", "member": 0}).ok     # no such group
    for name in ("b", "c"):
        assert c.leader_group(1, {"do": "start", "name": name, "members": [0, 2, 3][:3] if name == "b" else [0, 2]}).ok
    assert "already run 3" in c.leader_group(1, {"do": "start", "members": [0, 3]}).message
    assert c.leader_group(2, {"do": "leave", "group": "g1"}).ok and c.leader_group(3, {"do": "leave", "group": "g1"}).ok
    assert c.groups["g1"]["closed"] and all(x["id"] != "g1" for x in c.agents[1].groups)
    c._deliver(1, "g1", "anyone?")
    assert c.entries[-1]["text"] != "anyone?"                                          # a closed group takes no messages


def test_leaders_are_back_in_their_groups_after_a_restart(tmp_path):
    c = three_leaders(ScriptedClient(lambda s, m: "{}"))
    c.leader_group(1, {"do": "start", "name": "Old friends", "members": [2, 3]})
    c.save_memories(tmp_path)
    c2 = three_leaders(ScriptedClient(lambda s, m: "{}"))
    c2.load_memories(tmp_path)
    assert [g["id"] for g in c2.agents[3].groups] == ["g1"]


# ---- what you haven't met stays hidden (Spy shows it) ----------------------------------
def test_news_about_a_leader_you_havent_met_is_secret():
    monty = once(reply(messages=[{"to": "all", "text": "The sun demands tribute!"}],
                       actions=[{"type": "declare_war", "target": 1}]))
    c = three_leaders(ScriptedClient(lambda s, m: reply(messages=[{"to": "all", "text": "Sumeria stands."}])),
                      monty=monty)
    c.game.has_met = lambda a, b: not ({a, b} == {0, 3})    # you haven't met Montezuma; everyone else has
    c.game.event_civs = lambda t: frozenset({3}) if "Montezuma" in t else frozenset({1})
    notes = []
    c.game.notify = lambda *a: notes.append(a)
    c.end_turn()
    world = [e for e in c.entries if e["channel"] == "world"]
    said = {e["text"]: e.get("secret") for e in world if e["kind"] == "message"}
    assert said == {"The sun demands tribute!": True, "Sumeria stands.": None}
    war = next(e for e in world if e["kind"] == "war")
    assert war["secret"] and "Montezuma" in war["text"]
    assert all("tribute" not in str(n) for n in notes) and any("Sumeria stands" in str(n) for n in notes)
    for text in ("T2: Montezuma of Aztec now has 2 cities (+1).", "T2: Gilgamesh of Sumeria founded Ur."):
        c._record("world", "event", text, **c._event_hush(text))
    assert [e.get("secret") for e in c.entries[-2:]] == [True, None]


# ---- who you mean ---------------------------------------------------------------
def test_names_in_your_messages_are_spelled_out_for_the_leader():
    from civ_council.council import names_in
    from civ_council.game import Civ
    civs = [Civ(0, "England", "Victoria", True), Civ(1, "Egypt", "Cleopatra"), Civ(2, "Aztec", "Montezuma"),
            Civ(3, "Sumeria", "Gilgamesh"), Civ(4, "Rome", "Trajan"), Civ(5, "Korea", "Seondeok")]
    found = {w: c.leader for w, c in names_in(
        "cleo told me she's worried about egpyt. the sumerian army is big, ask monte or gil. romee too. "
        "the korea guy? I don't know, nothing north of london, can't chain it", civs).items()}
    assert found == {"cleo": "Cleopatra", "egpyt": "Cleopatra", "sumerian": "Gilgamesh", "monte": "Montezuma",
                     "gil": "Gilgamesh", "romee": "Trajan", "korea": "Seondeok"}


def test_a_leader_reading_your_message_is_told_who_you_mean():
    gil = ScriptedClient(lambda s, m: "{}")
    c = three_leaders(gil)
    c.game.has_met = lambda a, b: not ({a, b} == {1, 3})    # Gilgamesh hasn't met Montezuma
    c.say(1, "egypt asked about the aztec guy. gil, monte wants your iron")
    inbox = json.loads(gil.calls[-1][1][0]["content"].split("\n")[1])["inbox"]
    assert inbox[-1]["names_in_message"] == {
        "egypt": "Cleopatra of Egypt", "aztec": "Montezuma of Aztec (you haven't met them)",
        "gil": "you (Gilgamesh of Sumeria)", "monte": "Montezuma of Aztec (you haven't met them)"}
    assert "a country for its leader" in gil.calls[-1][0]                        # and how you write
    c._deliver_now(2, 1, "Egypt greets Sumeria.")                                 # a leader's message: no notes
    assert "names_in_message" not in c._inbox[1][-1]


def test_a_stuck_reply_to_you_is_tried_again_before_the_other_model():
    import subprocess
    from civ_council.agent import LeaderAgent
    from civ_council.personas import PERSONAS

    class Stuck:
        name = "claude-code:sonnet"
        def __init__(self):
            self.calls = 0
        def complete(self, system, messages, max_tokens=1200):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("claude", 45)
            return reply(messages=[{"to": 0, "text": "sorry, here now"}])
    talk, routine = Stuck(), ScriptedClient(lambda s, m: reply(messages=[{"to": 0, "text": "haiku guess"}]))
    agent = LeaderAgent(1, PERSONAS["gilgamesh"], routine, talk_client=talk)
    c = make_world({})
    d = agent.decide(c.game, "reply", [{"from_id": 0, "from": "Me", "to": "you", "text": "hi"}], with_human=True)
    assert talk.calls == 2 and routine.calls == [] and d.messages[0]["text"] == "sorry, here now"


# ---- counter-offers -------------------------------------------------------------
def test_you_can_counter_a_leaders_deal_with_your_own_terms():
    from civ_council.web import Server
    seen = []

    def gilgamesh(system, messages):
        seen.append(messages[0]["content"])
        return "{}"

    c = make_world({1: ScriptedClient(gilgamesh)})
    c.game.civ(0).gold, c.game.civ(1).gold = 100, 80
    theirs = c.game.apply(1, {"type": "propose_deal", "target": 0, "give": {"gold": 20}, "ask": {"gold": 60}})
    server = Server(c)
    [offer] = server.state(0)["proposals"]
    assert offer["kind"] == "propose_deal" and offer["you_give"] == {"gold": 60} and offer["you_get"] == {"gold": 20}
    assert server.handle("/api/deal", {"to": 1, "give": {"gold": 40}, "ask": {"gold": 20},
                                       "counter_to": theirs.proposal_id}) == {"ok": True}
    for _ in range(100):
        if server.pending == 0:
            break
        time.sleep(0.02)
    assert c.game.pending_proposals(0) == []                         # theirs is declined
    [mine] = c.game.pending_proposals(1)                             # yours waits for them
    assert mine.terms["give"] == {"gold": 40} and mine.terms["ask"] == {"gold": 20}
    said = [e["text"] for e in c.entries if e["channel"] == 1 and e["kind"] == "message"]
    assert said[0].startswith(f"🔁 Counter-offer #{mine.id} to your #{theirs.proposal_id} (")
    assert "I give 40 gold; you give 20 gold." in said[0]
    assert "Counter-offer" in seen[-1]                               # Gilgamesh reads it as a counter


def test_a_counter_to_an_offer_that_is_gone_is_just_a_new_deal():
    from civ_council.web import Server
    c = make_world()
    c.game.civ(0).gold = 100
    server = Server(c)
    assert server.handle("/api/deal", {"to": 1, "give": {"gold": 10}, "counter_to": 999}) == {"ok": True}
    assert not any("Counter-offer" in e["text"] for e in c.entries)


def test_leaders_know_envoys_cant_be_pulled_out():
    from civ_council.agent import LeaderAgent
    from civ_council.personas import PERSONAS
    prompt = LeaderAgent(1, PERSONAS["gilgamesh"], ScriptedClient(lambda s, m: "{}")).system_prompt()
    assert "Envoys are permanent" in prompt and "never promise to pull yours" in prompt
    assert "never promise to move a governor" in prompt


def test_a_real_game_saves_by_itself(tmp_path):
    from civ_council.web import Server
    c = make_world({1: ScriptedClient(lambda s, m: reply(messages=[{"to": 0, "text": "noted."}]))})
    c.memory_dir = tmp_path
    server = Server(c)
    assert server.state(0)["autosave"] is True
    server.handle("/api/say", {"to": 1, "text": "remember this"})
    for _ in range(200):
        if server.pending == 0:
            break
        time.sleep(0.02)
    chat = json.loads((tmp_path / "chat.json").read_text())["entries"]
    assert [e["text"] for e in chat if e["kind"] == "message"] == ["remember this", "noted."]
    assert (tmp_path / "gilgamesh.json").exists()
    offline = make_world()
    assert Server(offline).state(0)["autosave"] is False     # the demo only saves when you press Save


# ---- group pacts, joint wars, votes, goals ----------------------------------------
import re as _re   # noqa: E402


def _signs_group_pacts(extra=None):
    """A scripted leader that accepts every group pact offered to it (and sends `extra` fields too)."""
    def brain(system, messages):
        ids = _re.findall(r'"id":"(pact-\d+)","kind":"group pact"', messages[0]["content"])
        return reply(proposal_responses=[{"id": i, "accept": True} for i in ids], **(extra or {}))
    return ScriptedClient(brain)


def _wait(server):
    for _ in range(200):
        if server.pending == 0:
            return
        time.sleep(0.02)


def test_a_group_pact_needs_everyone_and_a_joint_war_starts_together():
    from civ_council.web import Server
    c = three_leaders(_signs_group_pacts(), _signs_group_pacts())
    g = c.create_group("Southern League", [1, 2])
    server = Server(c)
    assert server.handle("/api/group_pact", {"group": g["id"], "title": "Crush the Aztecs", "terms": "We all fight Montezuma.",
                                             "turns": 20, "war_on": 3}) == {"ok": True}
    _wait(server)
    [p] = c.pacts.of_group(g["id"])
    assert p.status == "active" and sorted(p.accepted) == [0, 1, 2] and p.ends == c.game.turn + 20
    assert c.game.stance(1, 3) == "war" and c.game.stance(2, 3) == "war"            # both declared, the same turn
    group = [e["text"] for e in c.entries if e["channel"] == g["id"]]
    assert any("agreed by everyone" in t for t in group)
    assert any("declare war on Montezuma in Civ" in t for t in group)               # yours is yours to declare
    [mine] = [x for x in server.state(0)["pacts"] if x["id"] == p.key]
    assert mine["group"] == g["id"] and mine["status"] == "active" and mine["war_on"] == "Montezuma"
    view = next(x for x in server.state(0)["groups"] if x["id"] == g["id"])
    assert view["pacts"][0]["binds"] == ["you", "Gilgamesh", "Cleopatra"]
    assert {"a": "Gilgamesh", "b": "Cleopatra", "stance": "peace"} in view["relations"]


def test_one_no_and_the_group_pact_is_off():
    c = three_leaders(ScriptedClient(lambda s, m: "{}"))
    g = c.create_group("Trio", [1, 2])
    res, _ = c.human_offer_group_pact(g["id"], "No wars", "No wars between us.", None)
    assert res.ok
    p = c.pacts.of_group(g["id"])[0]
    assert c.answer_pact(1, p.key, True).ok and p.status == "offered" and p.waiting_for() == [2]
    assert c.answer_pact(2, p.key, False).ok and p.status == "declined"
    assert any("Cleopatra turned down the group pact" in e["text"] for e in c.entries if e["channel"] == g["id"])


def test_walking_out_early_is_breaking_it_and_everyone_hears():
    c = three_leaders(ScriptedClient(lambda s, m: "{}"))
    g = c.create_group("Trio", [1, 2, 3])
    c.human_offer_group_pact(g["id"], "Peace", "No wars between us.", 30)
    p = c.pacts.of_group(g["id"])[0]
    for m in (1, 2, 3):
        c.answer_pact(m, p.key, True)
    assert p.status == "active"
    assert c.end_pact(3, p.key, "Huitzilopochtli demands it").message == "broke"
    assert p.parties == [0, 1, 2] and p.broke == [3] and p.status == "active"
    assert any("Montezuma broke the group pact" in e["text"] for e in c.entries if e["channel"] == g["id"])
    ctx = c.agents[1].build_context(c.game, "turn", [])
    assert any("Montezuma broke group pact" in x for x in ctx["pacts_that_ended_recently"])
    [view] = ctx["your_group_chats"]
    assert view["group_pacts"][0]["walked_out"] == ["Montezuma (broke it)"]


def test_a_member_can_be_voted_out_by_a_majority():
    c = three_leaders(ScriptedClient(lambda s, m: "{}"))
    g = c.create_group("Four", [1, 2, 3])
    c.human_offer_group_pact(g["id"], "Peace", "No wars.", None)
    p = c.pacts.of_group(g["id"])[0]
    assert c.vote_out(0, g["id"], 3).ok and "3" in g["votes"]                    # 1 of 2 needed
    ctx = c.agents[2].build_context(c.game, "turn", [])
    assert "you haven't voted" in ctx["your_group_chats"][0]["votes"][0]
    assert c.vote_out(1, g["id"], "Montezuma").message == "Montezuma voted out"
    assert g["members"] == [0, 1, 2] and 3 not in p.members and "3" not in g["votes"]
    assert not c.vote_out(0, g["id"], 3).ok                                     # not a member any more


def test_the_founder_sets_the_groups_goal_and_everyone_sees_it():
    c = three_leaders(ScriptedClient(lambda s, m: "{}"))
    g = c.create_group("Trio", [1, 2])
    assert not c.set_group_goal(1, g["id"], "Rule Sumeria's way").ok          # the founder is still here
    assert c.set_group_goal(0, g["id"], "Keep the Aztecs out of the south").ok
    view = c.agents[2].build_context(c.game, "turn", [])["your_group_chats"][0]
    assert view["goal"].startswith("Keep the Aztecs out of the south (set by Me")
    c.leave_group(0, g["id"])
    assert c.set_group_goal(1, g["id"], "Sumeria first").ok                    # founder gone: anyone may


def test_a_leader_proposes_a_group_pact_and_you_can_invite_from_the_browser():
    from civ_council.web import Server
    gil = once(reply(actions=[{"type": "propose_pact", "group": "g1", "title": "Bloc", "terms": "Vote together."}]))
    c = three_leaders(gil)
    g = c.create_group("Duo", [1])
    server = Server(c)
    assert server.handle("/api/group_invite", {"id": g["id"], "member": 2}) == {"ok": True}
    assert g["members"] == [0, 1, 2]
    c.end_turn()
    [p] = c.pacts.of_group(g["id"])
    assert p.a == 1 and p.members == [0, 1, 2]
    card = next(e for e in c.entries if e["channel"] == g["id"] and e["kind"] == "pact")
    assert card["pact_id"] == p.id and not card.get("secret")
    [offer] = [x for x in server.state(0)["pacts"] if x["id"] == p.key]
    assert offer["needs_you"] and offer["from"] == "Gilgamesh"
    assert p.status == "offered" and p.waiting_for() == [0, 2]                  # Cleopatra hasn't answered yet
    offers = c.agents[2].build_context(c.game, "turn", [])["proposals_to_you"]
    assert [x["id"] for x in offers if x["kind"] == "group pact"] == [p.key]


def test_group_pacts_are_saved(tmp_path):
    from civ_council.pacts import PactBook
    book = PactBook()
    p = book.offer_group(1, "g1", [0, 1, 2], "T", "Terms", 10, 5, war_on=3)
    book.answer_group(p.key, 0, True, 5)
    book.save(tmp_path / "pacts.json")
    again = PactBook.load(tmp_path / "pacts.json")
    q = again.group_pact(p.key)
    assert q.members == [0, 1, 2] and q.accepted == [1, 0] and q.war_on == 3 and again.next_id == 2


def test_leaders_answer_every_one_of_your_group_messages():
    gil = ScriptedClient(lambda s, m: reply(messages=[{"to": "g1", "text": "on it"}]))
    c = three_leaders(gil)
    g = c.create_group("Duo", [1])
    for i in range(5):                          # the per-turn cap used to swallow replies 4 and 5
        c.answer(c.post(g["id"], f"question {i}"))
    said = [e["text"] for e in c.entries if e["channel"] == g["id"] and e["kind"] == "message" and e.get("sender") == 1]
    assert len(said) == 5


def test_group_news_reaches_the_members_memory_and_pacts_say_if_they_are_in_force():
    c = three_leaders(ScriptedClient(lambda s, m: "{}"))
    g = c.create_group("Trio", [1, 2])
    c.human_offer_group_pact(g["id"], "Peace", "No wars.", None)
    p = c.pacts.of_group(g["id"])[0]
    c.answer_pact(1, p.key, True)
    view = c.agents[1].build_context(c.game, "turn", [])["your_group_chats"][0]
    assert view["group_pacts"][0]["status"].startswith("offered")
    c.answer_pact(2, p.key, True)
    view = c.agents[1].build_context(c.game, "turn", [])["your_group_chats"][0]
    assert view["group_pacts"][0]["status"].startswith("IN FORCE")
    assert any("agreed by everyone" in x["text"] for x in view["recent_conversation"])
    c.set_group_goal(0, g["id"], "Hold the south")
    assert any("goal" in x["text"] for x in c.agents[2].memory.conversations[g["id"]])


# ---- the History page --------------------------------------------------------------
def test_the_game_keeps_its_story_even_what_spy_would_hide(tmp_path):
    from civ_council.web import HISTORY_PAGE, Server

    def cleo(system, messages):
        ids = [int(x) for x in _re.findall(r'"id":\s*(\d+),\s*"kind":\s*"propose_deal"', messages[0]["content"])]
        return reply(proposal_responses=[{"id": i, "accept": True} for i in ids])
    gil = once(reply(messages=[{"to": "all", "text": "Sumeria stands."}],
                     actions=[{"type": "propose_deal", "target": 2, "give": {"gold": 5}, "ask": {"gold": 1}},
                              {"type": "declare_war", "target": 3}]))
    c = three_leaders(gil, ScriptedClient(cleo))
    c.game.civ(1).gold, c.game.civ(2).gold = 50, 50
    assert c.spy is False
    c.end_turn()
    kinds = {h["kind"] for h in c.history}
    assert {"said", "deal", "war"} <= kinds
    said = next(h for h in c.history if h["kind"] == "said")
    assert said["by"] == 1 and said["text"] == "Sumeria stands." and 1 in said["civs"]
    deal = next(h for h in c.history if h["kind"] == "deal")
    assert "made a deal" in deal["text"] and deal["civs"] == [1, 2]
    assert not any(e["channel"] == "spy" for e in c.entries)              # nothing overheard without Spy...
    c.save_memories(tmp_path)                                               # ...but the story keeps it
    again = three_leaders(ScriptedClient(lambda s, m: "{}"))
    again.load_memories(tmp_path)
    assert again.history == c.history
    h = Server(c).history()
    assert [x["leader"] for x in h["civs"]] == ["Me", "Gilgamesh", "Cleopatra", "Montezuma"]
    assert h["events"] == c.history and h["turn"] == c.game.turn
    assert "Trust web" in HISTORY_PAGE.read_text()


def test_the_story_is_rebuilt_from_old_chats_the_first_time(tmp_path):
    c = three_leaders(ScriptedClient(lambda s, m: "{}"))
    c.entries = [
        {"id": 1, "turn": 40, "channel": "world", "kind": "war", "text": "Gilgamesh of Sumeria declared war on Montezuma of Aztec!"},
        {"id": 2, "turn": 41, "channel": "spy", "kind": "treaty", "text": "Gilgamesh of Sumeria and Cleopatra of Egypt made a deal: x."},
        {"id": 3, "turn": 41, "channel": "spy", "kind": "treaty", "text": "Cleopatra of Egypt rejected Gilgamesh of Sumeria's offer of a deal."},
        {"id": 4, "turn": 42, "channel": "minds", "kind": "thought", "text": "private"},
        {"id": 5, "turn": 42, "channel": "world", "kind": "message", "sender": 2, "text": "Egypt is eternal."},
        {"id": 6, "turn": 43, "channel": 1, "kind": "error", "pact_id": 3, "text": "Gilgamesh broke the pact with you: Peace."},
    ]
    c._load_history(tmp_path)                                              # no history.json yet
    assert [(h["turn"], h["kind"]) for h in c.history] == [(40, "war"), (41, "deal"), (42, "said"), (43, "pact")]
    assert c.history[0]["civs"] == [1, 3] and c.history[2]["by"] == 2


# ---- your advisor ------------------------------------------------------------------
PICKS = json.dumps({"headline": "Get walls up before Montezuma arrives.",
                    "research": {"pick": "Masonry", "why": "walls"}, "civic": {"pick": "Code of Laws", "why": "slots"},
                    "cities": [{"city": "London", "build": "Ancient Walls", "why": "closest to the Aztecs"}],
                    "diplomacy": ["Take Cleopatra's friendship."], "watch_out": ["Aztec warriors near London."]})


def _with_advisor(brain, **kw):
    from civ_council.advisor import Advisor
    c = three_leaders(ScriptedClient(lambda s, m: "{}"), **kw)
    seen = []
    def wrapped(system, messages):
        seen.append(messages[0]["content"])
        return brain(system, messages)
    c.advisor = Advisor(ScriptedClient(wrapped, name="claude-code:sonnet"), usage_log=c.usage)
    return c, seen


def test_the_advisor_knows_only_what_you_would_know():
    c, seen = _with_advisor(lambda s, m: "Trust her, for now.", spy=True)
    c.game.has_met = lambda a, b: not ({a, b} == {0, 3})            # you haven't met Montezuma
    c._deliver(1, 0, "Gilgamesh to you: let's be friends.")
    c._deliver(1, 2, "Gilgamesh to Cleopatra: the Englishman is weak.")   # private between AIs: Spy only
    c._deliver(3, "all", "Montezuma to the world: tremble.")              # you haven't met him: secret
    assert c.ask_advisor("should I trust Gilgamesh?")
    c.advisor_reply()
    ctx = seen[-1]
    assert "let's be friends" in ctx and "should I trust Gilgamesh?" in ctx
    assert "the Englishman is weak" not in ctx and "tremble" not in ctx               # Spy-only and secret talk
    assert '"trust":' not in ctx and "your_view_of_them" not in ctx                  # the leaders' minds
    said = [e for e in c.entries if e["channel"] == "advisor"]
    assert [(e["kind"], e.get("advisor", False)) for e in said] == [("message", False), ("message", True)]
    assert said[-1]["text"] == "Trust her, for now."
    assert c.usage[-1]["leader"] == "Advisor" and c.usage[-1]["purpose"] == "advisor"


def test_the_advisor_gives_its_picks_each_turn_next_to_civs():
    c, seen = _with_advisor(lambda s, m: PICKS)
    notes = []
    c.game.notify = lambda *a: notes.append(a)
    c.end_turn()
    [card] = [e for e in c.entries if e["channel"] == "advisor" and e["kind"] == "picks"]
    assert card["text"] == "Get walls up before Montezuma arrives."
    assert card["picks"]["research"]["pick"] == "Masonry" and card["picks"]["cities"][0]["build"] == "Ancient Walls"
    assert notes and "research Masonry" in notes[0][2] and "London: Ancient Walls" in notes[0][2]
    assert c.advisor.last_picks == card["picks"]
    c.advisor.client._fn = lambda s, m: "not json"                       # a bad answer: no card, no harm
    assert c.advisor_picks() is None


def test_you_chat_with_the_advisor_from_the_browser_and_can_turn_it_off():
    from civ_council.web import Server
    c, _ = _with_advisor(lambda s, m: "Build a Campus in York.")
    server = Server(c)
    assert server.state(0)["advisor"] is True
    assert server.handle("/api/advisor", {"text": "what next?"}) == {"ok": True}
    _wait(server)
    assert [e["text"] for e in c.entries if e["channel"] == "advisor"] == ["what next?", "Build a Campus in York."]
    off = build(make_parser("t").parse_args(["--advisor-model", "none"]))
    assert off.advisor is None and Server(off).handle("/api/advisor", {"text": "hi"})["ok"] is False
    offline = build(make_parser("t").parse_args([]))
    assert offline.advisor is not None and offline.advisor_picks() is None   # the fake provider gives no picks


# ---- Settings: models and the advisor's switch -----------------------------------------
def test_models_can_be_switched_from_the_browser_and_are_saved_with_the_game(tmp_path):
    from civ_council.web import Server
    made = []
    def factory(model, timeout=None):
        client = ScriptedClient(lambda s, m: "{}", name=f"claude-code:{model}")
        client.timeout = timeout
        made.append((model, timeout))
        return client
    c, _ = _with_advisor(lambda s, m: PICKS)
    c.llm_factory, c.memory_dir = factory, tmp_path
    server = Server(c)
    assert server.state(0)["settings"]["editable"] is True
    assert server.handle("/api/settings", {"turn": "sonnet", "strategy": "off", "advisor": "opus"}) == {"ok": True}
    st = server.state(0)["settings"]
    assert (st["turn"], st["strategy"], st["advisor"]) == ("sonnet", "off", "opus")
    assert all(a.client.name == "claude-code:sonnet" and a.strategy_client is None for a in c.agents.values())
    assert len({id(a.client) for a in c.agents.values()}) == len(c.agents)     # each leader its own client
    assert server.handle("/api/settings", {"talk": "gpt"})["ok"] is False
    assert server.handle("/api/settings", {"talk": "haiku"}) == {"ok": True}
    assert ("haiku", 45) in made                                                # you're waiting on talk: a timeout
    _wait(server)
    c.save_memories(tmp_path)
    again, _ = _with_advisor(lambda s, m: "{}")
    again.llm_factory = factory
    again.load_memories(tmp_path)
    st = again.model_settings()
    assert (st["turn"], st["talk"], st["strategy"], st["advisor"]) == ("sonnet", "haiku", "off", "opus")


def test_the_advisor_switch_stops_its_picks_and_answers():
    from civ_council.web import Server
    c, seen = _with_advisor(lambda s, m: PICKS)
    server = Server(c)
    assert server.handle("/api/settings", {"advisor_on": False}) == {"ok": True}
    assert server.state(0)["settings"]["advisor_on"] is False
    c.end_turn()
    assert seen == [] and not any(e["channel"] == "advisor" for e in c.entries)   # nothing used
    assert "off" in server.handle("/api/advisor", {"text": "hi"})["error"]
    server.handle("/api/settings", {"advisor_on": True})
    assert c.advisor_picks() is not None


def test_other_providers_keep_their_models_as_started():
    from civ_council.web import Server
    offline = build(make_parser("t").parse_args([]))
    st = Server(offline).state(0)["settings"]
    assert st["editable"] is False and "only" in offline.set_models(turn="opus")
