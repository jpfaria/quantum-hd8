import json
from pathlib import Path

from quantum_hd8 import ucnet
from quantum_hd8.client import Client, flatten

FX = Path(__file__).parent / "fixtures"


class FakeSock:
    def __init__(self, rx: bytes):
        self.rx, self.tx = [rx], b""

    def sendall(self, b):
        self.tx += b

    def recv(self, n):
        return self.rx.pop(0) if self.rx else b""

    def settimeout(self, t):
        pass

    def close(self):
        pass


def test_flatten_walks_children_and_values():
    tree = {
        "children": {
            "line": {
                "children": {
                    "ch1": {"values": {"volume": -3, "preampgain": 12.0}}
                }
            }
        }
    }
    assert flatten(tree) == {
        "line/ch1/volume": -3,
        "line/ch1/preampgain": 12.0,
    }


def test_flatten_matches_real_synchronize_fixture():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    [zm, *_] = ucnet.Decoder().feed(rx)
    tree = ucnet.parse_state(zm)
    flat = flatten(tree)

    params = json.loads((Path(__file__).parent.parent / "quantum_hd8" / "params.json").read_text())
    expected_paths = {p["path"] for p in params}

    assert set(flat.keys()) == expected_paths
    assert flat["line/ch1/preampgain"] == 0.25999999046325684


def test_connect_reads_state_sends_expected_bytes_and_gets_values():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    fake = FakeSock(rx)
    c = Client(sock_factory=lambda *a, **k: fake)

    state = c.connect()

    assert state["line/ch1/preampgain"] == 0.25999999046325684
    assert c.get("line/ch1/preampgain") == 0.25999999046325684
    assert fake.tx == (FX / "probe-device-tx.bin").read_bytes()
    c.close()


def test_connect_parses_scene_list():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    fake = FakeSock(rx)
    c = Client(sock_factory=lambda *a, **k: fake)

    c.connect()

    assert c.scenes[0] == "ELEMENT.scene"
    c.close()


def test_human_formats_linear_curve_with_units():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    fake = FakeSock(rx)
    c = Client(sock_factory=lambda *a, **k: fake)
    c.connect()

    assert c.human("line/ch1/preampgain") == "19.5 dB"
    c.close()


def test_human_notes_normalized_for_non_linear_curve():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    fake = FakeSock(rx)
    c = Client(sock_factory=lambda *a, **k: fake)
    c.connect()

    assert "normalizado" in c.human("line/ch1/volume")
    c.close()


def test_get_unknown_path_raises_key_error():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    fake = FakeSock(rx)
    c = Client(sock_factory=lambda *a, **k: fake)
    c.connect()

    try:
        c.get("line/ch1/does-not-exist")
        assert False, "should have raised"
    except KeyError:
        pass
    c.close()
