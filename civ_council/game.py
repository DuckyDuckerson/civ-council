"""Game interface plus a mock Civ 6 world for building and testing without the real game.

The real Civ 6 bridge (see bridge/) will implement the same `Game` protocol, so the
agents and council never need to know whether they're talking to the mock or Civ 6.
"""
from __future__ import annotations

import itertools
import random
import re
from dataclasses import dataclass, field
from typing import Protocol

# Diplomatic stances between two civs, roughly matching Civ 6.
WAR = "war"
PEACE = "peace"
FRIENDS = "declared_friends"
ALLIED = "allied"

ACTION_TYPES = {
    "declare_war",
    "make_peace",          # proposal: target must accept
    "propose_friendship",  # proposal
    "propose_alliance",    # proposal
    "denounce",
    "break_alliance",
    "propose_deal",        # gold deal: proposal, or a gift (nothing asked back) that goes through at once
    "gift_units",          # hand units over (real game with the Gift It To Me mod)
}
PROPOSAL_ACTIONS = {"make_peace", "propose_friendship", "propose_alliance", "propose_deal"}
PROPOSAL_LABELS = {"make_peace": "peace", "propose_friendship": "friendship", "propose_alliance": "an alliance",
                   "propose_deal": "a deal"}
MAX_DEAL_GOLD = 2000
DEAL_NUMBERS = ("gold", "gold_per_turn", "favor")
CITY_NAME_RE = re.compile(r"^[^\"\\|\n\r]{1,40}$")


def _side(raw) -> tuple[dict | None, str]:
    """One side of a deal: {"gold": n, "gold_per_turn": n, "favor": n, "resources": {KEY: n}, "open_borders": bool}."""
    if raw in (None, ""):
        return {}, ""
    if not isinstance(raw, dict):
        return None, "each side of a deal must be an object"
    side: dict = {}
    for key in DEAL_NUMBERS:
        try:
            n = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            return None, f"{key} must be a number"
        if n < 0 or n > MAX_DEAL_GOLD:
            return None, f"{key} must be between 0 and {MAX_DEAL_GOLD}"
        if n:
            side[key] = n
    res = raw.get("resources") or {}
    if isinstance(res, list):
        res = {r: 1 for r in res}
    if not isinstance(res, dict):
        return None, "resources must be like {\"RESOURCE_IRON\": 5}"
    clean = {}
    for name, n in list(res.items())[:6]:
        name = str(name).strip().upper()
        if not name.startswith("RESOURCE_") or not name.replace("_", "").isalnum():
            return None, f"unknown resource {name!r}"
        try:
            n = int(n)
        except (TypeError, ValueError):
            return None, f"amount of {name} must be a number"
        if n < 1 or n > 100:
            return None, f"amount of {name} must be 1-100"
        clean[name] = n
    if clean:
        side["resources"] = clean
    if raw.get("open_borders") is True:
        side["open_borders"] = True
    cities = raw.get("cities") or []
    if isinstance(cities, str):
        cities = [cities]
    if not isinstance(cities, list):
        return None, "cities must be a list of city names"
    names = []
    for name in cities[:3]:
        name = str(name).strip()
        if not CITY_NAME_RE.match(name):
            return None, f"odd city name {name!r}"
        names.append(name)
    if names:
        side["cities"] = names
    return side, ""


def deal_terms(action: dict) -> tuple[dict | None, str]:
    """{"give": {...}, "ask": {...}} from an action, checked. give_gold/ask_gold still work.

    Returns (terms, "") or (None, why).
    """
    give, why = _side(action.get("give"))
    if give is None:
        return None, why
    ask, why = _side(action.get("ask"))
    if ask is None:
        return None, why
    for key, side in (("give_gold", give), ("ask_gold", ask)):
        if action.get(key) not in (None, "", 0):
            extra, why = _side({"gold": action.get(key)})
            if extra is None:
                return None, why.replace("gold", key)
            side["gold"] = side.get("gold", 0) + extra.get("gold", 0)
    if not give and not ask:
        return None, "a deal needs something in give or ask"
    return {"give": give, "ask": ask}, ""


def side_text(side: dict) -> str:
    parts = []
    if side.get("gold"):
        parts.append(f"{side['gold']} gold")
    if side.get("gold_per_turn"):
        parts.append(f"{side['gold_per_turn']} gold per turn")
    for name, n in side.get("resources", {}).items():
        label = name.removeprefix("RESOURCE_").replace("_", " ").title()
        parts.append(f"{label} x{n}" if n > 1 else label)
    if side.get("favor"):
        parts.append(f"{side['favor']} diplomatic favor")
    if side.get("open_borders"):
        parts.append("open borders")
    for name in side.get("cities", []):
        parts.append(f"the city of {name}")
    return ", ".join(parts)


