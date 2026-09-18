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
    echo_ch1 = ucnet.encode(
        "PV", ucnet.pv_payload("line/ch1/preampgain", 0.5), cbytes=DEVICE_REPLY_CB
    )

    fake = FakeSock([zeroed_ch1 + recalled, echo_ch1])
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
