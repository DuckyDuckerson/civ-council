"""Play the council in a terminal against the mock world. (For the browser chat: python -m civ_council.web)

    python -m civ_council                      # offline, rule-based fake leaders
    python -m civ_council --provider claude-code --model haiku   # Claude via your subscription
    python -m civ_council --provider anthropic # real Claude leaders (needs ANTHROPIC_API_KEY)
    python -m civ_council --mix                # Claude + GPT + local Ollama leaders together
    python -m civ_council --demo 5 --spy       # watch 5 turns play out without typing
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .agent import LeaderAgent
from .council import Council
from .game import Civ, MockGame, proposal_label
from .llm import make_client
from .personas import DEFAULT_ROSTER, PERSONAS, persona_for

HELP = """Commands:
  @<leader> <message>     talk privately, e.g.  @gilgamesh want to be allies?
  /all <message>          speak to every leader at once
  /end                    end your turn (the AI leaders take theirs)
  /status                 the world as you see it
  /war /peace /friend /ally /denounce /break <leader>
  /accept <id>  /decline <id>   answer a proposal
  /mind <leader>          peek at what that leader thinks of everyone (cheat/debug)
  /save  /load            keep leader memories in ./memories
  /quit"""

ACTION_CMDS = {"/war": "declare_war", "/peace": "make_peace", "/friend": "propose_friendship",
               "/ally": "propose_alliance", "/denounce": "denounce", "/break": "break_alliance"}


def build(args) -> Council:
    if getattr(args, "game", "mock") == "civ6":
        return build_civ6(args)
    keys = args.leaders.split(",") if args.leaders else DEFAULT_ROSTER
    players = [Civ(0, args.civ, args.name, is_human=True, military=12)]
    mixes = ["anthropic", "openai", "ollama"]
    per_leader = dict(kv.split("=", 1) for kv in (args.leader_models or "").split(",") if "=" in kv)
    agents = {}
    for i, key in enumerate(keys, start=1):
        key = key.strip()
        p = PERSONAS[key]
        players.append(Civ(i, p.civ, p.leader, military=8 + (i * 3) % 7))
        provider = mixes[(i - 1) % 3] if args.mix else args.provider
        model = per_leader.get(key, None if args.mix else args.model)
        client = make_client(provider, model, args.base_url, getattr(args, "claude_bin", None))
        agents[i] = LeaderAgent(i, p, client, voice=getattr(args, "voice", "casual"),
                                talk_client=_talk_client(args, provider, model))
        agents[i].strategy_client = _strategy_client(args, provider, model, client)
        if getattr(args, "deep_strategy_every", None) is not None:
            agents[i].deep_every = args.deep_strategy_every
    game = MockGame(players, seed=args.seed)
    return Council(game, agents, human_id=0, spy=args.spy, summarize_after=args.summarize_after)


def _strategy_client(args, provider: str, model: str | None, client):
    """The strong model for grand strategies: the first one, then one every --deep-strategy-every turns (the talk
    model writes the rewrites in between).

    With Claude Code the default is Opus. 'same' uses the leader's own model; 'none' turns grand strategies off.
    """
    pick = getattr(args, "strategy_model", None)
    if pick is None:
        pick = "opus" if provider == "claude-code" else "none"
    if pick == "none":
        return None
    if pick == "same" or pick == (model or ""):
        return client
    return make_client(provider, pick, args.base_url, getattr(args, "claude_bin", None))


def _talk_client(args, provider: str, model: str | None):
    """The model a leader switches to when you're talking with it (and for memory summaries).

    With Claude Code the default is: Haiku for routine turns, Sonnet when you're in the conversation.
    """
    talk = getattr(args, "talk_model", None)
    if talk is None and provider == "claude-code" and (model or "haiku") == "haiku":
        talk = "sonnet"
    if not talk or talk == "same" or talk == (model or ""):
        return None
    client = make_client(provider, talk, args.base_url, getattr(args, "claude_bin", None))
    if hasattr(client, "timeout"):
        client.timeout = 45   # you're waiting on this one (it answers in ~4s, 9 at most): a stuck call is
                              # tried once more, then the regular model answers instead
    return client


def build_civ6(args) -> Council:
    """Leaders for every AI civ in the running Civ 6 game, with memories kept per game."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
    from civ6_game import Civ6Game, GameNotReady, Link

    game = Civ6Game(link=Link(port=args.tuner_port))
    try:
        civs = game.civs()
        agendas = game.leader_agendas()
    except GameNotReady as e:
        raise RuntimeError(f"{e}\nStart Civ 6 (EnableTuner 1 in AppOptions.txt), load a game, then try again.")
    per_leader = dict(kv.split("=", 1) for kv in (args.leader_models or "").split(",") if "=" in kv)
    agents = {}
    for c in civs:
        if c.is_human or not c.alive:
            continue
        names, text = agendas.get(c.id, ("", ""))
        persona = persona_for(game.leader_type(c.id), c.leader, c.name, names, text)
        model = per_leader.get(persona.key, args.model)
        client = make_client(args.provider, model, args.base_url, getattr(args, "claude_bin", None))
        agents[c.id] = LeaderAgent(c.id, persona, client, voice=getattr(args, "voice", "casual"),
                                   talk_client=_talk_client(args, args.provider, model))
        agents[c.id].strategy_client = _strategy_client(args, args.provider, model, client)
        if getattr(args, "deep_strategy_every", None) is not None:
            agents[c.id].deep_every = args.deep_strategy_every
    council = Council(game, agents, human_id=game.local_player, spy=args.spy,
                      summarize_after=args.summarize_after, parallel=min(5, max(1, len(agents))),
                      memory_dir=Path("memories") / f"civ6-{game.game_id}")
    council.load_memories(council.memory_dir)   # leaders remember you from earlier sessions of this game
    council.sync_moments()                      # Civ's own timeline fills the History page back to turn 1
    game.end_turn()                              # baseline: report only what changes from now on
    return council


