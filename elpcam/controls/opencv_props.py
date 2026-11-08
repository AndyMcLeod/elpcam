"""Portable fallback: drive controls through OpenCV ``CAP_PROP_*`` properties.

Used on macOS (AVFoundation) or whenever the native backend is unavailable.
OpenCV cannot report ranges, so typical UVC ranges for the ELP MI5100 module
are assumed; values outside what the driver accepts are simply ignored by it.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import cv2

from .base import Control, ControlBackend, ControlError, VideoMode

#: key, label, group, cv2 prop, min, max, step, default, auto prop (or None)
_TABLE = [
    ("brightness", "Brightness", "image", cv2.CAP_PROP_BRIGHTNESS, -64, 64, 1, 0, None),
    ("contrast", "Contrast", "image", cv2.CAP_PROP_CONTRAST, 0, 64, 1, 32, None),
    ("saturation", "Saturation", "image", cv2.CAP_PROP_SATURATION, 0, 128, 1, 64, None),
    ("hue", "Hue", "image", cv2.CAP_PROP_HUE, -40, 40, 1, 0, None),
    ("sharpness", "Sharpness", "image", cv2.CAP_PROP_SHARPNESS, 0, 6, 1, 3, None),
    ("gamma", "Gamma", "image", cv2.CAP_PROP_GAMMA, 72, 500, 1, 100, None),
    ("gain", "Gain", "image", cv2.CAP_PROP_GAIN, 0, 100, 1, 0, None),
    ("white_balance", "White Balance", "image", cv2.CAP_PROP_WB_TEMPERATURE, 2800, 6500, 10, 4600,
     cv2.CAP_PROP_AUTO_WB),
    ("backlight_compensation", "Backlight Comp.", "image", cv2.CAP_PROP_BACKLIGHT, 0, 2, 1, 1, None),
    ("exposure", "Exposure", "camera", cv2.CAP_PROP_EXPOSURE, -13, -1, 1, -6, cv2.CAP_PROP_AUTO_EXPOSURE),
    ("focus", "Focus", "camera", cv2.CAP_PROP_FOCUS, 0, 255, 1, 0, cv2.CAP_PROP_AUTOFOCUS),
    ("zoom", "Zoom", "camera", cv2.CAP_PROP_ZOOM, 0, 100, 1, 0, None),
]


class OpenCVControls(ControlBackend):
    name = "opencv"
    enumerates_modes = False

    def __init__(self, get_cap: Callable[[], Any]) -> None:
        self._get_cap = get_cap
        self._last: dict[str, int] = {}
        self._auto: dict[str, bool] = {}

    def _cap(self):
        cap = self._get_cap()
        if cap is None or not cap.isOpened():
            raise ControlError("Camera is not open")
        return cap

    def list_controls(self) -> list[Control]:
        try:
            cap = self._cap()
        except ControlError:
            return []
        out = []
        for key, label, group, prop, mn, mx, st, df, auto_prop in _TABLE:
            v = cap.get(prop)
            if v == -1 and key not in self._last:
                continue
            value = int(round(v)) if v not in (-1,) else self._last.get(key, df)
            c = Control(id=key, name=label, group=group, min=mn, max=mx, step=st, default=df,
                        value=max(mn, min(mx, value)), auto_supported=auto_prop is not None,
                        auto=self._auto.get(key, False), native_id=prop)
            out.append(c)
        return out

    def set_control(self, cid: str, value: Optional[int] = None, auto: Optional[bool] = None) -> Control:
        cap = self._cap()
        row = next((r for r in _TABLE if r[0] == cid), None)
        if row is None:
            raise ControlError(f"Unknown control '{cid}'")
        key, _label, _group, prop, *_rest, auto_prop = row
        if auto is not None and auto_prop is not None:
            if auto_prop == cv2.CAP_PROP_AUTO_EXPOSURE:
                cap.set(auto_prop, 0.75 if auto else 0.25)  # V4L2/DSHOW convention
            else:
                cap.set(auto_prop, 1 if auto else 0)
            self._auto[key] = bool(auto)
        if value is not None:
            cap.set(prop, float(value))
            self._last[key] = int(value)
        return self.get_control(cid)

    def list_modes(self) -> list[VideoMode]:
        return []
