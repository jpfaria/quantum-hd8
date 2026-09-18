import json
import socket
from pathlib import Path

from quantum_hd8 import ucnet
from quantum_hd8.client import Client, flatten

FX = Path(__file__).parent / "fixtures"

# Offset in probe-device-rx.bin where the ZM+JM messages end and the FD
# (scene list) message starts -- computed from the fixture's own framed
# message sizes (magic+size header), not hardcoded blindly.
_FD_OFFSET_IN_PROBE_DEVICE_RX = 2881


class FakeSock:
    """A TCP-like socket whose recv() hands back one queued chunk at a time.

    A single chunk (bytes) is wrapped as a one-item queue -- one recv()
    returns everything, as most existing tests want. Pass a list of chunks
    to simulate data arriving split across several TCP segments (recv()
    calls), including a byte-by-byte split.
    """

    def __init__(self, rx):
        self.rx = [rx] if isinstance(rx, (bytes, bytearray)) else list(rx)
        self.tx = b""

    def sendall(self, b):
        self.tx += b

    def recv(self, n):
        return self.rx.pop(0) if self.rx else b""

    def settimeout(self, t):
        pass

    def close(self):
        pass


class TimeoutSock(FakeSock):
    """Like FakeSock, but recv() raises socket.timeout once the queue is
    empty instead of returning b"" (closed connection) -- simulating a
    daemon that is still connected but has nothing more to say."""

    def recv(self, n):
        if self.rx:
            return self.rx.pop(0)
        raise socket.timeout()


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


def test_connect_waits_for_fd_delivered_in_a_later_tcp_segment():
    # Regression: FD (scene list) arriving in a separate recv() call after
    # the one carrying ZM/JM must not be dropped just because connect()
    # already saw the state and returned.
    rx = (FX / "probe-device-rx.bin").read_bytes()
    fake = FakeSock([rx[:_FD_OFFSET_IN_PROBE_DEVICE_RX], rx[_FD_OFFSET_IN_PROBE_DEVICE_RX:]])
    c = Client(sock_factory=lambda *a, **k: fake)

    c.connect()

    assert c.scenes[0] == "ELEMENT.scene"
    c.close()


def test_connect_waits_for_fd_split_byte_by_byte():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    fake = FakeSock([rx[i:i + 1] for i in range(len(rx))])
    c = Client(sock_factory=lambda *a, **k: fake)

    c.connect()

    assert c.state["line/ch1/preampgain"] == 0.25999999046325684
    assert c.scenes[0] == "ELEMENT.scene"
    c.close()


def test_connect_gives_up_on_fd_after_timeout_instead_of_hanging():
    # FD never arrives (only ZM+JM); connect() must not hang forever --
    # it returns once connect_timeout elapses, with scenes still empty.
    rx = (FX / "probe-device-rx.bin").read_bytes()
    fake = TimeoutSock([rx[:_FD_OFFSET_IN_PROBE_DEVICE_RX]])
    c = Client(sock_factory=lambda *a, **k: fake, connect_timeout=0.05)

    state = c.connect()

    assert state["line/ch1/preampgain"] == 0.25999999046325684
    assert c.scenes == []
    c.close()


def test_connect_feeds_raw_sink_every_recv_chunk():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    chunks = [rx[:_FD_OFFSET_IN_PROBE_DEVICE_RX], rx[_FD_OFFSET_IN_PROBE_DEVICE_RX:]]
    fake = FakeSock(list(chunks))
    seen = []
    c = Client(sock_factory=lambda *a, **k: fake, raw_sink=seen.append)

    c.connect()

    assert seen == chunks
    c.close()


def test_events_feeds_raw_sink_every_recv_chunk():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    pl = (FX / "uc-pl.bin").read_bytes()
    fake = FakeSock([rx, pl])
    seen = []
    c = Client(sock_factory=lambda *a, **k: fake, raw_sink=seen.append)
    c.connect()
    seen.clear()

    list(c.events(timeout=0))

    assert seen == [pl]
    c.close()


def test_events_yields_pl_updates_lists_and_state():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    pl = (FX / "uc-pl.bin").read_bytes()
    fake = FakeSock([rx, pl])
    c = Client(sock_factory=lambda *a, **k: fake)
    c.connect()

    events = list(c.events(timeout=0))

    assert ("global/phones1_src", 0.0) in events
    assert c.lists["global/phones1_src"][:2] == ["Main L/R", "Out  3/4"]
    assert c.lists["global/phones1_src"][-1] == "Loopback  2"
    assert c.state["global/phones1_src"] == 0.0
    c.close()