def make_parser(description: str = "Talk, scheme and fight with AI leaders.") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--game", default="mock", choices=["mock", "civ6"],
                    help="civ6 = the Civ 6 game running on this Mac (EnableTuner 1), mock = a simulated world")
    ap.add_argument("--tuner-port", type=int, default=None, help="Civ 6 tuner port (default: try 4318-4321)")
    ap.add_argument("--provider", default="fake",
                    choices=["fake", "claude-code", "anthropic", "openai", "ollama"],
                    help="claude-code = your Claude subscription via `claude -p` (no API key)")
    ap.add_argument("--model", default=None, help="model for every leader, e.g. haiku or sonnet for claude-code")
    ap.add_argument("--talk-model", default=None,
                    help="model leaders switch to when you're talking with them, e.g. sonnet "
                         "(claude-code default: sonnet when --model is haiku; 'same' to turn off)")
    ap.add_argument("--strategy-model", default=None,
                    help="model that writes each leader's grand strategy (claude-code default: opus; "
                         "'same' = the leader's own model, 'none' = no grand strategies)")
    ap.add_argument("--deep-strategy-every", type=int, default=None,
                    help="turns between grand strategies by the strategy model (default 50; the talk model, Sonnet, "
                         "writes the rewrites in between; 0 = the strategy model writes every one)")
    ap.add_argument("--leader-models", default=None,
                    help="per-leader override, e.g. cleopatra=sonnet,montezuma=haiku")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--claude-bin", default=None,
                    help="path to the claude CLI, if it isn't on PATH or in ~/.local/bin")
    ap.add_argument("--mix", action="store_true", help="rotate leaders across Claude, GPT and Ollama")
    ap.add_argument("--leaders", default=None, help=f"comma list from: {', '.join(PERSONAS)}")
    ap.add_argument("--name", default="You")
    ap.add_argument("--civ", default="Your Empire")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--spy", action="store_true", help="see private AI-to-AI messages")
    ap.add_argument("--voice", default="casual", choices=["casual", "roleplay"],
                    help="casual = leaders chat like modern players (default); roleplay = speak like the historical leader")
    ap.add_argument("--summarize-after", type=int, default=70,
                    help="a leader condenses its memory after N new messages and notes (0 = never)")
    return ap


