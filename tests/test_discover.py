"""The daemon's port and the HD 8's session byte move (measured 2026-09-23)."""
import socket
from pathlib import Path

import pytest

from quantum_hd8.client import Client, daemon_ports

FX = Path(__file__).parent / "fixtures"
RX = (FX / "probe-device-rx.bin").read_bytes()

NETSTAT = """\
tcp4  0 0  127.0.0.1.62586  *.*  LISTEN  0 0 131072 131072  ucdaemon:20298  00180
tcp4  0 0  192.168.15.60.62586  *.*  LISTEN  0 0 131072 131072  ucdaemon:20298  00180
tcp4  0 0  127.0.0.1.4123  *.*  LISTEN  0 0 131072 131072  openrig:13984  00100
tcp4  0 0  127.0.0.1.62586  127.0.0.1.62596  ESTABLISHED 0 0 1 1  ucdaemon:20298 00182
"""


class Daemon:
    """Answers only on `port`, and only to a client addressing `session`."""

    def __init__(self, port, session):
        self.port, self.session, self.opened = port, session, []

    def __call__(self, addr, *a, **k):
        self.opened.append(addr[1])
        if addr[1] != self.port:
            raise ConnectionRefusedError(61, "Connection refused")
        daemon = self

        class Sock:
            tx = b""

            def sendall(self, b):
                self.tx += b

            def recv(self, n):
                if bytes([0x6a, 0, daemon.session, 0]) in self.tx and not getattr(self, "done", 0):
                    self.done = 1
                    return RX
                raise socket.timeout()

            def settimeout(self, t):
                pass

            def close(self):
                pass
        return Sock()


def ports(*p):
    return lambda: list(p)


def test_daemon_ports_reads_only_ucdaemon_on_loopback():
    run = lambda *a, **k: type("R", (), {"stdout": NETSTAT})()
    assert daemon_ports(run) == [62586]


def test_connect_finds_a_moved_port_and_session():
    d = Daemon(62586, 0x66)
    c = Client(sock_factory=d, discover=True, ports_finder=ports(62586))
    c.connect()
    assert (c.port, c.device_cb) == (62586, b"\x6a\x00\x66\x00")
    assert c.get("line/ch1/preampgain") == 0.25999999046325684
    c.close()


def test_connect_keeps_the_defaults_when_they_answer():
    d = Daemon(59791, 0x69)
    c = Client(sock_factory=d, discover=True, ports_finder=ports())
    c.connect()
    assert d.opened == [59791] and c.device_cb == b"\x6a\x00\x69\x00"
    c.close()


def test_no_discovery_with_an_injected_socket_by_default():
    d = Daemon(62586, 0x66)
    with pytest.raises(ConnectionRefusedError):
        Client(sock_factory=d, ports_finder=ports(62586)).connect()


def test_nothing_answers_is_a_timeout_and_keeps_the_defaults():
    d = Daemon(62586, 0x7f)
    c = Client(sock_factory=d, discover=True, ports_finder=ports(62586))
    with pytest.raises(TimeoutError):
        c.connect()
    assert (c.port, c.device_cb) == (59791, b"\x6a\x00\x69\x00")
