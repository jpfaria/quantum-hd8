import struct
from pathlib import Path

from quantum_hd8 import ucnet
from quantum_hd8.client import Client

FX = Path(__file__).parent / "fixtures"


def test_parse_meters_splits_in_aux_main_from_real_fixture():
    packet = (FX / "meters-udp-1.bin").read_bytes()

    m = ucnet.parse_meters(packet)

    assert len(m["in"]) == 36
    assert len(m["aux"]) == 28
    assert len(m["main"]) == 2
    # MK-300 return noise on ADAT 6/7 (docs/protocol.md, "Medidores").
    assert m["in"][15] > 100
    assert m["in"][16] > 100
    assert all(v <= 10 for v in m["in"][0:10])
    assert all(v == 0 for v in m["aux"])
    assert all(v == 0 for v in m["main"])


def _build_ms_packet(values: list[int], footer: bytes) -> bytes:
    header = ucnet.MAGIC + b"\x00\x00" + b"MS" + b"\x69\x00\x6a\x00" + b"levl\x00\x00"
    body = struct.pack("<H", len(values)) + struct.pack(f"<{len(values)}H", *values)
    return header + body + footer


def test_parse_meters_falls_back_to_raw_when_footer_does_not_match():
    values = list(range(5))
    packet = _build_ms_packet(values, footer=b"\x00" * 18)

    m = ucnet.parse_meters(packet)

    assert m == {"raw": values}


class FakeUdpSock:
    def __init__(self, port: int, packets):
        self._port = port
        self._packets = list(packets)
        self.closed = False

    def getsockname(self):
        return ("127.0.0.1", self._port)

    def settimeout(self, t):
        pass

    def recv(self, n):
        return self._packets.pop(0)

    def close(self):
        self.closed = True


class FakeTcpSock:
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


def test_connect_without_udp_factory_never_binds_a_socket_and_announces_legacy_port():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    tcp = FakeTcpSock(rx)
    c = Client(sock_factory=lambda *a, **k: tcp)

    c.connect()

    assert c.udp_sock is None
    # First message sent is UM: magic(4) + size(2) + "UM" + cbytes(4) + payload.
    assert tcp.tx[:4] == ucnet.MAGIC
    assert tcp.tx[6:8] == b"UM"
    port = struct.unpack_from("<H", tcp.tx, 12)[0]
    assert port == 47809
    c.close()


def test_connect_with_udp_factory_announces_the_socket_real_port():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    tcp = FakeTcpSock(rx)
    fake_udp = FakeUdpSock(port=55123, packets=[])
    c = Client(sock_factory=lambda *a, **k: tcp, udp_factory=lambda: fake_udp)

    c.connect()

    assert c.udp_sock is fake_udp
    port = struct.unpack_from("<H", tcp.tx, 12)[0]
    assert port == 55123
    c.close()
    assert fake_udp.closed


def test_read_meters_parses_one_packet_from_the_udp_socket():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    tcp = FakeTcpSock(rx)
    meter_packet = (FX / "meters-udp-1.bin").read_bytes()
    fake_udp = FakeUdpSock(port=55123, packets=[meter_packet])
    c = Client(sock_factory=lambda *a, **k: tcp, udp_factory=lambda: fake_udp)
    c.connect()

    m = c.read_meters()

    assert len(m["in"]) == 36
    c.close()


def test_read_meters_without_udp_factory_raises():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    tcp = FakeTcpSock(rx)
    c = Client(sock_factory=lambda *a, **k: tcp)
    c.connect()

    try:
        c.read_meters()
        assert False, "should have raised"
    except RuntimeError:
        pass
    c.close()


def test_meter_labels_uses_the_synchronize_username_per_channel():
    rx = (FX / "probe-device-rx.bin").read_bytes()
    tcp = FakeTcpSock(rx)
    c = Client(sock_factory=lambda *a, **k: tcp)
    c.connect()

    labels = c.meter_labels()

    assert labels["in"][0] == "In  1"  # line/ch1/username, measured
    assert labels["in"][15] == "ADAT  6"  # line/ch16/username, measured
    assert labels["in"][35] == "USB  10"  # line/ch36/username, measured
    assert labels["aux"][0] == "aux/ch1 L"
    assert labels["aux"][1] == "aux/ch1 R"
    assert labels["main"] == ["main L", "main R"]
    c.close()


def test_meter_labels_falls_back_to_the_path_when_state_has_no_username():
    c = Client(sock_factory=lambda *a, **k: None)  # never connect()ed -- empty state

    labels = c.meter_labels()

    assert labels["in"][0] == "line/ch1"
    assert labels["in"][35] == "line/ch36"
