"""UCNet framing (the protocol Universal Control speaks to ucdaemon). Pure, no I/O.

Framing is measured against a real capture (see docs/protocol.md), not the
public StudioLive UCNet hypothesis, where the two differ:

    "UC" 00 01 | size: uint16 LE | code: 2 ASCII | cbytes: 4 | payload
    size = 6 + len(payload)   (covers code + cbytes + payload)

- cbytes are the session address (docs/protocol.md, "cbytes são o
  endereço da sessão"); the daemon replies with the two pairs swapped.
  CB (68 00 65 00, encode()'s default) is the pair of the first probe,
  which only reaches the root session (MIDI endpoints). The client talks
  to the HD 8 on the device session 6a 00 69 00 (replies 69 00 6a 00) and
  sends UM on 00 00 69 00 with payload = uint16 LE UDP port only
  (see client.py: DEVICE_CB, UM_CB).
- JM: payload = uint32 LE len + JSON.
- ZM: payload = uint32 LE len + zlib body. The uint32 does NOT bound the
  zlib body's length (measured) -- decompress the whole remainder of the
  payload, never a slice of it.
- KA: keepalive, empty payload.
"""
from dataclasses import dataclass
import json
import struct
import zlib

MAGIC = b"UC\x00\x01"
CB = b"\x68\x00\x65\x00"


@dataclass
class Message:
    code: str
    cbytes: bytes
    payload: bytes


def encode(code: str, payload: bytes, cbytes: bytes = CB) -> bytes:
    return MAGIC + struct.pack("<H", 6 + len(payload)) + code.encode() + cbytes + payload


class Decoder:
    """Bufferiza dados parciais e decodifica pacotes UCNet completos.

    Bytes que não fazem parte de um pacote (lixo antes do magic) são
    descartados silenciosamente.
    """

    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes) -> list[Message]:
        self._buf += data
        out = []
        while True:
            i = self._buf.find(MAGIC)
            if i < 0:
                # Keep a tail long enough to contain a split magic.
                self._buf = self._buf[-(len(MAGIC) - 1):]
                return out
            self._buf = self._buf[i:]
            if len(self._buf) < 6:
                return out
            size = struct.unpack_from("<H", self._buf, 4)[0]
            if len(self._buf) < 6 + size:
                return out
            body, self._buf = self._buf[6:6 + size], self._buf[6 + size:]
            out.append(Message(body[:2].decode("latin1"), body[2:6], body[6:]))


def json_payload(obj) -> bytes:
    b = json.dumps(obj).encode()
    return struct.pack("<I", len(b)) + b


def compact_json_payload(obj) -> bytes:
    """Like json_payload, but with the comma/colon spacing the real UC app
    uses for JM RestorePreset -- no space after a comma (measured,
    tests/fixtures/uc-restore.bin)."""
    b = json.dumps(obj, separators=(",", ": ")).encode()
    return struct.pack("<I", len(b)) + b


def parse_json(m: Message) -> dict:
    n = struct.unpack_from("<I", m.payload)[0]
    return json.loads(m.payload[4:4 + n])


def pv_payload(path: str, value: float) -> bytes:
    return path.encode() + b"\x00\x00\x00" + struct.pack("<f", value)


def parse_pv(m: Message) -> tuple[str, float]:
    key, _, rest = m.payload.partition(b"\x00")
    return key.decode(), round(struct.unpack("<f", rest[-4:])[0], 4)


def parse_pl(m: Message) -> tuple[str, float, list[str]]:
    """Parse a PL (parameter + label list) payload.

    Measured (tests/fixtures/uc-pl.bin, docs/protocol.md): path + 0x00 +
    uint16 LE flag + float32 LE normalized value + labels joined by "\\n"
    + a trailing 0x00.
    """
    path, _, rest = m.payload.partition(b"\x00")
    value = struct.unpack_from("<f", rest, 2)[0]
    labels_blob = rest[6:]
    if labels_blob.endswith(b"\x00"):
        labels_blob = labels_blob[:-1]
    labels = labels_blob.decode().split("\n")
    return path.decode(), round(value, 4), labels


def parse_meters(packet: bytes) -> dict[str, list[int]]:
    """Parse an MS (meter) UDP packet (docs/protocol.md, "Medidores").

    Measured layout: "UC" 00 01 + 2 bytes (unidentified -- not a UCNet
    frame `size`; the value seen was the TCP port, unexplained) + "MS" +
    cbytes(4) + "levl" 00 00 + uint16 LE n + n * uint16 LE values + an
    18-byte footer (+ 1 trailing 00 byte). Unlike the rest of the packet,
    the footer's fields are big-endian (measured against
    tests/fixtures/meters-udp-1.bin: little-endian gives nonsense like
    9216/1024, big-endian gives the documented 0/36/4/36/28/7/64/2). It
    encodes 3 sections as (?, offset, count) triples at indices
    (0,1,2)/(3,4,5)/(6,7,8) -- fields 1,2 / 4,5 / 7,8 are (offset, count)
    pairs (measured: (0, 36), (36, 28), (64, 2)) for in/aux/main.

    Returns {"in": values[0:36], "aux": values[36:64], "main": values[64:66]}
    when the footer matches that known layout, else {"raw": values} (task-9
    ruling 3).
    """
    n = struct.unpack_from("<H", packet, 18)[0]
    values = list(struct.unpack_from(f"<{n}H", packet, 20))

    footer = packet[20 + 2 * n:]
    if len(footer) >= 18:
        f = struct.unpack_from(">9H", footer)
        sections = [(f[1], f[2]), (f[4], f[5]), (f[7], f[8])]
        if sections == [(0, 36), (36, 28), (64, 2)]:
            return {
                "in": values[0:36],
                "aux": values[36:64],
                "main": values[64:66],
            }
    return {"raw": values}


def parse_state(m: Message) -> dict:
    """Parse a ZM/ZB payload's zlib-compressed JSON tree.

    The leading uint32 LE in the payload is NOT the length of the zlib body
    that follows (measured against a real capture); slicing the payload by
    that value truncates the zlib stream and fails to decompress. The whole
    remainder of the payload (payload[4:]) must be handed to
    zlib.decompress as-is.
    """
    raw = zlib.decompress(m.payload[4:])
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw.decode("latin1")}
