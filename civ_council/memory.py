"""What each leader remembers between turns (and between sessions, via save/load).

Memory is per leader, so Gilgamesh's opinion of you is his alone. Lists are capped so
prompts stay small and older details naturally fade, roughly like Civ's decaying modifiers.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_NOTES = 12
MAX_PROMISES = 8
MAX_CONVO = 16
MAX_JOURNAL = 12
MAX_HISTORY = 300


@dataclass
class Relationship:
    trust: int = 0                      # -100 (hated) .. 100 (trusted)
    notes: list[str] = field(default_factory=list)
    promises: list[str] = field(default_factory=list)  # what they promised me
    summary: str = ""                   # long-term memory: only what really matters, rewritten every N turns
    history: list[list[int]] = field(default_factory=list)   # [turn, trust] each time it changed (Spy timeline)

    def adjust(self, delta: int, note: str = "", promise: str = "", turn: int | None = None) -> None:
        before = self.trust
        self.trust = max(-100, min(100, self.trust + max(-25, min(25, int(delta)))))
        if turn is not None and self.trust != before:
            if self.history and self.history[-1][0] == turn:
                self.history[-1][1] = self.trust
            else:
                if not self.history:
                    self.history.append([turn - 1, before])
                self.history = (self.history + [[turn, self.trust]])[-MAX_HISTORY:]
        tag = f"T{turn}: " if turn is not None else ""
        if note:
            self.notes = (self.notes + [tag + note])[-MAX_NOTES:]
        if promise:
            self.promises = (self.promises + [tag + promise])[-MAX_PROMISES:]

    @property
    def feeling(self) -> str:
        t = self.trust
        if t >= 60: return "trusted friend"
        if t >= 25: return "friendly"
        if t > -25: return "neutral"
        if t > -60: return "suspicious"
        return "hated enemy"


@dataclass
class LeaderMemory:
    relationships: dict[int, Relationship] = field(default_factory=dict)
    # Conversation logs keyed by counterpart civ id, or "public" for messages to everyone.
    conversations: dict[str, list[dict]] = field(default_factory=dict)
    journal: list[str] = field(default_factory=list)
    story: str = ""                     # long-term memory of the game so far, in the leader's own words
    strategy: dict = field(default_factory=dict)   # the grand strategy (written by the strategy model), see agent.py
    fresh: int = 0                      # messages and notes since the last summary (summaries come when it's grown)
    summarized: int | None = None       # the turn of the last summary

    def rel(self, civ_id: int) -> Relationship:
        return self.relationships.setdefault(int(civ_id), Relationship())

    def log(self, channel: int | str, turn: int, speaker: str, text: str) -> None:
        convo = self.conversations.setdefault(str(channel), [])
        convo.append({"turn": turn, "from": speaker, "text": text})
        self.fresh += 1
        del convo[:-MAX_CONVO]

    def add_journal(self, turn: int, line: str) -> None:
        line = re.sub(r"^\s*T\d+\s*:\s*", "", line or "")   # models often add the "T81:" themselves
        if line:
            self.journal = (self.journal + [f"T{turn}: {line}"])[-MAX_JOURNAL:]

    def compress(self, summaries: dict[int, str], story: str, keep: int = 3) -> None:
        """Swap detailed short-term memory for summaries. Keeps only the last few raw items."""
        for civ_id, text in summaries.items():
            self.rel(civ_id).summary = text.strip()[:600]
        if story.strip():
            self.story = story.strip()[:900]
        for r in self.relationships.values():
            r.notes = r.notes[-keep:]
            r.promises = r.promises[-keep:]
        for channel in self.conversations:
            self.conversations[channel] = self.conversations[channel][-keep:]
        self.journal = self.journal[-keep:]
        self.fresh = 0

    # ---- persistence ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "relationships": {str(k): asdict(v) for k, v in self.relationships.items()},
            "conversations": self.conversations,
            "journal": self.journal,
            "story": self.story,
            "strategy": self.strategy,
            "fresh": self.fresh,
            "summarized": self.summarized,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LeaderMemory":
        return cls(
            relationships={int(k): Relationship(**{f: x for f, x in v.items() if f in Relationship.__dataclass_fields__})
                           for k, v in d.get("relationships", {}).items()},
            conversations=d.get("conversations", {}),
            journal=d.get("journal", []),
            story=d.get("story", ""),
            fresh=int(d.get("fresh") or 0),
            summarized=d.get("summarized"),
            strategy=d.get("strategy", {}) if isinstance(d.get("strategy"), dict) else {},
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> "LeaderMemory":
        return cls.from_dict(json.loads(path.read_text())) if path.exists() else cls()
