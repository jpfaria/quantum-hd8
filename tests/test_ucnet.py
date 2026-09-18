import json
import struct
import zlib
from pathlib import Path

import pytest

from quantum_hd8 import ucnet

FX = Path(__file__).parent / "fixtures"


def test_roundtrip_ka():
    raw = ucnet.encode("KA", b"")
    [m] = ucnet.Decoder().feed(raw)
    assert (m.code, m.payload) == ("KA", b"")


def test_split_packet_is_buffered():
    raw = ucnet.encode("JM", ucnet.json_payload({"id": "X"}))
    d = ucnet.Decoder()
    assert d.feed(raw[:5]) == []
    [m] = d.feed(raw[5:])
    assert ucnet.parse_json(m) == {"id": "X"}


def test_pv_roundtrip():
    m = ucnet.Decoder().feed(ucnet.encode("PV", ucnet.pv_payload("line/ch1/volume", -12.5)))[0]
    assert ucnet.parse_pv(m) == ("line/ch1/volume", -12.5)


def test_live_subscribe_capture():
    msgs = ucnet.Decoder().feed((FX / "probe-subscribe-rx.bin").read_bytes())
    assert [m.code for m in msgs] == ["ZM", "JM"]

    zm, jm = msgs
    assert ucnet.parse_state(zm) == {"id": "UpdateMidiEndpoints", "endpointList": []}
    assert ucnet.parse_json(jm) == {"id": "SubscriptionReply"}
    assert zm.cbytes == b"\x65\x00\x68\x00"
    assert jm.cbytes == b"\x65\x00\x68\x00"


def test_parse_state_decompresses_whole_payload_not_sliced_by_uint32():
    # The uint32 length prefix in a ZM payload is NOT the length of the zlib
    # body that follows it (measured in docs/protocol.md). It must not be
    # used to slice the zlib data; the whole remainder of the payload has to
    # be handed to zlib.decompress as-is.
    obj = {"id": "State", "tree": {"line/ch1/preampgain": 12.0}}
    json_bytes = json.dumps(obj).encode()
    raw = zlib.compress(json_bytes)
    uncompressed_len = len(json_bytes)
    # Sanity: for real payloads the uncompressed length differs from the
    # compressed (zlib body) length, so slicing payload[4:4+uncompressed_len]
    # would not equal payload[4:].
    assert uncompressed_len != len(raw)

    payload = struct.pack("<I", uncompressed_len) + raw
    m = ucnet.Message(code="ZM", cbytes=ucnet.CB, payload=payload)

    # Correct behavior: decompress the whole remainder.
    assert ucnet.parse_state(m) == obj

    # Wrong behavior (slicing by the uint32 field) would corrupt/truncate
    # the zlib stream and fail to decompress it.
    with pytest.raises(zlib.error):
        zlib.decompress(payload[4:4 + uncompressed_len])


def test_decoder_skips_garbage_before_magic():
    raw = ucnet.encode("KA", b"")
    d = ucnet.Decoder()
    [m] = d.feed(b"\x00garbage\xffbytes" + raw)
    assert (m.code, m.payload) == ("KA", b"")


def test_parse_pv_against_real_write_capture():
    # tests/fixtures/uc-pvwrite.bin: PV the real UC app wrote for
    # global/mixerMode, value 0.0 (docs/protocol.md, "Escrita").
    [m] = ucnet.Decoder().feed((FX / "uc-pvwrite.bin").read_bytes())
    assert m.code == "PV"
    assert ucnet.parse_pv(m) == ("global/mixerMode", 0.0)


def test_parse_pl_splits_path_value_and_labels():
    # tests/fixtures/uc-pl.bin: path + 00 + uint16 LE flag + float32 LE
    # value + labels joined by "\n" + 00 (measured, docs/protocol.md).
    msgs = ucnet.Decoder().feed((FX / "uc-pl.bin").read_bytes())
    pl = next(m for m in msgs if m.code == "PL")
    path, value, labels = ucnet.parse_pl(pl)
    assert path == "global/phones1_src"
    assert value == 0.0
    assert labels[:2] == ["Main L/R", "Out  3/4"]
    assert labels[-1] == "Loopback  2"


def test_parse_pl_nonzero_value_and_short_label_list():
    msgs = ucnet.Decoder().feed((FX / "uc-pl.bin").read_bytes())
    pl = next(m for m in msgs if b"mainOutVolumeLink" in m.payload)
    path, value, labels = ucnet.parse_pl(pl)
    assert path == "global/mainOutVolumeLink"
    assert value == 0.3333
    assert labels == ["None", "All", "1-2", "1-4", "1-6", "1-8", "All + ADAT"]
