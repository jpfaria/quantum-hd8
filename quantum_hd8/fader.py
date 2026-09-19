"""The curve "fader" dB <-> normalized 0..1 mapping.

Measured 19/09 on a single send, `line/ch30/aux13` (Loopback 1): a known tone
into `line/ch30` (USB 4), `line/ch30/aux13` varied, aux13's calibrated meter
read after 1.2 s -- see tests/fixtures/fader-curve.json for the method and
the 17 points below. **Assumption, not separately measured**: every other
param whose params.json `curve` is "fader" (every `volume` and `auxN` send,
`main/ch1/volume`) shares the same curve, since they share the same XML
curve name and the daemon reports the same range (-96..10, docs/protocol.md).

0 (fader bottom) is off (-inf), never on this piecewise line -- it is not
"below" the first measured point, it is a hard floor. Below the first
measured point (0 < normalized < 0.05) there is no measurement: the mapping
extrapolates the first measured segment's slope and clamps at -96 dB (the
param's own floor, params.json min), so a near-zero normalized value never
reports something below the fader's actual floor.
"""
from __future__ import annotations

import math

# (normalized, gain_db), sorted ascending by both -- measured 19/09,
# tests/fixtures/fader-curve.json.
_POINTS: tuple[tuple[float, float], ...] = (
    (0.05, -49.48),
    (0.1, -39.3),
    (0.15, -35.27),
    (0.2, -31.32),
    (0.3, -23.44),
    (0.4, -15.53),
    (0.5, -8.87),
    (0.6, -5.1),
    (0.65, -3.21),
    (0.7, -1.32),
    (0.735, 0.0),
    (0.75, 0.57),
    (0.8, 2.45),
    (0.85, 4.34),
    (0.9, 6.23),
    (0.95, 8.11),
    (1.0, 10.0),
)

MIN_DB = -96.0
MAX_DB = _POINTS[-1][1]


def fader_db(normalized: float) -> float:
    """normalized (0..1) -> gain in dB on the measured fader curve.

    0 -> -inf (fader bottom). Between 0 and the first measured point
    (0.05), there is no measurement: extrapolate the first segment's slope
    and clamp at MIN_DB (-96 dB, the param's floor) rather than reporting
    something below it.
    """
    if not (0.0 <= normalized <= 1.0):
        raise ValueError(f"{normalized}: fora da faixa normalizada [0, 1]")
    if normalized == 0.0:
        return -math.inf

    x0, y0 = _POINTS[0]
    if normalized < x0:
        x1, y1 = _POINTS[1]
        slope = (y1 - y0) / (x1 - x0)
        return max(y0 + slope * (normalized - x0), MIN_DB)

    for (x0, y0), (x1, y1) in zip(_POINTS, _POINTS[1:]):
        if x0 <= normalized <= x1:
            t = (normalized - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)

    # normalized == 1.0, the last point -- the loop above's last segment
    # already covers it via its closed upper bound, but be explicit.
    return _POINTS[-1][1]


def fader_normalized(db: float | str) -> float:
    """gain in dB -> normalized (0..1) on the measured fader curve, the
    inverse of fader_db(). "-inf" (or any db <= MIN_DB) -> 0.0 (fader
    bottom). db > MAX_DB (the curve's measured top, +10 dB) raises
    ValueError -- the fader physically cannot go higher.
    """
    if isinstance(db, str):
        db = float(db)
    if db != db:  # NaN
        raise ValueError(f"{db}: dB inválido")
    if db <= MIN_DB or math.isinf(db):
        return 0.0
    if db > MAX_DB:
        raise ValueError(f"{db}: acima do topo do fader (+{MAX_DB} dB)")

    x0, y0 = _POINTS[0]
    if db < y0:
        x1, y1 = _POINTS[1]
        slope = (y1 - y0) / (x1 - x0)
        return max(x0 + (db - y0) / slope, 0.0)

    for (x0, y0), (x1, y1) in zip(_POINTS, _POINTS[1:]):
        if y0 <= db <= y1:
            t = (db - y0) / (y1 - y0)
            return x0 + t * (x1 - x0)

    return _POINTS[-1][0]
