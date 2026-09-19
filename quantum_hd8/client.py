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


class SceneSaveTimeout(Exception):
    """Client.save_scene sent StorePreset but no StoredPreset arrived."""

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
# After RecalledPreset the daemon re-sends every PV (docs/protocol.md,
# "Escrita"). load_scene(keep_gains=True) keeps reading until the link has
# been quiet this long, bounded by SCENE_SETTLE_MAX, before comparing gains.
SCENE_QUIET = 0.3
SCENE_SETTLE_MAX = 3.0
# save_scene() timeout waiting for JM StoredPreset after StorePreset
# (measured 19/09: uc-store.bin/uc-stored.bin, same 3 s budget as load_scene).
SCENE_SAVE_TIMEOUT = 3.0
# FR "Listscene" request counter, first two bytes of the FR payload
# (docs/protocol.md: measured 01 00 at connect, 02 00 after a save).
INITIAL_FR_COUNTER = 1
# Echo tolerance for non-int params (int params compare quantized steps).
ECHO_TOLERANCE = 1e-3
# load_scene() snapshots every state key under this prefix before/after a
# recall to detect side effects like the mixerMode flip below.
GLOBAL_PREFIX = "global/"
# Measured 18/09 (docs/protocol.md, "Escrita"): loading a scene can change
# this without being asked (Mixer Bypass -> Analog + ADAT loading
# MK300-FRFR). load_scene(keep_mode=True) writes it back.
MIXER_MODE_PATH = "global/mixerMode"
# params.json types whose value is text, not a number -- never writable here.
TEXT_TYPES = ("string", "color")

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
        # Keepalive thread and main thread share the socket: every sendall
        # goes through _send(), under this lock.
        self._send_lock = threading.Lock()
        self.scene_quiet = SCENE_QUIET
        self.scene_settle_max = SCENE_SETTLE_MAX
        # FR "Listscene" request counter (docs/protocol.md): 1 is sent by
        # connect(); save_scene() increments it before each refresh.
        self._fr_counter = INITIAL_FR_COUNTER

    def _send(self, data: bytes) -> None:
        with self._send_lock:
            self.sock.sendall(data)

    def connect(self) -> dict:
        self.sock = self._sock_factory((self.host, self.port))
        self.sock.settimeout(self.connect_timeout)

        udp_port = UDP_DISCOVERY_PORT
        if self._udp_factory is not None:
            self.udp_sock = self._udp_factory()
            udp_port = self.udp_sock.getsockname()[1]
        self._send(ucnet.encode(
            "UM", struct.pack("<H", udp_port), cbytes=UM_CB))
        self._send(ucnet.encode(
            "JM", ucnet.json_payload(SUBSCRIBE_PAYLOAD), cbytes=DEVICE_CB))
        self._send(ucnet.encode(
            "FR", struct.pack("<H", self._fr_counter) + b"Listscene" + b"\x00\x00",
            cbytes=DEVICE_CB))

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
        if row.get("type") in TEXT_TYPES:
            raise ValueError(f"{path}: parâmetro de texto não suportado")

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
        echoed = self._write_pv(path, normalized, row)
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
        echoed = self._write_pv(path, normalized, row)
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
        if not isinstance(current, (int, float)):
            return False, None
        if self._values_match(path, normalized, current, row, 1e-4):
            return True, current
        return False, None

    def _values_match(self, path: str, wanted: float, got: float,
                      row: dict | None, tolerance: float) -> bool:
        """int params: same quantized step (round(v * (max-min)));
        everything else: abs diff < tolerance."""
        if row is not None and row.get("type") == "int":
            _, lo, hi = self._curve_and_range(path)
            if lo is not None and hi is not None and hi != lo:
                step = hi - lo
                return round(wanted * step) == round(got * step)
        return abs(wanted - got) < tolerance

    def _global_changed(self, path: str, before: object, after: object) -> bool:
        """True when `before` -> `after` is a real change for a global/*
        param tracked by load_scene(), not just float-rounding noise (fix:
        `before` comes from the initial Synchronize/ZM read -- full float
        precision -- while `after` comes from the daemon's re-sent PV,
        rounded to 4 decimals by ucnet.parse_pv; measured live:
        global/mainOutVolumeLink 0.3333333432674408 -> 0.3333 is not a real
        change). Same tolerance as echo matching (quantized step for int
        params via _values_match, ECHO_TOLERANCE otherwise); non-numeric
        values (unexpected here) fall back to plain !=."""
        if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
            return before != after
        try:
            row = self._param_row(path)
        except KeyError:
            row = None
        return not self._values_match(path, after, before, row, ECHO_TOLERANCE)

    def set_raw(self, path: str, normalized: float) -> object:
        """Write a raw normalized value directly, bypassing curve conversion,
        range validation and undo recording. Used by the CLI `undo` command
        to restore the exact previous value (ruling: "bypassing human
        conversion"). No-op (returns the current value, sends nothing) when
        the state already holds `normalized` -- the daemon would not echo."""
        if not isinstance(normalized, (int, float)):
            raise ValueError(f"{path}: valor anterior desconhecido ({normalized!r})")
        try:
            row = self._param_row(path)
        except KeyError:
            row = None
        is_noop, current = self._is_noop_write(path, normalized, row)
        if is_noop:
            return current
        return self._write_pv(path, normalized, row)

    def _write_pv(self, path: str, normalized: float, row: dict | None = None) -> object:
        """Send the PV and wait for *its* echo: a PV for `path` whose value
        matches `normalized` (quantized step for int params, ECHO_TOLERANCE
        otherwise). Stale PVs for the same path -- e.g. re-sent by a scene
        recall still in flight -- update state but are not the echo."""
        self._send(ucnet.encode("PV", ucnet.pv_payload(path, normalized), cbytes=DEVICE_CB))

        def is_echo(m):
            if m.code != "PV":
                return False
            p, v = ucnet.parse_pv(m)
            return p == path and self._values_match(path, normalized, v, row, ECHO_TOLERANCE)

        m = self._drain_until(is_echo, timeout=1.0)
        if m is None:
            raise WriteNotConfirmed(f"{path}: o daemon não confirmou a escrita em 1 s")
        return ucnet.parse_pv(m)[1]

    def load_scene(self, name: str, keep_gains: bool = False,
                    keep_mode: bool = False) -> dict:
        """Send JM RestorePreset for scene `name` (".scene" appended if
        missing) and wait up to 3 s for JM RecalledPreset.

        Always snapshots every global/* value before sending, and again
        after the recall settles (measured 18/09: loading MK300-FRFR flips
        global/mixerMode from Mixer Bypass to Analog + ADAT --
        docs/protocol.md, "Escrita" -- and a scene recall can change any
        other global/* the same way). The settle wait (link quiet for
        scene_quiet s, bounded by scene_settle_max s, since the daemon
        re-sends every PV *after* RecalledPreset) always runs, not only for
        keep_gains, so the global/* comparison sees the daemon's re-sent
        values rather than stale in-flight ones.

        With keep_gains=True: snapshot line/ch1..8/preampgain (human dB)
        before sending; after settling, re-set (via `set`, one at a time)
        every channel whose value changed -- loading a scene resets preamp
        gains on the rig.

        With keep_mode=True: if global/mixerMode changed, write the
        pre-scene value back (via `set`, which journals it for undo like
        any other write) and report it in "mode_restored".

        Returns {"preset_file": ..., "gains": [(path, before, after), ...],
        "failed": [(path, before, after, error), ...],
        "changed_globals": [(path, before, after), ...],
        "mode_restored": (path, scene_value, restored_value) | None}.
        A channel whose gain re-set fails (WriteNotConfirmed/ValueError) is
        collected in "failed" and the rest still run.
        """
        preset_file = name if name.endswith(".scene") else f"{name}.scene"
        gain_paths = [f"line/ch{ch}/preampgain" for ch in range(1, 9)]
        before_gains = {p: self._human_value(p) for p in gain_paths} if keep_gains else {}
        before_globals = {p: v for p, v in self.state.items() if p.startswith(GLOBAL_PREFIX)}

        payload = ucnet.compact_json_payload({
            "id": "RestorePreset",
            "url": "presets",
            "presetTarget": "",
            "presetTargetSlave": 0,
            "presetFile": f"scene/{preset_file}",
        })
        self._send(ucnet.encode("JM", payload, cbytes=DEVICE_CB))

        m = self._drain_until(
            lambda m: m.code == "JM" and ucnet.parse_json(m).get("id") == "RecalledPreset",
            timeout=3.0)
        if m is None:
            raise SceneLoadTimeout(name)

        # The daemon re-sends every PV after RecalledPreset; wait for the
        # link to go quiet before reading back global/* (and, below, gains)
        # so we compare against the settled post-recall state.
        self._drain_quiet(self.scene_quiet, self.scene_settle_max)

        after_globals = {p: self.state.get(p) for p in before_globals}
        changed_globals = [
            (p, before_globals[p], after_globals[p])
            for p in before_globals
            if self._global_changed(p, before_globals[p], after_globals[p])
        ]

        gains = []
        failed = []
        if keep_gains:
            after_gains = {p: self._human_value(p) for p in gain_paths}
            for p in gain_paths:
                b, a = before_gains[p], after_gains[p]
                if a != b:
                    try:
                        self.set(p, b)
                    except (WriteNotConfirmed, ValueError) as e:
                        failed.append((p, b, a, str(e)))
                        continue
                    gains.append((p, b, a))

        mode_restored = None
        if keep_mode:
            before_mode = before_globals.get(MIXER_MODE_PATH)
            after_mode = after_globals.get(MIXER_MODE_PATH)
            if (before_mode is not None and after_mode is not None
                    and after_mode != before_mode):
                try:
                    self.set(MIXER_MODE_PATH, before_mode)
                except (WriteNotConfirmed, ValueError):
                    pass
                else:
                    mode_restored = (MIXER_MODE_PATH, after_mode, before_mode)

        return {"preset_file": preset_file, "gains": gains, "failed": failed,
                "changed_globals": changed_globals, "mode_restored": mode_restored}

    def _drain_quiet(self, quiet: float, max_total: float) -> None:
        """Feed every incoming message through _handle() until nothing
        arrives for `quiet` s, the connection closes, or `max_total` s pass."""
        deadline = time.monotonic() + max_total
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.sock.settimeout(min(quiet, remaining))
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                return
            if not data:
                return
            if self._raw_sink is not None:
                self._raw_sink(data)
            for m in self._decoder.feed(data):
                self._handle(m)

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

    def save_scene(self, name: str) -> str:
        """Send JM StorePreset for scene `name` (".scene" appended if
        missing) and wait up to SCENE_SAVE_TIMEOUT s for JM StoredPreset
        (measured 19/09 from a capture of the UC app saving a scene:
        tests/fixtures/uc-store.bin/uc-stored.bin). Raises SceneSaveTimeout
        if it never arrives.

        On success, refreshes self.scenes the same way connect() populates
        it: FR "Listscene" with the next request counter (docs/protocol.md:
        01 00 at connect, 02 00 after save), waiting for the daemon's FD
        reply. A missing FD (unlikely, but not fatal) leaves self.scenes as
        it was -- same leniency as connect()'s scene-list wait.

        Returns the presetFile the daemon confirms in StoredPreset.
        """
        preset_file = name if name.endswith(".scene") else f"{name}.scene"
        payload = ucnet.compact_json_payload({
            "id": "StorePreset",
            "url": "presets",
            "presetTarget": "",
            "presetFile": f"scene/{preset_file}",
        })
        self._send(ucnet.encode("JM", payload, cbytes=DEVICE_CB))

        m = self._drain_until(
            lambda m: m.code == "JM" and ucnet.parse_json(m).get("id") == "StoredPreset",
            timeout=SCENE_SAVE_TIMEOUT)
        if m is None:
            raise SceneSaveTimeout(name)
        stored = ucnet.parse_json(m)

        self._fr_counter += 1
        self._send(ucnet.encode(
            "FR", struct.pack("<H", self._fr_counter) + b"Listscene" + b"\x00\x00",
            cbytes=DEVICE_CB))
        self._drain_until(lambda m: m.code == "FD", timeout=self.connect_timeout)

        return stored.get("presetFile", f"scene/{preset_file}")

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
                    self._send(ucnet.encode("KA", b"", cbytes=DEVICE_CB))
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
