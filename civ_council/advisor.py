"""Your advisor: a Claude that works for you, not for a rival.

It knows only what you'd know (your empire, the leaders you've met, what they've said to you and in public,
the world news you'd hear), never the leaders' private talk or their trust in you. You chat with it in the
browser's Advisor channel, and at the start of each of your turns it gives its own picks (research, civic,
what each city should build, diplomacy) alongside Civ's own recommendations.
"""
from __future__ import annotations

import json
import re
import time

from .game import proposal_label

ADVISOR = "advisor"     # the browser channel
ADVISOR_NAME = "Advisor"

SYSTEM = """You are the human player's own advisor in a game of Civilization VI. You work for them alone.
The AI leaders they deal with are rivals played by other AIs, with their own agendas: they may bluff, flatter or
lie. Be the honest, sharp counselor a ruler wants: say what you'd do and why, warn about danger, call out an
offer that's bad for them, and say plainly when you don't know.

You only know what the player knows: their empire, the leaders they've met, what those leaders said to them and
in public, and the world news they'd hear. You never see the leaders' private talk. Never invent facts about
the map, units or other civs; when you're inferring, say so.

How this game works (Civ Council): the human plays Civ 6 normally and chats with the AI leaders in a browser.
Leaders can make deals, pacts (agreements in words that Civ doesn't enforce), group pacts, and war or peace.
Envoys can't be withdrawn by anyone, and AI civs can't move their governors. Talk like a person: short, plain,
no headers or bullet walls unless asked for a list. Use the player's own words for things."""

PICKS_TASK = """It's the start of the player's turn. Give your own recommendations, next to Civ's built-in ones.
Reply with ONE JSON object and nothing else:
{
  "headline": "one sentence: the most important thing this turn",
  "research": {"pick": "<a tech from options.can_research, by its name>", "why": "short"},
  "civic": {"pick": "<a civic from options.can_study, by its name>", "why": "short"},
  "cities": [{"city": "<one of your cities>", "build": "<what it should produce next>", "why": "short"}],
  "diplomacy": ["<at most 3 short, concrete moves or warnings about the leaders>"],
  "watch_out": ["<at most 2 dangers, only if real>"]
}
Pick research and civics only from the options given (leave a field out if there are none). Name 2-4 cities that
most need a decision, not every city. Be concrete and brief."""

ASK_TASK = """The player just asked you something (the last line of your conversation). Answer it as their
advisor, in 1-5 short sentences unless they asked for detail. Reply with plain text only."""

MAX_TALK = 16   # past advisor exchanges it remembers


def _name(game, cid) -> str:
    try:
        return game.civ(cid).leader
    except (KeyError, ValueError):
        return str(cid)


