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
from typing import Iterator

from . import ucnet

# cbytes for the UM (UDP-port announce) message -- the root session's pair.
UM_CB = b"\x00\x00\x69\x00"
# cbytes for the HD 8 device session (client -> daemon).
DEVICE_CB = b"\x6a\x00\x69\x00"
# What the daemon replies with on the device session (the pair swapped).
DEVICE_REPLY_CB = b"\x69\x00\x6a\x00"

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
                 raw_sink=None):
        self.host = host
        self.port = port
        self._sock_factory = sock_factory
        self.connect_timeout = connect_timeout
        self._raw_sink = raw_sink
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
        value = self.get(path)
        r = self.ranges.get(path)
        if not r or r.get("curve") != "linear":
            return f"{value} (normalizado)"
        lo, hi = r["min"], r["max"]
        db = lo + value * (hi - lo)
        return f"{db:.1f} dB"

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
