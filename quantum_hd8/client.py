"""Read-only UCNet client for the Quantum HD 8's ucdaemon session.

Two UCNet sessions matter here (see docs/protocol.md, "cbytes são o
endereço da sessão"): the root session (endpoints only) and the device
session, which carries the HD 8's parameter tree, scene list and live
events. This client only ever opens the device session.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Iterator

from . import ucnet
from . import undo

# cbytes for the UM (UDP-port announce) message -- the root session's pair.
UM_CB = b"\x00\x00\x69\x00"
# cbytes for the HD 8 device session (client -> daemon).
DEVICE_CB = b"\x6a\x00\x69\x00"
# What the daemon replies with on the device session (the pair swapped).
DEVICE_REPLY_CB = b"\x69\x00\x6a\x00"

PARAMS_PATH = Path(__file__).with_name("params.json")
_PARAMS: dict[str, dict] = {
    row["path"]: row for row in json.loads(PARAMS_PATH.read_text())
}


class WriteNotConfirmed(Exception):
    """Client.set/set_raw sent a PV write but no echo of it arrived in time."""


class SceneLoadTimeout(Exception):
    """Client.load_scene sent RestorePreset but no RecalledPreset arrived."""

# Same clientName/clientDescription/clientIdentifier as
# tests/fixtures/probe-device-tx.bin, which is a real, working capture.
SUBSCRIBE_PAYLOAD = {
    "id": "Subscribe",
    "clientName": "quantum-hd8",
    "clientInternalName": "ucapp",
    "clientType": "Mac",
    "clientDescription": "quantum-hd8",
    "clientIdentifier": "quantum-hd8",
    "clientOptions": "",
    "clientEncoding": 23117,
}

UDP_DISCOVERY_PORT = 47809
CONNECT_TIMEOUT = 3.0
KEEPALIVE_INTERVAL = 1.0

NOT_RESPONDING = (
    "ucdaemon não respondeu -- o Universal Control está instalado e o "
    "daemon rodando?"
)


def flatten(tree: dict, prefix: str = "") -> dict[str, object]:
    """Flatten a Synchronize-shaped node tree into path -> raw value.

    Real shape (measured): a node has "values" (leaf name -> value) and
    "children" (child name -> node), walked recursively. Paths are
    "<prefix>/<key>".
    """
    out: dict[str, object] = {}
    for key, value in tree.get("values", {}).items():
        out[f"{prefix}/{key}" if prefix else key] = value
    for key, child in tree.get("children", {}).items():
        out.update(flatten(child, f"{prefix}/{key}" if prefix else key))
    return out


def flatten_ranges(tree: dict, prefix: str = "") -> dict[str, dict]:
    """Same walk as flatten(), but collects the "ranges" dicts instead."""
    out: dict[str, dict] = {}
    for key, value in tree.get("ranges", {}).items():
        out[f"{prefix}/{key}" if prefix else key] = value
    for key, child in tree.get("children", {}).items():
        out.update(flatten_ranges(child, f"{prefix}/{key}" if prefix else key))
    return out


def _parse_scene_list(m: ucnet.Message) -> list[str]:
    # FD payload: 14-byte binary header, then a JSON body (measured).
    body = m.payload[14:]
    return [f["name"] for f in json.loads(body).get("files", [])]


class Client:
    def __init__(self, host: str = "127.0.0.1", port: int = 59791,
                 sock_factory=socket.create_connection,
                 connect_timeout: float = CONNECT_TIMEOUT,
                 raw_sink=None,
                 undo_journal: Path | None = None):
        self.host = host
        self.port = port
        self._sock_factory = sock_factory
        self.connect_timeout = connect_timeout
        self._raw_sink = raw_sink
        self.undo_journal = Path(undo_journal) if undo_journal is not None else undo.DEFAULT_JOURNAL
        self.sock = None
        self.state: dict[str, object] = {}
        self.ranges: dict[str, dict] = {}
        self.scenes: list[str] = []
        self.lists: dict[str, list[str]] = {}
        self._decoder = ucnet.Decoder()
        self._keepalive_stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None

    def connect(self) -> dict:
        self.sock = self._sock_factory((self.host, self.port))
        self.sock.settimeout(self.connect_timeout)

        self.sock.sendall(ucnet.encode(
            "UM", b"\x00\x00" + struct.pack("<H", UDP_DISCOVERY_PORT), cbytes=UM_CB))
        self.sock.sendall(ucnet.encode(
            "JM", ucnet.json_payload(SUBSCRIBE_PAYLOAD), cbytes=DEVICE_CB))
        self.sock.sendall(ucnet.encode(
            "FR", struct.pack("<H", 1) + b"Listscene" + b"\x00\x00", cbytes=DEVICE_CB))

        # ZM/ZB (state) and FD (scene list) can arrive in separate TCP
        # segments -- i.e. separate recv() calls, possibly split at any byte
        # boundary. Wait for both, bounded by one deadline covering every
        # recv() in this loop (not reset per call), so a daemon that answers
        # state but never sends FD does not hang connect() forever.
        got_state = False
        got_scenes = False
        deadline = time.monotonic() + self.connect_timeout
        while not (got_state and got_scenes):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not got_state:
                    raise TimeoutError(NOT_RESPONDING)
                break  # scenes never arrived -- proceed with scenes=[]
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                if not got_state:
                    raise TimeoutError(NOT_RESPONDING)
                break
            if not data:
                if not got_state:
                    raise TimeoutError(NOT_RESPONDING)
                break  # connection closed -- proceed with what we have
            if self._raw_sink is not None:
                self._raw_sink(data)
            for m in self._decoder.feed(data):
                kind = self._handle(m)
                if kind == "state":
                    got_state = True
                elif kind == "scenes":
                    got_scenes = True

        self._start_keepalive()
        return self.state

    def _handle(self, m: ucnet.Message) -> str | None:
        if m.code in ("ZM", "ZB"):
            tree = ucnet.parse_state(m)
            self.state = flatten(tree)
            self.ranges = flatten_ranges(tree)
            return "state"
        if m.code == "FD":
            self.scenes = _parse_scene_list(m)
            return "scenes"
        if m.code == "PV":
            path, value = ucnet.parse_pv(m)
            self.state[path] = value
            return "pv"
        if m.code == "PL":
            path, value, labels = ucnet.parse_pl(m)
            self.lists[path] = labels
            self.state[path] = value
            return "pl"
        return None

    def get(self, path: str) -> object:
        return self.state[path]

    def human(self, path: str) -> str:
        r = self.ranges.get(path)
        if not r or r.get("curve") != "linear":
            return f"{self.get(path)} (normalizado)"
        return f"{self._human_value(path):.1f} dB"

    def _human_value(self, path: str) -> float:
        r = self.ranges[path]
        lo, hi = r["min"], r["max"]
        return lo + self.state[path] * (hi - lo)

    def _param_row(self, path: str) -> dict:
        try:
            return _PARAMS[path]
        except KeyError:
            raise KeyError(f"caminho desconhecido: {path}") from None

    def set(self, path: str, value) -> object:
        """Write `path`, validated/converted per params.json + self.ranges,
        wait for the daemon's echo, record before/after in the undo journal
        and return the echoed (raw normalized) value.

        Ruling (task-7-brief.md): curve "linear" params take HUMAN units
        (e.g. dB), converted with self.ranges' min/max; type "toggle" takes
        0/1; every other curve (fader, exp, unknown/None) takes a raw
        normalized value in 0..1.
        """
        row = self._param_row(path)
        if "readonly" in row.get("flags", []):
            raise PermissionError(f"{path} é readonly")

        r = self.ranges.get(path)
        if r and r.get("curve") == "linear":
            lo, hi = r["min"], r["max"]
            if not (lo <= value <= hi):
                raise ValueError(f"{path}: {value} fora da faixa [{lo}, {hi}]")
            normalized = (value - lo) / (hi - lo)
        elif row.get("type") == "toggle":
            if value not in (0, 1):
                raise ValueError(f"{path}: {value} não é 0/1 (toggle)")
            normalized = float(value)
        else:
            if not (0.0 <= value <= 1.0):
                raise ValueError(f"{path}: {value} fora da faixa normalizada [0, 1]")
            normalized = float(value)

        before = self.state.get(path)
        echoed = self._write_pv(path, normalized)
        undo.record(path, before, echoed, journal=self.undo_journal)
        return echoed

    def set_raw(self, path: str, normalized: float) -> object:
        """Write a raw normalized value directly, bypassing curve conversion,
        range validation and undo recording. Used by the CLI `undo` command
        to restore the exact previous value (ruling: "bypassing human
        conversion")."""
        return self._write_pv(path, normalized)

    def _write_pv(self, path: str, normalized: float) -> object:
        self.sock.sendall(ucnet.encode("PV", ucnet.pv_payload(path, normalized), cbytes=DEVICE_CB))
        m = self._drain_until(
            lambda m: m.code == "PV" and ucnet.parse_pv(m)[0] == path, timeout=1.0)
        if m is None:
            raise WriteNotConfirmed(path)
        return ucnet.parse_pv(m)[1]

    def load_scene(self, name: str, keep_gains: bool = False) -> dict:
        """Send JM RestorePreset for scene `name` (".scene" appended if
        missing) and wait up to 3 s for JM RecalledPreset.

        With keep_gains=True: snapshot line/ch1..8/preampgain (human dB)
        before sending, snapshot them again once RecalledPreset arrives, and
        re-set (via `set`, one at a time) every channel whose value changed
        -- loading a scene resets preamp gains on the rig (docs/protocol.md).
        Returns {"preset_file": ..., "gains": [(path, before, after), ...]}
        for channels that were restored.
        """
        preset_file = name if name.endswith(".scene") else f"{name}.scene"
        gain_paths = [f"line/ch{ch}/preampgain" for ch in range(1, 9)]
        before = {p: self._human_value(p) for p in gain_paths} if keep_gains else {}

        payload = ucnet.compact_json_payload({
            "id": "RestorePreset",
            "url": "presets",
            "presetTarget": "",
            "presetTargetSlave": 0,
            "presetFile": f"scene/{preset_file}",
        })
        self.sock.sendall(ucnet.encode("JM", payload, cbytes=DEVICE_CB))

        m = self._drain_until(
            lambda m: m.code == "JM" and ucnet.parse_json(m).get("id") == "RecalledPreset",
            timeout=3.0)
        if m is None:
            raise SceneLoadTimeout(name)

        gains = []
        if keep_gains:
            after = {p: self._human_value(p) for p in gain_paths}
            for p in gain_paths:
                b, a = before[p], after[p]
                if a != b:
                    self.set(p, b)
                    gains.append((p, b, a))
        return {"preset_file": preset_file, "gains": gains}

    def _drain_until(self, predicate, timeout: float):
        """Read from the socket, feeding every decoded message through
        _handle() (so state/scenes/lists stay current, same as connect() and
        events()), until `predicate(message)` is true. Returns that message,
        or None on timeout / closed connection."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not data:
                return None
            if self._raw_sink is not None:
                self._raw_sink(data)
            for m in self._decoder.feed(data):
                self._handle(m)
                if predicate(m):
                    return m

    def events(self, timeout: float | None = None) -> Iterator[tuple[str, object]]:
        self.sock.settimeout(timeout)
        while True:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                return
            if not data:
                return
            if self._raw_sink is not None:
                self._raw_sink(data)
            for m in self._decoder.feed(data):
                if m.code == "PV":
                    path, value = ucnet.parse_pv(m)
                    self.state[path] = value
                    yield path, value
                elif m.code == "PL":
                    path, value, labels = ucnet.parse_pl(m)
                    self.lists[path] = labels
                    self.state[path] = value
                    yield path, value
                else:
                    self._handle(m)

    def _start_keepalive(self):
        def loop():
            while not self._keepalive_stop.wait(KEEPALIVE_INTERVAL):
                try:
                    self.sock.sendall(ucnet.encode("KA", b"", cbytes=DEVICE_CB))
                except OSError:
                    return
        self._keepalive_thread = threading.Thread(target=loop, daemon=True)
        self._keepalive_thread.start()

    def close(self):
        self._keepalive_stop.set()
        if self._keepalive_thread is not None:
            self._keepalive_thread.join(timeout=KEEPALIVE_INTERVAL + 0.5)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
