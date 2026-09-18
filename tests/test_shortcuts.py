"""Tests for the `preamp` and `route` CLI shortcuts (task-8-brief.md)."""
from pathlib import Path

from quantum_hd8 import ucnet
from quantum_hd8.cli import main, STATIC_LABELS, _resolve_label_index

FX = Path(__file__).parent / "fixtures"


class ShortcutFakeClient:
    """Stands in for Client in preamp/route CLI tests."""

    closed = False
    lists: dict[str, list[str]] = {}

    _LINEAR = {
        f"line/ch{ch}/preampgain": (0.0, 75.0, "float") for ch in range(1, 9)
    }

    def __init__(self, *a, **k):
        self.set_calls = []
        self.set_list_calls = []

    def connect(self):
        return {}

    def set(self, path, value):
        self.set_calls.append((path, value))
        if path in self._LINEAR:
            lo, hi, _ = self._LINEAR[path]
            if not (lo <= value <= hi):
                raise ValueError(f"{path}: {value} fora da faixa [{lo}, {hi}]")
            return (value - lo) / (hi - lo)
        return float(value)

    def set_list(self, path, index, n):
        self.set_list_calls.append((path, index, n))
        return index / (n - 1)

    def to_human(self, path, normalized):
        if path in self._LINEAR:
            lo, hi, _ = self._LINEAR[path]
            return lo + normalized * (hi - lo)
        return None

    def param_row(self, path):
        if path in self._LINEAR:
            return {"type": "float"}
        return {"type": "toggle"}

    def close(self):
        self.closed = True


# --- preamp --------------------------------------------------------------

