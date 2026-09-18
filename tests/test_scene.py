from pathlib import Path

import pytest

from quantum_hd8 import ucnet
from quantum_hd8.client import Client, DEVICE_REPLY_CB, SceneLoadTimeout

FX = Path(__file__).parent / "fixtures"


class FakeSock:
    def __init__(self, rx=b""):
        self.tx = b""
        self.rx = [rx] if isinstance(rx, (bytes, bytearray)) else list(rx)

    def sendall(self, b):
        self.tx += b

    def recv(self, n):
        return self.rx.pop(0) if self.rx else b""

    def settimeout(self, t):
        pass

    def close(self):
        pass


def test_load_scene_sends_byte_identical_restore_preset():
    fake = FakeSock((FX / "uc-recalled.bin").read_bytes())
    c = Client(sock_factory=lambda *a, **k: fake)
    c.sock = fake
    c.state = {}
    c.ranges = {}

    c.load_scene("MK300-FRFR")

    assert fake.tx == (FX / "uc-restore.bin").read_bytes()


def test_load_scene_appends_scene_suffix_if_missing():
    fake = FakeSock((FX / "uc-recalled.bin").read_bytes())
    c = Client(sock_factory=lambda *a, **k: fake)
    c.sock = fake
    c.state = {}
    c.ranges = {}

    c.load_scene("MK300-FRFR.scene")

    assert fake.tx == (FX / "uc-restore.bin").read_bytes()


def test_load_scene_raises_when_no_recalled_preset_arrives():
    fake = FakeSock(b"")
    c = Client(sock_factory=lambda *a, **k: fake)
    c.sock = fake
    c.state = {}
    c.ranges = {}

    with pytest.raises(SceneLoadTimeout):
        c.load_scene("MK300-FRFR")


def test_load_scene_keep_gains_resets_only_changed_channels(tmp_path):
    # Rig behaviour (docs/protocol.md): loading a scene resets preamp gains.
    # Here ch1's gain comes back at 0.0 (changed); the daemon resends PVs for
    # every channel before/around RecalledPreset -- ch2..8 stay at 0.0 too,
    # matching their "before" value, so they must NOT be re-set.
    zeroed_ch1 = ucnet.encode(
        "PV", ucnet.pv_payload("line/ch1/preampgain", 0.0), cbytes=DEVICE_REPLY_CB
    )
    recalled = (FX / "uc-recalled.bin").read_bytes()

    fake = EchoSceneSock([zeroed_ch1 + recalled])  # the ch1 echo comes from the write
    c = Client(sock_factory=lambda *a, **k: fake)
    c.sock = fake
    c.undo_journal = tmp_path / "undo.jsonl"
    c.ranges = {
        f"line/ch{n}/preampgain": {"min": 0.0, "max": 75.0, "curve": "linear"}
        for n in range(1, 9)
    }
    c.state = {"line/ch1/preampgain": 0.5, **{f"line/ch{n}/preampgain": 0.0 for n in range(2, 9)}}

    result = c.load_scene("MK300-FRFR", keep_gains=True)

    assert result["gains"] == [("line/ch1/preampgain", pytest.approx(37.5), pytest.approx(0.0))]


def test_load_scene_without_keep_gains_does_not_rewrite_anything(tmp_path):
    recalled = (FX / "uc-recalled.bin").read_bytes()
    fake = FakeSock(recalled)
    c = Client(sock_factory=lambda *a, **k: fake)
    c.sock = fake
    c.ranges = {"line/ch1/preampgain": {"min": 0.0, "max": 75.0, "curve": "linear"}}
    c.state = {"line/ch1/preampgain": 0.0}

    result = c.load_scene("MK300-FRFR")

    assert result["gains"] == []
    assert fake.tx == (FX / "uc-restore.bin").read_bytes()  # no extra PV sent


# --- final review fix wave -------------------------------------------------

import socket as _socket


class EchoSceneSock:
    """Daemon double for scene tests in the documented message order
    (docs/protocol.md: RestorePreset -> RecalledPreset, *then* the daemon
    re-sends every PV). recv() hands out queued chunks one at a time and
    raises socket.timeout when the queue is empty -- a daemon still
    connected but quiet. sendall() echoes every PV write back (queued),
    as the real daemon does."""

    def __init__(self, rx):
        self.tx = b""
        self.rx = list(rx)

    def sendall(self, b):
        self.tx += b
        for m in ucnet.Decoder().feed(b):
            if m.code == "PV":
                path, value = ucnet.parse_pv(m)
                self.rx.append(ucnet.encode(
                    "PV", ucnet.pv_payload(path, value), cbytes=DEVICE_REPLY_CB))

    def recv(self, n):
        if self.rx:
            return self.rx.pop(0)
        raise _socket.timeout()

    def settimeout(self, t):
        pass

    def close(self):
        pass


