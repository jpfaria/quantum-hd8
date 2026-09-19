"""fader_db/fader_normalized: the curve "fader" dB <-> normalized 0..1
mapping, measured 19/09 on line/ch30/aux13 (tests/fixtures/fader-curve.json)
and applied to every param with curve "fader" (docs/protocol.md,
docs/camada-b-reamp.md and README document the single-send assumption)."""
import json
import math
from pathlib import Path

import pytest

from quantum_hd8.fader import fader_db, fader_normalized

FX = Path(__file__).parent / "fixtures" / "fader-curve.json"
POINTS = json.loads(FX.read_text())["points"]


def test_fader_db_zero_normalized_is_minus_inf():
    assert fader_db(0.0) == -math.inf


@pytest.mark.parametrize("point", POINTS, ids=[str(p["normalized"]) for p in POINTS])
def test_fader_db_matches_every_measured_point(point):
    assert fader_db(point["normalized"]) == pytest.approx(point["gain_db"], abs=0.01)


def test_fader_db_interpolates_between_measured_points():
    # Midpoint of the 0.5 -> 0.6 segment (-8.87 -> -5.1 dB).
    expected = (-8.87 + -5.1) / 2
    assert fader_db(0.55) == pytest.approx(expected, abs=0.01)


def test_fader_db_below_first_point_extrapolates_first_segment():
    # First segment: 0.05 -> -49.48, 0.1 -> -39.3 dB (slope ~203.6 dB/unit).
    slope = (-39.3 - -49.48) / (0.1 - 0.05)
    expected = -49.48 + slope * (0.02 - 0.05)
    assert fader_db(0.02) == pytest.approx(expected, abs=0.01)


def test_fader_db_extrapolation_never_goes_below_minus_96():
    # The measured slope alone would put very small normalized values well
    # above -96 dB, but the clamp is a hard safety floor regardless.
    for v in (0.001, 0.0001, 0.00001):
        assert fader_db(v) >= -96.0


def test_fader_db_rejects_out_of_range():
    with pytest.raises(ValueError):
        fader_db(-0.1)
    with pytest.raises(ValueError):
        fader_db(1.1)


def test_fader_normalized_minus_inf_is_zero():
    assert fader_normalized(float("-inf")) == 0.0
    assert fader_normalized("-inf") == 0.0


def test_fader_normalized_at_or_below_minus_96_is_zero():
    assert fader_normalized(-96.0) == 0.0
    assert fader_normalized(-150.0) == 0.0


def test_fader_normalized_rejects_above_10():
    with pytest.raises(ValueError):
        fader_normalized(10.1)


@pytest.mark.parametrize("point", POINTS, ids=[str(p["normalized"]) for p in POINTS])
def test_fader_normalized_round_trips_every_measured_point(point):
    got = fader_normalized(point["gain_db"])
    assert got == pytest.approx(point["normalized"], abs=0.05)


def test_fader_db_and_normalized_are_inverses_at_unity():
    assert fader_db(0.735) == pytest.approx(0.0, abs=0.01)
    assert fader_normalized(0.0) == pytest.approx(0.735, abs=0.01)
