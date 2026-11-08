import math
import sys

import numpy as np
import pytest

from elpcam.processing import ProcessingSettings, Processor, build_dewarp_maps


def test_settings_clamp_and_types():
    s = ProcessingSettings()
    s.update({"rotate": 450, "zoom": 20, "dewarp": "bogus", "mirror": 1, "label": "x" * 500, "unknown": 5})
    assert s.rotate == 90
    assert s.zoom == 8.0
    assert s.dewarp == "off"
    assert s.mirror is True
    assert len(s.label) == 120


def test_dewarp_center_maps_to_center():
    w, h = 640, 480
    m1, _m2 = build_dewarp_maps(w, h, w, h, "perspective", 170, 90, 0, 0, 0)
    cy, cx = h // 2, w // 2
    x, y = m1[cy, cx]
    assert abs(x - (w - 1) / 2) <= 1.5 and abs(y - (h - 1) / 2) <= 1.5


def test_equidistant_model_radius():
    """A ray at angle theta must land at r = f*theta for the uncalibrated model."""
    w, h, lens = 800, 600, 160.0
    f = math.hypot(w, h) / 2 / math.radians(lens / 2)
    # perspective view with 90 deg hfov: pixel at the right edge is 45 deg off-axis
    m1, _ = build_dewarp_maps(w, h, w, h, "perspective", lens, 90, 0, 0, 0, max_compute_width=w)
    x, _y = m1[(h - 1) // 2, w - 1]
    expected = (w - 1) / 2 + f * math.radians(45)
    assert abs(x - expected) < 3


def test_yaw_moves_view_right():
    w, h = 640, 480
    m_a, _ = build_dewarp_maps(w, h, w, h, "perspective", 170, 90, 0, 0, 0)
    m_b, _ = build_dewarp_maps(w, h, w, h, "perspective", 170, 90, 20, 0, 0)
    assert m_b[h // 2, w // 2][0] > m_a[h // 2, w // 2][0] + 20


@pytest.mark.parametrize("mode", ["perspective", "panorama"])
def test_processor_keeps_size(mode):
    p = Processor()
    p.settings.update({"dewarp": mode, "zoom": 2.0, "mirror": True})
    frame = np.random.randint(0, 255, (480, 640, 3), np.uint8)
    out = p.geometry(frame)
    assert out.shape == frame.shape


def test_rotation_swaps_dims_and_overlays():
    p = Processor()
    p.settings.update({"rotate": 90, "timestamp": True, "grid": True, "crosshair": True, "label": "ELP"})
    frame = np.zeros((480, 640, 3), np.uint8)
    out = p.overlays(p.geometry(frame))
    assert out.shape == (640, 480, 3)
    assert out.any()


@pytest.mark.skipif(sys.platform != "win32", reason="DirectShow is Windows only")
def test_fourcc_from_subtype():
    from comtypes import GUID

    from elpcam.controls.dshow import _subtype_to_fourcc

    assert _subtype_to_fourcc(GUID("{47504A4D-0000-0010-8000-00AA00389B71}")) == "MJPG"
    assert _subtype_to_fourcc(GUID("{32595559-0000-0010-8000-00AA00389B71}")) == "YUY2"
