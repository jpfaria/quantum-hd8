import io
import json
import socket
import sys

from quantum_hd8 import ucnet
from quantum_hd8.cli import main
from quantum_hd8.client import SceneLoadTimeout, SceneSaveTimeout, WriteNotConfirmed


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
        "global/mixerMode": 0.5,
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


def test_state_shows_mixer_mode_label(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    assert main(["state"]) == 0
    out = capsys.readouterr().out
    # global/mixerMode = 0.5 -> index 1 of 3 labels -> "Analog + ADAT".
    assert "mixer: Analog + ADAT" in out


def test_state_shows_source_label_when_lists_known(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    assert main(["state"]) == 0
    out = capsys.readouterr().out
    # global/phones1_src = 0.0, lists[...][0] == "Main L/R" -- index 0.
    assert "phones1_src = Main L/R" in out


def test_state_falls_back_to_static_labels_when_lists_unknown(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", FakeClient)
    assert main(["state"]) == 0
    out = capsys.readouterr().out
    # global/spdifSource isn't in FakeClient.lists (the daemon sends no PL at
    # subscribe) -- falls back to STATIC_LABELS, like `route` does.
    assert "spdifSource = Main L/R" in out


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
    scenes = ["ELEMENT.scene", "PEDAIS-SYN2-5050.scene"]

    def __init__(self, *a, **k):
        from quantum_hd8 import undo as _undo
        self.undo_journal = k.get("undo_journal") or _undo.DEFAULT_JOURNAL
        self.set_calls = []
        self.set_raw_calls = []
        self.load_scene_calls = []
        self.save_scene_calls = []

    def connect(self):
        return {}

    # path -> (min, max, type), mirrors the linear-curve rows a real params.json
    # (+ daemon ranges fallback) would report -- see Client._curve_and_range.
    _LINEAR = {
        "line/ch1/preampgain": (0.0, 75.0, "float"),
        "global/ledBrightness": (1.0, 100.0, "int"),
    }

    def set(self, path, value):
        self.set_calls.append((path, value))
        if path.startswith("nope/"):
            raise KeyError(f"caminho desconhecido: {path}")
        if path == "line/ch1/preampgain" and value == 80:
            raise ValueError(f"{path}: {value} fora da faixa [0.0, 75.0]")
        if path == "line/ch1/clip":
            raise PermissionError(f"{path} é readonly")
        if path == "global/ledBrightness" and value == 0.9:
            raise WriteNotConfirmed(path)
        if path in self._LINEAR:
            lo, hi, _ = self._LINEAR[path]
            return (value - lo) / (hi - lo)
        return 0.5

    def set_raw(self, path, normalized):
        self.set_raw_calls.append((path, normalized))
        return normalized

    def to_human(self, path, normalized):
        if path in self._LINEAR:
            lo, hi, _ = self._LINEAR[path]
            return lo + normalized * (hi - lo)
        return None

    _TYPES = {"main/ch1/volume": "float", "line/ch1/username": "string"}

    def param_row(self, path):
        if path.startswith("nope/"):
            raise KeyError(f"caminho desconhecido: {path}")
        if path in self._TYPES:
            return {"type": self._TYPES[path]}
        if path in self._LINEAR:
            return {"type": self._LINEAR[path][2]}
        return {"type": "toggle"}

    def load_scene(self, name, keep_gains=False, keep_mode=False):
        self.load_scene_calls.append((name, keep_gains, keep_mode))
        if name == "TIMEOUT":
            raise SceneLoadTimeout(name)
        if name == "WNC":
            raise WriteNotConfirmed("line/ch1/preampgain: o daemon não confirmou a escrita em 1 s")
        gains = [("line/ch1/preampgain", 37.5, 0.0)] if keep_gains else []
        failed = []
        if name == "PARTIAL":
            failed = [("line/ch2/preampgain", 30.0, 0.0,
                       "line/ch2/preampgain: o daemon não confirmou a escrita em 1 s")]
        changed_globals = []
        mode_restored = None
        if name == "MODE":
            changed_globals = [("global/mixerMode", 0.0, 0.5)]
            if keep_mode:
                mode_restored = ("global/mixerMode", 0.5, 0.0)
        return {"preset_file": f"{name}.scene" if not name.endswith(".scene") else name,
                "gains": gains, "failed": failed,
                "changed_globals": changed_globals, "mode_restored": mode_restored}

    def save_scene(self, name):
        self.save_scene_calls.append(name)
        if name == "TIMEOUT":
            raise SceneSaveTimeout(name)
        preset_file = name if name.endswith(".scene") else f"{name}.scene"
        return f"scene/{preset_file}"

    def close(self):
        self.closed = True


def test_set_prints_raw_echoed_value_when_not_linear(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    assert main(["set", "line/ch1/48v", "1"]) == 0
    assert "line/ch1/48v = 0.5" in capsys.readouterr().out


def test_set_prints_human_units_for_linear_float_param(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    assert main(["set", "line/ch1/preampgain", "37.5"]) == 0
    assert "line/ch1/preampgain = 37.5 (0.500)" in capsys.readouterr().out


def test_set_prints_human_units_rounded_for_linear_int_param(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    assert main(["set", "global/ledBrightness", "20"]) == 0
    assert "global/ledBrightness = 20 (0.192)" in capsys.readouterr().out


def test_set_out_of_range_prints_error_and_returns_2(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["set", "line/ch1/preampgain", "80"])
    assert rc == 2
    assert "fora da faixa" in capsys.readouterr().err


def test_set_readonly_prints_error_and_returns_2(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["set", "line/ch1/clip", "1"])
    assert rc == 2
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


def test_scene_load_warns_on_stderr_when_global_param_changed(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["scene", "load", "MODE"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "global/mixerMode: Mixer Bypass -> Analog + ADAT" in err


def test_scene_load_keep_mode_passes_flag_and_prints_restore(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    instances = _capture_instances(monkeypatch, WriteFakeClient)
    rc = main(["scene", "load", "MODE", "--keep-mode"])
    assert rc == 0
    assert instances[0].load_scene_calls == [("MODE", False, True)]
    err = capsys.readouterr().err
    assert "global/mixerMode: Mixer Bypass -> Analog + ADAT" in err
    assert "global/mixerMode: restaurado para Mixer Bypass" in err


def test_scene_load_timeout_prints_error_and_returns_1(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["scene", "load", "TIMEOUT"])
    assert rc == 1
    assert "TIMEOUT" in capsys.readouterr().err


def test_scene_save_prints_success(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    instances = _capture_instances(monkeypatch, WriteFakeClient)
    rc = main(["scene", "save", "NEWSCENE"])
    assert rc == 0
    assert instances[0].save_scene_calls == ["NEWSCENE"]
    assert "cena salva: NEWSCENE" in capsys.readouterr().out


def test_scene_save_refuses_existing_scene_without_overwrite(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    instances = _capture_instances(monkeypatch, WriteFakeClient)
    rc = main(["scene", "save", "ELEMENT.scene"])
    assert rc == 2
    assert instances[0].save_scene_calls == []
    err = capsys.readouterr().err
    assert "ELEMENT.scene" in err


def test_scene_save_refuses_existing_scene_by_bare_name(capsys, monkeypatch):
    # "ELEMENT" (no .scene suffix) must also be recognized as a clash with
    # the stored "ELEMENT.scene".
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    instances = _capture_instances(monkeypatch, WriteFakeClient)
    rc = main(["scene", "save", "ELEMENT"])
    assert rc == 2
    assert instances[0].save_scene_calls == []


def test_scene_save_overwrite_flag_allows_existing_scene(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    instances = _capture_instances(monkeypatch, WriteFakeClient)
    rc = main(["scene", "save", "ELEMENT.scene", "--overwrite"])
    assert rc == 0
    assert instances[0].save_scene_calls == ["ELEMENT.scene"]
    assert "cena salva: ELEMENT.scene" in capsys.readouterr().out


def test_scene_save_timeout_prints_error_and_returns_1(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["scene", "save", "TIMEOUT"])
    assert rc == 1
    assert "TIMEOUT" in capsys.readouterr().err


class MetersFakeClient:
    """Stands in for Client for `meters` CLI tests. read_meters() yields one
    snapshot then raises KeyboardInterrupt, like Ctrl-C stopping the redraw
    loop -- so the loop body runs exactly once in a test."""
    closed = False

    def __init__(self, *a, **k):
        self._calls = 0

    def connect(self):
        return {}

    def meter_labels(self):
        return {"in": ["In  1", "In  2"], "aux": ["aux/ch1 L", "aux/ch1 R"], "main": ["main L", "main R"]}

    def read_meters(self, timeout=1.0):
        self._calls += 1
        if self._calls > 1:
            raise KeyboardInterrupt
        return {"in": [5, 0], "aux": [0, 0], "main": [0, 0]}

    def close(self):
        self.closed = True


def test_meters_once_prints_json_snapshot_by_label(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", MetersFakeClient)
    rc = main(["meters", "--once"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "in": {"In  1": {"raw": 5, "dbfs": round(ucnet.meter_dbfs(5), 1)},
               "In  2": {"raw": 0, "dbfs": None}},
        "aux": {"aux/ch1 L": {"raw": 0, "dbfs": None}, "aux/ch1 R": {"raw": 0, "dbfs": None}},
        "main": {"main L": {"raw": 0, "dbfs": None}, "main R": {"raw": 0, "dbfs": None}},
    }


def test_meters_prints_header_and_one_line_per_channel_until_interrupted(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", MetersFakeClient)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    rc = main(["meters"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dBFS = 20·log10(cru/65535), calibrado 18/09" in out
    assert f"In  1: 5 ({ucnet.meter_dbfs(5):.1f} dBFS)" in out
    assert "aux/ch1 L: 0 (-inf dBFS)" in out
    assert "main L: 0 (-inf dBFS)" in out


class TimeoutThenValueMetersClient:
    """read_meters times out twice, then returns one snapshot, then raises
    KeyboardInterrupt (Ctrl-C) -- exercises the fix-round-1 requirement
    that a lone socket.timeout must not kill the command."""
    closed = False

    def __init__(self, *a, **k):
        self._calls = 0

    def connect(self):
        return {}

    def meter_labels(self):
        return {"in": ["In  1"], "aux": ["aux/ch1 L"], "main": ["main L"]}

    def read_meters(self, timeout=1.0):
        self._calls += 1
        if self._calls <= 2:
            raise socket.timeout()
        if self._calls == 3:
            return {"in": [7], "aux": [0], "main": [0]}
        raise KeyboardInterrupt

    def close(self):
        self.closed = True


def test_meters_continuous_survives_occasional_timeouts(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", TimeoutThenValueMetersClient)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    rc = main(["meters"])

    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("dBFS = 20·log10(cru/65535)") == 1  # header printed once, non-tty
    assert f"In  1: 7 ({ucnet.meter_dbfs(7):.1f} dBFS)" in out


class AlwaysTimeoutMetersClient:
    closed = False

    def __init__(self, *a, **k):
        pass

    def connect(self):
        return {}

    def meter_labels(self):
        return {"in": [], "aux": [], "main": []}

    def read_meters(self, timeout=1.0):
        raise socket.timeout()

    def close(self):
        self.closed = True


def test_meters_gives_up_after_5_consecutive_timeouts(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", AlwaysTimeoutMetersClient)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    rc = main(["meters"])

    assert rc == 1
    assert "sem medidores do daemon há 5 s" in capsys.readouterr().err


class OSErrorMetersClient:
    closed = False

    def __init__(self, *a, **k):
        pass

    def connect(self):
        return {}

    def meter_labels(self):
        return {"in": [], "aux": [], "main": []}

    def read_meters(self, timeout=1.0):
        raise OSError("network down")

    def close(self):
        self.closed = True


def test_meters_os_error_prints_clean_message_and_exits_1(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", OSErrorMetersClient)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    rc = main(["meters"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "network down" in err


class TwoFramesMetersClient:
    closed = False

    def __init__(self, *a, **k):
        self._calls = 0

    def connect(self):
        return {}

    def meter_labels(self):
        return {"in": ["In  1"], "aux": ["aux/ch1 L"], "main": ["main L"]}

    def read_meters(self, timeout=1.0):
        self._calls += 1
        if self._calls > 2:
            raise KeyboardInterrupt
        return {"in": [self._calls], "aux": [0], "main": [0]}

    def close(self):
        self.closed = True


def test_meters_tty_redraw_clears_screen_and_repeats_header_each_frame(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", TwoFramesMetersClient)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    rc = main(["meters"])

    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("\x1b[2J") == 2  # one clear per frame, no scrollback
    assert out.count("dBFS = 20·log10(cru/65535)") == 2  # header re-drawn every frame
    assert f"In  1: 1 ({ucnet.meter_dbfs(1):.1f} dBFS)" in out
    assert f"In  1: 2 ({ucnet.meter_dbfs(2):.1f} dBFS)" in out


class ForeverMetersClient:
    """read_meters() never stops on its own -- exercises that a downstream
    reader closing the pipe (e.g. `quantum-hd8 meters | head -5`) is what
    ends the command, not the fake reaching some call limit."""
    closed = False

    def __init__(self, *a, **k):
        pass

    def connect(self):
        return {}

    def meter_labels(self):
        return {"in": ["In  1"], "aux": ["aux/ch1 L"], "main": ["main L"]}

    def read_meters(self, timeout=1.0):
        return {"in": [1], "aux": [0], "main": [0]}

    def close(self):
        self.closed = True


class BrokenPipeStdout:
    """Fake stdout whose write() raises BrokenPipeError once the reader on
    the other end of a pipe has gone away (e.g. `| head -5`), after
    `n_ok` successful writes. fileno() raises like a non-fd stream (a
    StringIO-style double) so the fix's best-effort os.dup2 redirect must
    not itself blow up in a test."""

    def __init__(self, n_ok):
        self.n_ok = n_ok
        self.calls = 0

    def write(self, s):
        self.calls += 1
        if self.calls > self.n_ok:
            raise BrokenPipeError()
        return len(s)

    def isatty(self):
        return False

    def flush(self):
        pass

    def fileno(self):
        raise io.UnsupportedOperation("fileno")


def test_meters_broken_pipe_returns_0_without_a_traceback(monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", ForeverMetersClient)
    monkeypatch.setattr(sys, "stdout", BrokenPipeStdout(n_ok=1))

    rc = main(["meters"])

    assert rc == 0


# --- final review fix wave -------------------------------------------------

from quantum_hd8 import undo as undo_mod
from quantum_hd8.client import NOT_RESPONDING


def _capture_instances(monkeypatch, cls):
    instances = []
    orig_init = cls.__init__

    def capture_init(self, *a, **k):
        orig_init(self, *a, **k)
        instances.append(self)

    monkeypatch.setattr(cls, "__init__", capture_init)
    return instances


def test_scene_load_keep_gains_partial_failure_reports_and_returns_1(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["scene", "load", "PARTIAL", "--keep-gains"])
    assert rc == 1
    cap = capsys.readouterr()
    assert "line/ch1/preampgain" in cap.out and "restaurado" in cap.out
    assert "line/ch2/preampgain" in cap.err
    assert "Traceback" not in cap.err


def test_scene_load_write_not_confirmed_is_caught_and_returns_1(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["scene", "load", "WNC", "--keep-gains"])
    assert rc == 1
    assert "não confirmou" in capsys.readouterr().err


class RefusedClient(WriteFakeClient):
    def connect(self):
        raise ConnectionRefusedError(61, "Connection refused")


import pytest as _pytest


@_pytest.mark.parametrize("argv", [
    ["state"], ["dump"], ["get", "line/ch1/preampgain"], ["listen"],
    ["set", "line/ch1/48v", "1"], ["undo"], ["scene", "list"],
    ["preamp", "1", "gain", "20"], ["meters", "--once"], ["route", "phones1", "0"],
])
def test_connection_refused_prints_not_responding_and_returns_1(argv, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("quantum_hd8.cli.Client", RefusedClient)
    monkeypatch.setattr("quantum_hd8.cli.undo.DEFAULT_JOURNAL", tmp_path / "undo.jsonl")
    rc = main(argv)
    assert rc == 1
    assert NOT_RESPONDING in capsys.readouterr().err


class UndoFakeClient(WriteFakeClient):
    fail = False

    def set_raw(self, path, normalized):
        self.set_raw_calls.append((path, normalized))
        if self.fail:
            raise WriteNotConfirmed(f"{path}: o daemon não confirmou a escrita em 1 s")
        return normalized


def test_undo_keeps_entry_when_write_not_confirmed(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", UndoFakeClient)
    monkeypatch.setattr(UndoFakeClient, "fail", True)
    journal = tmp_path / "undo.jsonl"
    monkeypatch.setattr("quantum_hd8.cli.undo.DEFAULT_JOURNAL", journal)
    undo_mod.record("line/ch1/preampgain", 0.26, 0.5, journal=journal)

    rc = main(["undo"])

    assert rc == 1
    assert undo_mod.peek(journal=journal) == ("line/ch1/preampgain", 0.26)


def test_undo_removes_entry_only_after_success(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", UndoFakeClient)
    journal = tmp_path / "undo.jsonl"
    monkeypatch.setattr("quantum_hd8.cli.undo.DEFAULT_JOURNAL", journal)
    undo_mod.record("a/b", 0.1, 0.2, journal=journal)
    undo_mod.record("line/ch1/preampgain", 0.26, 0.5, journal=journal)

    assert main(["undo"]) == 0

    assert undo_mod.peek(journal=journal) == ("a/b", 0.1)


def test_undo_with_unknown_before_drops_entry_and_returns_1(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", UndoFakeClient)
    instances = _capture_instances(monkeypatch, UndoFakeClient)
    journal = tmp_path / "undo.jsonl"
    monkeypatch.setattr("quantum_hd8.cli.undo.DEFAULT_JOURNAL", journal)
    undo_mod.record("a/b", 0.1, 0.2, journal=journal)
    undo_mod.record("line/ch1/preampgain", None, 0.5, journal=journal)

    rc = main(["undo"])

    assert rc == 1
    assert "valor anterior desconhecido" in capsys.readouterr().err
    assert instances[0].set_raw_calls == []
    assert undo_mod.peek(journal=journal) == ("a/b", 0.1)


def test_set_on_for_non_toggle_is_rejected_with_exit_2(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    instances = _capture_instances(monkeypatch, WriteFakeClient)
    rc = main(["set", "main/ch1/volume", "on"])
    assert rc == 2
    assert instances[0].set_calls == []
    assert "toggle" in capsys.readouterr().err


def test_set_unknown_path_error_has_no_quotes_and_returns_2(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    rc = main(["set", "nope/path", "1"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("caminho desconhecido: nope/path")


def test_set_invalid_value_returns_2(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    assert main(["set", "line/ch1/48v", "banana"]) == 2
    assert "valor inválido" in capsys.readouterr().err


def test_preamp_validation_error_returns_2_and_no_quotes(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    assert main(["preamp", "1", "gain", "80"]) == 2
    assert "fora da faixa" in capsys.readouterr().err


def test_scene_without_subcommand_prints_usage_exits_2_without_connecting(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", WriteFakeClient)
    instances = _capture_instances(monkeypatch, WriteFakeClient)
    rc = main(["scene"])
    assert rc == 2
    assert instances == []
    assert "usage" in capsys.readouterr().err


class TimeoutOnceMetersClient(MetersFakeClient):
    def read_meters(self, timeout=1.0):
        raise socket.timeout("timed out")


def test_meters_once_timeout_prints_clean_message_and_returns_1(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", TimeoutOnceMetersClient)
    rc = main(["meters", "--once"])
    assert rc == 1
    assert "sem medidores do daemon (timeout)" in capsys.readouterr().err
