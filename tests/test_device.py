"""Tests that touch the real Quantum HD 8 through ucdaemon.

Marked `device`, so they are deselected by default (pyproject addopts
`-m 'not device'`). Read-only: they never write a parameter. Run only by
hand, with Universal Control running: `python3 -m pytest -m device`.
"""
import pytest

from quantum_hd8.client import Client


@pytest.mark.device
def test_device_connect_and_read_preamp_gain():
    c = Client()
    try:
        c.connect()
        value = c.get("line/ch1/preampgain")
    finally:
        c.close()
    assert 0.0 <= value <= 1.0
