import json

from quantum_hd8.cli import main
from quantum_hd8.client import SceneLoadTimeout, WriteNotConfirmed


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


class WriteFakeClient:
    """Stands in for Client in `set`/`undo`/`scene` CLI tests."""

    closed = False

    def __init__(self, *a, **k):
        from quantum_hd8 import undo as _undo
        self.undo_journal = k.get("undo_journal") or _undo.DEFAULT_JOURNAL
        self.set_calls = []
        self.set_raw_calls = []
        self.load_scene_calls = []

    def connect(self):
        return {}

    def set(self, path, value):
        self.set_calls.append((path, value))
        if path == "line/ch1/preampgain" and value == 80:
            raise ValueError(f"{path}: {value} fora da faixa [0.0, 75.0]")
        if path == "line/ch1/clip":
            raise PermissionError(f"{path} é readonly")
        if path == "global/ledBrightness" and value == 0.9:
            raise WriteNotConfirmed(path)
        return 0.5

    def set_raw(self, path, normalized):
        self.set_raw_calls.append((path, normalized))
        return normalized

    def load_scene(self, name, keep_gains=False):
        self.load_scene_calls.append((name, keep_gains))
        if name == "TIMEOUT":
            raise SceneLoadTimeout(name)
        gains = [("line/ch1/preampgain", 37.5, 0.0)] if keep_gains else []
        return {"preset_file": f"{name}.scene" if not name.endswith(".scene") else name,
                "gains": gains}

    def close(self):
        self.closed = True


def test_set_prints_echoed_value(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    assert main(["set", "line/ch1/preampgain", "37.5"]) == 0
    assert "line/ch1/preampgain = 0.5" in capsys.readouterr().out


def test_set_out_of_range_prints_error_and_returns_1(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["set", "line/ch1/preampgain", "80"])
    assert rc == 1
    assert "fora da faixa" in capsys.readouterr().err


def test_set_readonly_prints_error_and_returns_1(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["set", "line/ch1/clip", "1"])
    assert rc == 1
    assert "readonly" in capsys.readouterr().err


def test_set_not_confirmed_prints_error_and_returns_1(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["set", "global/ledBrightness", "0.9"])
    assert rc == 1
    assert "global/ledBrightness" in capsys.readouterr().err


def test_set_accepts_on_off_for_toggles(monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    client_instances = []
    orig_init = WriteFakeClient.__init__

    def capture_init(self, *a, **k):
        orig_init(self, *a, **k)
        client_instances.append(self)

    monkeypatch.setattr(WriteFakeClient, "__init__", capture_init)

    assert main(["set", "line/ch1/48v", "on"]) == 0

    assert client_instances[0].set_calls == [("line/ch1/48v", 1)]


def test_undo_pops_and_sets_raw(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    from quantum_hd8 import undo as undo_mod
    monkeypatch.setattr("quantum_hd8.cli.undo.DEFAULT_JOURNAL", tmp_path / "undo.jsonl")
    undo_mod.record("line/ch1/preampgain", 0.26, 0.5, journal=tmp_path / "undo.jsonl")

    rc = main(["undo"])

    assert rc == 0
    assert "line/ch1/preampgain" in capsys.readouterr().out


def test_undo_with_empty_journal_prints_message(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    monkeypatch.setattr("quantum_hd8.cli.undo.DEFAULT_JOURNAL", tmp_path / "undo.jsonl")

    rc = main(["undo"])

    assert rc == 0
    assert "nada para desfazer" in capsys.readouterr().out


def test_scene_list_prints_scenes(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    assert main(["scene", "list"]) == 0
    out = capsys.readouterr().out
    assert "ELEMENT.scene" in out
    assert "PEDAIS-SYN2-5050.scene" in out


def test_scene_load_prints_nothing_extra_without_keep_gains(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["scene", "load", "MK300-FRFR"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_scene_load_keep_gains_prints_before_after(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["scene", "load", "MK300-FRFR", "--keep-gains"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "line/ch1/preampgain" in out
    assert "37.5" in out
    assert "0.0" in out


def test_scene_load_timeout_prints_error_and_returns_1(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["scene", "load", "TIMEOUT"])
    assert rc == 1
    assert "TIMEOUT" in capsys.readouterr().err


def test_scene_save_not_implemented(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["scene", "save", "whatever"])
    assert rc == 2
    assert "não capturado" in capsys.readouterr().err