def _pv(path, value):
    return ucnet.encode("PV", ucnet.pv_payload(path, value), cbytes=DEVICE_REPLY_CB)


def _gain_client(fake, tmp_path, ch1=0.5):
    c = Client(sock_factory=lambda *a, **k: fake)
    c.sock = fake
    c.undo_journal = tmp_path / "undo.jsonl"
    c.ranges = {
        f"line/ch{n}/preampgain": {"min": 0.0, "max": 75.0, "curve": "linear"}
        for n in range(1, 9)
    }
    c.state = {"line/ch1/preampgain": ch1,
               **{f"line/ch{n}/preampgain": 0.0 for n in range(2, 9)}}
    return c


def test_keep_gains_restores_gains_zeroed_by_pvs_arriving_after_recalled_preset(tmp_path):
    # Realistic order: RecalledPreset first, then the re-sent PVs zeroing
    # gains (ch1 changes; ch2 re-sent at its unchanged value).
    recalled = (FX / "uc-recalled.bin").read_bytes()
    fake = EchoSceneSock([
        recalled,
        _pv("line/ch1/preampgain", 0.0) + _pv("line/ch2/preampgain", 0.0),
    ])
    c = _gain_client(fake, tmp_path)

    result = c.load_scene("MK300-FRFR", keep_gains=True)

    assert result["gains"] == [("line/ch1/preampgain", pytest.approx(37.5), pytest.approx(0.0))]
    assert result["failed"] == []
    assert c.state["line/ch1/preampgain"] == pytest.approx(0.5, abs=1e-4)


def test_keep_gains_waits_for_recall_pvs_split_across_segments(tmp_path):
    # The re-sent PVs trickle in over several recv()s after RecalledPreset;
    # a PV still in flight must not be taken as the echo of our re-set.
    recalled = (FX / "uc-recalled.bin").read_bytes()
    zero1 = _pv("line/ch1/preampgain", 0.0)
    zero3 = _pv("line/ch3/preampgain", 0.0)
    fake = EchoSceneSock([recalled, zero1[:7], zero1[7:], zero3])
    c = _gain_client(fake, tmp_path)
    c.state["line/ch3/preampgain"] = 0.4

    result = c.load_scene("MK300-FRFR", keep_gains=True)

    restored = [p for p, _, _ in result["gains"]]
    assert restored == ["line/ch1/preampgain", "line/ch3/preampgain"]
    assert c.state["line/ch1/preampgain"] == pytest.approx(0.5, abs=1e-4)
    assert c.state["line/ch3/preampgain"] == pytest.approx(0.4, abs=1e-4)


def test_keep_gains_settle_is_bounded_when_daemon_never_goes_quiet(tmp_path):
    import time as _time

    class ChattySock(EchoSceneSock):
        def recv(self, n):
            if self.rx:
                return self.rx.pop(0)
            return _pv("aux/ch1/volume", 0.5)  # never quiet

    recalled = (FX / "uc-recalled.bin").read_bytes()
    fake = ChattySock([recalled])
    c = _gain_client(fake, tmp_path, ch1=0.0)
    c.scene_settle_max = 0.2

    t0 = _time.monotonic()
    c.load_scene("MK300-FRFR", keep_gains=True)
    assert _time.monotonic() - t0 < 1.0


def test_keep_gains_collects_failed_channels_and_continues(tmp_path):
    recalled = (FX / "uc-recalled.bin").read_bytes()
    fake = EchoSceneSock([
        recalled,
        _pv("line/ch1/preampgain", 0.0) + _pv("line/ch2/preampgain", 0.0),
    ])
    c = _gain_client(fake, tmp_path)
    c.state["line/ch2/preampgain"] = 0.4
    real_sendall = fake.sendall

    def sendall(b):
        # swallow (no echo) the ch1 re-set only
        if b"line/ch1/preampgain" in b:
            fake.tx += b
            return
        real_sendall(b)

    fake.sendall = sendall

    result = c.load_scene("MK300-FRFR", keep_gains=True)

    assert [p for p, *_ in result["failed"]] == ["line/ch1/preampgain"]
    assert [p for p, *_ in result["gains"]] == ["line/ch2/preampgain"]
