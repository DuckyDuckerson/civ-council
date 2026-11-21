"""Browser chat for the council.

    python -m civ_council.web                                   # http://localhost:8765
    python -m civ_council.web --provider claude-code --model haiku
    python -m civ_council.web --lan     # also reachable from your phone on the same wifi

One background worker runs the slow LLM calls in order; the page polls for new messages,
so it stays responsive while leaders are thinking.
"""
from __future__ import annotations

import json
import queue
import time
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .cli import build, make_parser
from .council import SPY, WORLD, Council
from .game import proposal_label

PAGE = Path(__file__).with_name("static") / "index.html"
HISTORY_PAGE = Path(__file__).with_name("static") / "history.html"
MEMORY_DIR = Path("memories")
ACTIONS = {"declare_war", "make_peace", "propose_friendship", "propose_alliance", "denounce", "break_alliance"}


class Server:
    def __init__(self, council: Council):
        self.c = council
        self.live = hasattr(council.game, "wait_for_turns")   # the real Civ 6: turns end in the game
        self.memory_dir = council.memory_dir or MEMORY_DIR
        self.jobs: queue.Queue = queue.Queue()
        self.pending = 0
        self.turn_queued = False     # a leaders' turn is waiting or running
        self.turn_again = False      # Civ moved on while it ran: run once more when it's done
        self.boot = f"{time.time():.3f}"   # pages reset when the server restarts (message ids start over)
        self._lock = threading.Lock()
        # A real game saves by itself (a per-game folder); the offline demo only when you press Save.
        self.autosave = council.memory_dir is not None
        threading.Thread(target=self._work, daemon=True).start()

    def save(self) -> bool:
        """Everything to disk: memories, chat, pacts, usage. Tries again if a chat changed mid-save."""
        for _ in range(3):
            try:
                self.c.save_memories(self.memory_dir)
                return True
            except RuntimeError:   # "dictionary changed size during iteration": someone was writing
                time.sleep(0.05)
        return False

    def _work(self) -> None:
        while True:
            fn = self.jobs.get()
            try:
                fn()
            except Exception as e:  # keep the worker alive whatever happens
                self.c._record(WORLD, "error", f"Something went wrong: {e}")
            finally:
                if self.autosave and self.jobs.empty():   # caught up: save what just happened
                    try:
                        self.save()
                    except Exception:
                        pass
                with self._lock:
                    self.pending -= 1

    def submit(self, fn) -> None:
        with self._lock:
            self.pending += 1
        self.jobs.put(fn)

    def new_game_turn(self, turn: int) -> None:
        """Called by the turn watcher when you end your turn in Civ 6.

        If the leaders are still busy with the last turn, they take one more turn when they finish
        (covering everything that happened meanwhile) instead of one per missed turn.
        """
        with self._lock:
            if self.turn_queued:
                self.turn_again = True
                return
            self.turn_queued = True

        def run():
            try:
                self.c.on_new_turn()
            finally:
                with self._lock:
                    again, self.turn_again = self.turn_again, False
                    self.turn_queued = again
                if again:
                    self.submit(run)
        self.submit(run)

    def state(self, since: int) -> dict:
        c = self.c
        snap = c.game.snapshot(c.human_id)
        leaders = []
        for o in snap["others"]:
            agent = c.agents.get(o["id"])
            leaders.append({**{k: o[k] for k in ("id", "leader", "civ", "stance_with_you", "they_denounced_you",
                                                  "you_denounced_them", "cities", "military", "score")},
                            "diplomacy": self._diplomacy(o, agent, snap),
                            "model": (agent.client.name + (f" (talks with {agent.talk_client.name.split(':')[-1]})"
                                                             if agent.talk_client else "")) if agent else "human"})
        lasts = getattr(c.game, "proposal_turns", None)   # how long an offer waits (real game)
        dead = [{"id": x.id, "leader": x.leader, "civ": x.name} for x in c.game.civs()
                if not x.alive and x.id != c.human_id]
        return {
            "turn": snap["turn"],
            "me": snap["you"],
            "leaders": leaders,
            "eliminated": dead,
            "proposals": [{"id": p.id, "from_id": p.sender, "from": c.game.civ(p.sender).leader,
                           "label": proposal_label(p), "turn": p.turn, "kind": p.kind,
                           **({"you_give": p.terms.get("ask") or {}, "you_get": p.terms.get("give") or {},
                               "peace": bool(p.terms.get("peace"))} if p.kind == "propose_deal" else {}),
                           **({"expires_in": max(0, lasts - (snap["turn"] - p.turn))} if lasts else {})}
                          for p in c.game.pending_proposals(c.human_id)],
            "wars": snap["world_wars"],
            "alliances": snap["world_alliances"],
            "status": c.status,
            "busy": self.pending > 0,
            "autosave": self.autosave,
            "advisor": c.advisor is not None,
            "settings": c.model_settings(),
            "live": self.live,
            "boot": self.boot,
            "spy": c.spy,
            "groups": self._groups(snap["turn"]),
            "pacts": self._pacts(snap["turn"]),
            "usage": self._usage(),
            "game": str(getattr(c.game, "game_id", "") or "offline"),   # the page keeps what you've read per game
            **({"trust": self._trust(), "trust_ids": {c._name(x.id): x.id for x in c.game.civs()}} if c.spy else {}),
            "civ_notify": c.civ_notify if hasattr(c.game, "notify") else None,
            "quiet_civ_ai": getattr(c.game, "quiet_ai", None),
            "block_civ_screen": getattr(c.game, "block_screen", None),
            "entries": [e for e in list(c.entries) if e["id"] > since],
        }

    DIPLOMACY_KEYS = ("their_feelings_about_you", "feelings_total", "friendship_ends_in_turns", "alliance_ends_in_turns",
                      "grievances", "trade_routes_between_you", "government", "era", "age", "religion",
                      "nuclear_weapons", "their_army", "science_per_turn", "culture_per_turn", "gold", "faith",
                      "techs_known", "civics_known", "districts", "wonders", "their_cities", "their_continents",
                      "cities_close_to_yours", "their_troops_near_your_cities", "which_of_their_units",
                      "cities_they_conquered", "city_states_backing_them", "military_with_city_states")

    def _diplomacy(self, o: dict, agent, snap: dict) -> dict:
        """Everything Civ's own leader screen would show about this leader (so it never needs opening)."""
        c, leader = self.c, o["leader"]
        out = {k: o[k] for k in self.DIPLOMACY_KEYS if o.get(k) not in (None, [], {}, "")}
        if agent is not None:
            out["agenda"] = agent.persona.agenda
        rel = {"friends": [], "allies": [], "at_war_with": [], "denounced": []}
        for x in c.game.civs():
            if x.id in (o["id"], c.human_id) or not x.alive or not c._met(c.human_id, x.id):
                continue
            stance = c.game.stance(o["id"], x.id)
            key = {"declared_friends": "friends", "allied": "allies", "war": "at_war_with"}.get(stance)
            if key:
                rel[key].append(x.leader)
            if c.game.is_denouncing(o["id"], x.id):
                rel["denounced"].append(x.leader)
        out["relationships"] = {k: v for k, v in rel.items() if v}
        deals = [d for d in snap["you"].get("your_ongoing_deals", []) if leader in d]
        if deals:
            out["deals_with_you"] = deals
        pacts = [f"{p.title}: {p.terms}" + (f" ({p.ends - snap['turn']} turns left)" if p.ends else " (open-ended)")
                 for p in c.pacts.active(c.human_id) if p.other(c.human_id) == o["id"]]
        if pacts:
            out["pacts_with_you"] = pacts
        return out

    def _trust(self) -> dict:
        """Spy only: each leader's trust in everyone it knows, over time (from its own memory)."""
        c, out = self.c, {}
        for aid, agent in c.agents.items():
            lines = {}
            for other, rel in agent.memory.relationships.items():
                points = [list(p) for p in rel.history] or ([[c.game.turn, rel.trust]] if rel.trust else [])
                if points:
                    if points[-1][0] < c.game.turn:
                        points = points + [[c.game.turn, rel.trust]]
                    lines[c._name(other)] = points[-120:]
            if lines:
                out[c._name(aid)] = lines
        return out

    def _usage(self) -> dict:
        """The usage meter: per turn (calls, seconds, tokens, equivalent cost) and totals by model and purpose."""
        c = self.c
        calls = list(c.usage)
        turns: dict[int, dict] = {}
        for u in calls:
            t = turns.setdefault(int(u.get("turn", 0)), {"turn": int(u.get("turn", 0)), "calls": 0, "model_seconds": 0.0,
                                                          "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "models": {}})
            t["calls"] += 1
            t["model_seconds"] += float(u.get("seconds") or 0)
            t["tokens_in"] += int(u.get("input_tokens") or 0)
            t["tokens_out"] += int(u.get("output_tokens") or 0)
            t["cost"] += float(u.get("cost_usd") or 0)
            t["models"][u.get("model", "?")] = t["models"].get(u.get("model", "?"), 0) + 1
        for t in turns.values():
            t["wall_seconds"] = c.turn_seconds.get(t["turn"])
            t["model_seconds"], t["cost"] = round(t["model_seconds"], 1), round(t["cost"], 3)
        ordered = sorted(turns.values(), key=lambda t: -t["turn"])
        recent = ordered[:25]
        game = c.usage_totals()
        rows = sorted(({"for": k.split("|")[0], "model": k.split("|")[1], "calls": r["calls"], "cost": round(r["cost"], 3)}
                       for k, r in game.pop("rows", {}).items()), key=lambda r: -r["cost"])
        game = {k: round(v, 3) if isinstance(v, float) else v for k, v in game.items()}
        return {"turns": ordered[:12], "game": game, "rows": rows, "recent_turns": len(recent),
                "recent_cost": round(sum(t["cost"] for t in recent), 3)}

    def _report(self, res) -> None:
        if not res.ok:
            self.c._record(WORLD, "error", res.message, f"❌ {res.message}")

    def _pacts(self, turn: int) -> list[dict]:
        """Your pacts: offers waiting for your answer, yours waiting for theirs, and the ones in force."""
        c, me, out = self.c, self.c.human_id, []
        for p in c.pacts.pacts.values():
            if me not in (p.a, p.b) or p.status not in ("offered", "active"):
                continue
            other = p.other(me)
            out.append({"id": p.key, "with_id": other, "with": c.game.civ(other).leader, "title": p.title,
                        "terms": p.terms, "status": p.status, "from_me": p.a == me, "length": p.length(),
                        "payment": p.payment("you" if p.a == me else c.game.civ(p.a).leader,
                                             "you" if p.b == me else c.game.civ(p.b).leader),
                        **({"turns_left": p.ends - turn} if p.status == "active" and p.ends else {}),
                        **({"expires_in": max(0, 3 - (turn - p.offered))} if p.status == "offered" else {})})
        for p in c.pacts.group_pacts.values():   # group pacts you're bound by or asked to join
            if me not in p.parties or p.status not in ("offered", "active"):
                continue
            g = c.groups.get(p.group, {})
            out.append({"id": p.key, "group": p.group, "with": g.get("name", p.group), "title": p.title,
                        "terms": p.terms, "status": p.status, "from_me": p.a == me, "length": p.length(),
                        "from": c._group_name(p.a), "needs_you": p.status == "offered" and me in p.waiting_for(),
                        "binds": [c._group_name(m) for m in p.parties],
                        "waiting_for": [c._group_name(m) for m in p.waiting_for()] if p.status == "offered" else [],
                        **({"war_on": c.game.civ(p.war_on).leader} if p.war_on is not None else {}),
                        **({"turns_left": p.ends - turn} if p.status == "active" and p.ends else {}),
                        **({"expires_in": max(0, 3 - (turn - p.offered))} if p.status == "offered" else {})})
        return out

    def _groups(self, turn: int) -> list[dict]:
        """Group chats, with what the info panel shows: goal, votes, group pacts, and how the members get along."""
        c, me, out = self.c, self.c.human_id, []
        for g in c.groups.values():
            view = dict(g)
            if me in g["members"] or c.spy:
                pairs = []
                ms = g["members"]
                for i, a in enumerate(ms):
                    for b in ms[i + 1:]:
                        known = c.spy or all(x == me or c._met(me, x) for x in (a, b))
                        pairs.append({"a": c._group_name(a), "b": c._group_name(b),
                                      "stance": c.game.stance(a, b) if known else "unknown"})
                view["relations"] = pairs
                view["pacts"] = [
                    {"id": p.key, "title": p.title, "terms": p.terms, "status": p.status, "length": p.length(),
                     "from": c._group_name(p.a), "binds": [c._group_name(m) for m in p.parties],
                     "mine": me in p.parties, "needs_you": p.status == "offered" and me in p.waiting_for(),
                     "from_me": p.a == me,
                     "waiting_for": [c._group_name(m) for m in p.waiting_for()] if p.status == "offered" else [],
                     "walked_out": [c._group_name(m) + (" (broke it)" if m in p.broke else "") for m in p.left],
                     **({"war_on": c.game.civ(p.war_on).leader} if p.war_on is not None else {}),
                     **({"turns_left": p.ends - turn} if p.status == "active" and p.ends else {})}
                    for p in c.pacts.of_group(g["id"]) if p.status in ("offered", "active")]
                view["vote_list"] = [{"member": int(t), "name": c._group_name(int(t)), "yes": len(v["yes"]),
                                      "no": len(v["no"]), "needed": c._votes_needed(g, int(t)),
                                      "by": c._group_name(v["by"]),
                                      "your_vote": "yes" if me in v["yes"] else "no" if me in v["no"] else None}
                                     for t, v in (g.get("votes") or {}).items()]
                if g.get("goal"):
                    view["goal_text"] = g["goal"]["text"]
                    view["goal_by"] = c._group_name(g["goal"]["by"])
                founder = me if g.get("started_by") is None else g["started_by"]
                view["can_set_goal"] = me in g["members"] and (founder == me or founder not in g["members"])
            out.append(view)
        return out

    def history(self) -> dict:
        """The History page: the game's story, every civ, and everyone's trust in everyone over time."""
        c = self.c
        civs = [{"id": x.id, "leader": x.leader, "civ": x.name, "human": x.id == c.human_id, "alive": x.alive}
                for x in c.game.civs() if x.id == c.human_id or x.id in c.agents]
        trust = {}
        for aid, agent in c.agents.items():
            lines = {}
            for other, rel in agent.memory.relationships.items():
                pts = [list(p) for p in rel.history] or ([[c.game.turn, rel.trust]] if rel.trust else [])
                if pts:
                    lines[str(other)] = pts
            if lines:
                trust[str(aid)] = lines
        return {"turn": c.game.turn, "civs": civs, "events": list(c.history), "trust": trust}

    def trade(self, with_id: int) -> dict:
        """What you and one leader could put in a deal right now."""
        options = getattr(self.c.game, "trade_options", None)
        if options is None:
            return {"ok": False, "error": "this game has no trading"}
        try:
            mine = options(self.c.human_id)
        except Exception as e:   # the game being busy shouldn't break the page
            return {"ok": False, "error": f"couldn't read the trade options ({e})"}
        for entry in mine.values():
            if entry.get("civ_id") == with_id:
                return {"ok": True, "you_can_give": entry["you_can_give"], "they_can_give": entry["they_can_give"],
                        "at_war": bool(entry.get("at_war"))}
        return {"ok": False, "error": "you can't trade with them right now (not met, or at war)"}

    def handle(self, path: str, body: dict) -> dict:
        c = self.c
        if path == "/api/say":
            text = str(body.get("text", "")).strip()[:1000]
            if not text:
                return {"ok": False, "error": "empty message"}
            to = body.get("to")
            target = "all" if to == WORLD else to if str(to) in c.groups else int(to)
            responders = c.post(target, text, body.get("reply_to"))   # shows up right away, even mid-turn
            if responders:
                self.submit(lambda: c.answer(responders))
        elif path == "/api/deal":
            res, responders = c.offer_deal(body.get("to"), body.get("give") or {}, body.get("ask") or {},
                                           str(body.get("note", "")), counter_to=body.get("counter_to"))
            if not res.ok:
                return {"ok": False, "error": res.message}
            if responders:
                self.submit(lambda: c.answer(responders))
        elif path == "/api/group":
            members = [int(m) for m in body.get("members", [])]
            g = c.create_group(str(body.get("name", "")), members)
            return {"ok": True, "group": g}
        elif path == "/api/group_leave":
            res = c.leave_group(c.human_id, str(body.get("id", "")))
            return {"ok": res.ok, **({} if res.ok else {"error": res.message})}
        elif path == "/api/act":
            if body.get("type") not in ACTIONS:
                return {"ok": False, "error": "unknown action"}
            self.submit(lambda: c.act({"type": body["type"], "target": int(body["target"])}))
        elif path == "/api/pact":
            give, ask = body.get("give") or {}, body.get("ask") or {}
            res, responders = c.human_offer_pact(body.get("to"), body.get("title"), body.get("terms"),
                                                 body.get("turns"), give=give, ask=ask, note=body.get("note", ""))
            if not res.ok:
                return {"ok": False, "error": res.message}
            if responders:
                self.submit(lambda: c.answer(responders))
        elif path == "/api/settings":
            if "advisor_on" in body:
                c.advisor_on = bool(body["advisor_on"])
            why = c.set_models(**{k: body[k] for k in c.MODEL_JOBS if body.get(k)}) if any(
                body.get(k) for k in c.MODEL_JOBS) else ""
            if why:
                return {"ok": False, "error": why}
        elif path == "/api/advisor":
            if not c.ask_advisor(body.get("text", "")):
                return {"ok": False, "error": "your advisor is off (turn it on in ⚙ Settings)" if c.advisor is not None
                        else "no advisor in this game (started with --advisor-model none?)"}
            self.submit(c.advisor_reply)
        elif path == "/api/advisor_picks":
            if c.advisor is None:
                return {"ok": False, "error": "no advisor in this game"}
            self.submit(c.advisor_picks)
        elif path == "/api/group_pact":
            res, responders = c.human_offer_group_pact(str(body.get("group", "")), body.get("title"), body.get("terms"),
                                                       body.get("turns"), war_on=body.get("war_on"),
                                                       note=body.get("note", ""))
            if not res.ok:
                return {"ok": False, "error": res.message}
            if responders:
                self.submit(lambda: c.answer(responders))
        elif path in ("/api/group_invite", "/api/group_vote", "/api/group_goal"):
            gid = str(body.get("id", ""))
            res = (c.invite_to_group(c.human_id, gid, body.get("member")) if path == "/api/group_invite"
                   else c.vote_out(c.human_id, gid, body.get("member"), bool(body.get("yes", True)))
                   if path == "/api/group_vote" else c.set_group_goal(c.human_id, gid, body.get("text", "")))
            if not res.ok:
                return {"ok": False, "error": res.message}
        elif path == "/api/pact_answer":   # a payment may go through the game: on the worker, like other moves
            pid, yes = body.get("id"), bool(body.get("accept"))
            self.submit(lambda: self._report(c.answer_pact(c.human_id, pid, yes)))
        elif path == "/api/pact_end":
            pid, note = body.get("id"), body.get("note", "")
            self.submit(lambda: self._report(c.end_pact(c.human_id, pid, note)))
        elif path == "/api/prefs":
            if "civ_notify" in body:
                c.civ_notify = bool(body["civ_notify"])
            if "block_civ_screen" in body and hasattr(c.game, "set_block_screen"):
                on_b = bool(body["block_civ_screen"])
                self.submit(lambda: c.game.set_block_screen(on_b))
            if "quiet_civ_ai" in body and hasattr(c.game, "set_quiet_ai"):
                on = bool(body["quiet_civ_ai"])
                self.submit(lambda: c.game.set_quiet_ai(on))   # touches the game: on the worker
        elif path == "/api/respond":
            self.submit(lambda: c.respond(int(body["id"]), bool(body.get("accept"))))
        elif path == "/api/end_turn":
            if self.live:
                return {"ok": False, "error": "End your turn in Civ 6; the leaders follow automatically."}
            self.submit(c.end_turn)
        elif path == "/api/spy":
            c.spy = bool(body.get("on"))
        elif path == "/api/save":
            self.submit(lambda: (c.save_memories(self.memory_dir),
                                 c._record(WORLD, "info", f"Leader memories saved to {self.memory_dir.resolve()}")))
        elif path == "/api/load":
            self.submit(lambda: (c.load_memories(self.memory_dir),
                                 c._record(WORLD, "info", "Leader memories loaded.")))
        else:
            return {"ok": False, "error": "not found"}
        if self.autosave and path not in ("/api/save", "/api/load"):
            self.submit(lambda: None)   # the worker saves once it's caught up
        return {"ok": True}


def make_handler(server: Server):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path in ("/history", "/history.html"):
                self._send(200, HISTORY_PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/history"):
                self._send(200, json.dumps(server.history()).encode(), "application/json")
            elif self.path.startswith("/api/trade"):
                try:
                    with_id = int(self.path.split("with=")[1].split("&")[0])
                except (IndexError, ValueError):
                    self._send(400, b'{"ok": false, "error": "which leader?"}', "application/json")
                    return
                self._send(200, json.dumps(server.trade(with_id)).encode(), "application/json")
            elif self.path.startswith("/api/state"):
                since = 0
                if "since=" in self.path:
                    try:
                        since = int(self.path.split("since=")[1].split("&")[0])
                    except ValueError:
                        pass
                self._send(200, json.dumps(server.state(since)).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            try:
                n = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                result = server.handle(self.path, body)
            except (ValueError, KeyError, TypeError) as e:
                result = {"ok": False, "error": str(e)}
            self._send(200 if result.get("ok") else 400, json.dumps(result).encode(), "application/json")

    return Handler


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "your-mac-ip"


def main(argv: list[str] | None = None) -> None:
    ap = make_parser("Civ Council in your browser.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true", help="allow other devices on your wifi (e.g. your phone)")
    ap.add_argument("--open", action="store_true", help="open the page in your browser once it's running")
    args = ap.parse_args(argv)
    try:
        council = build(args)
    except RuntimeError as e:   # e.g. claude CLI missing or logged out: say how to fix it
        raise SystemExit(str(e))
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    server = Server(council)
    httpd = ThreadingHTTPServer((host, args.port), make_handler(server))
    if server.live:
        threading.Thread(target=council.game.wait_for_turns, args=(server.new_game_turn,), daemon=True).start()
        met = [o["leader"] for o in council.game.snapshot(council.human_id)["others"]]
        print(f"Connected to Civ 6, turn {council.game.turn}. Leaders: "
              + ", ".join(a.persona.leader for a in council.agents.values()))
        print(f"You've met: {', '.join(met) or 'nobody yet'}. Memories: {server.memory_dir}")
        print("End your turns in Civ 6; the leaders take theirs automatically.")
    print(f"Civ Council running at http://localhost:{args.port}")
    if args.open:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()
    if args.lan:
        print(f"On your phone (same wifi): http://{lan_ip()}:{args.port}  — anyone on your wifi can open this.")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if server.autosave:
            print("\nSaving…", end=" ", flush=True)
            print("saved." if server.save() else "couldn't save (try again).")


if __name__ == "__main__":
    main()
