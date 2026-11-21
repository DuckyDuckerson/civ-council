"""A rule-based stand-in for an LLM, so the whole system runs offline and in tests.

It reads the same context a real model gets and returns the same JSON format. It's dumb
on purpose: just enough behavior to exercise talking, proposals, grudges and wars.
"""
from __future__ import annotations

import json
import zlib

from .agent import CONTEXT_END, CONTEXT_START


def _context(messages: list[dict]) -> dict:
    text = messages[-1]["content"]
    a = text.index(CONTEXT_START) + len(CONTEXT_START)
    return json.loads(text[a:text.index(CONTEXT_END)])


def fake_leader_brain(system: str, messages: list[dict]) -> str:
    ctx = _context(messages)
    me = ctx["you"]
    others = {o["id"]: o for o in ctx["others"]}
    if ctx["mode"] == "summarize":
        rels = []
        for o in others.values():
            v = o["your_view_of_them"]
            bits = [v["long_term_memory"]] if v["long_term_memory"] else []
            bits += [n.split(": ", 1)[-1] for n in v["recent_notes"][-2:]]
            rels.append({"about": o["id"], "summary": f"{v['feeling'].capitalize()}. " + "; ".join(bits)[:200]})
        return json.dumps({"relationships": rels,
                           "story": f"Turn {ctx['turn']}: {me['cities']} cities, still in the game."})
    roll = zlib.crc32(f"{me['id']}-{ctx['turn']}-{ctx['mode']}".encode()) % 100
    out = {"thoughts": "", "messages": [], "actions": [], "proposal_responses": [], "memory": [], "journal": ""}

    # Answer proposals: accept from anyone not disliked.
    for p in ctx["proposals_to_you"]:
        trust = others.get(p["from_id"], {}).get("your_view_of_them", {}).get("trust", 0)
        accept = trust >= -10
        out["proposal_responses"].append({"id": p["id"], "accept": accept})
        out["memory"].append({"about": p["from_id"], "trust_change": 5 if accept else -2,
                              "note": f"offered {p['kind'].replace('_', ' ')}"})

    # React to inbox.
    for m in ctx["inbox"]:
        sender = m.get("from_id")
        text = m.get("text", "").lower()
        hostile = any(w in text for w in ("war", "destroy", "crush", "threat", "weak"))
        friendly = any(w in text for w in ("friend", "ally", "peace", "trade", "together", "hello", "greetings"))
        delta = -10 if hostile else 6 if friendly else 1
        if sender is not None and sender in others:
            out["memory"].append({"about": sender, "trust_change": delta,
                                  "note": f"said: {m.get('text', '')[:60]}"})
            needs_answer = others[sender]["is_human"] or hostile or "?" in text
            if m.get("group") and not others[sender]["is_human"]:
                needs_answer = "?" in text
            if not needs_answer:
                continue
            reply = (f"{me['leader']} will not forget those words." if hostile else
                     f"{me['leader']} welcomes your words. Let our peoples prosper together." if friendly else
                     f"{me['leader']} has heard you.")
            to = m["group"] if m.get("group") else "all" if m.get("to") == "everyone" else sender
            out["messages"].append({"to": to, "text": reply})
            if friendly and others[sender]["stance_with_you"] == "peace" and roll < 50:
                out["actions"].append({"type": "propose_friendship", "target": sender})

    # On its own turn, occasionally do something unprompted.
    if ctx["mode"] == "turn" and others:
        ranked = sorted(others.values(), key=lambda o: o["your_view_of_them"]["trust"])
        worst, best = ranked[0], ranked[-1]
        if worst["your_view_of_them"]["trust"] <= -30 and worst["stance_with_you"] != "war" \
                and worst["military"] < me["military"]:
            out["actions"].append({"type": "declare_war", "target": worst["id"]})
            out["messages"].append({"to": "all", "text": f"{worst['leader']} has insulted us for the last time."})
        elif roll < 20 and best["stance_with_you"] == "peace":
            out["messages"].append({"to": best["id"], "text": "Our envoys bring greetings. Shall we be friends?"})
            out["actions"].append({"type": "propose_friendship", "target": best["id"]})
        elif roll < 45:
            ais = [o for o in others.values() if not o["is_human"]]
            if ais:
                pick = ais[roll % len(ais)]
                jab = roll % 2 == 0
                out["messages"].append({"to": pick["id"], "text": (
                    "Your borders creep toward mine. Pull back, or be made weak." if jab
                    else "A trade, perhaps? Our merchants are eager.")})
        out["journal"] = f"Watching {worst['leader']} closely; {best['leader']} seems the best partner."
    out["thoughts"] = "Fake brain: simple rules, no real reasoning."
    return json.dumps(out)