def deal_summary(terms: dict, sender: str, target: str) -> str:
    """'Tokugawa gives 50 gold; Elizabeth I gives Iron x5' style."""
    verb = lambda who: "give" if who.lower() == "you" else "gives"  # noqa: E731
    parts = []
    if terms.get("give"):
        parts.append(f"{sender} {verb(sender)} {side_text(terms['give'])}")
    if terms.get("ask"):
        parts.append(f"{target} {verb(target)} {side_text(terms['ask'])}")
    return "; ".join(parts)


def is_gift(terms: dict) -> bool:
    return not terms.get("ask")


def proposal_label(p: "Proposal") -> str:
    """What's on offer, from the receiver's point of view."""
    if p.kind != "propose_deal":
        return PROPOSAL_LABELS[p.kind]
    parts = []
    if p.terms.get("give"):
        parts.append(f"you get {side_text(p.terms['give'])}")
    if p.terms.get("ask"):
        parts.append(f"you give {side_text(p.terms['ask'])}")
    return ("peace, if " if p.terms.get("peace") else "a deal: ") + " and ".join(parts)


@dataclass
class Civ:
    id: int
    name: str          # civilization, e.g. "Sumeria"
    leader: str        # e.g. "Gilgamesh"
    is_human: bool = False
    cities: int = 1
    military: int = 10
    science: int = 5
    culture: int = 5
    gold: int = 50
    alive: bool = True

    @property
    def label(self) -> str:
        return f"{self.leader} of {self.name}"


@dataclass
class Proposal:
    id: int
    kind: str      # one of PROPOSAL_ACTIONS
    sender: int
    target: int
    turn: int
    note: str = ""
    terms: dict = field(default_factory=dict)   # deals: {"give_gold": n, "ask_gold": n}


@dataclass
class ActionResult:
    ok: bool
    message: str
    proposal_id: int | None = None   # set when the action created a proposal


class Game(Protocol):
    """What the council needs from a game. MockGame and the future Civ6Game implement this."""

    turn: int

    def civs(self) -> list[Civ]: ...
    def civ(self, civ_id: int) -> Civ: ...
    def stance(self, a: int, b: int) -> str: ...
    def is_denouncing(self, a: int, b: int) -> bool: ...
    def snapshot(self, viewer: int) -> dict: ...
    def apply(self, actor: int, action: dict) -> ActionResult: ...
    def pending_proposals(self, target: int) -> list[Proposal]: ...
    def resolve_proposal(self, proposal_id: int, responder: int, accept: bool) -> ActionResult: ...
    def end_turn(self) -> list[str]: ...


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


