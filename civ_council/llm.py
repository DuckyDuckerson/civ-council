"""LLM clients. Standard library only, so there's nothing to install.

- ClaudeCodeClient: Claude through `claude -p`, using your Claude subscription (no API key).
- AnthropicClient: Claude models via the Anthropic API (needs ANTHROPIC_API_KEY).
- OpenAICompatibleClient: OpenAI, or a free local model through Ollama / LM Studio.
- ScriptedClient: canned or programmatic replies, for tests and offline demos.

Mixing clients is the point: one leader can be Claude, another GPT, another a local model.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
import time
import os
import shutil
import subprocess
import tempfile
import urllib.request
from typing import Callable, Protocol


class LLMClient(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict], max_tokens: int = 1200) -> str: ...


def _post(url: str, headers: dict, body: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class AnthropicClient:
    def __init__(self, model: str = "claude-sonnet-5-5", api_key: str | None = None):
        self.model = model
        self.name = f"anthropic:{model}"
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("Set ANTHROPIC_API_KEY to use Claude.")

    def complete(self, system: str, messages: list[dict], max_tokens: int = 1200) -> str:
        data = _post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages},
        )
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class OpenAICompatibleClient:
    """Works with OpenAI and with local servers: Ollama is http://localhost:11434/v1."""

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1", api_key: str | None = None):
        self.model = model
        self.name = f"openai-compatible:{model}"
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "not-needed-for-local")

    def complete(self, system: str, messages: list[dict], max_tokens: int = 1200) -> str:
        data = _post(
            f"{self.base_url}/chat/completions",
            {"authorization": f"Bearer {self.api_key}"},
            {"model": self.model, "max_tokens": max_tokens,
             "messages": [{"role": "system", "content": system}, *messages]},
        )
        return data["choices"][0]["message"]["content"] or ""


# Where the Claude Code CLI usually lives when it isn't on PATH (e.g. ~/.local/bin missing from
# zsh's PATH after the native installer). The Claude desktop app's own bundled copy is not used:
# it's only logged in while the app drives it.
CLAUDE_PATHS = ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")

INSTALL_HELP = """Claude Code's command-line tool (`claude`) isn't installed on this Mac.
The Claude desktop app's built-in copy can't be used on its own. Install the CLI in Terminal:
    curl -fsSL https://claude.ai/install.sh | bash
then log in once with your Claude subscription:
    ~/.local/bin/claude auth login
(Or point to an existing install: --claude-bin /path/to/claude, or CIV_COUNCIL_CLAUDE=/path/to/claude.)"""

LOGIN_HELP = """Claude Code ({binary}) isn't logged in, so the leaders can't think.
Log in once with your Claude subscription, in Terminal:
    "{binary}" auth login
then start Civ Council again."""


def find_claude(explicit: str | None = None) -> str | None:
    """Path to the `claude` CLI: --claude-bin, then $CIV_COUNCIL_CLAUDE, then PATH, then usual installs."""
    for given in (explicit, os.environ.get("CIV_COUNCIL_CLAUDE")):
        if given:
            return shutil.which(os.path.expanduser(given))
    found = shutil.which("claude")
    if found:
        return found
    for p in CLAUDE_PATHS:
        p = os.path.expanduser(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _leader_env() -> dict:
    """Environment for `claude -p`: without ANTHROPIC_API_KEY, so it always bills your subscription."""
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}


FLAGGED_DIR = Path(__file__).resolve().parents[1] / "logs" / "flagged"


def _save_flagged(model: str, system: str, prompt: str, error: str) -> str:
    """A request Claude's safeguards refused: save it (local only, gitignored) to find what set it off."""
    try:
        FLAGGED_DIR.mkdir(parents=True, exist_ok=True)
        path = FLAGGED_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{model}.txt"
        path.write_text(f"MODEL: {model}\nERROR: {error}\n\n=== SYSTEM ===\n{system}\n\n=== PROMPT ===\n{prompt}\n")
        return str(path)
    except OSError:
        return "(couldn't save it)"


@functools.lru_cache(maxsize=None)
def check_claude_login(binary: str | None) -> None:
    """Fail fast, with instructions, if the CLI is missing or logged out. Checked once per binary."""
    if not binary:
        raise RuntimeError(INSTALL_HELP)
    try:
        proc = subprocess.run([binary, "auth", "status", "--json"], capture_output=True, text=True,
                              timeout=30, env=_leader_env(), cwd=tempfile.gettempdir())
        status = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return   # older CLI without `auth status`: let the first real call report any problem
    if isinstance(status, dict) and status.get("loggedIn") is False:
        raise RuntimeError(LOGIN_HELP.format(binary=binary))


