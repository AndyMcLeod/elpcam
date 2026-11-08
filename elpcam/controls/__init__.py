"""Platform-specific camera control backends behind one interface."""

from __future__ import annotations

import sys
from typing import Any, Callable

from .base import AUTO_BY_DEFAULT, Control, ControlBackend, ControlError, DeviceInfo, VideoMode

__all__ = [
    "AUTO_BY_DEFAULT", "Control", "ControlBackend", "ControlError", "DeviceInfo", "VideoMode",
    "list_devices", "make_backend", "capture_api",
]


def capture_api() -> int:
    """The OpenCV capture API matching our control backend's device numbering."""
    import cv2

    if sys.platform == "win32":
        return cv2.CAP_DSHOW
    if sys.platform.startswith("linux"):
        return cv2.CAP_V4L2
    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def list_devices() -> list[DeviceInfo]:
    """Enumerate cameras; indices match ``cv2.VideoCapture(index, capture_api())``."""
    try:
        if sys.platform == "win32":
            from .dshow import list_devices as _ld
            return _ld()
        if sys.platform.startswith("linux"):
            from .v4l2 import list_devices as _ld
            return _ld()
    except Exception:
        pass
    return _probe_opencv()


def _probe_opencv(max_index: int = 6) -> list[DeviceInfo]:
    import cv2

    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i, capture_api())
        if cap.isOpened():
            found.append(DeviceInfo(index=i, name=f"Camera {i}"))
        cap.release()
    return found


def make_backend(device: DeviceInfo, get_cap: Callable[[], Any]) -> ControlBackend:
    """Best available control backend for ``device`` (native first, OpenCV fallback)."""
    try:
        if sys.platform == "win32":
            from .dshow import DirectShowControls
            return DirectShowControls(device)
        if sys.platform.startswith("linux"):
            from .v4l2 import V4L2Controls
            b = V4L2Controls(device)
            b.list_controls()  # raises if v4l2-ctl is missing
            return b
    except Exception:
        pass
    from .opencv_props import OpenCVControls
    return OpenCVControls(get_cap)
