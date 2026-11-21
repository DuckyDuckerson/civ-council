# Civ Council: notes for Claude Code

LLM rival leaders for Civilization VI with memory, private/group/public chat, and a browser UI.
The player runs Civ 6 on a Mac and chats with the leaders in a browser (maybe on a phone).
Read README.md first. What works in the real game, and why, is in `bridge/PROBE_RESULTS.md` (probes 1-17).

## Design decisions (keep these)
- Hybrid control: Civ's built-in AI moves units; the LLM leader decides everything else that Civ lets a
  script decide. Design rule: every decision that isn't moving a unit (government, friends, war, peace,
  deals, research, civics...) should be Claude's. When Civ's own AI makes one anyway, the leader is told
  (`decided_without_you`) and gets the final say where Civ allows it.
- Single-player only, not multiplayer (scripted changes risk desyncs).
- Leaders use `claude -p` on the player's own Claude subscription: never `--bare` (it skips the subscription login),
  `--safe-mode`, `--tools ""`, run from an empty temp dir, ANTHROPIC_API_KEY stripped. Cheap models by default
  (haiku for routine turns, sonnet when the human is in the conversation, for memory summaries and most
  strategy rewrites, opus for a leader's first grand strategy, one every 50 turns after, and personas).
- Standard library only. No pip installs needed to run.
- House rules: a peace both sides accept is signed at once, even inside Civ's minimum war length; a
  fresh peace can be broken at once (only that lock is lifted; Civ's other reasons to refuse a war stand).
- Mods: interface mods don't matter; any mod that touches gameplay or diplomacy needs checking for integration
  (Gift It To Me is integrated; a mod for making deals in Civ's own screen bypasses the leaders, because Civ's AI,
  not the leader, answers those).

## Layout
- `civ_council/`: `agent.py` (prompts, context, decisions, grand strategy, personas), `council.py` (turns,
  routing, pacts, news, notifications, usage), `web.py` + `static/index.html` (browser UI), `game.py`
  (`Game` interface + `MockGame`), `pacts.py`, `memory.py`, `llm.py` (clients; `claude -p`), `personas.py`.
- `bridge/civ6_game.py`: `Civ6Game`, the real game through the tuner port (Lua strings + parsing).
- Per-game files in `memories/civ6-<game id>/`: one `<leader>.json` per leader (relationships with trust
  history, conversations, journal, story, grand strategy), `game_state.json` (orders, proposals, pins,
  denouncements, queued renewals), `chat.json` (the chat, restored on start; rebuilt from leader memories if
  missing), `pacts.json`, `personas.json`, `prefs.json` (Civ notifications, quiet/block switches),
  `usage.json`. `logs/flagged/` keeps requests Claude's safeguards refused (gitignored).
- Tests: `tests/test_civ6_game.py` (FakeLink: every Lua string the bridge sends needs a route there),
  `test_pacts.py`, `test_strategy.py`, `test_new_features.py`.

## What leaders see (briefing, `Civ6Game.snapshot` -> `LeaderAgent.build_context`)
- Every snapshot field must reach the leader: `build_context` passes them all through (a test checks; this
  bug once hid `how_to_win` from every leader).
- Up front: `how_to_win` (enabled victories; last-civ-standing here), `headlines` (cities under attack,
  nukes, treaties/deals/pacts ending, cities about to flip, offers waiting), `your_grand_strategy`.
- Their empire: the leader ribbon's numbers (score, Civ's own military strength, science, culture, gold,
  faith), economy per turn, era and age, army makeup and where it stands, cities (size, defense, damage,
  amenities, war weariness, production), loyalty, resources, policy cards, governors, spies, great people,
  trade routes, ongoing deals, nukes, fights (combat hook), caught spies, emergencies (an Aid Request is worded
  as a race to help its target, never "against" it), barbarians, continents.
- Others they've met: the same ribbon numbers, government, era, army, cities (places in words, never
  coordinates), troops in or near their land (ships too), treaty timers, grievances (signed per pair),
  Civ's relationship reasons, conquests, city-state types/envoys.
- On routine turns `SLOW_FIELDS` are left out (full books on empire reviews and when the human talks); the
  context is compact JSON. Grand strategies get the full detail.

