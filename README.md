# Civ Council

**AI rival leaders for Civilization VI that remember you, talk to you, and scheme with each other.**

Each AI civ in your game is run by Claude. Every leader has their own personality, hidden agenda and grand
strategy, and remembers every promise, insult and betrayal. You talk to them in a chat in your browser (or on
your phone) while you play Civ 6 as normal. They answer, make offers, form alliances, start group chats, gang
up on you, and lie to each other.

- **Talk to any leader you've met**: private chats, group chats, and a public World channel everyone hears.
- **They act on what they say**: friendship, alliances, denouncements, war and peace, real trade deals through
  Civ's own deal system, and pacts in words (vassals, border lines, joint wars, voting blocs) that Civ can't
  enforce but they remember.
- **They run their empires**: research, civics, government, expansion, city focus, great people and more. Civ's
  own AI still moves the units.
- **Group chats with teeth**: group pacts everyone must sign, joint war declarations, votes to kick a member,
  and a shared goal.
- **Your own advisor**: a Claude that works for you. Ask it anything in the Advisor channel, and at the start
  of each turn it gives its own picks (research, civic, what each city should build, diplomacy) next to Civ's
  own recommendations. It knows only what you know.
- **History page**: a timelapse of the whole game, with who trusts whom on every turn and what happened when.
- **Spy and Minds** (optional cheats): read the leaders' private messages to each other and their private
  reasoning.
- Their messages also pop up in Civ's own notification stack, and can be read aloud.

Single player only. Tested on macOS with Civilization VI: Gathering Storm.

---

## What you need

- **A Mac** with Civilization VI (Steam). Gathering Storm is recommended; the base game is untested. Windows
  might work but hasn't been tried.
- **A Claude Pro or Max subscription.** The leaders use Claude Code on your own subscription, so there's no API
  key and no extra bill, but they do use your plan's usage limits (see [Usage](#usage-and-cost)).
- **Python 3.9 or newer.** The `python3` that comes with macOS is fine. Nothing else to install: Civ Council uses
  only Python's standard library.

## Install (once)

**1. Install Claude Code and log in.** In Terminal:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Then run `claude`, log in with your Claude account when it asks, and quit it (type `/exit`).

**2. Download Civ Council.**

```bash
git clone https://github.com/DuckyDuckerson/civ-council.git
```

(Or download the ZIP from GitHub, unzip it, and use that folder wherever these steps say `civ-council`.) If
macOS asks to install the command line developer tools when you run `git` or `python3`, say yes.

**3. Turn on Civ 6's tuner.** This is how Civ Council talks to the game.

1. Quit Civ 6 completely. (Civ rewrites this file when it starts, so edit it only while Civ is closed.)
2. Open the file in TextEdit:

   ```bash
   open -e ~/Library/Application\ Support/Sid\ Meier\'s\ Civilization\ VI/Firaxis\ Games/Sid\ Meier\'s\ Civilization\ VI/AppOptions.txt
   ```

   (On Windows it's `Documents\My Games\Sid Meier's Civilization VI\AppOptions.txt`.)
3. Under `[Debug]`, find the line `EnableTuner 0` and change it to `EnableTuner 1`. Save.

The tuner lets programs on your computer run commands in your game, which is exactly what Civ Council needs.
Civ may turn off achievements while it's on. Set it back to `0` whenever you want to play without Civ Council.

**4. Optional: the Gift It To Me mod** (Steam Workshop). With it, leaders can hand military units to each other
and to you: arming a proxy, paying a debt in steel.

## Play

1. Start Civ 6 and load (or start) a **single-player** game. Wait until you're on the map.
2. **Double-click `play.command`** in the civ-council folder. A Terminal window opens, Civ Council connects to
   your game, and the chat opens in your browser.
3. Play Civ as normal. When you end your turn in Civ, the leaders take theirs: they read the news, answer you
   and each other, and make their moves. Talk to them any time.

It starts with the defaults: Haiku for the leaders' turns, Sonnet when they talk with you and for your advisor,
Opus for grand strategy. Change any of them, or turn the advisor off, in **⚙️ Settings** in the browser; your
choices are saved with each game.

You can only talk to leaders you've met in the game. To stop, close the Terminal window (or press `Ctrl+C` in
it). Everything is saved automatically: next time you load the same game, the leaders remember you.

If macOS says it can't open `play.command` (this happens with a downloaded ZIP), right-click it, choose
**Open**, and confirm once. Or run it from Terminal instead:

```bash
cd civ-council
./play.command
```

### On your phone

`play.command` also lets your phone in. Terminal shows an address like `http://192.168.1.20:8765`: open
that on your phone while it's on the same wifi. Anyone on your wifi could open it too, so play this way at home.
To keep it on this Mac only, run the full command without `--lan`:

```bash
python3 -m civ_council.web --game civ6 --provider claude-code --model haiku --open
```

### Try it without Civ 6

```bash
python3 -m civ_council.web                                        # simple rule-based leaders, no Claude
python3 -m civ_council.web --provider claude-code --model haiku   # Claude leaders in a pretend world
```

## Options

| Option | What it does |
| --- | --- |
| `--model haiku` | The model for routine turns. Haiku is cheapest. |
| `--talk-model sonnet` | The model a leader switches to when you're in the conversation, and for its memory summaries (default `sonnet`; `same` turns it off). |
| `--strategy-model opus` | The model for each leader's long-term grand strategy (default `opus`; `none` turns grand strategies off). |
| `--deep-strategy-every 50` | Opus writes a strategy this often; Sonnet writes the rewrites in between. |
| `--leader-models gandhi=sonnet` | A different model for one leader. |
| `--advisor-model sonnet` | The model for your own advisor (default: the talk model; `none` turns the advisor off). |
| `--spy` | Start with the Spy channel on (you can switch it on in the browser too). |
| `--voice roleplay` | Leaders talk like historical figures instead of the default casual chat. |
| `--summarize-after 70` | A leader condenses its memory after this many new messages and notes. |
| `--port 8765` | The browser port. |

## Usage and cost

Every leader decision is one Claude Code call that counts against your Claude plan's usage limits (the same
5-hour and weekly limits as your normal Claude use). Roughly: one Haiku call per leader per turn, Sonnet when
you talk with them, one Sonnet call a turn for your advisor's picks, and an occasional Opus call for grand
strategy. Expect somewhere around 8-10 calls a turn
with six AI civs, more when you chat a lot. On a subscription you pay nothing extra; it just uses your limits.
The ⏱ meter in the browser shows what your game has used, with the equivalent API price.

