#!/bin/sh
# Civ Council: double-click to play with your real Civ 6 game.
# Start Civ 6 and load a single-player game first. Leaders use Claude Code on your subscription with the
# defaults (Haiku for turns, Sonnet when they talk with you, Opus for grand strategy, Sonnet for your advisor);
# change any of it in the browser's ⚙️ Settings. --lan lets your phone open it on the same wifi.
# Close this window (or press Ctrl+C) to stop. Anything you add after the command is passed along.
cd "$(dirname "$0")" || exit 1

# A Python new enough to run Civ Council (3.9+). The first "python3" a double-click finds can be an old one.
PY=""
for p in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3.13 python3.12 python3.11 python3.10 \
         python3.9 /usr/bin/python3; do
  if command -v "$p" >/dev/null 2>&1 && \
     "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PY="$p"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Civ Council needs Python 3.9 or newer, and this Mac doesn't seem to have it."
  echo "Run  xcode-select --install  in Terminal (it installs Apple's Python), then try again."
  printf "Press Return to close this window. "; read -r _
  exit 1
fi

"$PY" -m civ_council.web --game civ6 --provider claude-code --model haiku --lan --open "$@"
status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 130 ]; then   # 130 = you pressed Ctrl+C: nothing went wrong
  echo
  echo "Civ Council stopped. The message above says why."
  printf "Press Return to close this window. "; read -r _
fi
exit "$status"