@dataclass
class MockGame:
    """A tiny stand-in for Civ 6: stances, proposals, and some numbers that move each turn."""

    players: list[Civ]
    seed: int = 0
    turn: int = 1
    _stances: dict[tuple[int, int], str] = field(default_factory=dict)
    _denounced: set[tuple[int, int]] = field(default_factory=set)  # (who, whom)
    _proposals: dict[int, Proposal] = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # ---- reads -------------------------------------------------------------
    def civs(self) -> list[Civ]:
        return list(self.players)

    def civ(self, civ_id: int) -> Civ:
        for c in self.players:
            if c.id == civ_id:
                return c
        raise KeyError(f"no civ with id {civ_id}")

    def stance(self, a: int, b: int) -> str:
        return self._stances.get(_pair(a, b), PEACE)

    def is_denouncing(self, a: int, b: int) -> bool:
        return (a, b) in self._denounced

    def score(self, c: Civ) -> int:
        return c.cities * 20 + c.military + c.science * 2 + c.culture * 2

    def snapshot(self, viewer: int) -> dict:
        me = self.civ(viewer)
        others = []
        for c in self.players:
            if c.id == viewer or not c.alive:
                continue
            others.append({
                "id": c.id,
                "leader": c.leader,
                "civ": c.name,
                "is_human": c.is_human,
                "stance_with_you": self.stance(viewer, c.id),
                "you_denounced_them": self.is_denouncing(viewer, c.id),
                "they_denounced_you": self.is_denouncing(c.id, viewer),
                "score": self.score(c),
                "cities": c.cities,
                "military": c.military,
            })
        wars = sorted({
            f"{self.civ(a).leader} vs {self.civ(b).leader}"
            for (a, b), s in self._stances.items() if s == WAR
        })
        alliances = sorted({
            f"{self.civ(a).leader} + {self.civ(b).leader}"
            for (a, b), s in self._stances.items() if s == ALLIED
        })
        return {
            "turn": self.turn,
            "you": {
                "id": me.id, "leader": me.leader, "civ": me.name,
                "cities": me.cities, "military": me.military, "science": me.science,
                "culture": me.culture, "gold": me.gold, "score": self.score(me),
            },
            "others": others,
            "world_wars": wars,
            "world_alliances": alliances,
            "recent_events": self.events[-10:],
        }

    # ---- actions -----------------------------------------------------------
    def apply(self, actor: int, action: dict) -> ActionResult:
        kind = action.get("type")
        if kind == "gift_units":
            return ActionResult(False, "gifting units needs a real game with the Gift It To Me mod")
        target = action.get("target")
        if kind not in ACTION_TYPES:
            return ActionResult(False, f"unknown action {kind!r}")
        try:
            target = int(target)
            them = self.civ(target)
        except (TypeError, ValueError, KeyError):
            return ActionResult(False, f"bad target {target!r}")
        if target == actor:
            return ActionResult(False, "cannot target yourself")
        if not them.alive:
            return ActionResult(False, f"{them.label} is gone")
        me = self.civ(actor)
        cur = self.stance(actor, target)

        if kind == "declare_war":
            if cur == WAR:
                return ActionResult(False, "already at war")
            if cur == ALLIED:
                return ActionResult(False, "break the alliance first")
            self._set(actor, target, WAR)
            return self._log(f"{me.label} declared war on {them.label}!")

        if kind == "denounce":
            if self.is_denouncing(actor, target):
                return ActionResult(False, "already denounced")
            if cur in (FRIENDS, ALLIED):
                self._set(actor, target, PEACE)
            self._denounced.add((actor, target))
            return self._log(f"{me.label} denounced {them.label}.")

        if kind == "break_alliance":
            if cur not in (FRIENDS, ALLIED):
                return ActionResult(False, "no friendship or alliance to break")
            self._set(actor, target, PEACE)
            return self._log(f"{me.label} ended their {cur.replace('_', ' ')} with {them.label}.")

        if kind == "propose_deal":
            terms, why = deal_terms(action)
            if terms is None:
                return ActionResult(False, why)
            if any(set(side) - {"gold"} for side in terms.values()):
                return ActionResult(False, "the practice world only trades lump-sum gold")
            if cur == WAR:
                return ActionResult(False, "no trading while at war")
            give, ask = terms["give"].get("gold", 0), terms["ask"].get("gold", 0)
            if me.gold < give:
                return ActionResult(False, f"{me.leader} only has {me.gold} gold")
            if not is_gift(terms):
                for q in self._proposals.values():
                    if q.sender == actor and q.target == target and q.kind == kind:
                        return ActionResult(False, f"you already offered {them.leader} a deal (proposal #{q.id}); "
                                                   "wait for their answer")
            if is_gift(terms):   # a gift goes through at once
                me.gold -= give
                them.gold += give
                return self._log(f"{me.label} sent {them.label} {side_text(terms['give'])}.")
            p = Proposal(next(self._ids), kind, actor, target, self.turn, str(action.get("note", ""))[:200], terms)
            self._proposals[p.id] = p
            return ActionResult(True, f"{me.leader} offered {them.leader} a deal: "
                                      f"{deal_summary(terms, me.leader, them.leader)} (proposal #{p.id}).", p.id)

        # Proposals need the other side to say yes.
        if kind == "make_peace" and cur != WAR:
            return ActionResult(False, "not at war")
        if kind == "propose_friendship" and cur != PEACE:
            return ActionResult(False, f"current stance is {cur}")
        if kind == "propose_alliance" and cur not in (PEACE, FRIENDS):
            return ActionResult(False, f"current stance is {cur}")
        for p in self._proposals.values():
            if p.sender == actor and p.target == target and p.kind == kind:
                return ActionResult(False, "that proposal is already pending")
        p = Proposal(next(self._ids), kind, actor, target, self.turn, str(action.get("note", ""))[:200])
        self._proposals[p.id] = p
        return ActionResult(True, f"{me.leader} offered {them.leader} {PROPOSAL_LABELS[kind]} (proposal #{p.id}).", p.id)

    def pending_proposals(self, target: int) -> list[Proposal]:
        return [p for p in self._proposals.values() if p.target == target]

    def trade_options(self, civ_id: int) -> dict:
        """What this civ could put in a deal with each other civ (the practice world only trades gold)."""
        me = self.civ(civ_id)
        return {c.leader: {"civ_id": c.id, "you_can_give": {"gold": me.gold} if me.gold > 0 else {},
                           "they_can_give": {"gold": c.gold} if c.gold > 0 else {}}
                for c in self.players if c.id != civ_id and c.alive and self.stance(civ_id, c.id) != WAR}

    def offers_from(self, sender: int) -> list[Proposal]:
        """Proposals this civ made that are still waiting for an answer."""
        return [p for p in self._proposals.values() if p.sender == sender]

    def resolve_proposal(self, proposal_id: int, responder: int, accept: bool) -> ActionResult:
        p = self._proposals.get(proposal_id)
        if p is None or p.target != responder:
            return ActionResult(False, f"no pending proposal #{proposal_id} for you")
        del self._proposals[proposal_id]
        a, b = self.civ(p.sender), self.civ(p.target)
        if not accept:
            return self._log(f"{b.label} rejected {a.label}'s offer of {PROPOSAL_LABELS[p.kind]}.")
        if p.kind == "propose_deal":
            give, ask = p.terms["give"].get("gold", 0), p.terms["ask"].get("gold", 0)
            if a.gold < give or b.gold < ask:
                self._log(f"{a.label} and {b.label} agreed to a deal, but it fell through: "
                          "one side can't afford it any more.")
                return ActionResult(False, f"{a.leader} and {b.leader} agreed, but the deal fell through: "
                                           "one side can't afford it any more.")
            a.gold += ask - give
            b.gold += give - ask
            return self._log(f"{a.label} and {b.label} made a deal: {deal_summary(p.terms, a.leader, b.leader)}.")
        new = {"make_peace": PEACE, "propose_friendship": FRIENDS, "propose_alliance": ALLIED}[p.kind]
        self._set(p.sender, p.target, new)
        self._denounced.discard((p.sender, p.target))
        self._denounced.discard((p.target, p.sender))
        for pid, q in list(self._proposals.items()):   # both offered the same thing: the other offer is moot
            if q.kind == p.kind and {q.sender, q.target} == {p.sender, p.target}:
                del self._proposals[pid]
        return self._log(f"{a.label} and {b.label} are now {new.replace('_', ' ')}.")

    # ---- turn --------------------------------------------------------------
    def end_turn(self) -> list[str]:
        start = len(self.events)
        rng = self._rng
        for c in self.players:
            if not c.alive:
                continue
            c.science += rng.randint(0, 2)
            c.culture += rng.randint(0, 2)
            c.military += rng.randint(-1, 3)
            c.gold += 5 + c.cities * 2
            if rng.random() < 0.08:
                c.cities += 1
                self.events.append(f"{c.label} founded a new city.")
        # Wars: the stronger side may take a city. Allies add half their military.
        for (a, b), s in list(self._stances.items()):
            if s != WAR:
                continue
            ca, cb = self.civ(a), self.civ(b)
            if not (ca.alive and cb.alive):
                continue
            pa, pb = self._power(a), self._power(b)
            ca.military = max(1, ca.military - rng.randint(0, 2))
            cb.military = max(1, cb.military - rng.randint(0, 2))
            if rng.random() < 0.25:
                win, lose = (ca, cb) if rng.random() < pa / (pa + pb) else (cb, ca)
                # A last city (the capital) only falls to an overwhelming army.
                if lose.cities == 1 and self._power(win.id) < 2 * self._power(lose.id):
                    continue
                lose.cities -= 1
                win.cities += 1
                self.events.append(f"{win.label} captured a city from {lose.label}.")
                if lose.cities <= 0:
                    lose.alive = False
                    self.events.append(f"{lose.label} has been eliminated!")
        # Proposals expire after a few turns.
        for pid, p in list(self._proposals.items()):
            if self.turn - p.turn >= 3:
                del self._proposals[pid]
        self.turn += 1
        return self.events[start:]

    # ---- helpers -----------------------------------------------------------
    def _power(self, civ_id: int) -> float:
        base = self.civ(civ_id).military
        for c in self.players:
            if c.id != civ_id and c.alive and self.stance(civ_id, c.id) == ALLIED:
                base += c.military / 2
        return max(base, 1)

    def _set(self, a: int, b: int, stance: str) -> None:
        self._stances[_pair(a, b)] = stance

    def _log(self, msg: str) -> ActionResult:
        self.events.append(f"T{self.turn}: {msg}")
        return ActionResult(True, msg)