## What leaders do
- Diplomacy: war, peace, friendship, alliance, denounce (proposals the other side accepts). An early
  friendship renewal is queued and sent when it lapses (the briefing shows it queued; a repeat is refused quietly). Deals (`propose_deal` give/ask: gold, gold per turn,
  resources, favor, open borders, cities never capitals) through Civ's DealManager; at war a deal is peace
  terms (peace and terms together, or the war resumes). Pacts in words (vassal, borders, joint/proxy wars)
  with optional payment. Rumors (`rumor_about`). Map pins for the human (`pin`). `gift_units` with the mod.
- Group chats (`group_chats`: start / invite / leave): a leader starts one with 2+ leaders it has met, the human
  only if it adds them (max 3 open per founder). Entries said while the human isn't a member carry `secret` and
  show only with Spy (sidebar "Secret group chats"), never in Civ's notifications. Joining later doesn't reveal
  earlier messages. `ever` keeps a group in your list after you leave; under 2 members it closes.
- Group pacts (`propose_pact` with `group`, `pacts.GroupPact`): bind every member at the time of the offer and
  start only when ALL accept (one no kills it). `war_on` makes it a joint war: on acceptance every AI member
  declares war the same turn (the human player is told to declare their own in Civ). Walking out early (`end_pact`) is
  breaking it, shown to the group. Votes to remove a member (`kick`/`keep`): a majority of the others, lapse
  after 3 turns; the removed member is out of the group's pacts without breaking them. A goal (`goal`): set by
  the founder, by anyone once the founder has left; in every member's briefing. Browser: Group info panel
  (goal, members and votes, how each pair gets along, group pacts), Group pact form, Invite.
  The 3-messages-per-turn cap per chat (`max_pair_msgs`, against AI-to-AI chatter) resets for a group each time
  the human player posts there. Group news (joins, pacts agreed, votes, goal) is logged into members' memory of the chat.
- Orders: research, civic, expand, district, settle (founds a city when a settler is within 8 tiles),
  government (10-turn cooldown), ban/allow units, envoy, delegation, embassy, attack (Civ's "Attack Enemy
  City" operation), city_focus, recruit (great person, when it has the points), overrule.
- Envoys can't be withdrawn by anyone (probe 16), and AI governors can't be moved by script (only governor
  points are exposed); the system prompt says both, so leaders don't promise either. Not controllable (no call for AI civs): unit moves, policy cards, exact build queues, beliefs, governors,
  spy missions, World Congress votes, buying great people.
- Grand strategy: written by the strategy model at the start, on big news, on request (at most once every 8
  turns), and on a timer that follows the leader's era (`STRATEGY_GAPS`: 50 turns Ancient/Classical, 35
  Medieval/Renaissance, 25 Industrial/Modern, 20 Atomic on; `strategy_every` when the era is unknown).
  Opus writes the first one and then one every `deep_every` (50, `--deep-strategy-every`) turns; Sonnet writes
  the rewrites in between, and each falls back to the other. Routine turns tick its checklist. Leaders without a hand-written persona get one written once per game.
- Memory summaries: per leader, once it has `summarize_after` (70) new messages and notes, at most every 10
  turns, and after 80 turns if a few things are new. Quiet early turns rarely need one.

## Browser (for the player)
- Chats: private, groups (yours, ones a leader added you to, a Leave button), World, World News (a digest per
  turn), Spy and Minds (cheats, behind Spy; Minds hides quiet turns; Spy has the trust timeline, each civ in its avatar's color in every chart). Offers
  tray, pact form and cards, deal screen (peace terms at war; Counter on a leader's deal opens it filled in and
  declines theirs once yours is sent), Diplomacy panel per leader (what Civ's leader
  screen shows), replies, turn dividers, new-message button, read state kept per game in the browser,
  notifications/sound/read-aloud, usage meter.
- Advisor (`advisor.py`, channel `advisor`, `--advisor-model`, default the talk model): the player's own Claude.
  Its context is only what the player would know (their snapshot, steering options, offers, their pacts, their
  private/group chats and public statements; never Spy, secret entries or leaders' trust). Chat via
  `/api/advisor`; picks (research, civic, city builds, diplomacy, dangers) each turn after the leaders move, as a
  `picks` card and one line in Civ's notifications; Civ's own recommendations are left alone.
- ⚙️ Settings (browser): the advisor's on/off switch and a model per job (turn, talk, strategy incl. off,
  advisor), switched live via `Council.set_models` + `llm_factory` (Claude Code only), saved in prefs.json.
- History page (`/history`, `static/history.html`, `/api/history`, the ⏳ button): a god's-eye story of the game
  (everything, not filtered by what you know). `Council.history` (`history.json`, never trimmed like the chat) gets a
  line for wars, treaties, deals (AI-to-AI too, even with Spy off), pacts agreed/broken, group events, public
  statements (with `by`) and Civ's world events; the first load rebuilds it from the chat that's still kept.
  Civ's own Historic Moments (every civ, back to turn 1; probe 17) are merged in at startup and each turn
  (`Council.sync_moments`, kind `moment`). A
  turn scrubber with a timelapse, a trust web (arrows colored by trust at that turn, tap to focus), trust over
  time per leader (both directions), and the events up to that turn with filters. Trust comes from each leader's
  own trust history.
- A real game saves by itself: after every leader turn, whenever the worker catches up after anything you
  do, and on Ctrl+C. The Save button shows only in the offline demo.
- Read-aloud uses the browser's own voices: novelty and old robotic ones are skipped, the clearest (Enhanced/
  Premium/Siri/Google) go first, one per leader, with a speed and per-leader voice picker in the bell menu.
  Texting slang ("rn", "barbs", "T74") is said in full.
