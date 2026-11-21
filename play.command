#!/bin/sh
# Civ Council: double-click to play with your real Civ 6 game.
# Start Civ 6 and load a single-player game first. Leaders use Claude Code on your subscription with the
# defaults (Haiku for turns, Sonnet when they talk with you, Opus for grand strategy, Sonnet for your advisor);
# change any of it in the browser's ⚙️ Settings. --lan lets your phone open it on the same wifi.
# Close this window (or press Ctrl+C) to stop. Anything you add after the command is passed along.
cd "$(dirname "$0")" || exit 1
exec python3 -m civ_council.web --game civ6 --provider claude-code --model haiku --lan --open "$@"
