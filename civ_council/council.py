"""The council: routes messages between you and the AI leaders, runs their turns, applies actions.

Everything the human can see is recorded as an entry with a channel:
  - a leader's civ id  -> your private chat with that leader
  - "world"            -> the group chat: public statements, wars, treaties, world events
  - "spy"              -> private AI-to-AI messages (only when spy mode is on)
The terminal UI prints entry lines; the browser UI groups entries by channel.
"""
from __future__ import annotations

import difflib
import itertools
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .advisor import ADVISOR, picks_text
from .agent import Decision, LeaderAgent
from .game import PROPOSAL_ACTIONS, ActionResult, Civ, Game, deal_terms, proposal_label, side_text
from .memory import LeaderMemory
from .pacts import PACT_ACTIONS, GroupPact, PactBook
from .personas import PERSONAS

ALL = "all"
WORLD = "world"
NEWS = "news"      # a digest each turn of what you'd know happened (World News)
SPY = "spy"
MINDS = "minds"   # each leader's private reasoning, model and plan: a peek behind the curtain


CHAT_KEEP = 3000
HISTORY_KEEP = 20000   # the game's story for the History page: kept far longer than the chat
MAX_LEADER_GROUPS = 3   # open group chats one leader may have started at a time
USAGE_KEEP = 5000  # model calls kept for the usage meter
ACTION_NOUNS = {"propose_friendship": "an offer of friendship", "propose_alliance": "an offer of alliance",
                "make_peace": "an offer of peace", "propose_deal": "a deal", "declare_war": "a declaration of war",
                "denounce": "a denouncement", "break_alliance": "an end to your alliance"}   # chat entries kept across restarts


def history_kind(channel, kind: str, extra: dict) -> str | None:
    """Whether a chat entry belongs in the game's story (the History page), and as what."""
    if channel in (MINDS, NEWS) or kind == "turn":
        return None
    if extra.get("pact_id") is not None:
        return "pact" if kind in ("treaty", "error") else None   # agreed / broken / ended / turned down
    if channel == WORLD:
        return {"event": "event", "war": "war", "treaty": "treaty", "message": "said"}.get(kind)
    if kind == "membership":
        return "group"
    if kind == "treaty" and "rejected" not in str(extra.get("text", "")):   # deals made and gifts sent
        return "deal"
    return None


# Words in a leader's or civ's name that don't pick out anyone on their own ("North" Korea, "Lady" Trieu).
_NOT_NAMES = {"the", "of", "and", "i", "ii", "iii", "iv", "jr", "de", "la", "le", "von", "van", "al", "bin", "da",
              "lady", "king", "queen", "north", "south", "east", "west", "new", "great", "united", "states",
              "empire", "kingdom", "republic", "people", "s"}


_WORDS: set[str] | None = None


def _english(word: str) -> bool:
    """An ordinary English word ("chain" isn't a typo of China). Uses the Mac's word list when there is one."""
    global _WORDS
    if _WORDS is None:
        try:
            _WORDS = {w for w in Path("/usr/share/dict/words").read_text().split() if w.islower()}
        except OSError:
            _WORDS = set()
    return word in _WORDS


def names_in(text: str, civs: list[Civ]) -> dict[str, Civ]:
    """Who each name in your message means: "egypt" -> Cleopatra, "monte" -> Montezuma, typos too ("egpyt").

    Leaders mixed people up when you wrote a country for its leader, a nickname or a misspelling.
    """
    table: dict[str, Civ] = {}
    firsts: dict[str, Civ] = {}
    for c in civs:
        lead = [t for t in re.split(r"[^a-z]+", c.leader.lower()) if t and t not in _NOT_NAMES]
        for t in lead + [t for t in re.split(r"[^a-z]+", c.name.lower()) if t and t not in _NOT_NAMES]:
            if len(t) >= 3 or (lead and t == lead[0]):   # a two-letter first name counts, a two-letter tail doesn't
                table.setdefault(t, c)
        if lead:
            firsts[lead[0]] = c
    long = [a for a in table if len(a) >= 4]
    found: dict[str, Civ] = {}
    for w in dict.fromkeys(re.findall(r"[a-z']+", text.lower())):
        w = w.removesuffix("'s").strip("'")
        c = table.get(w)
        if c is None and 3 <= len(w) <= 5:   # cleo, monte, gil: the start of a first name
            c = next((c for f, c in firsts.items() if len(f) > len(w) and f.startswith(w)), None)
        if c is None and len(w) >= 4 and not _english(w):   # egpyt, sumera, romee: misspelled
            close = [a for a in difflib.get_close_matches(w, long, n=3, cutoff=0.8) if a[0] == w[0]]
            c = table[close[0]] if close else None
        if c is not None and w not in found:
            found[w] = c
    return found


