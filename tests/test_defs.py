from pathlib import Path

from quantum_hd8.defs import load_template

FX = Path(__file__).parent / "fixtures" / "defs_min.xml"


def test_paths_inherit_mixin_foreach():
    params = {p.path: p for p in load_template(str(FX))}
    assert set(params) == {
        "global/dim", "global/phones2_src",
        "line/ch1/volume", "line/ch1/preampgain",
        "line/ch2/volume", "line/ch2/preampgain",
    }
    g = params["line/ch2/preampgain"]
    assert (g.type, g.min, g.max, g.units) == ("float", 0.0, 75.0, "gain.0")
    assert g.flags == ["storable", "mutable"]
    assert g.component == "line/ch2"


def test_curve_attr_is_read():
    params = {p.path: p for p in load_template(str(FX))}
    assert params["line/ch2/preampgain"].curve == "linear"
    assert params["line/ch2/volume"].curve == "fader"
    assert params["global/dim"].curve is None