def test_preamp_gain_writes_preampgain_in_human_db(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", ShortcutFakeClient)
    rc = main(["preamp", "3", "gain", "37.5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "line/ch3/preampgain = 37.5 (0.500)" in out


def test_preamp_phantom_on_writes_48v(monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", ShortcutFakeClient)
    fake = {}
    orig_init = ShortcutFakeClient.__init__

    def capture_init(self, *a, **k):
        orig_init(self, *a, **k)
        fake["client"] = self

    monkeypatch.setattr(ShortcutFakeClient, "__init__", capture_init)
    rc = main(["preamp", "1", "phantom", "on"])
    assert rc == 0
    assert fake["client"].set_calls == [("line/ch1/48v", 1)]


def test_preamp_phantom_off(monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", ShortcutFakeClient)
    fake = {}
    orig_init = ShortcutFakeClient.__init__

    def capture_init(self, *a, **k):
        orig_init(self, *a, **k)
        fake["client"] = self

    monkeypatch.setattr(ShortcutFakeClient, "__init__", capture_init)
    rc = main(["preamp", "2", "phantom", "off"])
    assert rc == 0
    assert fake["client"].set_calls == [("line/ch2/48v", 0)]


def test_preamp_pad_writes_pad(monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", ShortcutFakeClient)
    fake = {}
    orig_init = ShortcutFakeClient.__init__

    def capture_init(self, *a, **k):
        orig_init(self, *a, **k)
        fake["client"] = self

    monkeypatch.setattr(ShortcutFakeClient, "__init__", capture_init)
    rc = main(["preamp", "5", "pad", "on"])
    assert rc == 0
    assert fake["client"].set_calls == [("line/ch5/pad", 1)]


def test_preamp_hpf_writes_hpf(monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", ShortcutFakeClient)
    fake = {}
    orig_init = ShortcutFakeClient.__init__

    def capture_init(self, *a, **k):
        orig_init(self, *a, **k)
        fake["client"] = self

    monkeypatch.setattr(ShortcutFakeClient, "__init__", capture_init)
    rc = main(["preamp", "8", "hpf", "off"])
    assert rc == 0
    assert fake["client"].set_calls == [("line/ch8/hpf", 0)]


def test_preamp_invalid_channel_returns_2_and_lists_range(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", ShortcutFakeClient)
    rc = main(["preamp", "9", "gain", "10"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "1" in err and "8" in err


def test_preamp_non_numeric_channel_returns_2(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", ShortcutFakeClient)
    rc = main(["preamp", "x", "gain", "10"])
    assert rc == 2
    assert capsys.readouterr().err


def test_preamp_out_of_range_gain_prints_error_returns_2(capsys, monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", ShortcutFakeClient)
    rc = main(["preamp", "1", "gain", "999"])
    assert rc == 2  # validation error (final review #8: usage/validation = 2)
    assert "fora da faixa" in capsys.readouterr().err


# --- route -----------------------------------------------------------------

def test_route_by_label(capsys, monkeypatch):
    class Fake(ShortcutFakeClient):
        lists = {"global/phones1_src": ["Main L/R", "Out  3/4", "Out  5/6"]}

    monkeypatch.setattr("quantum_hd8.cli.Client", Fake)
    rc = main(["route", "phones1", "Out  3/4"])
    assert rc == 0
    assert "phones1 = Out  3/4" in capsys.readouterr().out


def test_route_by_label_case_insensitive_and_whitespace_collapsed(capsys, monkeypatch):
    class Fake(ShortcutFakeClient):
        lists = {"global/phones1_src": ["Main L/R", "Out  3/4", "Out  5/6"]}

    monkeypatch.setattr("quantum_hd8.cli.Client", Fake)
    rc = main(["route", "phones1", "out 3/4"])
    assert rc == 0
    assert "phones1 = Out  3/4" in capsys.readouterr().out


def test_route_by_index(capsys, monkeypatch):
    class Fake(ShortcutFakeClient):
        lists = {"global/phones2_src": ["Main L/R", "Out  3/4", "Out  5/6"]}

    monkeypatch.setattr("quantum_hd8.cli.Client", Fake)
    rc = main(["route", "phones2", "2"])
    assert rc == 0
    assert "phones2 = Out  5/6" in capsys.readouterr().out


def test_route_uses_client_lists_when_present_over_static(monkeypatch):
    class Fake(ShortcutFakeClient):
        lists = {"global/spdifSource": ["A", "B"]}

    monkeypatch.setattr("quantum_hd8.cli.Client", Fake)
    fake = {}
    orig_init = Fake.__init__

    def capture_init(self, *a, **k):
        orig_init(self, *a, **k)
        fake["client"] = self

    monkeypatch.setattr(Fake, "__init__", capture_init)
    rc = main(["route", "spdif", "B"])
    assert rc == 0
    assert fake["client"].set_list_calls == [("global/spdifSource", 1, 2)]


def test_route_falls_back_to_static_labels_when_client_lists_empty(monkeypatch):
    monkeypatch.setattr("quantum_hd8.cli.Client", ShortcutFakeClient)
    fake = {}
    orig_init = ShortcutFakeClient.__init__

    def capture_init(self, *a, **k):
        orig_init(self, *a, **k)
        fake["client"] = self

    monkeypatch.setattr(ShortcutFakeClient, "__init__", capture_init)
    rc = main(["route", "spdif", "S/PDIF Out"])
    assert rc == 0
    n = len(STATIC_LABELS["global/spdifSource"])
    assert fake["client"].set_list_calls == [("global/spdifSource", n - 1, n)]


def test_route_unknown_label_returns_2_and_lists_valid(capsys, monkeypatch):
    class Fake(ShortcutFakeClient):
        lists = {"global/phones1_src": ["Main L/R", "Out  3/4"]}

    monkeypatch.setattr("quantum_hd8.cli.Client", Fake)
    rc = main(["route", "phones1", "Nonexistent"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Main L/R" in err and "Out  3/4" in err


# --- static label constants, measured from the real fixture ---------------

def test_static_labels_match_fixture_measurement():
    data = (FX / "uc-pl.bin").read_bytes()
    msgs = ucnet.Decoder().feed(data)
    measured = {}
    for m in msgs:
        if m.code == "PL":
            path, _, labels = ucnet.parse_pl(m)
            measured[path] = labels

    assert STATIC_LABELS["global/phones1_src"] == measured["global/phones1_src"]
    assert STATIC_LABELS["global/phones2_src"] == measured["global/phones2_src"]
    assert STATIC_LABELS["global/spdifSource"] == measured["global/spdifSource"]


# --- label/index resolution (unit) -----------------------------------------

def test_resolve_label_index_exact_match():
    assert _resolve_label_index("Loopback  1", ["Main L/R", "Loopback  1"]) == 1


def test_resolve_label_index_case_and_whitespace_insensitive():
    assert _resolve_label_index("loopback 1", ["Main L/R", "Loopback  1"]) == 1


def test_resolve_label_index_numeric_index():
    assert _resolve_label_index("1", ["Main L/R", "Loopback  1"]) == 1


def test_resolve_label_index_numeric_index_out_of_range_is_none():
    assert _resolve_label_index("5", ["Main L/R", "Loopback  1"]) is None


def test_resolve_label_index_unknown_label_is_none():
    assert _resolve_label_index("nope", ["Main L/R", "Loopback  1"]) is None
