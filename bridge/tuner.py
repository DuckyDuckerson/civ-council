"""Minimal client for Civ 6's tuner (debug) port, TCP 4318. No FireTuner app needed.

Wire format (checked against civ6-mcp's tuner client and a live Civ 6 on macOS):
  [uint32 little-endian: payload length incl. trailing NUL][int32 little-endian: tag][payload][NUL]
  tag 4 = handshake: "APP:" -> "Civ6\\0Sid Meier's Civilization 6\\0<path>"
                     "LSQ:" -> Lua states as NUL-separated index/name pairs: "0\\0FrontEnd\\01\\0GameCore_Tuner..."
                     (empty until a game is loaded)
  tag 3 = command:   "CMD:<state index>:<lua code>"
Replies to a command: each print() comes back as "O\\0<state name>: <text>", a Lua error as "ERR:<message>",
plus empty acks. We append print("---END---") to know when a command finished.

Civ 6 keeps a client slot open even after the client disconnects uncleanly, and then listens on
the next port (4319, ...). So connect() tries a few ports, and nothing should poke the port
with a bare connect-and-close (like `nc -z`).
"""
from __future__ import annotations

import re
import socket
import struct
import time

TAG_COMMAND = 3
TAG_HANDSHAKE = 4
END = "---END---"
PORTS = (4318, 4319, 4320, 4321)


def output_text(payload: str) -> str | None:
    """The printed text from an output message ("O\\0InGame: text"), or None if it isn't one."""
    if not payload.startswith("O"):
        return None
    sep = payload.find(": ", 2)
    return payload[sep + 2:] if sep >= 0 else payload.lstrip("O").lstrip("\x00 ")


class Tuner:
    def __init__(self, host: str = "127.0.0.1", port: int | None = None, timeout: float = 20):
        """Connect to the game. port=None tries 4318, then the fallback ports Civ moves to."""
        errors = []
        for p in ([port] if port else PORTS):
            try:
                self.sock = socket.create_connection((host, p), timeout=5)
                self.port = p
                break
            except OSError as e:
                errors.append(f"{p}: {e}")
        else:
            raise ConnectionRefusedError("no tuner port open (" + "; ".join(errors) + ")")
        self.timeout = timeout
        self.app = ""
        self.raw_handshake: list[bytes] = []
        self.states: list[str] = []   # position = Lua state index; "" for unused indexes

    # ---- framing ------------------------------------------------------------------
    def send(self, tag: int, payload: str) -> None:
        data = payload.encode("utf-8") + b"\0"
        self.sock.sendall(struct.pack("<Ii", len(data), tag) + data)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("game closed the connection")
            buf += chunk
        return buf

    def recv(self, wait: float) -> tuple[int, bytes] | None:
        self.sock.settimeout(wait)
        try:
            length, tag = struct.unpack("<Ii", self._recv_exact(8))
        except socket.timeout:
            return None
        self.sock.settimeout(self.timeout)
        return tag, self._recv_exact(length)

    def drain(self, wait: float = 0.2) -> list[bytes]:
        """Read and return whatever the game sent on its own (stray prints from other scripts)."""
        out = []
        while True:
            msg = self.recv(wait)
            if msg is None:
                return out
            out.append(msg[1])

    def _reply(self, wait: float) -> bytes | None:
        """Next handshake reply, skipping stray print output."""
        deadline = time.time() + wait
        while time.time() < deadline:
            msg = self.recv(max(0.05, deadline - time.time()))
            if msg is None:
                return None
            if output_text(msg[1].decode("utf-8", "replace")) is None:
                return msg[1]
        return None

    # ---- handshake ------------------------------------------------------------------
    def handshake(self) -> list[str]:
        self.drain(0.3)
        self.send(TAG_HANDSHAKE, "APP:")
        app = self._reply(5)
        self.send(TAG_HANDSHAKE, "LSQ:")
        lsq = self._reply(5)
        self.raw_handshake = [r for r in (app, lsq) if r is not None]
        self.app = (app or b"").split(b"\0")[0].decode("utf-8", "replace")
        self.states = self._parse_states([lsq] if lsq else [])
        return self.states

    @staticmethod
    def _parse_states(raw: list[bytes]) -> list[str]:
        """LSQ reply -> list where position = state index. Handles "i\\0name" pairs, "i:name", or bare names."""
        for payload in raw:
            text = payload.decode("utf-8", "replace")
            if text.startswith("LSQ:"):
                text = text[4:]
            parts = [p.strip() for p in re.split(r"[\x00\n]", text) if p.strip()]
            names: list[str] = []

            def put(idx: int, name: str) -> None:
                while len(names) <= idx:
                    names.append("")
                names[idx] = name

            i = 0
            while i < len(parts):
                p = parts[i]
                m = re.match(r"^(\d+)[:=](.+)$", p)
                if m:
                    put(int(m.group(1)), m.group(2).strip())
                elif p.isdigit() and i + 1 < len(parts) and not parts[i + 1].isdigit():
                    put(int(p), parts[i + 1])
                    i += 1
                elif not p.isdigit():
                    names.append(p)
                i += 1
            if any(names):
                return names
        return []

    def index_of(self, wanted: str) -> int | None:
        exact = [i for i, s in enumerate(self.states) if s == wanted]
        if exact:
            return exact[0]
        loose = [i for i, s in enumerate(self.states) if s and wanted.lower() in s.lower()]
        return loose[0] if loose else None

    # ---- running Lua ------------------------------------------------------------------
    def run(self, state_index: int, lua: str) -> str:
        """Run Lua in a state and return everything it printed, one line per print().

        A Lua error that escapes the chunk ends the command early with an "[Lua error] ..." line.
        """
        self.drain(0.1)
        # Run the chunk as a function, so a script that stops early with `return` still reaches END.
        self.send(TAG_COMMAND, f"CMD:{state_index}:(function()\n{lua}\nend)()\nprint(\"{END}\")")
        out: list[str] = []
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            msg = self.recv(1.0)
            if not msg:
                continue
            payload = msg[1].decode("utf-8", "replace").rstrip("\x00")
            if payload.startswith("ERR:"):
                out.append(f"[Lua error] {payload[4:].strip()}")
                return "\n".join(out)
            text = output_text(payload)
            if text is None:
                continue          # empty ack
            if text.strip() == END:
                return "\n".join(out)
            out.append(text)
        out.append("[timed out waiting for the game's reply]")
        return "\n".join(out)

    def close(self) -> None:
        self.sock.close()
