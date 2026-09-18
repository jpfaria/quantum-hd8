import json

from quantum_hd8.cli import main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "quantum-hd8" in capsys.readouterr().out


class RawSinkFakeClient:
    """Like FakeClient, but records the raw_sink it was constructed with so
    listen --raw can feed it bytes, and its events() writes through it --
    matching how the real Client's events() calls raw_sink per recv chunk."""

    closed = False

    def __init__(self, *a, raw_sink=None, **k):
        self.raw_sink = raw_sink

    def connect(self):
        return {}

    def events(self, timeout=None):
        chunks = [b"chunk-one", b"chunk-two"]
        for chunk in chunks:
            if self.raw_sink is not None:
                self.raw_sink(chunk)
        yield "line/ch1/volume", -6.0

    def close(self):
        self.closed = True


class FakeClient:
    """Stands in for quantum_hd8.client.Client in CLI tests."""

    state = {
        "line/ch1/volume": -3.0,
        "main/ch1/mute": 0.0,
        "main/ch1/volume": 0.5,
        "global/phones1_src": 0.0,
        "global/phones2_src": 0.0,
        "global/spdifSource": 0.0,
        **{
            f"line/ch{ch}/{key}": v
            for ch in range(1, 9)
            for key, v in (("preampgain", 0.25999999046325684), ("48v", 0.0), ("pad", 0.0))
        },
    }
    ranges = {"line/ch1/preampgain": {"min": 0.0, "max": 75.0, "curve": "linear"}}
    scenes = ["ELEMENT.scene", "PEDAIS-SYN2-5050.scene"]
    # global/spdifSource has no known labels -- state must fall back to raw.
    lists = {
        "global/phones1_src": ["Main L/R", "Out  3/4", "Out  5/6"],
        "global/phones2_src": ["Main L/R", "Out  3/4", "Out  5/6"],
    }
    closed = False

    def __init__(self, *a, **k):
        pass

    def connect(self):
        return self.state

    def get(self, path):
        return self.state[path]

    def human(self, path):
        v = self.get(path)
        r = self.ranges.get(path)
        if not r or r.get("curve") != "linear":
            return f"{v} (normalizado)"
        lo, hi = r["min"], r["max"]
        return f"{lo + v * (hi - lo):.1f} dB"

    def events(self, timeout=None):
        yield "line/ch1/volume", -6.0
        yield "main/ch1/mute", 1.0

    def close(self):
        self.closed = True


def test_dump_prints_sorted_json(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    assert main(["dump"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data == FakeClient.state
    assert list(data.keys()) == sorted(FakeClient.state.keys())


def test_dump_writes_to_file(tmp_path, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    out_file = tmp_path / "a.json"
    assert main(["dump", "--out", str(out_file)]) == 0
    data = json.loads(out_file.read_text())
    assert data == FakeClient.state


def test_get_known_path(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    assert main(["get", "line/ch1/preampgain"]) == 0
    assert "0.25999999046325684" in capsys.readouterr().out


def test_get_unknown_path_suggests_close_matches(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    rc = main(["get", "line/ch1/preampgan"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "line/ch1/preampgain" in err


def test_state_shows_summary(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    assert main(["state"]) == 0
    out = capsys.readouterr().out
    assert "19.5 dB" in out  # line/ch1 preamp gain, human
    assert "ELEMENT.scene" in out


def test_state_shows_source_label_when_lists_known(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    assert main(["state"]) == 0
    out = capsys.readouterr().out
    # global/phones1_src = 0.0, lists[...][0] == "Main L/R" -- index 0.
    assert "phones1_src = Main L/R" in out


def test_state_falls_back_to_raw_when_lists_unknown(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    assert main(["state"]) == 0
    out = capsys.readouterr().out
    # global/spdifSource isn't in FakeClient.lists -- raw value, not a label.
    assert "spdifSource = 0.0" in out


def test_listen_prints_events(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    assert main(["listen"]) == 0
    out = capsys.readouterr().out
    assert "line/ch1/volume = -6.0" in out
    assert "main/ch1/mute = 1.0" in out


def test_listen_raw_writes_every_chunk_to_file(tmp_path, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", RawSinkFakeClient)
    out_file = tmp_path / "raw.bin"

    assert main(["listen", "--raw", str(out_file)]) == 0

    assert out_file.read_bytes() == b"chunk-onechunk-two"