def status(c: Council) -> str:
    s = c.game.snapshot(c.human_id)
    me = s["you"]
    lines = [f"Turn {s['turn']} — you: {me['cities']} cities, military {me['military']}, score {me['score']}"]
    for o in s["others"]:
        flags = " (denounced you)" if o["they_denounced_you"] else ""
        model = c.agents[o["id"]].client.name if o["id"] in c.agents else ""
        lines.append(f"  [{o['id']}] {o['leader']} of {o['civ']}: {o['stance_with_you']}{flags}, "
                     f"{o['cities']} cities, military {o['military']}  ({model})")
    for p in c.game.pending_proposals(c.human_id):
        lines.append(f"  📜 pending #{p.id}: {c.game.civ(p.sender).leader} offers {proposal_label(p)}")
    if s["world_wars"]:
        lines.append("  Wars: " + "; ".join(s["world_wars"]))
    if s["world_alliances"]:
        lines.append("  Alliances: " + "; ".join(s["world_alliances"]))
    return "\n".join(lines)


def mind(c: Council, who: str) -> str:
    aid = c.resolve(who)
    if aid not in c.agents:
        return "No such AI leader."
    a = c.agents[aid]
    out = [f"{a.persona.leader}'s private view:"]
    for oid, r in a.memory.relationships.items():
        out.append(f"  {c.game.civ(oid).leader}: trust {r.trust} ({r.feeling})")
        out += [f"     - {n}" for n in r.notes[-3:]]
    if a.memory.journal:
        out.append(f"  Plan: {a.memory.journal[-1]}")
    if a.last_decision and a.last_decision.thoughts:
        out.append(f"  Last thought: {a.last_decision.thoughts}")
    return "\n".join(out)


def handle(c: Council, line: str) -> list[str] | str | None:
    line = line.strip()
    if not line:
        return []
    if line.startswith("@"):
        who, _, text = line[1:].partition(" ")
        return c.say(who, text) if text else "Say something after the name."
    cmd, _, rest = line.partition(" ")
    rest = rest.strip()
    if cmd == "/all":
        return c.say("all", rest)
    if cmd == "/end":
        return c.end_turn()
    if cmd == "/status":
        return status(c)
    if cmd in ACTION_CMDS:
        target = c.resolve(rest)
        if not isinstance(target, int):
            return f"Who? ({rest!r})"
        return c.act({"type": ACTION_CMDS[cmd], "target": target})
    if cmd in ("/accept", "/decline") and rest.isdigit():
        return c.respond(int(rest), cmd == "/accept")
    if cmd == "/mind":
        return mind(c, rest)
    if cmd == "/save":
        c.save_memories(Path("memories"))
        return "Leader memories saved to ./memories"
    if cmd == "/load":
        c.load_memories(Path("memories"))
        return "Leader memories loaded."
    if cmd == "/quit":
        return None
    return HELP


def show(result) -> None:
    if isinstance(result, str):
        print(result)
    else:
        for line in result:
            print(line)


def main(argv: list[str] | None = None) -> None:
    ap = make_parser()
    ap.add_argument("--demo", type=int, default=0, help="auto-play N turns and exit")
    args = ap.parse_args(argv)

    try:
        c = build(args)
    except RuntimeError as e:   # e.g. claude CLI missing or logged out: say how to fix it
        raise SystemExit(str(e))
    print(status(c))
    if args.demo:
        show(c.say("all", "Greetings, fellow leaders. I come in peace and hope we can trade."))
        for _ in range(args.demo):
            show(c.end_turn())
        print(status(c))
        return
    print(HELP)
    while True:
        try:
            line = input("\n> ")
        except (EOFError, KeyboardInterrupt):
            break
        result = handle(c, line)
        if result is None:
            break
        show(result)


if __name__ == "__main__":
    main()
