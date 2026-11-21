"""Pacts: agreements in words between two leaders (or a whole group chat), beyond what Civ's deal screen can trade.

A vassal or puppet arrangement, a non-aggression pact, a border line, a joint war on someone, a voting
bloc, splitting a continent... Civ can't enforce any of it. A pact holds because the leaders keep it, and
everyone remembers who kept theirs and who broke them. Kept here (and saved with the memories), shown in
both leaders' briefings while it lasts.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

PACT_ACTIONS = {"propose_pact", "end_pact"}
OFFER_TURNS = 3            # an offer waits this long for an answer
MAX_TITLE, MAX_TERMS = 60, 500
MAX_TURNS = 1000           # "turns": None means it lasts until someone ends it
RECENT = 5                 # how long an ended pact stays in the briefing


def pact_key(pid: int) -> str:
    return f"pact-{pid}"


def parse_key(value) -> int | None:
    """'pact-3' (or 3) -> 3."""
    text = str(value or "").strip().lower().removeprefix("pact-").removeprefix("#")
    return int(text) if text.isdigit() else None


@dataclass
class Pact:
    id: int
    a: int                  # who proposed it
    b: int
    title: str
    terms: str
    turns: int | None       # length once agreed; None = until either side ends it
    offered: int            # turn it was offered
    status: str = "offered"   # offered, active, declined, expired, failed (payment fell through), ended, broken
    since: int | None = None
    ends: int | None = None
    closed: int | None = None   # turn it stopped being offered/active
    by: int | None = None       # who declined, ended or broke it
    note: str = ""
    trade: dict | None = None   # a payment that goes with it ({"give": ..., "ask": ...}, as in deals), made on acceptance

    @property
    def key(self) -> str:
        return pact_key(self.id)

    def other(self, who: int) -> int:
        return self.b if who == self.a else self.a

    def length(self) -> str:
        return f"for {self.turns} turns" if self.turns else "until either side ends it"

    def label(self) -> str:
        return f"a pact, {self.title}: {self.terms} ({self.length()})" + (f"; {self.payment()}" if self.trade else "")

    def payment(self, a_name: str = "the proposer", b_name: str = "the other side") -> str:
        from .game import side_text
        verb = lambda who: "give" if who.lower() in ("i", "you") else "gives"  # noqa: E731
        bits = []
        if self.trade and self.trade.get("give"):
            bits.append(f"{a_name} {verb(a_name)} {side_text(self.trade['give'])}")
        if self.trade and self.trade.get("ask"):
            bits.append(f"{b_name} {verb(b_name)} {side_text(self.trade['ask'])}")
        return ("with it, " + " and ".join(bits)) if bits else ""



@dataclass
class GroupPact:
    """A pact offered in a group chat: it binds every member, and starts only once all of them accept."""
    id: int
    group: str              # the group chat it belongs to
    a: int                  # who proposed it
    members: list[int]      # who it binds: the group's members when it was offered
    title: str
    terms: str
    turns: int | None
    offered: int
    war_on: int | None = None   # a joint war: once all accept, every member declares war on this civ
    accepted: list[int] = field(default_factory=list)
    status: str = "offered"     # offered, active, declined, expired, ended
    since: int | None = None
    ends: int | None = None
    closed: int | None = None
    by: int | None = None       # who declined or withdrew it
    left: list[int] = field(default_factory=list)    # members who walked out of it (or were voted out of the group)
    broke: list[int] = field(default_factory=list)   # ...of those, who walked out before its agreed end
    events: list[list] = field(default_factory=list)  # [turn, civ, "left"|"broke"|"removed"], for briefings

    @property
    def key(self) -> str:
        return pact_key(self.id)

    @property
    def parties(self) -> list[int]:
        return [m for m in self.members if m not in self.left]

    def waiting_for(self) -> list[int]:
        return [m for m in self.members if m not in self.accepted]

    def length(self) -> str:
        return f"for {self.turns} turns" if self.turns else "until the members end it"

    def label(self) -> str:
        return f"a group pact, {self.title}: {self.terms} ({self.length()})"

@dataclass
class PactBook:
    pacts: dict[int, Pact] = field(default_factory=dict)
    next_id: int = 1
    group_pacts: dict[int, GroupPact] = field(default_factory=dict)   # same numbering as pacts: pact-N is unique

    @staticmethod
    def _clean(title, terms, turns) -> tuple[str, str, int | None] | str:
        title = " ".join(str(title or "").split())[:MAX_TITLE]
        terms = " ".join(str(terms or "").split())[:MAX_TERMS]
        if not terms:
            return "a pact needs terms: say what each side agrees to"
        if turns in (None, "", 0, "0", "forever", "open", "open-ended"):
            return title, terms, None
        try:
            turns = int(turns)
        except (TypeError, ValueError):
            return f"turns should be a number, not {turns!r}"
        if not 1 <= turns <= MAX_TURNS:
            return f"turns should be between 1 and {MAX_TURNS} (or left out for open-ended)"
        return title, terms, turns

    # ---- changes -----------------------------------------------------------------
    def offer(self, a: int, b: int, title, terms, turns, turn: int, trade: dict | None = None) -> Pact | str:
        if a == b:
            return "a pact needs two sides"
        clean = self._clean(title, terms, turns)
        if isinstance(clean, str):
            return clean
        title, terms, turns = clean
        if any(p.status == "offered" and p.a == a and p.b == b for p in self.pacts.values()):
            return "you already have a pact offer waiting for their answer"
        p = Pact(self.next_id, a, b, title or terms[:40], terms, turns, turn,
                 trade=trade if trade and (trade.get("give") or trade.get("ask")) else None)
        self.pacts[p.id] = p
        self.next_id += 1
        return p

    def answer(self, pid, who: int, accept: bool, turn: int) -> Pact | str:
        p = self.pacts.get(parse_key(pid) or -1)
        if p is None or p.status != "offered" or p.b != who:
            return f"no pact offer {pid} waiting for you"
        if accept:
            p.status, p.since = "active", turn
            p.ends = turn + p.turns if p.turns else None
        else:
            p.status, p.closed, p.by = "declined", turn, who
        return p

    def end(self, pid, who: int, turn: int, note: str = "") -> Pact | str:
        """End a pact. Before its agreed length is up, that's breaking it."""
        p = self.pacts.get(parse_key(pid) or -1)
        if p is None or who not in (p.a, p.b):
            return f"no pact {pid} of yours"
        if p.status == "offered" and who == p.a:
            p.status, p.closed, p.by = "expired", turn, who   # withdrawn
            return p
        if p.status != "active":
            return f"pact {p.key} isn't in force"
        p.status = "broken" if p.ends and turn < p.ends else "ended"
        p.closed, p.by, p.note = turn, who, " ".join(str(note or "").split())[:200]
        return p

    def tick(self, turn: int) -> list[Pact]:
        """A new turn: unanswered offers expire, pacts that ran their length end. Returns what changed."""
        changed = []
        for p in self.pacts.values():
            if p.status == "offered" and turn - p.offered >= OFFER_TURNS:
                p.status, p.closed = "expired", turn
                changed.append(p)
            elif p.status == "active" and p.ends is not None and turn >= p.ends:
                p.status, p.closed = "ended", turn
                changed.append(p)
        for p in self.group_pacts.values():
            if p.status == "offered" and turn - p.offered >= OFFER_TURNS:
                p.status, p.closed = "expired", turn
                changed.append(p)
            elif p.status == "active" and p.ends is not None and turn >= p.ends:
                p.status, p.closed = "ended", turn
                changed.append(p)
        return changed

    def drop_civ(self, civ: int, turn: int) -> list[Pact]:
        """A civ is gone: its pacts and offers lapse."""
        out = []
        for p in self.pacts.values():
            if civ in (p.a, p.b) and p.status in ("offered", "active"):
                p.status, p.closed = ("ended" if p.status == "active" else "expired"), turn
                out.append(p)
        return out + self.remove_member("", civ, turn)

    # ---- group pacts ---------------------------------------------------------------
    def group_pact(self, pid) -> GroupPact | None:
        return self.group_pacts.get(parse_key(pid) or -1)

    def offer_group(self, a: int, group: str, members: list[int], title, terms, turns, turn: int,
                    war_on: int | None = None) -> GroupPact | str:
        members = sorted(set(members))
        if a not in members or len(members) < 2:
            return "a group pact needs you and at least one other member"
        if war_on is not None and war_on in members:
            return "a joint war can't be on one of the group's own members"
        clean = self._clean(title, terms, turns)
        if isinstance(clean, str):
            return clean
        title, terms, turns = clean
        if any(p.status == "offered" and p.a == a and p.group == group for p in self.group_pacts.values()):
            return "you already have a pact offer waiting in this group"
        p = GroupPact(self.next_id, group, a, members, title or terms[:40], terms, turns, turn, war_on, accepted=[a])
        self.group_pacts[p.id] = p
        self.next_id += 1
        return p

    def answer_group(self, pid, who: int, accept: bool, turn: int) -> GroupPact | str:
        p = self.group_pact(pid)
        if p is None or p.status != "offered" or who not in p.waiting_for():
            return f"no group pact offer {pid} waiting for you"
        if not accept:
            p.status, p.closed, p.by = "declined", turn, who
            return p
        p.accepted.append(who)
        if not p.waiting_for():
            p.status, p.since = "active", turn
            p.ends = turn + p.turns if p.turns else None
        return p

    def leave_group_pact(self, pid, who: int, turn: int) -> GroupPact | str:
        """Withdraw your offer, or walk out of a pact in force (before its end, that's breaking it)."""
        p = self.group_pact(pid)
        if p is None or who not in p.parties:
            return f"no group pact {pid} of yours"
        if p.status == "offered":
            if who != p.a:
                return self.answer_group(pid, who, False, turn)
            p.status, p.closed, p.by = "expired", turn, who   # withdrawn
            return p
        if p.status != "active":
            return f"pact {p.key} isn't in force"
        early = p.ends is not None and turn < p.ends
        p.left.append(who)
        if early:
            p.broke.append(who)
        p.events.append([turn, who, "broke" if early else "left"])
        if len(p.parties) < 2:
            p.status, p.closed = "ended", turn
        return p

    def remove_member(self, group: str, who: int, turn: int) -> list[GroupPact]:
        """Someone is out of the group (voted out) or out of the game: out of its pacts too, without breaking them."""
        out = []
        for p in self.group_pacts.values():
            if (group and p.group != group) or who not in p.parties or p.status not in ("offered", "active"):
                continue
            if p.status == "offered":
                if who == p.a:
                    p.status, p.closed = "expired", turn
                else:
                    p.members.remove(who)
                    if who in p.accepted:
                        p.accepted.remove(who)
                    if len(p.members) < 2:
                        p.status, p.closed = "expired", turn
                    elif not p.waiting_for():
                        p.status, p.since = "active", turn
                        p.ends = turn + p.turns if p.turns else None
            else:
                p.left.append(who)
                p.events.append([turn, who, "removed"])
                if len(p.parties) < 2:
                    p.status, p.closed = "ended", turn
            out.append(p)
        return out

    def group_offers_to(self, who: int) -> list[GroupPact]:
        return [p for p in self.group_pacts.values() if p.status == "offered" and who in p.waiting_for()]

    def group_active(self, who: int) -> list[GroupPact]:
        return [p for p in self.group_pacts.values() if p.status == "active" and who in p.parties]

    def of_group(self, group: str) -> list[GroupPact]:
        return [p for p in self.group_pacts.values() if p.group == group]

    # ---- views -----------------------------------------------------------------
    def offers_to(self, who: int) -> list[Pact]:
        return [p for p in self.pacts.values() if p.status == "offered" and p.b == who]

    def offers_from(self, who: int) -> list[Pact]:
        return [p for p in self.pacts.values() if p.status == "offered" and p.a == who]

    def active(self, who: int) -> list[Pact]:
        return [p for p in self.pacts.values() if p.status == "active" and who in (p.a, p.b)]

    def recently_closed(self, who: int, turn: int) -> list[Pact]:
        return [p for p in self.pacts.values() if p.status in ("ended", "broken", "declined", "expired", "failed")
                and who in (p.a, p.b) and p.closed is not None and turn - p.closed <= RECENT]

    # ---- saving ----------------------------------------------------------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"next_id": self.next_id, "pacts": [asdict(p) for p in self.pacts.values()],
                                    "group_pacts": [asdict(p) for p in self.group_pacts.values()]}))

    @classmethod
    def load(cls, path: Path) -> "PactBook":
        book = cls()
        if not path.exists():
            return book
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return book
        for raw in data.get("pacts", []):
            try:
                p = Pact(**raw)
            except TypeError:
                continue
            book.pacts[p.id] = p
        for raw in data.get("group_pacts", []):
            try:
                g = GroupPact(**raw)
            except TypeError:
                continue
            book.group_pacts[g.id] = g
        book.next_id = max([int(data.get("next_id", 1))] + [p.id + 1 for p in book.pacts.values()]
                           + [p.id + 1 for p in book.group_pacts.values()])
        return book