class ClaudeCodeClient:
    """Runs each leader through `claude -p`, so it uses your Claude subscription. No API key.

    - Runs from an empty temp folder with --safe-mode, so your CLAUDE.md, hooks, plugins and
      MCP servers never load into a leader (safe mode keeps your normal login).
    - --tools "" turns off file and shell access: a leader can only talk.
    - Not --bare: bare mode skips your subscription login and would need an API key.
    - ANTHROPIC_API_KEY is removed from its environment, so it can't switch to API billing.
    Usage counts against your plan's 5-hour and weekly limits.
    """

    def __init__(self, model: str = "haiku", binary: str | None = None, timeout: float = 180):
        self.model = model
        self.name = f"claude-code:{model}"
        self.binary = find_claude(binary) or binary or "claude"
        self.timeout = timeout
        self._safe_mode = True
        self._workdir = tempfile.mkdtemp(prefix="civ-council-leader-")

    def _cmd(self, system_file: str) -> list[str]:
        cmd = [self.binary, "-p", "--model", self.model, "--output-format", "json",
               "--system-prompt-file", system_file, "--tools", "", "--disallowedTools", "mcp__*",
               "--max-turns", "1", "--no-session-persistence"]
        if self._safe_mode:
            cmd.append("--safe-mode")
        return cmd

    def complete(self, system: str, messages: list[dict], max_tokens: int = 1200) -> str:
        prompt = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
        system_file = os.path.join(self._workdir, "system.txt")
        with open(system_file, "w") as f:
            f.write(system)
        for _ in range(2):
            try:
                proc = subprocess.run(self._cmd(system_file), input=prompt, capture_output=True, text=True,
                                      cwd=self._workdir, timeout=self.timeout, env=_leader_env())
            except FileNotFoundError:
                raise RuntimeError(INSTALL_HELP) from None
            if proc.returncode != 0 and self._safe_mode and "safe-mode" in proc.stderr:
                self._safe_mode = False   # older Claude Code without --safe-mode: retry without it
                continue
            break
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude -p failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        if data.get("is_error"):
            result = str(data.get("result"))
            if "logged in" in result.lower() or "/login" in result:
                result += f" (run: \"{self.binary}\" auth login)"
            if "safeguard" in result.lower():   # a false alarm we want to understand: keep what was sent
                result += f" (prompt saved to {_save_flagged(self.model, system, prompt, result)})"
            raise RuntimeError(f"claude -p error: {result[:900]}")
        u = data.get("usage") or {}
        self.last_usage = {   # what this call cost: the usage meter adds these up
            "seconds": round((data.get("duration_ms") or 0) / 1000, 1),
            "input_tokens": sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                               "cache_creation_input_tokens")),
            "output_tokens": int(u.get("output_tokens") or 0),
            "cost_usd": float(data.get("total_cost_usd") or 0),
        }
        return str(data.get("result", ""))


class ScriptedClient:
    """Returns replies from a function (system, messages) -> str, or from a list in order."""

    def __init__(self, replies: Callable[[str, list[dict]], str] | list[str], name: str = "scripted"):
        self.name = name
        self._fn = replies if callable(replies) else None
        self._queue = list(replies) if not callable(replies) else []
        self.calls: list[tuple[str, list[dict]]] = []

    def complete(self, system: str, messages: list[dict], max_tokens: int = 1200) -> str:
        self.calls.append((system, messages))
        if self._fn:
            return self._fn(system, messages)
        return self._queue.pop(0) if self._queue else "{}"


def make_client(provider: str, model: str | None = None, base_url: str | None = None,
                claude_bin: str | None = None) -> LLMClient:
    if provider == "claude-code":
        client = ClaudeCodeClient(model or "haiku", binary=claude_bin)
        check_claude_login(find_claude(claude_bin))
        return client
    if provider == "anthropic":
        return AnthropicClient(model or "claude-sonnet-5-5")
    if provider == "openai":
        return OpenAICompatibleClient(model or "gpt-5", base_url or "https://api.openai.com/v1")
    if provider == "ollama":
        return OpenAICompatibleClient(model or "llama3.1", base_url or "http://localhost:11434/v1")
    if provider == "fake":
        from .fake import fake_leader_brain
        return ScriptedClient(fake_leader_brain, name="fake")
    raise ValueError(f"unknown provider {provider!r}")
