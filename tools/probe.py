"""Read-only probe of ucdaemon: connect, optionally say hello + subscribe, dump raw bytes.

    python3 tools/probe.py listen      # connect and read 5 s without sending anything
    python3 tools/probe.py subscribe   # UM hello + JM Subscribe, keepalive, read 5 s
"""
import json
import socket
import struct
import sys
import time
from pathlib import Path

FX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
CB = b"\x68\x00\x65\x00"


def pkt(code: bytes, payload: bytes) -> bytes:
    return b"UC\x00\x01" + struct.pack("<H", 6 + len(payload)) + code + CB + payload


def jm(obj) -> bytes:
    body = json.dumps(obj).encode()
    return pkt(b"JM", struct.pack("<I", len(body)) + body)


def main(mode: str):
    s = socket.create_connection(("127.0.0.1", 59791), timeout=1)
    sent = b""
    if mode == "subscribe":
        sent += pkt(b"UM", b"\x00\x00" + struct.pack("<H", 47809))
        sent += jm({"id": "Subscribe", "clientName": "quantum-hd8",
                    "clientInternalName": "quantumhd8", "clientType": "Mac",
                    "clientDescription": "quantum-hd8 probe",
                    "clientIdentifier": "quantum-hd8-probe",
                    "clientOptions": "perm users levl redu rtan", "clientEncoding": 23117})
        s.sendall(sent)
    rx = b""
    end = time.time() + 5
    while time.time() < end:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            if mode == "subscribe":
                s.sendall(pkt(b"KA", b""))
            continue
        if not chunk:
            break
        rx += chunk
    s.close()
    FX.mkdir(parents=True, exist_ok=True)
    (FX / f"probe-{mode}-tx.bin").write_bytes(sent)
    (FX / f"probe-{mode}-rx.bin").write_bytes(rx)
    print(f"sent {len(sent)} B, got {len(rx)} B")
    print(rx[:256])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "listen")