@dataclass
class Council:
    game: Game
    agents: dict[int, LeaderAgent]
    human_id: int
    reply_rounds: int = 2        # after the main turn, how many rounds AIs get to answer each other
    spy: bool = False            # record AI-to-AI private messages in the "spy" channel
    max_pair_msgs: int = 3       # per turn, per AI sender → AI receiver; stops endless pleasantries
    summarize_after: int = 70    # a leader condenses its memory after this many new messages and notes (0 = never)
    summary_min_gap: int = 10    # ...but not more often than every this many turns
    summary_max_gap: int = 80    # ...and at least this often, once a few things are new
    feed: list[str] = field(default_factory=list)      # printable lines, for the terminal
    entries: list[dict] = field(default_factory=list)  # structured, for the browser
    status: str = ""             # e.g. "Gilgamesh is thinking…", shown while LLMs work
    _inbox: dict[int, list[dict]] = field(default_factory=dict)
    _pair_count: dict[tuple[int, int], int] = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    groups: dict[str, dict] = field(default_factory=dict)   # "g1" -> {id, name, members, member_names}
    memory_dir: Path | None = None   # if set, memories (and game state) are saved here after every turn
    parallel: int = 1                # leaders thinking at once on a turn (each is a slow LLM call)
    _save_lock: threading.RLock = field(default_factory=threading.RLock)
    _mail_lock: threading.RLock = field(default_factory=threading.RLock)   # inboxes: web thread + worker
    review_every: int = 5            # real game: each leader reviews its empire at least this often (0 = never)
    _last_review: dict[int, int] = field(default_factory=dict)
    pacts: PactBook = field(default_factory=PactBook)   # agreements in words (vassals, borders, joint wars...)
    civ_notify: bool = True          # real game: leaders' messages and offers to you also show in Civ's notifications
    strategy_every: int = 25         # grand strategies are rewritten at least this often when the era isn't known
                                     # (0 = never); in a real game the gap comes from the leader's era
    usage: list[dict] = field(default_factory=list)   # every model call: turn, leader, model, purpose, seconds, tokens
    advisor: object | None = None   # your own Claude advisor (advisor.Advisor): the Advisor channel and its picks
    advisor_on: bool = True         # the browser's switch: off = no picks, no answers, no usage
    llm_factory: object | None = None   # model name -> a new client (set by the builder), for switching models live
    models: dict = field(default_factory=dict)   # models chosen in the browser's Settings (saved per game)
    history: list[dict] = field(default_factory=list)   # [{turn, kind, text, civs}]: wars, deals, pacts... (History page)
    usage_archived: dict = field(default_factory=dict)   # totals of calls too old to keep one by one
    _replan: dict[int, str] = field(default_factory=dict)   # leader -> why it asked for a new strategy
    turn_seconds: dict[int, float] = field(default_factory=dict)   # turn -> how long the leaders took on it
    personas: dict[str, dict] = field(default_factory=dict)   # persona key -> written temperament/voice (this game)
    _persona_tried: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        for aid in self.agents:
            self._inbox.setdefault(aid, [])
            self.agents[aid].pact_book = self.pacts
            self.agents[aid].usage_log = self.usage

    # ---- group chats -------------------------------------------------------------
    def create_group(self, name: str, members: list[int], by: int | None = None) -> dict:
        """A private group chat. You start one with some leaders (you're always in it); a leader starts one
        with leaders it has met, and you only if it adds you. Only members see it (and you, with Spy on)."""
        founder = self.human_id if by is None else by
        ids = sorted({int(m) for m in members if int(m) in self.agents or int(m) == self.human_id} | {founder})
        if by is None:
            ids = sorted(set(ids) | {self.human_id})
        if len(ids) < 2:
            raise ValueError("a group needs at least one leader")
        gid = f"g{len(self.groups) + 1}"
        g = {"id": gid, "name": (name or "").strip()[:40] or "Group chat", "members": ids, "started_by": founder,
             "ever": list(ids)}   # everyone who was ever in it: you keep the chat in your list after leaving
        self.groups[gid] = g
        self._sync_groups()
        names = ", ".join(self._group_name(i) for i in ids if i != founder)
        who = "You" if founder == self.human_id else self.game.civ(founder).leader
        self._group_note(g, f"{who} started a group chat with {names}.", sender=None if by is None else by)
        return g

    def _group_name(self, civ_id: int) -> str:
        return "you" if civ_id == self.human_id else self.game.civ(civ_id).leader

    def _sync_groups(self) -> None:
        """Member names and each leader's list of its group chats (closed ones drop out)."""
        for g in self.groups.values():
            g["member_names"] = [self.game.civ(i).leader for i in g["members"]]
        for aid, agent in self.agents.items():
            agent.groups = [g for g in self.groups.values() if aid in g["members"] and not g.get("closed")]

    def _group_memory(self, gid: str, text: str) -> None:
        """Group news (who joined, a pact agreed, a vote, the goal) in every member's memory of that chat."""
        g = self.groups.get(gid)
        for m in (g or {}).get("members", []):
            if m in self.agents:
                self.agents[m].memory.log(gid, self.game.turn, "(group news)", text)

    def _group_note(self, g: dict, text: str, sender: int | None = None) -> None:
        """Who started, joined or left: in the group's own chat (secret unless you're in it)."""
        self._group_memory(g["id"], text)
        extra = {} if self.human_id in g["members"] else {"secret": True}
        self._record(g["id"], "membership", text, f"👥 [{g['name']}] {text}" if self.spy or not extra else None,
                     sender=sender, **extra)

    def leader_group(self, actor: int, op: dict) -> ActionResult:
        """A leader starts a group chat, invites someone into one, or leaves one."""
        do = str(op.get("do", "")).strip().lower()
        me = self.game.civ(actor).leader
        if do == "start":
            mine = [g for g in self.groups.values() if g.get("started_by") == actor and not g.get("closed")]
            if len(mine) >= MAX_LEADER_GROUPS:
                return ActionResult(False, f"you already run {len(mine)} group chats ({', '.join(g['id'] for g in mine)}): "
                                           "leave one first")
            raw = op.get("members") if isinstance(op.get("members"), list) else []
            ids = set()
            for m in raw[:8]:
                t = self.resolve(m)
                if isinstance(t, int) and t != actor and self.game.civ(t).alive and self._met(actor, t):
                    ids.add(t)
            if len(ids) < 2:
                return ActionResult(False, "a group chat needs at least two others you've met (for one, send a private message)")
            same = next((g for g in self.groups.values() if not g.get("closed") and set(g["members"]) == ids | {actor}), None)
            if same:
                return ActionResult(False, f"you're already in a group chat with exactly them: {same['id']}")
            g = self.create_group(str(op.get("name") or ""), sorted(ids), by=actor)
            if str(op.get("message") or "").strip():
                self._deliver(actor, g["id"], str(op["message"]).strip()[:600])
            return ActionResult(True, f"started group chat {g['id']}")
        g = self.groups.get(str(op.get("group", "")).strip())
        if g is None or g.get("closed") or actor not in g["members"]:
            return ActionResult(False, f"you're not in a group chat called {op.get('group')!r}")
        if do == "invite":
            return self.invite_to_group(actor, g["id"], op.get("member"))
        if do == "leave":
            return self.leave_group(actor, g["id"])
        if do in ("kick", "remove", "vote_out"):
            return self.vote_out(actor, g["id"], op.get("member"), True)
        if do in ("keep", "vote_keep"):
            return self.vote_out(actor, g["id"], op.get("member"), False)
        if do == "goal":
            return self.set_group_goal(actor, g["id"], op.get("text") or op.get("goal") or "")
        return ActionResult(False, f"unknown group_chats step {do!r} (start, invite, leave, kick, keep or goal)")

    def leave_group(self, who: int, gid: str) -> ActionResult:
        g = self.groups.get(str(gid))
        if g is None or g.get("closed") or who not in g["members"]:
            return ActionResult(False, "not in that group chat")
        self._group_note(g, f"{'You' if who == self.human_id else self.game.civ(who).leader} left the group chat.",
                         sender=None if who == self.human_id else who)
        g["members"] = [m for m in g["members"] if m != who]
        if len(g["members"]) < 2:
            g["closed"] = True
            self._group_note(g, "The group chat is closed: nobody's left to talk to.")
        self._sync_groups()
        return ActionResult(True, "left")

    # ---- group pacts ------------------------------------------------------------
    def _gp_record(self, p: GroupPact, text: str, kind: str, sender: int | None = None) -> None:
        """A group pact's news, in its group's chat (secret unless you're in the group)."""
        g = self.groups.get(p.group) or {"members": [], "name": p.group}
        if kind != "pact":   # an offer already reached them through their inbox
            self._group_memory(p.group, text)
        extra = {} if self.human_id in g["members"] or self.human_id in p.members else {"secret": True}
        self._record(p.group, kind, text, f"📜 [{g['name']}] {text}" if self.spy or not extra else None,
                     sender=sender, pact_id=p.id, **extra)

    def _gp_names(self, ids) -> str:
        return ", ".join(self._group_name(i) for i in ids) or "nobody"

    def _gp_text(self, p: GroupPact) -> str:
        war = (f" Joint war: once everyone accepts, every member declares war on {self.game.civ(p.war_on).label}."
               if p.war_on is not None else "")
        return f"{p.title}. {p.terms} ({p.length()}).{war}"

    def offer_group_pact(self, actor: int, gid: str, title, terms, turns, war_on=None,
                         note: str = "") -> ActionResult:
        """A pact for the whole group chat: every member must accept before it starts."""
        note = str(note or "")
        g = self.groups.get(str(gid))
        if g is None or g.get("closed") or actor not in g["members"]:
            return ActionResult(False, f"you're not in a group chat called {gid!r}")
        target = None
        if war_on not in (None, "", False):
            target = self.resolve(war_on)
            if not isinstance(target, int) or target in g["members"]:
                return ActionResult(False, f"a joint war needs a civ outside the group ({war_on!r})")
            if not self.game.civ(target).alive or not self._met(actor, target):
                return ActionResult(False, f"you can only call a joint war on a civ you've met ({war_on!r})")
        p = self.pacts.offer_group(actor, g["id"], g["members"], title, terms, turns, self.game.turn, target)
        if isinstance(p, str):
            return ActionResult(False, p)
        who = "You" if actor == self.human_id else self.game.civ(actor).leader
        offer = f"{who} proposes a group pact {p.key} to everyone here: {self._gp_text(p)}"
        if note.strip():
            offer += f" {note.strip()[:300]}"
        if actor != self.human_id:   # yours is posted as your message (human_offer_group_pact)
            self._gp_record(p, offer, "pact", sender=actor)
            with self._mail_lock:   # the others hear it now, so they can answer this turn
                for m in g["members"]:
                    if m in self.agents and m != actor:
                        self.agents[m].memory.log(g["id"], self.game.turn, self.game.civ(actor).leader, offer)
                        self._inbox[m].append({"from_id": actor, "from": self.game.civ(actor).leader,
                                               "group": g["id"], "to": f"group chat {g['id']} '{g['name']}'",
                                               "text": offer + f" (answer in proposal_responses as {p.key})"})
        return ActionResult(True, f"group pact offer {p.key} sent to {g['id']}")

    def human_offer_group_pact(self, gid: str, title, terms, turns, war_on=None,
                               note: str = "") -> tuple[ActionResult, list[int]]:
        res = self.offer_group_pact(self.human_id, gid, title, terms, turns, war_on=war_on, note=note)
        if not res.ok:
            return res, []
        p = max((q for q in self.pacts.of_group(str(gid)) if q.a == self.human_id), key=lambda q: q.id)
        note = str(note or "").strip()[:300]
        text = (f"📜 Group pact offer {p.key}: {self._gp_text(p)}" + (f" {note}" if note else "")
                + f" (everyone must accept: answer as {p.key})")
        return res, self.post(str(gid), text)

    def answer_group_pact(self, who: int, key, accept: bool) -> ActionResult:
        p = self.pacts.answer_group(key, who, accept, self.game.turn)
        if isinstance(p, str):
            return ActionResult(False, p)
        me = self._group_name(who)
        if not accept:
            self._gp_record(p, f"{me.capitalize()} turned down the group pact {p.key} ({p.title}), so it won't happen.",
                            "error", sender=None if who == self.human_id else who)
            if p.a in self.agents:
                self.agents[p.a].note_setback(self.game.turn, f"{me} turned down your group pact {p.key} ({p.title})")
            return ActionResult(True, "declined")
        if p.status == "offered":
            self._gp_record(p, f"{me.capitalize()} agreed to the group pact {p.key} ({p.title}). Still waiting for "
                               f"{self._gp_names(p.waiting_for())}.", "info", sender=None if who == self.human_id else who)
            return ActionResult(True, "agreed; waiting for the others")
        self._gp_record(p, f"Group pact {p.key} agreed by everyone ({self._gp_names(p.members)}): {self._gp_text(p)}",
                        "treaty")
        if p.war_on is not None:
            self._joint_war(p)
        return ActionResult(True, "agreed: the pact is in force")

    def _joint_war(self, p: GroupPact) -> None:
        """Every member declares war on the target the turn the pact starts (you declare yours in Civ)."""
        t = p.war_on
        for m in p.parties:
            if self.game.stance(m, t) == "war":
                continue
            if m == self.human_id:
                self._gp_record(p, f"Your part of the joint war: declare war on {self.game.civ(t).leader} in Civ.", "info")
                continue
            res = self.game.apply(m, {"type": "declare_war", "target": t, "note": f"joint war, group pact {p.key}"})
            if res.ok:
                self._record(WORLD, "war", res.message, f"⚔️  {res.message}", **self._hush(m, t))
            else:
                self._gp_record(p, f"{self.game.civ(m).leader} couldn't declare war on {self.game.civ(t).leader}: "
                                   f"{res.message}.", "error")
                if m in self.agents:
                    self.agents[m].note_setback(self.game.turn, f"joint war on {self.game.civ(t).leader} "
                                                                f"({p.key}) didn't go through: {res.message}")

    def leave_group_pact(self, who: int, key, note: str = "") -> ActionResult:
        p = self.pacts.leave_group_pact(key, who, self.game.turn)
        if isinstance(p, str):
            return ActionResult(False, p)
        me = self._group_name(who).capitalize()
        sender = None if who == self.human_id else who
        why = f" ({str(note).strip()[:200]})" if str(note or "").strip() else ""
        if p.status in ("declined", "expired") and p.closed == self.game.turn and p.by == who:
            verb = "withdrew" if p.status == "expired" else "turned down"
            self._gp_record(p, f"{me} {verb} the group pact {p.key} ({p.title}).{why}", "error", sender=sender)
            return ActionResult(True, p.status)
        broke = who in p.broke
        left = f"{me} {'broke' if broke else 'left'} the group pact {p.key} ({p.title})"
        if broke:
            left += f", {p.ends - self.game.turn} turns before it was due to end"
        rest = (f"; it holds for {self._gp_names(p.parties)}" if p.status == "active"
                else "; with fewer than two left, it's over")
        self._gp_record(p, left + rest + "." + why, "error", sender=sender)
        return ActionResult(True, "broke" if broke else "left")

    def _group_pact_changed(self, p: GroupPact) -> None:
        """A turn went by: an offer expired, or a pact ran its course."""
        if p.status == "expired":
            self._gp_record(p, f"The group pact offer {p.key} ({p.title}) expired: not everyone answered "
                               f"({self._gp_names(p.waiting_for())} didn't).", "error")
            if p.a in self.agents:
                self.agents[p.a].note_setback(self.game.turn, f"your group pact {p.key} ({p.title}) expired unanswered")
        elif p.status == "ended":
            self._gp_record(p, f"The group pact {p.key} has run its course: {p.title}.", "treaty")

    # ---- group votes and goals ------------------------------------------------------
    VOTE_TURNS = 3   # a vote to remove someone lapses after this many turns

    def vote_out(self, voter: int, gid: str, member, yes: bool = True) -> ActionResult:
        """Call (or vote in) a vote to remove a member. It passes with a majority of the other members."""
        g = self.groups.get(str(gid))
        if g is None or g.get("closed") or voter not in g["members"]:
            return ActionResult(False, f"you're not in a group chat called {gid!r}")
        t = self.resolve(member)
        if not isinstance(t, int) or t not in g["members"] or t == voter:
            return ActionResult(False, f"{member!r} isn't another member of {g['id']}")
        votes = g.setdefault("votes", {})
        v = votes.get(str(t))
        me, them = self._group_name(voter), self._group_name(t)
        sender = None if voter == self.human_id else voter
        if v is None:
            if not yes:
                return ActionResult(False, f"there's no vote on {them} to vote against")
            v = votes[str(t)] = {"yes": [voter], "no": [], "turn": self.game.turn, "by": voter}
            self._group_note(g, f"{me.capitalize()} called a vote to remove {them} from the group "
                                f"({self._votes_needed(g, t)} of {len(g['members']) - 1} yes votes needed).", sender=sender)
        else:
            for side in ("yes", "no"):
                if voter in v[side]:
                    v[side].remove(voter)
            v["yes" if yes else "no"].append(voter)
            self._group_note(g, f"{me.capitalize()} voted {'to remove' if yes else 'to keep'} {them} "
                                f"({len(v['yes'])} yes, {len(v['no'])} no).", sender=sender)
        need, voters = self._votes_needed(g, t), len(g["members"]) - 1
        if len(v["yes"]) >= need:
            del votes[str(t)]
            g["members"] = [m for m in g["members"] if m != t]
            self._group_note(g, f"{them.capitalize()} {'were' if t == self.human_id else 'was'} voted out of the group.")
            for gp in self.pacts.remove_member(g["id"], t, self.game.turn):
                self._gp_record(gp, f"{them.capitalize()} {'are' if t == self.human_id else 'is'} out of the group pact "
                                    f"{gp.key} ({gp.title}) too.", "info")
            if len(g["members"]) < 2:
                g["closed"] = True
                self._group_note(g, "The group chat is closed: nobody's left to talk to.")
            self._sync_groups()
            return ActionResult(True, f"{them} voted out")
        if len(v["no"]) > voters - need:
            del votes[str(t)]
            self._group_note(g, f"The vote to remove {them} failed.")
        return ActionResult(True, "voted")

    @staticmethod
    def _votes_needed(g: dict, target: int) -> int:
        return (len(g["members"]) - 1) // 2 + 1   # a majority of everyone but the one being voted on

    def _tick_votes(self) -> None:
        for g in self.groups.values():
            for t, v in list(g.get("votes", {}).items()):
                if self.game.turn - int(v.get("turn", 0)) >= self.VOTE_TURNS:
                    del g["votes"][t]
                    self._group_note(g, f"The vote to remove {self._group_name(int(t))} lapsed without a majority.")

    def set_group_goal(self, who: int, gid: str, text: str) -> ActionResult:
        """The founder sets the group's goal (anyone, once the founder has left). Every member's briefing shows it."""
        g = self.groups.get(str(gid))
        if g is None or g.get("closed") or who not in g["members"]:
            return ActionResult(False, f"you're not in a group chat called {gid!r}")
        founder = self.human_id if g.get("started_by") is None else g["started_by"]
        if founder in g["members"] and who != founder:
            return ActionResult(False, f"only the group's founder ({self._group_name(founder)}) sets its goal; "
                                       "suggest one in the chat")
        text = " ".join(str(text or "").split())[:160]
        me = self._group_name(who).capitalize()
        if not text:
            g.pop("goal", None)
            self._group_note(g, f"{me} cleared the group's goal.", sender=None if who == self.human_id else who)
        else:
            g["goal"] = {"text": text, "by": who, "turn": self.game.turn}
            self._group_note(g, f"{me} set the group's goal: {text}", sender=None if who == self.human_id else who)
        self._sync_groups()
        return ActionResult(True, "goal set" if text else "goal cleared")

    def invite_to_group(self, actor: int, gid: str, member) -> ActionResult:
        g = self.groups.get(str(gid))
        if g is None or g.get("closed") or actor not in g["members"]:
            return ActionResult(False, f"you're not in a group chat called {gid!r}")
        t = self.resolve(member)
        if not isinstance(t, int) or t not in self.agents and t != self.human_id:
            return ActionResult(False, f"no leader {member!r}")
        if not self.game.civ(t).alive or not self._met(actor, t):
            return ActionResult(False, f"you can only invite someone you've met ({member!r})")
        if t in g["members"]:
            return ActionResult(False, f"{self._group_name(t)} is already in {g['id']}")
        g["members"] = sorted(g["members"] + [t])
        g["ever"] = sorted(set(g.get("ever", g["members"])) | {t})
        self._sync_groups()
        self._group_note(g, f"{self._group_name(actor).capitalize()} added {self._group_name(t)} to the group chat.",
                         sender=None if actor == self.human_id else actor)
        return ActionResult(True, f"added {self._group_name(t)} to {g['id']}")

    # ---- your advisor -----------------------------------------------------------------
    def ask_advisor(self, text: str) -> bool:
        """Your question goes into the Advisor channel at once; the answer comes from advisor_reply (slow)."""
        if self.advisor is None or not self.advisor_on or not str(text or "").strip():
            return False
        self._record(ADVISOR, "message", str(text).strip()[:1500], sender=self.human_id)
        return True

    def advisor_reply(self) -> None:
        self.status = "Your advisor is thinking…"
        try:
            text = self.advisor.answer(self)
        except Exception as e:   # a failed call shouldn't break the chat
            text = f"(Your advisor couldn't answer just now: {e})"
        finally:
            self.status = ""
        self._record(ADVISOR, "message", text, f"🧭 Advisor: {text}", advisor=True)

    def advisor_picks(self) -> dict | None:
        """The advisor's recommendations for this turn, next to Civ's own: in the Advisor channel and in Civ."""
        if self.advisor is None or not self.advisor_on:
            return None
        self.status = "Your advisor is looking over your empire…"
        try:
            picks = self.advisor.picks(self)
        except Exception:
            picks = None
        finally:
            self.status = ""
        if not picks:
            return None
        line = picks_text(picks)
        self._record(ADVISOR, "picks", picks.get("headline") or line, f"🧭 Advisor's picks: {line}",
                     advisor=True, picks=picks)
        notify = getattr(self.game, "notify", None)
        if notify is not None and self.civ_notify and line:
            try:
                notify(self.human_id, "Your advisor (Civ Council)", (picks.get("headline", "") + " " + line).strip()[:300])
            except Exception:
                pass
        return picks

    # ---- models (the browser's Settings) ------------------------------------------------
    MODEL_JOBS = ("turn", "talk", "strategy", "advisor")
    MODEL_CHOICES = ("haiku", "sonnet", "opus")
    TIMEOUTS = {"talk": 45, "advisor": 120}   # you're waiting on these; past that, the fallback answers

    @staticmethod
    def _model_of(client) -> str | None:
        if client is None:
            return None
        return getattr(client, "model", None) or str(getattr(client, "name", "")).split(":")[-1] or None

    def model_settings(self) -> dict:
        """What each job runs on now, and whether the browser may change it (Claude Code only)."""
        first = next(iter(self.agents.values()), None)
        return {
            "editable": self.llm_factory is not None,
            "choices": list(self.MODEL_CHOICES),
            "turn": self._model_of(first.client) if first else None,
            "talk": self._model_of((first.talk_client or first.client)) if first else None,
            "strategy": (self._model_of(first.strategy_client) or "off") if first else "off",
            "advisor": self._model_of(self.advisor.client) if self.advisor else None,
            "advisor_on": bool(self.advisor is not None and self.advisor_on),
            "has_advisor": self.advisor is not None,
        }

    def set_models(self, **jobs) -> str:
        """Switch models live: turn (routine turns), talk (with you, and memory summaries), strategy (grand
        strategies, or "off"), advisor. Each leader gets its own new client. Returns "" or what's wrong."""
        if self.llm_factory is None:
            return "models can only be changed with Claude Code"
        jobs = {k: str(v).strip().lower() for k, v in jobs.items() if k in self.MODEL_JOBS and v}
        for k, v in jobs.items():
            if v not in self.MODEL_CHOICES and not (k == "strategy" and v == "off"):
                return f"unknown model {v!r} for {k}"
        make = self.llm_factory
        for agent in self.agents.values():
            if "turn" in jobs:
                agent.client = make(jobs["turn"], None)
            if "talk" in jobs:
                agent.talk_client = make(jobs["talk"], self.TIMEOUTS["talk"])
            if "strategy" in jobs:
                agent.strategy_client = None if jobs["strategy"] == "off" else make(jobs["strategy"], None)
        if "advisor" in jobs and self.advisor is not None:
            self.advisor.client = make(jobs["advisor"], self.TIMEOUTS["advisor"])
        self.models.update(jobs)
        return ""

    # ---- lookups -------------------------------------------------------------
    def resolve(self, who: str | int) -> int | str | None:
        """Turn 'all', an id, a leader name or a civ name into a target."""
        if isinstance(who, int):
            return who if any(c.id == who for c in self.game.civs()) else None
        s = str(who).strip().lower()
        if s in (ALL, "everyone", "public", WORLD):
            return ALL
        if s in self.groups:
            return s
        if s.lstrip("-").isdigit():
            return self.resolve(int(s))
        for c in self.game.civs():
            if s in (c.leader.lower(), c.name.lower()) or c.leader.lower().startswith(s):
                return c.id
        return None

    def _met(self, a: int, b: int) -> bool:
        """Real games only let you talk to leaders you've met. The mock world: everyone knows everyone."""
        has_met = getattr(self.game, "has_met", None)
        return has_met is None or has_met(a, b)

    def _who(self, text: str, reader: int) -> dict:
        """For a leader reading your message: who each name in it means (country, nickname or typo)."""
        out = {}
        for word, c in names_in(text, list(self.game.civs())).items():
            if c.id == reader:
                out[word] = f"you ({c.label})"
            elif c.id == self.human_id:
                out[word] = f"{c.label}, the human player who wrote this"
            else:
                out[word] = c.label + ("" if self._met(reader, c.id) else " (you haven't met them)")
        return {"names_in_message": out} if out else {}

    def _name(self, civ_id: int) -> str:
        c: Civ = self.game.civ(civ_id)
        return "you" if civ_id == self.human_id else c.leader

    def _record(self, channel: int | str, kind: str, text: str, line: str | None = None,
                sender: int | None = None, **extra) -> None:
        self.entries.append({"id": next(self._ids), "turn": self.game.turn, "channel": channel,
                             "kind": kind, "sender": sender,
                             "sender_name": None if sender is None else self._name(sender),
                             "text": text, **extra})
        if channel != SPY:   # what's overheard is logged where it happens (with or without Spy on)
            kind_h = history_kind(channel, kind, {**extra, "text": text})
            if kind_h:
                self._add_history(kind_h, text, by=sender)
        secret = bool(extra.get("secret"))
        if line and (self.spy or not secret):
            self.feed.append(line)
        if not secret:
            self._civ_notification(channel, kind, text, sender)

    def _add_history(self, kind: str, text: str, by: int | None = None) -> None:
        """One line of the game's story: who's involved is read from the names in it (and who said it)."""
        civs = {c.id for c in names_in(text, list(self.game.civs())).values()} | ({by} if by is not None else set())
        self.history.append({"turn": self.game.turn, "kind": kind, "text": text, "civs": sorted(civs),
                             **({"by": by} if by is not None else {})})
        if len(self.history) > HISTORY_KEEP:
            del self.history[:len(self.history) - HISTORY_KEEP]

    def _hush(self, *civs) -> dict:
        """Marks an entry secret when it's about a civ you haven't met: you wouldn't know (Spy shows it)."""
        unknown = any(c is not None and c != self.human_id and not self._met(self.human_id, c) for c in civs)
        return {"secret": True} if unknown else {}

    def _event_hush(self, event: str) -> dict:
        civs = getattr(self.game, "event_civs", None)
        return self._hush(*(civs(event) if civs else ()))

    def _civ_notification(self, channel, kind: str, text: str, sender: int | None) -> None:
        """A leader wrote to you or made you an offer: say so in Civ's own notification stack too."""
        notify = getattr(self.game, "notify", None)
        if (not self.civ_notify or notify is None or sender is None or sender == self.human_id
                or kind not in ("message", "proposal", "pact", "membership") or channel in (SPY, MINDS)):
            return
        name = self._name(sender)
        if kind in ("proposal", "pact"):
            head = f"Offer from {name} (Civ Council)"
        elif channel == WORLD:
            head = f"{name}, to everyone (Civ Council)"
        elif channel in self.groups:
            if self.human_id not in self.groups[channel]["members"]:
                return   # a group you're not in stays secret
            head = f"{name} in {self.groups[channel]['name']} (Civ Council)"
        else:
            head = f"{name} writes to you (Civ Council)"
        try:
            notify(sender, head, text)
        except Exception:   # a notification is a nicety; never lose a message over it
            pass

    def _thinking(self, aid: int | None) -> None:
        self.status = f"{self.game.civ(aid).leader} is thinking…" if aid is not None else ""

    # ---- messaging -------------------------------------------------------------
    def _deliver(self, sender: int, to: int | str, text: str, quote: dict | None = None,
                 rumor: str | None = None) -> None:
        with self._mail_lock:
            self._deliver_now(sender, to, text, quote, rumor)

    def _deliver_now(self, sender: int, to: int | str, text: str, quote: dict | None = None,
                     rumor: str | None = None) -> None:
        turn = self.game.turn
        sname = self.game.civ(sender).leader
        human = sender == self.human_id
        extra = {"reply_to": quote} if quote else {}
        if rumor:
            extra["rumor_about"] = rumor
        shown = text
        if quote:   # leaders hear which message you're answering
            text = f'(replying to {quote["name"]}: "{quote["text"]}") {text}'
        if rumor:   # labelled as hearsay, so the listener weighs it instead of taking it as fact
            text = f"(rumor about {rumor}, unverified) {text}"
        if to == ALL:   # heard by everyone who has met the speaker
            for aid, agent in self.agents.items():
                if aid != sender and not self._met(sender, aid):
                    continue
                agent.memory.log("public", turn, sname, text)
                if aid != sender:
                    self._inbox[aid].append({"from_id": sender, "from": sname, "to": "everyone", "text": text,
                                             **(self._who(shown, aid) if human else {})})
            self._record(WORLD, "message", shown, None if human else f"📢 {sname} (to everyone): {shown}",
                         sender=sender, **extra, **self._hush(sender))
            return
        if isinstance(to, str) and to in self.groups:
            g = self.groups[to]
            if sender not in g["members"] or g.get("closed"):
                return
            if self.human_id not in g["members"]:   # a leaders-only group: you see it only with Spy on
                extra["secret"] = True
            for aid in g["members"]:
                if aid in self.agents:
                    self.agents[aid].memory.log(to, turn, sname, text)
                    if aid != sender:
                        self._inbox[aid].append({"from_id": sender, "from": sname, "group": to,
                                                 "to": f"group chat {to} '{g['name']}'", "text": text,
                                                 **(self._who(shown, aid) if human else {})})
            self._record(to, "message", shown, None if human or (extra.get("secret") and not self.spy)
                         else f"👥 [{g['name']}] {sname}: {shown}",
                         sender=sender, **extra)
            return
        if sender in self.agents:
            self.agents[sender].memory.log(to, turn, sname, text)
        if to in self.agents:
            self.agents[to].memory.log(sender, turn, sname, text)
            self._inbox[to].append({"from_id": sender, "from": sname, "to": "you", "text": text,
                                   **(self._who(shown, to) if human else {})})
        if human:
            self._record(to, "message", shown, sender=sender, **extra)
        elif to == self.human_id:
            self._record(sender, "message", shown, f"✉️  {sname}: {shown}", sender=sender, **extra)
        elif self.spy:
            self._record(SPY, "message", shown, f"🕵️  [overheard] {sname} → {self._name(to)}: {shown}",
                         sender=sender, to=to, to_name=self._name(to), **extra)

    def _carry_out(self, actor: int, d: Decision) -> None:
        """Apply an AI leader's decision: messages, proposal answers, actions."""
        leader = self.game.civ(actor).leader
        if d.error:
            self._record(WORLD, "error", f"{leader}'s message was garbled ({d.error}).",
                         f"⚠️  {leader}'s advisor garbled the message ({d.error}).", **self._hush(actor))
        for op in d.group_chats[:3]:
            res = self.leader_group(actor, op)
            if not res.ok:
                self.agents[actor].note_setback(self.game.turn, f"group chat ({op.get('do')}): {res.message}")
        for m in d.messages:
            to = self.resolve(m.get("to", ""))
            if to is None or to == actor:
                continue
            if isinstance(to, int) and not self._met(actor, to):
                continue
            if (isinstance(to, int) and to in self.agents) or to in self.groups:
                n = self._pair_count.get((actor, to), 0)
                if n >= self.max_pair_msgs:
                    continue
                self._pair_count[(actor, to)] = n + 1
            rumor = None
            if m.get("rumor_about") not in (None, "", False):
                about = self.resolve(m["rumor_about"])
                rumor = self._name(about) if isinstance(about, int) else str(m["rumor_about"])[:40]
            self._deliver(actor, to, str(m["text"]).strip()[:600], rumor=rumor)
            if m.get("pin") not in (None, "") and self._reaches_human(actor, to):
                self._pin(actor, to, m["pin"], m.get("pin_label") or "")
        for r in d.proposal_responses:
            if str(r.get("id", "")).strip().lower().startswith("pact"):
                res = self.answer_pact(actor, r.get("id"), bool(r.get("accept")))
                if not res.ok:
                    self.agents[actor].note_setback(self.game.turn, f"answering {r.get('id')}: {res.message}")
                continue
            try:
                pid = int(r.get("id"))
                p = next((x for x in self.game.pending_proposals(actor) if x.id == pid), None)
                res = self.game.resolve_proposal(pid, actor, bool(r.get("accept")))
            except (TypeError, ValueError):
                continue
            if p is None:
                continue
            kind = "treaty" if res.ok else "error"
            if p.kind == "propose_deal" and self.human_id in (p.sender, p.target):
                other = p.sender if p.sender != self.human_id else p.target
                self._record(other, kind, res.message, f"📜 {res.message}")   # your deals: in that leader's chat
            elif p.kind == "propose_deal":
                if res.ok and "rejected" not in res.message:
                    self._add_history("deal", res.message)
                if self.spy:   # deals between AI leaders are private, like in Civ
                    self._record(SPY, kind, res.message, f"🕵️  [overheard] {res.message}", sender=actor)
            elif res.ok:
                self._record(WORLD, "treaty", res.message, f"📜 {res.message}", **self._hush(actor, p.sender, p.target))
        set_orders = getattr(self.game, "set_orders", None)
        if d.orders and set_orders:
            try:
                notes = set_orders(actor, d.orders)
            except Exception as e:   # a game hiccup shouldn't lose the leader's messages
                notes = [f"orders failed: {e}"]
            if notes:
                text = f"{leader}'s orders: " + "; ".join(notes)
                self._record(MINDS, "orders", text, f"🧠 {text}" if self.spy else None, sender=actor)
        for a in d.actions:
            if a.get("type") in PACT_ACTIONS:
                if a["type"] == "propose_pact" and str(a.get("group") or "").strip() in self.groups:
                    res = self.offer_group_pact(actor, str(a["group"]).strip(), a.get("title"), a.get("terms"),
                                                a.get("turns"), war_on=a.get("war_on"))
                elif a["type"] == "propose_pact":
                    try:
                        target = int(a.get("target"))
                    except (TypeError, ValueError):
                        target = -1
                    res = self.offer_pact(actor, target, a.get("title"), a.get("terms"), a.get("turns"),
                                          give=a.get("give"), ask=a.get("ask"))
                else:
                    res = self.end_pact(actor, a.get("pact"), a.get("note", ""))
                if not res.ok:
                    self._setback(actor, a, res.message)
                continue
            res = self.game.apply(actor, a)
            if not res.ok:
                self._setback(actor, a, res.message)
                continue
            target = int(a["target"])
            if a["type"] == "propose_deal" and res.proposal_id is None:   # a gift: already paid
                if target == self.human_id:
                    self._record(actor, "treaty", res.message, f"💰 {res.message}", sender=actor)
                else:
                    self._add_history("deal", res.message)
                    if self.spy:
                        self._record(SPY, "treaty", res.message, f"🕵️  [overheard] {res.message}", sender=actor)
            elif a["type"] in PROPOSAL_ACTIONS and res.proposal_id is None:   # a renewal queued for later
                if target == self.human_id:
                    self._record(actor, "info", res.message, f"📜 {res.message}", sender=actor)
                elif self.spy:
                    self._record(SPY, "proposal", res.message, f"🕵️  [overheard] {res.message}", sender=actor)
            elif a["type"] in PROPOSAL_ACTIONS:
                if target == self.human_id:
                    p = next((x for x in self.game.pending_proposals(self.human_id) if x.id == res.proposal_id),
                             None) or self.game.pending_proposals(self.human_id)[-1]
                    text = f"{leader} offers you {proposal_label(p)}."
                    self._record(actor, "proposal", text,
                                 f"📜 {text} (#{p.id}) — /accept {p.id} or /decline {p.id}",
                                 sender=actor, proposal_id=p.id)
                elif self.spy:
                    self._record(SPY, "proposal", res.message, f"🕵️  [overheard] {res.message}", sender=actor)
            else:
                war = a["type"] == "declare_war"
                self._record(WORLD, "war" if war else "treaty", res.message,
                             f"⚔️  {res.message}" if war else f"📜 {res.message}", **self._hush(actor, target))

    # ---- pacts ---------------------------------------------------------------------
    def _pact_record(self, p, text: str, kind: str, sender: int | None = None) -> None:
        """Pacts are private: in your chat with that leader when you're in it, otherwise only under Spy."""
        if self.human_id in (p.a, p.b):
            other = p.other(self.human_id)
            self._record(other, kind, text, f"📜 {text}", sender=sender, pact_id=p.id)
            return
        if kind in ("treaty", "error"):   # agreed, broken, ended, turned down: part of the story
            self._add_history("pact", text)
        if self.spy:
            self._record(SPY, kind, text, f"🕵️  [overheard] {text}", sender=sender, pact_id=p.id)

    def offer_pact(self, actor: int, target: int, title, terms, turns, note: str = "",
                   give=None, ask=None) -> ActionResult:
        try:
            them = self.game.civ(target)
        except (KeyError, ValueError):
            return ActionResult(False, f"no civ {target!r}")
        if not them.alive:
            return ActionResult(False, f"{them.leader} is gone")
        if not self._met(actor, target):
            return ActionResult(False, f"you haven't met {them.leader}")
        trade = None
        if give or ask:   # a payment that goes with the pact (a proxy war, tribute, protection money...)
            trade, why = deal_terms({"give": give or {}, "ask": ask or {}})
            if trade is None:
                return ActionResult(False, f"the payment: {why}")
        p = self.pacts.offer(actor, target, title, terms, turns, self.game.turn, trade)
        if isinstance(p, str):
            return ActionResult(False, p)
        me = self.game.civ(actor).leader
        pay = p.payment(me, them.leader)
        if actor != self.human_id:   # yours reach them as your message (see human_offer_pact)
            self._pact_record(p, f"{me} proposes a pact to {them.leader}: {p.title}. {p.terms} ({p.length()})"
                              + (f"; {pay}" if pay else "") + ".", "pact", sender=actor)
        return ActionResult(True, f"pact offer {p.key} sent to {them.leader}")

    def human_offer_pact(self, to: int, title, terms, turns, give=None, ask=None,
                         note: str = "") -> tuple[ActionResult, list[int]]:
        """You offer a leader a pact. It reaches them as your message, so they answer right away."""
        to = int(to)
        if to not in self.agents:
            return ActionResult(False, "you can only make pacts with AI leaders"), []
        res = self.offer_pact(self.human_id, to, title, terms, turns, give=give, ask=ask)
        if not res.ok:
            return res, []
        p = self.pacts.offers_from(self.human_id)[-1]
        pay = p.payment("I", "you")
        text = (f"📜 Pact offer {p.key}: {p.title}. {p.terms} ({p.length()})" + (f"; {pay}" if pay else "") + "."
                + (f" {str(note).strip()[:400]}" if str(note or "").strip() else ""))
        return res, self.post(to, text)

    def answer_pact(self, who: int, key, accept: bool) -> ActionResult:
        if self.pacts.group_pact(key):
            return self.answer_group_pact(who, key, accept)
        p = self.pacts.answer(key, who, accept, self.game.turn)
        if isinstance(p, str):
            return ActionResult(False, p)
        a, b = self.game.civ(p.a).leader, self.game.civ(p.b).leader
        if accept and p.trade:   # the payment goes through with it, or the pact doesn't start
            paid = self._pay(p)
            if not paid.ok:
                p.status, p.closed, p.since, p.ends = "failed", self.game.turn, None, None
                self._pact_record(p, f"{b} agreed to {a}'s pact ({p.title}), but the payment fell through "
                                     f"({paid.message}), so the pact didn't start.", "error")
                for x in (p.a, p.b):
                    if x in self.agents:
                        self.agents[x].note_setback(self.game.turn, f"pact {p.key} ({p.title}) didn't start: "
                                                                    f"the payment fell through ({paid.message})")
                return ActionResult(False, f"the payment fell through: {paid.message}")
        if accept:
            self._pact_record(p, f"Pact agreed between {a} and {b}: {p.title}. {p.terms} ({p.length()}).", "treaty")
        else:
            self._pact_record(p, f"{b} turned down {a}'s pact: {p.title}.", "error")
            if p.a in self.agents:
                self.agents[p.a].note_setback(self.game.turn, f"{b} turned down your pact {p.key} ({p.title})")
        return ActionResult(True, "agreed" if accept else "declined")

    def _pay(self, p) -> ActionResult:
        """Carry out a pact's payment through the game's own deal (a gift goes at once; otherwise a deal both
        sides have already agreed to)."""
        if self.game.stance(p.a, p.b) == "war":
            return ActionResult(False, "no payments between civs at war (make peace with terms first)")
        res = self.game.apply(p.a, {"type": "propose_deal", "target": p.b, "give": p.trade.get("give") or {},
                                    "ask": p.trade.get("ask") or {}, "note": f"pact {p.key}"})
        if not res.ok or res.proposal_id is None:
            return res
        return self.game.resolve_proposal(res.proposal_id, p.b, True)

    def end_pact(self, who: int, key, note: str = "") -> ActionResult:
        if self.pacts.group_pact(key):
            return self.leave_group_pact(who, key, note)
        p = self.pacts.end(key, who, self.game.turn, note)
        if isinstance(p, str):
            return ActionResult(False, p)
        me, other = self.game.civ(who).leader, self.game.civ(p.other(who)).leader
        if p.status == "expired":
            self._pact_record(p, f"{me} withdrew the pact offer to {other}: {p.title}.", "error")
        else:
            verb = "broke" if p.status == "broken" else "ended"
            when = f", {p.ends - self.game.turn} turns before it was due to end" if p.status == "broken" else ""
            self._pact_record(p, f"{me} {verb} the pact with {other}{when}: {p.title}."
                              + (f" ({p.note})" if p.note else ""), "error", sender=who)
        return ActionResult(True, p.status)

    def _tick_pacts(self) -> None:
        turn = self.game.turn
        self._tick_votes()
        changed = self.pacts.tick(turn)
        for civ in self.game.civs():
            if not civ.alive:
                changed += self.pacts.drop_civ(civ.id, turn)
        for p in changed:
            if isinstance(p, GroupPact):
                self._group_pact_changed(p)
                continue
            a, b = self.game.civ(p.a).leader, self.game.civ(p.b).leader
            if p.status == "expired":
                self._pact_record(p, f"{a}'s pact offer to {b} expired unanswered: {p.title}.", "error")
                if p.a in self.agents:
                    self.agents[p.a].note_setback(turn, f"your pact offer {p.key} ({p.title}) to {b} expired unanswered")
            else:
                self._pact_record(p, f"The pact between {a} and {b} has run its course: {p.title}.", "treaty")

    def _reaches_human(self, sender: int, to: int | str) -> bool:
        if to == self.human_id:
            return True
        if to == ALL:
            return self._met(sender, self.human_id)
        return to in self.groups and self.human_id in self.groups[to]["members"]

    def _pin(self, actor: int, to: int | str, where, label: str) -> None:
        """A leader marks a place on your map (real game only), and says so where you'll see it."""
        pin = getattr(self.game, "pin", None)
        if pin is None:
            return
        try:
            res = pin(actor, where, label)
        except Exception as e:   # a pin is a nicety; never lose the turn over it
            res = ActionResult(False, str(e))
        channel = actor if to == self.human_id else WORLD if to == ALL else to
        if res.ok:
            self._record(channel, "info", res.message, res.message, sender=actor)
        else:
            self.agents[actor].note_setback(self.game.turn, f"your map pin for {self._name(self.human_id)} "
                                                            f"didn't go up: {res.message}")

    def _setback(self, actor: int, action: dict, why: str) -> None:
        """An action Civ refused: the leader hears why (next turns), you see it if it was meant for you."""
        kind = ACTION_NOUNS.get(str(action.get("type", "")), str(action.get("type", "")).replace("_", " "))
        try:
            target = int(action.get("target"))
            whom = self.game.civ(target).leader
        except (TypeError, ValueError, KeyError):
            target, whom = None, "?"
        text = f"{kind} to {whom} didn't go through: {why}"
        self.agents[actor].note_setback(self.game.turn, text)
        leader = self.game.civ(actor).leader
        if "already pending" in why:
            return   # a repeat of an offer that's waiting anyway: the leader hears it, you needn't
        if target == self.human_id:
            self._record(actor, "error", f"{leader} tried to send you {kind}, but it didn't go through: {why}.",
                         f"⚠️  {leader}: {kind} didn't go through: {why}", sender=actor)
        elif self.spy:
            self._record(SPY, "error", f"{leader}: {kind} to {whom} didn't go through: {why}.",
                         f"🕵️  [overheard] {leader}: {kind} to {whom} failed: {why}", sender=actor)

    def expired(self, proposals: list) -> None:
        """Offers that ran out unanswered: the sender is told, and so are you if it was yours to answer."""
        for p in proposals:
            to = self.game.civ(p.target).leader
            if p.sender in self.agents:
                self.agents[p.sender].note_setback(self.game.turn,
                                                   f"your offer of {proposal_label(p)} to {to} expired unanswered")
            if p.target == self.human_id and p.sender in self.agents:
                self._record(p.sender, "error", f"{self.game.civ(p.sender).leader}'s offer of {proposal_label(p)} "
                             "expired unanswered.", None, sender=p.sender)

    # ---- World News ------------------------------------------------------------------
    NEWS_SECTIONS = (
        ("War and peace", ("war", "peace", "alliance", "friends", "friendship", "denounced", "Emergency")),
        ("Cities", ("founded", "changed hands", "broke away", "razed", "destroyed", "joined", " city", "cities")),
        ("Wonders, science and faith", ("wonder", "nuclear", "religion", "age", "era", "Great")),
        ("Disasters", ("struck", "Flood", "Storm", "Drought", "Eruption", "Tornado", "Hurricane", "Blizzard")),
    )

    def _world_news(self, events: list[str]) -> None:
        """What happened last turn that you'd know about, grouped, plus what leaders said in public."""
        turn = self.game.turn
        news_for = getattr(self.game, "news_for", None)
        if news_for is not None:
            known = news_for(self.human_id, turn)
        else:
            known = list(events)
        known = [re.sub(r"^T\d+:\s*", "", e) for e in known]
        sections: dict[str, list[str]] = {}
        for e in known:
            title = next((t for t, words in self.NEWS_SECTIONS if any(w.lower() in e.lower() for w in words)), "Elsewhere")
            sections.setdefault(title, []).append(e)
        said = [f"{e['sender_name']}: {e['text']}" for e in self.entries
                if e["channel"] == WORLD and e["kind"] == "message" and e.get("sender") not in (None, self.human_id)
                and not e.get("secret")
                and e["turn"] == turn - 1]
        if said:
            sections["Said in public"] = said[-8:]
        if not sections:
            return
        order = [t for t, _ in self.NEWS_SECTIONS] + ["Elsewhere", "Said in public"]
        items = [{"title": t, "items": sections[t]} for t in order if t in sections]
        n = sum(len(x["items"]) for x in items)
        self._record(NEWS, "news", f"World News, turn {turn}: {n} item{'s' if n != 1 else ''}",
                     f"📰 World News: {n} items", sections=items)

    # ---- grand strategy --------------------------------------------------------------
    STRATEGY_EVENTS = ("at war", "declared war", "made peace", "are now at peace", "lost it to", "changed hands",
                       "eliminated", "nuclear weapons", " met ", "broke away", "formed an alliance", "broke the pact",
                       "entered a dark age", "Emergency declared")

    # Turns between timed rewrites, by era (Ancient, Classical, Medieval, Renaissance, Industrial, Modern, Atomic on):
    # early eras are slow and quiet, later ones change fast. Big news still triggers a rewrite at any time.
    STRATEGY_GAPS = (50, 50, 35, 35, 25, 25, 20)

    def _strategy_gap(self, aid: int) -> int:
        era_of = getattr(self.game, "era_of", None)
        try:
            era = era_of(aid) if era_of else None
        except Exception:   # the era is a nicety; never lose a turn over it
            era = None
        if era is None:
            return self.strategy_every
        return self.STRATEGY_GAPS[max(0, min(int(era), len(self.STRATEGY_GAPS) - 1))]

    def _strategy_due(self, aid: int, events: list[str]) -> str:
        """Why this leader's grand strategy should be (re)written now, or "" if it shouldn't."""
        agent = self.agents[aid]
        st, turn = agent.memory.strategy, self.game.turn
        if not self.strategy_every or agent.strategy_client is None:
            return ""   # no strategy model set up (offline fake leaders, tests)
        if not st:
            return "you have no grand strategy yet"
        since = turn - int(st.get("turn") or 0)
        if since >= self._strategy_gap(aid):
            return f"it's been {since} turns since your strategy was written"
        if since < 3:
            return ""   # a fresh plan isn't rewritten over the next few turns' news
        name = self.game.civ(aid).leader
        big = [e for e in events if name in e and any(k in e for k in self.STRATEGY_EVENTS)]
        if big:
            return "big news: " + " ".join(big)[:300]
        if aid in self._replan and since >= 8:
            return "you asked for it: " + self._replan[aid]
        return ""

    def _write_personas(self) -> None:
        """Leaders with only a generic persona (not one of the hand-written ones) get a real one, once per game."""
        todo = [aid for aid, a in self.agents.items() if a.strategy_client is not None and self.game.civ(aid).alive
                and a.persona.key not in PERSONAS and a.persona.key not in self.personas
                and a.persona.key not in self._persona_tried]
        if not todo:
            return
        self._persona_tried.update(self.agents[aid].persona.key for aid in todo)   # once per run, even if it fails
        self.status = "Getting to know the leaders…"
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(self.parallel, len(todo)))) as pool:
                done = {aid: pool.submit(self.agents[aid].write_persona, self.game.turn) for aid in todo}
            for aid, fut in done.items():
                written = fut.result()
                if written:
                    self.personas[self.agents[aid].persona.key] = written
                    self._record(MINDS, "strategy", f"Who {self._name(aid)} is: {written['temperament']}", None,
                                 sender=aid, why="their personality, written once for this game", plan=written["voice"])
        finally:
            self.status = ""

    def _plan_strategies(self, events: list[str]) -> None:
        due = {aid: why for aid in self.agents if self.game.civ(aid).alive and (why := self._strategy_due(aid, events))}
        if not due:
            return
        self.status = f"{len(due)} leader{'s are' if len(due) > 1 else ' is'} planning a grand strategy…"
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(self.parallel, len(due)))) as pool:
                done = {aid: pool.submit(self.agents[aid].strategize, self.game, why) for aid, why in due.items()}
            for aid, fut in done.items():
                st = fut.result()
                self._replan.pop(aid, None)
                if st:
                    goals = "; ".join(o.get("goal", "") for o in st.get("objectives", []))
                    self._record(MINDS, "strategy", st["grand_strategy"], None, sender=aid, model=st.get("model"),
                                 why=due[aid], plan=goals, assessment=st.get("assessment", ""),
                                 checklist=[c.get("task") for c in st.get("checklist", [])])
        finally:
            self.status = ""

    def _record_mind(self, aid: int, d: Decision, mode: str, with_human: bool, review: bool = False) -> None:
        """What the leader privately thought, with which model, and its plan. Shown in the Minds channel."""
        agent = self.agents[aid]
        why = ("reading your message" if with_human else "replying") if mode == "reply" else (
            "taking its turn, with a message from you" if with_human else "taking its turn")
        if review:
            why += " (empire review)"
        said = " ".join(str(getattr(d, "raw", "") or "").split())[:240]
        text = d.thoughts or (f"(no usable answer: {d.error})" + (f' It replied: "{said}"' if said else "")
                              if d.error else "(kept its thoughts to itself)")
        acted = [a.get("type") for a in d.actions] + [
            f"{'accepted' if r.get('accept') else 'declined'} offer #{r.get('id')}" for r in d.proposal_responses]
        if d.replan:
            self._replan[aid] = d.replan
        quiet = not (d.messages or acted or d.error or with_human)   # nothing happened: hidden by default
        self._record(MINDS, "thought", text, sender=aid, model=agent.last_model.split(":")[-1],
                     why=why, plan=d.journal, said=len(d.messages), acted=acted, quiet=quiet)

    def _from_human(self, inbox: list[dict]) -> bool:
        """Is the human part of this conversation? Then the leader thinks with its talk model."""
        return any(m.get("from_id") == self.human_id for m in inbox)

    def _decide_all(self, aids: list[int], mode: str, review: set[int] | None = None) -> None:
        """Every leader in `aids` decides. With parallel > 1 they think at the same time (each sees the
        world as it was when the turn started), then their decisions are carried out in a fixed order."""
        aids = [a for a in aids if self.game.civ(a).alive]
        review = review or set()
        if self.parallel <= 1 or len(aids) < 2:
            for aid in aids:
                self._decide(aid, mode, aid in review)
            return
        with self._mail_lock:
            inboxes = {aid: self._inbox[aid] for aid in aids}
            for aid in aids:
                self._inbox[aid] = []
        self.status = f"{len(aids)} leaders are thinking…"
        try:
            with ThreadPoolExecutor(max_workers=self.parallel) as pool:
                futures = {aid: pool.submit(self.agents[aid].decide, self.game, mode, inboxes[aid],
                                            self._from_human(inboxes[aid]), aid in review) for aid in aids}
            for aid in aids:
                d = futures[aid].result()
                self._record_mind(aid, d, mode, with_human=self._from_human(inboxes[aid]), review=aid in review)
                self._carry_out(aid, d)
        finally:
            self.status = ""

    def _decide(self, aid: int, mode: str, review: bool = False) -> None:
        with self._mail_lock:
            inbox, self._inbox[aid] = self._inbox[aid], []
        self._thinking(aid)
        try:
            d = self.agents[aid].decide(self.game, mode, inbox, self._from_human(inbox), review)
            self._record_mind(aid, d, mode, with_human=self._from_human(inbox), review=review)
            self._carry_out(aid, d)
        finally:
            self._thinking(None)

    # ---- the human -------------------------------------------------------------
    def say(self, to: str | int, text: str) -> list[str]:
        """You speak. Whoever you addressed answers right away. Returns new feed lines."""
        start = len(self.feed)
        self.answer(self.post(to, text))
        return self.feed[start:]

    def quote(self, entry_id) -> dict | None:
        """A short quote of an earlier message, for replying to it."""
        try:
            eid = int(entry_id)
        except (TypeError, ValueError):
            return None
        e = next((x for x in reversed(self.entries) if x["id"] == eid and x["kind"] == "message"), None)
        if e is None:
            return None
        t = " ".join(str(e["text"]).split())
        return {"id": eid, "name": e.get("sender_name") or "?", "text": t if len(t) <= 160 else t[:157] + "…"}

    def post(self, to: str | int, text: str, reply_to=None) -> list[int]:
        """Your message goes out now (chat log + inboxes). Returns who should answer it. Fast: no LLM."""
        target = self.resolve(to)
        if target is None or target == self.human_id:
            self._record(WORLD, "error", f"No leader called {to!r}.", f"(No leader called {to!r}.)")
            return []
        if isinstance(target, int) and not self._met(self.human_id, target):
            self._record(WORLD, "error", "You haven't met that leader yet.", "(You haven't met that leader yet.)")
            return []
        self._deliver(self.human_id, target, text, self.quote(reply_to) if reply_to is not None else None)
        if target == ALL:
            responders = [aid for aid in self.agents if self._met(self.human_id, aid)]
        elif target in self.groups:
            responders = [m for m in self.groups[target]["members"] if m in self.agents]
            for m in responders:   # the per-turn cap is for AI-to-AI chatter: each of your messages may be answered
                self._pair_count.pop((m, target), None)
        else:
            responders = [target] if target in self.agents else []
        return [aid for aid in responders if self.game.civ(aid).alive]

    def answer(self, responders: list[int]) -> None:
        """The leaders you wrote to reply (the slow part). A leader whose turn already read your
        message, and so may have answered it there, doesn't answer twice."""
        with self._mail_lock:
            waiting = [aid for aid in responders
                       if any(m["from_id"] == self.human_id for m in self._inbox.get(aid, []))]
        for aid in waiting:
            self._decide(aid, "reply")
        self._settle_replies()

    def offer_deal(self, to: int, give: dict, ask: dict, note: str = "",
                   counter_to: int | None = None) -> tuple[ActionResult, list[int]]:
        """You offer a leader a deal (or a gift, when you ask for nothing). It reaches them as a message in
        your private chat, so they answer it right away. Returns (result, who should answer).

        counter_to: their deal you're answering with this one instead. It's declined once yours is on the table.
        """
        to = int(to)
        try:
            counter_to = int(counter_to) if counter_to not in (None, "") else None
        except (TypeError, ValueError):
            counter_to = None
        theirs = next((q for q in self.game.pending_proposals(self.human_id)
                       if q.id == counter_to and q.sender == to and q.kind == "propose_deal"), None)
        if to not in self.agents:
            return ActionResult(False, "you can only trade with AI leaders"), []
        if not self._met(self.human_id, to):
            return ActionResult(False, "you haven't met that leader yet"), []
        note = str(note or "").strip()[:300]
        res = self.game.apply(self.human_id, {"type": "propose_deal", "target": to, "give": give, "ask": ask,
                                              "note": note})
        if not res.ok:
            return res, []
        if res.proposal_id is None:
            text = f"🎁 I'm sending you {side_text(give)}." + (f" {note}" if note else "")
        else:
            parts = [f"I give {side_text(give)}" if give else "", f"you give {side_text(ask)}" if ask else ""]
            made = next((q for q in self.game.pending_proposals(to) if q.id == res.proposal_id), None)
            peace = bool(made and made.terms.get("peace"))
            head = (f"🕊️ Peace offer #{res.proposal_id}: peace, and " if peace else f"📜 Deal offer #{res.proposal_id}: ")
            if theirs:
                head = (f"🔁 Counter-offer #{res.proposal_id} to your #{theirs.id} ({proposal_label(theirs)}): "
                        + ("peace, and " if peace else ""))
            text = head + "; ".join(x for x in parts if x) + "." + (f" {note}" if note else "")
        if theirs:   # yours is on the table now: theirs is turned down
            self.respond(theirs.id, False)
        return res, self.post(to, text)

    def act(self, action: dict) -> list[str]:
        start = len(self.feed)
        res = self.game.apply(self.human_id, action)
        kind = "war" if res.ok and action.get("type") == "declare_war" else "treaty" if res.ok else "error"
        self._record(WORLD, kind, res.message, ("✅ " if res.ok else "❌ ") + res.message)
        return self.feed[start:]

    def respond(self, proposal_id: int, accept: bool) -> list[str]:
        start = len(self.feed)
        res = self.game.resolve_proposal(proposal_id, self.human_id, accept)
        self._record(WORLD, "treaty" if res.ok else "error", res.message, ("✅ " if res.ok else "❌ ") + res.message)
        return self.feed[start:]

    # ---- turns -----------------------------------------------------------------
    def end_turn(self) -> list[str]:
        """Every AI leader takes its turn, they answer each other, then the world moves on."""
        start = len(self.feed)
        for aid in self.agents:
            if self.game.civ(aid).alive:
                self._decide(aid, "turn")
        self._settle_replies()
        events = self.game.end_turn()
        for event in events:
            self._record(WORLD, "event", event, f"🌍 {event}", **self._event_hush(event))
        self._pair_count.clear()
        self.consolidate(self._summaries_due())
        self._record(WORLD, "turn", f"Turn {self.game.turn} begins", f"— Turn {self.game.turn} begins —")
        self._tick_pacts()
        self._world_news(events)
        self.advisor_picks()
        return self.feed[start:]

    def on_new_turn(self) -> list[str]:
        """Real game: you ended your turn in Civ 6 and a new one began. Report what happened, then
        every AI leader takes its turn. (The mock world uses end_turn, which moves the world itself.)"""
        start = len(self.feed)
        began = time.time()
        events = self.game.end_turn()
        for event in events:
            self._record(WORLD, "event", event, f"🌍 {event}", **self._event_hush(event))
        self._record(WORLD, "turn", f"Turn {self.game.turn} begins", f"— Turn {self.game.turn} begins —")
        self._tick_pacts()
        self._world_news(events)
        self.sync_moments()
        self._write_personas()
        self._plan_strategies(events)
        quieted: dict[int, list[str]] = {}
        for _turn, sender, what in getattr(self.game, "take_quieted", lambda: [])():   # Civ's own AI pop-ups we closed
            if what == "YOU_OPENED":
                continue   # you opened Civ's leader screen: closed on purpose, nothing to report
            if what not in quieted.setdefault(sender, []):
                quieted[sender].append(what)
        for sender, whats in quieted.items():
            if sender in self.agents:
                self._record(sender, "info", f"Civ's own AI sent you {', '.join(whats)} for {self._name(sender)}; "
                             f"Civ Council closed it ({self._name(sender)} speaks for itself here).", None)
        for q in getattr(self.game, "take_renewed", lambda: [])():   # queued friendship renewals that just went out
            if q.target == self.human_id and q.sender in self.agents:
                text = f"{self.game.civ(q.sender).leader} offers you {proposal_label(q)} (renewing your friendship)."
                self._record(q.sender, "proposal", text, f"📜 {text} (#{q.id})", sender=q.sender, proposal_id=q.id)
        take_expired = getattr(self.game, "take_expired", None)
        if take_expired:
            self.expired(take_expired())
        reapply = getattr(self.game, "reapply_orders", None)
        if reapply:
            notes = reapply()
            if notes:
                text = "Standing orders: " + "; ".join(notes)
                self._record(MINDS, "orders", text)
        due = {aid for aid in self.agents if self.game.civ(aid).alive and self._review_due(aid)}
        self._decide_all(list(self.agents), "turn", review=due)
        for aid in due:
            self._last_review[aid] = self.game.turn
        self._settle_replies()
        self._pair_count.clear()
        self.consolidate(self._summaries_due())
        self.advisor_picks()
        self.turn_seconds[self.game.turn] = round(time.time() - began, 1)   # before saving, so it's kept
        if self.memory_dir:
            self.save_memories(self.memory_dir)
        return self.feed[start:]

    def _review_due(self, aid: int) -> bool:
        """Time for this leader to run its empire: Civ's AI is choosing for it, or it's been a while."""
        if not self.review_every or not hasattr(self.game, "steering_options"):
            return False
        turn, last = self.game.turn, self._last_review.get(aid)
        pending = any(n.get("civ") == aid for n in getattr(self.game, "notices", {}).values())
        if pending or last is None:
            return True
        if turn - last < 2:
            return False
        standing = getattr(self.game, "orders", {}).get(aid, {})
        return turn - last >= self.review_every or not standing.get("research") or not standing.get("civic")

    def _summaries_due(self) -> list[int]:
        """Leaders whose memory has grown enough to condense. Quiet early turns rarely need it; a busy leader
        in a busy game does more often, and each leader goes on its own."""
        if not self.summarize_after:
            return []
        turn, due = self.game.turn, []
        for aid, agent in self.agents.items():
            m = agent.memory
            if not self.game.civ(aid).alive:
                continue
            if m.summarized is None:
                m.summarized = turn   # counting starts now (also for games from before this rule)
                continue
            since = turn - int(m.summarized)
            if since < self.summary_min_gap:
                continue
            if m.fresh >= self.summarize_after or (since >= self.summary_max_gap and m.fresh >= 5):
                due.append(aid)
        return due

    def consolidate(self, aids: list[int] | None = None) -> None:
        """Leaders condense their memory into a short long-term version (all of them, or just `aids`)."""
        done = []
        for aid, agent in self.agents.items():
            if not self.game.civ(aid).alive or (aids is not None and aid not in aids):
                continue
            self.status = f"{agent.persona.leader} is reflecting on the past…"
            agent.memory.summarized = self.game.turn   # tried: don't retry every turn if it fails
            if agent.consolidate(self.game):
                done.append(aid)
        self.status = ""
        known = [self.game.civ(a).leader for a in done if not self._hush(a)]   # only leaders you've met
        if known:
            text = f"{', '.join(known)} reflected on recent history (memory condensed)."
            self._record(WORLD, "info", text, f"📚 {text}")

    def _settle_replies(self) -> None:
        """AIs who got messages from other AIs may answer, for a limited number of rounds."""
        for _ in range(self.reply_rounds):
            waiting = [aid for aid, box in self._inbox.items()
                       if any(m["from_id"] != self.human_id for m in box) and self.game.civ(aid).alive]
            if not waiting:
                return
            self._decide_all(waiting, "reply")

    # ---- persistence -------------------------------------------------------------
    # ---- usage ------------------------------------------------------------------------
    @staticmethod
    def _add_usage(totals: dict, calls) -> dict:
        """Adds calls into running totals: overall, and per (what for, model)."""
        for u in calls:
            cost, turn = float(u.get("cost_usd") or 0), int(u.get("turn", 0))
            totals["calls"] = totals.get("calls", 0) + 1
            totals["cost"] = totals.get("cost", 0.0) + cost
            totals["tokens_in"] = totals.get("tokens_in", 0) + int(u.get("input_tokens") or 0)
            totals["tokens_out"] = totals.get("tokens_out", 0) + int(u.get("output_tokens") or 0)
            totals["model_seconds"] = totals.get("model_seconds", 0.0) + float(u.get("seconds") or 0)
            totals["first_turn"] = min(totals.get("first_turn", turn), turn)
            totals["last_turn"] = max(totals.get("last_turn", turn), turn)
            row = totals.setdefault("rows", {}).setdefault(f"{u.get('purpose', '?')}|{u.get('model', '?')}",
                                                           {"calls": 0, "cost": 0.0})
            row["calls"] += 1
            row["cost"] += cost
        return totals

    def _archive_old_usage(self) -> None:
        """Past USAGE_KEEP calls, the oldest are folded into the archived totals, so the game total never shrinks."""
        extra = len(self.usage) - USAGE_KEEP
        if extra > 0:
            self._add_usage(self.usage_archived, self.usage[:extra])
            del self.usage[:extra]   # in place: the leaders append to this same list

    def usage_totals(self) -> dict:
        """The whole game so far: archived totals plus every call still kept."""
        return self._add_usage(json.loads(json.dumps(self.usage_archived)), list(self.usage))

    def save_memories(self, folder: Path) -> None:
        with self._save_lock:   # the worker's autosave and a Ctrl+C save must not write the same files at once
            self._save_memories(folder)

    def _save_memories(self, folder: Path) -> None:
        for agent in self.agents.values():
            agent.memory.save(folder / f"{agent.persona.key}.json")
        save_state = getattr(self.game, "save_state", None)
        if save_state:
            save_state(folder / "game_state.json")
        self.pacts.save(folder / "pacts.json")
        folder.mkdir(parents=True, exist_ok=True)
        self._archive_old_usage()
        (folder / "usage.json").write_text(json.dumps({"calls": self.usage, "archived": self.usage_archived,
                                                       "turn_seconds": {str(k): v for k, v in self.turn_seconds.items()}}))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "personas.json").write_text(json.dumps(self.personas))
        (folder / "prefs.json").write_text(json.dumps({"civ_notify": self.civ_notify, "advisor_on": self.advisor_on,
                                                       "models": self.models,
                                                       "quiet_civ_ai": getattr(self.game, "quiet_ai", None),
                                                       "block_civ_screen": getattr(self.game, "block_screen", None)}))
        folder.mkdir(parents=True, exist_ok=True)   # your chats and group chats survive a restart
        hist = folder / "history.json.tmp"
        hist.write_text(json.dumps(self.history))
        hist.replace(folder / "history.json")
        tmp = folder / "chat.json.tmp"
        tmp.write_text(json.dumps({"entries": self.entries[-CHAT_KEEP:], "groups": self.groups}))
        tmp.replace(folder / "chat.json")

    def sync_moments(self) -> int:
        """Civ's Historic Moments into the story (back to turn 1, every civ). Returns how many were new."""
        read = getattr(self.game, "historic_moments", None)
        if read is None:
            return 0
        try:
            moments = read()
        except Exception:   # the timeline is an extra; never lose a turn over it
            return 0
        seen = {(h["turn"], h.get("by"), h["text"]) for h in self.history if h["kind"] == "moment"}
        civs, new = list(self.game.civs()), 0
        for turn, who, score, text in moments:
            if (turn, who, text) in seen:
                continue
            seen.add((turn, who, text))
            found = {c.id for c in names_in(text, civs).values()} | {who}
            self.history.append({"turn": turn, "kind": "moment", "text": text, "civs": sorted(found), "by": who,
                                 **({"score": score} if score else {})})
            new += 1
        if new:
            self.history.sort(key=lambda h: h["turn"])   # stable: same-turn lines keep their order
        return new

    def _load_history(self, folder: Path) -> None:
        """The game's story. The first time, rebuilt from the chat that's still kept (older turns are gone)."""
        try:
            self.history = json.loads((folder / "history.json").read_text())
            return
        except (OSError, ValueError):
            pass
        civs = list(self.game.civs())
        self.history = []
        for e in self.entries:
            k = history_kind(e.get("channel"), e.get("kind", ""), e)
            if k:
                by = e.get("sender")
                found = {c.id for c in names_in(e.get("text", ""), civs).values()} | ({by} if by is not None else set())
                self.history.append({"turn": e.get("turn", 0), "kind": k, "text": e.get("text", ""),
                                     "civs": sorted(found), **({"by": by} if by is not None else {})})

    def _rebuild_chat(self) -> None:
        """Old chats from the leaders' own conversation logs (messages only, deduplicated, oldest first)."""
        ids = {}
        for cid in [self.human_id, *self.agents]:
            try:
                ids[self.game.civ(cid).leader] = cid
            except (KeyError, ValueError):
                pass
        human = str(self.human_id)
        seen, rows = set(), []
        for aid, agent in self.agents.items():
            for key, convo in agent.memory.conversations.items():
                if key == human:
                    channel, extra = aid, {}
                elif key == "public":
                    channel, extra = WORLD, {}
                elif key.lstrip("-").isdigit() and int(key) in self.agents:   # leader to leader: Spy only
                    channel = SPY
                    extra = None
                else:
                    continue   # group chats weren't saved, so there's nothing to put them in
                for n, m in enumerate(convo):
                    if not isinstance(m, dict) or not m.get("text"):
                        continue
                    sender = ids.get(m.get("from"))
                    if channel == SPY:
                        to = aid if sender != aid else int(key)
                        extra = {"to": to, "to_name": self._name(to)}
                    sig = (m.get("turn"), m.get("from"), m.get("text"),
                           channel if channel != SPY else frozenset({aid, int(key)}))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    rows.append((int(m.get("turn") or 0), aid, n, channel, sender, str(m.get("from") or "?"),
                                 str(m["text"]), extra))
        if not rows:
            return
        rows.sort(key=lambda r: r[:3])
        for turn, _aid, _n, channel, sender, name, text, extra in rows:
            self.entries.append({"id": next(self._ids), "turn": turn, "channel": channel, "kind": "message",
                                 "sender": sender, "sender_name": name, "text": text, "restored": True, **extra})
        self._record(WORLD, "info", "Older messages were rebuilt from what the leaders remember "
                     "(offers, events and group chats from before can't be).")

    def load_memories(self, folder: Path) -> None:
        for agent in self.agents.values():
            agent.memory = LeaderMemory.load(folder / f"{agent.persona.key}.json")
        load_state = getattr(self.game, "load_state", None)
        if load_state:
            load_state(folder / "game_state.json")
        self.pacts = PactBook.load(folder / "pacts.json")
        try:
            self.personas = {k: v for k, v in json.loads((folder / "personas.json").read_text()).items()
                             if isinstance(v, dict) and v.get("temperament") and v.get("voice")}
        except (OSError, ValueError):
            self.personas = {}
        for agent in self.agents.values():
            if agent.persona.key in self.personas:
                agent.use_persona(self.personas[agent.persona.key])
        try:
            used = json.loads((folder / "usage.json").read_text())
            self.usage[:] = [u for u in used.get("calls", []) if isinstance(u, dict)]
            self.usage_archived = used.get("archived") if isinstance(used.get("archived"), dict) else {}
            self._archive_old_usage()
            self.turn_seconds = {int(k): float(v) for k, v in used.get("turn_seconds", {}).items()}
        except (OSError, ValueError):
            pass
        try:
            prefs = json.loads((folder / "prefs.json").read_text())
            self.civ_notify = bool(prefs.get("civ_notify", True))
            self.advisor_on = bool(prefs.get("advisor_on", True))
            if prefs.get("models") and self.llm_factory is not None:   # what you picked in Settings last time
                self.set_models(**prefs["models"])
            if prefs.get("quiet_civ_ai") is not None and hasattr(self.game, "quiet_ai"):
                self.game.quiet_ai = bool(prefs["quiet_civ_ai"])
            if prefs.get("block_civ_screen") is not None and hasattr(self.game, "block_screen"):
                self.game.block_screen = bool(prefs["block_civ_screen"])
        except (OSError, ValueError):
            pass
        for agent in self.agents.values():
            agent.pact_book = self.pacts
        chat = folder / "chat.json"
        if not chat.exists() and not self.entries:
            self._rebuild_chat()                  # from before chats were saved: what the leaders remember
        if chat.exists() and not self.entries:   # only at startup: the page is already showing what's here
            try:
                data = json.loads(chat.read_text())
            except (OSError, ValueError):
                return
            self.entries = [e for e in data.get("entries", []) if isinstance(e, dict) and "id" in e]
            self.groups = {str(k): v for k, v in data.get("groups", {}).items()} or self.groups
            self._load_history(folder)
            if self.advisor is not None:   # it remembers what it last recommended
                last = next((e for e in reversed(self.entries) if e.get("channel") == ADVISOR and e.get("picks")), None)
                self.advisor.last_picks = last["picks"] if last else None
            self._sync_groups()   # the leaders are back in their group chats too
            if self.entries:
                self._ids = itertools.count(max(e["id"] for e in self.entries) + 1)