To use less: keep Haiku for turns, play with fewer civs, or set `--strategy-model sonnet`.

## Safety and privacy

- The leaders can only talk. Each one runs from an empty folder with no tools, no file or shell access, and none
  of your Claude Code settings, hooks or plugins.
- Your chats and the leaders' memories stay on your computer, in `memories/` (one folder per game). Nothing is
  sent anywhere except the leaders' prompts to Claude.
- The tuner lets local programs control your game. Turn it off (`EnableTuner 0`) when you're not using Civ
  Council.

## Troubleshooting

- **"Civ 6 didn't answer" / can't connect**: Civ must be running with a game loaded, and `EnableTuner 1` set
  while Civ was closed. Only one program can use the tuner at a time, so close FireTuner or a second Civ Council.
  If you just restarted Civ Council, wait a few seconds and try again.
- **"claude: not found" or "not logged in"**: run `claude` in Terminal and log in. If it's installed somewhere
  unusual, pass `--claude-bin /path/to/claude`.
- **Leaders stop answering for a while**: you may have hit your Claude plan's usage limit. It resets on its own.
- **Multiplayer**: not supported. Scripted changes would desync a multiplayer game.

## How it works

```
 browser ──► web.py ──► Council ◄──► leader agents (persona + memory + Claude)
                          │
                   bridge/civ6_game.py ──► Civ 6's tuner port (Lua in the running game)
```

Civ's built-in AI keeps moving units and fighting battles. Everything else a script is allowed to decide
(diplomacy, deals, research, civics, government, expansion...) is the Claude leader's call, and when Civ's AI
decides something anyway, the leader is told and gets the final say where Civ allows it. What a script can and
can't control in Civ 6 was worked out by experiment: see [`bridge/PROBE_RESULTS.md`](bridge/PROBE_RESULTS.md).

| File | What it does |
| --- | --- |
| `civ_council/agent.py` | Each leader's briefing, prompts, decisions, grand strategy and memory summaries |
| `civ_council/council.py` | Turns, messages, group chats, pacts, deals, history, saving |
| `civ_council/web.py`, `static/` | The browser chat and the History page |
| `civ_council/memory.py`, `pacts.py` | Trust, notes, promises, conversations; pacts and group pacts |
| `civ_council/llm.py` | Claude Code (`claude -p`), and the Anthropic, OpenAI and Ollama APIs |
| `bridge/civ6_game.py`, `tuner.py` | The real game, through Civ 6's tuner port |

## Development

```bash
python3 -m pytest -q
```

Notes for working on the code (with or without Claude Code) are in [`CLAUDE.md`](CLAUDE.md).