class Advisor:
    def __init__(self, client, usage_log: list | None = None):
        self.client = client
        self.usage_log = usage_log
        self.last_picks: dict | None = None

    # ---- what it knows ---------------------------------------------------------------
    def context(self, council) -> dict:
        """Everything the player would know, and nothing they wouldn't."""
        game, me = council.game, council.human_id
        snap = game.snapshot(me)
        ctx = {"turn": snap.get("turn"), "you": snap.get("you"), "leaders_you_have_met": snap.get("others", []),
               "world": {"wars": snap.get("world_wars"), "alliances": snap.get("world_alliances"),
                         "recent_events": snap.get("recent_events")}}
        for k in ("headlines", "how_to_win", "city_states", "world_congress"):
            if snap.get(k):
                ctx[k] = snap[k]
        options = getattr(game, "steering_options", None)
        if options:
            try:
                o = options(me)
                ctx["options"] = {k: o.get(k) for k in ("researching", "can_research", "studying", "can_study",
                                                        "government", "envoys_to_send", "city_states_you_know",
                                                        "settlers", "city_sites", "trade_options")
                                  if o.get(k) not in (None, [], {})}
            except Exception:   # the game being busy shouldn't cost the advice
                pass
        ctx["offers_waiting_for_you"] = [
            {"id": p.id, "from": _name(game, p.sender), "offer": proposal_label(p)}
            for p in game.pending_proposals(me)]
        pacts = []
        for p in council.pacts.pacts.values():
            if me in (p.a, p.b) and p.status in ("active", "offered"):
                pacts.append(f"{p.key} with {_name(game, p.other(me))} ({p.status}): {p.title}. {p.terms}")
        for p in council.pacts.group_pacts.values():
            if me in p.parties and p.status in ("active", "offered"):
                pacts.append(f"{p.key} in group {p.group} ({p.status}): {p.title}. {p.terms}")
        if pacts:
            ctx["your_pacts"] = pacts
        ctx["your_conversations"] = self._talk(council)
        return ctx

    @staticmethod
    def _talk(council) -> dict:
        """What the leaders said to you, in your groups and in public (never Spy, never secret entries)."""
        me, out = council.human_id, {}
        def line(e):
            who = "you" if e.get("sender") == me else e.get("sender_name") or "news"
            return f"T{e.get('turn')} {who}: {str(e.get('text', ''))[:300]}"
        chats: dict = {}
        for e in council.entries:
            if e.get("secret") or e.get("kind") not in ("message", "proposal", "treaty", "pact", "error", "membership"):
                continue
            ch = e.get("channel")
            if isinstance(ch, int) and ch != me:
                key = f"private with {_name(council.game, ch)}"
            elif isinstance(ch, str) and ch in council.groups and me in council.groups[ch]["members"]:
                key = f"group chat '{council.groups[ch]['name']}'"
            elif ch == "world" and e.get("kind") == "message":
                key = "said in public"
            else:
                continue
            chats.setdefault(key, []).append(line(e))
        for key, lines in chats.items():
            out[key] = lines[-12:]
        return out

    def _history(self, council) -> list[str]:
        out = []
        for e in council.entries:
            if e.get("channel") == ADVISOR and e.get("kind") == "message":
                who = "player" if e.get("sender") == council.human_id else "you (advisor)"
                out.append(f"{who}: {str(e.get('text', ''))[:600]}")
        return out[-MAX_TALK:]

    # ---- asking ---------------------------------------------------------------------
    def _call(self, prompt: str, purpose: str, turn: int) -> str:
        started = time.time()
        try:
            return self.client.complete(SYSTEM, [{"role": "user", "content": prompt}])
        finally:
            if self.usage_log is not None:
                u = dict(getattr(self.client, "last_usage", None) or {})
                u.setdefault("seconds", round(time.time() - started, 1))
                self.usage_log.append({"turn": turn, "leader": ADVISOR_NAME, "model": self.client.name.split(":")[-1],
                                       "purpose": purpose, **u})
                if hasattr(self.client, "last_usage"):
                    self.client.last_usage = None

    def _prompt(self, council, task: str, extra: dict | None = None) -> str:
        ctx = self.context(council)
        if extra:
            ctx.update(extra)
        return f"<<WHAT YOU KNOW>>\n{json.dumps(ctx, separators=(',', ':'), ensure_ascii=False)}\n<<END>>\n\n{task}"

    def answer(self, council) -> str:
        """Reply to the player's latest message in the Advisor channel."""
        if self.client.name == "fake":
            return "I'd keep building up and watch your neighbors. (Offline mode: start with a real model for advice.)"
        extra = {"our_conversation": self._history(council)}
        if self.last_picks:
            extra["your_last_picks"] = self.last_picks
        text = self._call(self._prompt(council, ASK_TASK, extra), "advisor", council.game.turn)
        return re.sub(r"^```\w*\s*|\s*```$", "", (text or "").strip())[:2000] or "(no answer)"

    def picks(self, council) -> dict | None:
        """This turn's recommendations, or None if the model's answer wasn't usable."""
        if self.client.name == "fake":
            return None
        raw = self._call(self._prompt(council, PICKS_TASK, {"our_conversation": self._history(council)[-6:]}),
                         "advisor picks", council.game.turn)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
        try:
            data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        clean = {"turn": council.game.turn, "headline": str(data.get("headline", ""))[:240]}
        for k in ("research", "civic"):
            v = data.get(k)
            if isinstance(v, dict) and v.get("pick"):
                clean[k] = {"pick": str(v["pick"])[:60], "why": str(v.get("why", ""))[:200]}
        clean["cities"] = [{"city": str(c.get("city", ""))[:40], "build": str(c.get("build", ""))[:60],
                            "why": str(c.get("why", ""))[:200]}
                           for c in (data.get("cities") or []) if isinstance(c, dict) and c.get("build")][:5]
        for k in ("diplomacy", "watch_out"):
            clean[k] = [str(x)[:240] for x in (data.get(k) or []) if isinstance(x, str) and x.strip()][:3]
        self.last_picks = clean
        return clean


def picks_text(p: dict) -> str:
    """One line for Civ's notification stack and the chat log."""
    bits = []
    if p.get("research"):
        bits.append(f"research {p['research']['pick']}")
    if p.get("civic"):
        bits.append(f"civic {p['civic']['pick']}")
    bits += [f"{c['city']}: {c['build']}" for c in p.get("cities", [])[:3]]
    return " · ".join(bits)