- You only see what you'd know: World entries about a civ you haven't met (Civ events via `event_civs`,
  public statements, treaties, wars) are marked `secret` (Spy only, no Civ notification, not in World News).
- In the game: leader messages and offers also appear in Civ's notification stack; Civ's own AI pop-ups
  and Civ's leader screen are closed by InGame hooks (first meetings and war declarations still show).
  The hooks live in Civ's memory until a save is loaded or Civ restarts; switches in the bell menu.

## Running
- Tests: `python3 -m pytest -q`
- Offline fake leaders: `python3 -m civ_council.web`
- Real game: double-click `play.command` (= `python3 -m civ_council.web --game civ6 --provider claude-code
  --model haiku --lan --open`; `--open` opens the browser), or the command itself:
  `python3 -m civ_council.web --game civ6 --provider claude-code --model haiku --lan`
  (`--talk-model`, `--strategy-model opus|same|none`, `--spy`). Run it in a terminal and stop it
  (Ctrl+C) when a probe needs the tuner.
- If a model call fails or is refused, the leader retries once with its other model. A reply to you that gets
  stuck (45 s) is tried once more with the talk model first (the routine model misread the player too often).
- The player's messages carry `names_in_message` (`council.names_in`): who "egypt", "monty", "egpyt" means, and
  whether the reader has met them. The system prompt explains pasted "me: / india:" quotes.
- `claude -p` needs the standalone Claude Code CLI, logged in (`~/.local/bin/claude`). Over SSH the macOS
  Keychain may be locked: `security unlock-keychain` or a `claude setup-token` token.

## Tuner and Lua (bridge/tuner.py), confirmed on macOS
- Frames are `<uint32 LE length incl NUL><int32 LE tag><payload>\0`. Tag 4 handshake: `APP:`, `LSQ:` (Lua
  state list). Tag 3 commands: `CMD:<state index>:<lua>`; prints come back as `O\0<state>: <text>`, errors as
  `ERR:...`. Each chunk is wrapped in a function and followed by `print("---END---")`. Lua 5.1.
- Only the FIRST connected client gets the state list: stop Civ Council before a probe. Civ may hold a stale
  slot a few seconds after a client quits (retry). `Tuner()` tries ports 4318-4321; never `nc -z` the port.
- States: `GameCore_Tuner` for reads and direct changes; `InGame` (UI side) for UI-only reads and requests
  that only work for the local player. Many calls exist on only one side: check before using (e.g. InGame
  only: GetCurrentGovernment, ages/eras, grievances, favor per turn, amenities, production, trade routes,
  governors, GetAtWarChangeTurn/GetMinPeaceDuration, ribbon military strength; GameCore only: HasAllied,
  IsYieldFavored, GreatPeople Recruit; GameCore cities have no IsCapital).
- A wrong argument type in a GameCore call can segfault Civ 6: copy signatures from Civ's own scripts
  (`Civ6.app/Contents/Assets/...`, DLC scenario scripts, UI .lua files).
- Hooks (combat, disasters, diplomacy statements, leader screen) are refreshable: a global handler redefined
  on every read, added once per Lua state through a small wrapper.
- Line tags in Lua output must be unique per script (a `MOD|` clash once broke the whole awareness read).

## AppOptions.txt
`~/Library/Application Support/Sid Meier's Civilization VI/Firaxis Games/Sid Meier's Civilization VI/AppOptions.txt`
(CRLF line endings). Civ rewrites it a few seconds after launch from the values it read, so only
edit it while Civ 6 is fully quit.
