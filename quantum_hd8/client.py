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

# Number of "in" meter channels the daemon reports (docs/protocol.md,
# "Medidores"): line/ch1..36.
METER_IN_CHANNELS = 36
# Number of aux meter pairs (14 aux sends x L/R = 28 values).
METER_AUX_PAIRS = 14


def default_udp_factory() -> socket.socket:
    """A real UDP socket bound to an ephemeral port on 127.0.0.1 -- the
    default udp_factory for callers that want meters (e.g. the CLI `meters`
    command). Never used unless explicitly passed in: tests must inject a
    fake instead (see docs/protocol.md, "Medidores")."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    return s
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
                 undo_journal: Path | None = None,
                 udp_factory=None):
        self.host = host
        self.port = port
        self._sock_factory = sock_factory
        self.connect_timeout = connect_timeout
        self._raw_sink = raw_sink
        self.undo_journal = Path(undo_journal) if undo_journal is not None else undo.DEFAULT_JOURNAL
        # Meters are opt-in: no udp_factory (the default) means connect()
        # never binds a socket -- it just announces UDP_DISCOVERY_PORT in UM
        # as before (task-9-report.md: preserves existing behaviour/tests).
        # Pass udp_factory=default_udp_factory (or a fake in tests) to open
        # a real meter socket and announce its actual port instead.
        self._udp_factory = udp_factory
        self.udp_sock = None
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

        udp_port = UDP_DISCOVERY_PORT
        if self._udp_factory is not None:
            self.udp_sock = self._udp_factory()
            udp_port = self.udp_sock.getsockname()[1]
        self.sock.sendall(ucnet.encode(
            "UM", struct.pack("<H", udp_port), cbytes=UM_CB))
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
        curve, lo, hi = self._curve_and_range(path)
        if curve != "linear" or lo is None or hi is None:
            return f"{self.get(path)} (normalizado)"
        return f"{self._human_value(path):.1f} dB"

    def _human_value(self, path: str) -> float:
        _, lo, hi = self._curve_and_range(path)
        return lo + self.state[path] * (hi - lo)

    def to_human(self, path: str, normalized: float) -> float | None:
        """normalized -> human units for `path`, or None when it isn't a
        linear-curve param with a known range (daemon or params.json)."""
        curve, lo, hi = self._curve_and_range(path)
        if curve != "linear" or lo is None or hi is None:
            return None
        return lo + normalized * (hi - lo)

    def _curve_and_range(self, path: str) -> tuple[str | None, float | None, float | None]:
        """(curve, min, max) for `path`. Prefers the daemon's live `ranges`
        (self.ranges); falls back to params.json (the XML's curve/min/max,
        via tools/gen_params.py) when the daemon reported no range for it --
        e.g. global/ledBrightness: Synchronize carries no `ranges` entry for
        it, but the XML has curve="linear" min="1" max="100" (fix round 1,
        finding 1)."""
        r = self.ranges.get(path)
        if r and r.get("curve") is not None:
            return r.get("curve"), r.get("min"), r.get("max")
        row = self._param_row(path)
        return row.get("curve"), row.get("min"), row.get("max")

    def param_row(self, path: str) -> dict:
        """The params.json row for `path` (raises KeyError if unknown)."""
        return self._param_row(path)

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
        (e.g. dB), converted with the known min/max (self.ranges, falling
        back to params.json -- see _curve_and_range); type "toggle" takes
        0/1; every other curve (fader, exp, unknown/None) takes a raw
        normalized value in 0..1.
        """
        row = self._param_row(path)
        if "readonly" in row.get("flags", []):
            raise PermissionError(f"{path} é readonly")

        curve, lo, hi = self._curve_and_range(path)
        if curve == "linear" and lo is not None and hi is not None:
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

        is_noop, current = self._is_noop_write(path, normalized, row)
        if is_noop:
            return current

        before = self.state.get(path)
        echoed = self._write_pv(path, normalized)
        undo.record(path, before, echoed, journal=self.undo_journal)
        return echoed

    def set_list(self, path: str, index: int, n: int) -> object:
        """Write a list-type param (label list filled at runtime via PL,
        units="EmptyParamList") by index: normalized = index / (n - 1)
        (verified: global/mainOutVolumeLink 0.333 with 7 labels = index 2).
        Waits for the echo and records undo, same as set() -- reuses
        _write_pv/undo.record rather than duplicating that logic."""
        if n < 2:
            raise ValueError(f"{path}: lista precisa de ao menos 2 rótulos")
        if not (0 <= index < n):
            raise ValueError(f"{path}: índice {index} fora de [0, {n - 1}]")
        normalized = index / (n - 1)

        try:
            row = self._param_row(path)
        except KeyError:
            row = None
        is_noop, current = self._is_noop_write(path, normalized, row)
        if is_noop:
            return current

        before = self.state.get(path)
        echoed = self._write_pv(path, normalized)
        undo.record(path, before, echoed, journal=self.undo_journal)
        return echoed

    def _is_noop_write(self, path: str, normalized: float,
                        row: dict | None) -> tuple[bool, object]:
        """(True, current-state-value) when `normalized` already matches
        self.state[path] -- measured live: the daemon does not echo a PV
        when the written value equals the current one, so writing it would
        raise WriteNotConfirmed for what is really a no-op (fix round 1,
        finding 2a). int params compare after quantizing both values to the
        param's integer step (round(v * (max-min)) / (max-min)); every
        other type compares with abs diff < 1e-4."""
        current = self.state.get(path)
        if current is None:
            return False, None

        if row is not None and row.get("type") == "int":
            _, lo, hi = self._curve_and_range(path)
            if lo is not None and hi is not None and hi != lo:
                step = hi - lo
                if round(normalized * step) == round(current * step):
                    return True, current
                return False, None

        if abs(normalized - current) < 1e-4:
            return True, current
        return False, None

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
            raise WriteNotConfirmed(f"{path}: o daemon não confirmou a escrita em 1 s")
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

    def read_meters(self, timeout: float = 1.0) -> dict:
        """Read and parse one MS meter UDP packet (docs/protocol.md,
        "Medidores"). Requires udp_factory to have been passed to
        __init__ -- raises RuntimeError otherwise."""
        if self.udp_sock is None:
            raise RuntimeError(
                "medidores não habilitados -- passe udp_factory ao criar o Client")
        self.udp_sock.settimeout(timeout)
        data = self.udp_sock.recv(4096)
        return ucnet.parse_meters(data)

    def meter_labels(self) -> dict[str, list[str]]:
        """Labels matching the shape of ucnet.parse_meters()'s output:
        {"in": [...36], "aux": [...28], "main": ["L", "R"]} (ruling 4,
        task-9-brief.md). "in"[i] is line/ch{i+1}'s username (preferred) or
        chnum from the live Synchronize state when known, else the raw path
        itself -- state only carries chnum/username for the 8 analog
        channels (line/ch1..8) today; the rest fall back to "line/chN"."""
        in_labels = []
        for i in range(METER_IN_CHANNELS):
            path = f"line/ch{i + 1}"
            in_labels.append(
                self.state.get(f"{path}/username")
                or self.state.get(f"{path}/chnum")
                or path)
        aux_labels = []
        for k in range(1, METER_AUX_PAIRS + 1):
            aux_labels.append(f"aux/ch{k} L")
            aux_labels.append(f"aux/ch{k} R")
        return {"in": in_labels, "aux": aux_labels, "main": ["main L", "main R"]}

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
        if self.udp_sock is not None:
            try:
                self.udp_sock.close()
            except OSError:
                pass
