"""Frame processing: fisheye dewarping, orientation, digital zoom and overlays.

The BL170 lens is a 170° fisheye. Dewarping uses the OpenCV fisheye camera
model (θd = θ(1 + k1θ² + k2θ⁴ + k3θ⁶ + k4θ⁸), r = f·θd). Without a calibration
the lens is treated as an ideal equidistant fisheye whose focal length is
derived from the "lens FOV" setting; with a checkerboard calibration the real
K/D coefficients are used.

Two virtual views can be rendered from the fisheye image, each with a virtual
pan / tilt / roll (electronic PTZ):

* ``perspective`` - rectilinear view (straight lines stay straight)
* ``panorama``    - cylindrical projection (keeps the full horizontal sweep)
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import cv2
import numpy as np


@dataclass
class ProcessingSettings:
    # orientation
    mirror: bool = False
    flip: bool = False
    rotate: int = 0                  # 0, 90, 180, 270 (clockwise)
    # fisheye
    dewarp: str = "off"              # off | perspective | panorama
    lens_fov: float = 170.0          # lens field of view across the image diagonal (uncalibrated model)
    out_fov: float = 100.0           # horizontal FOV of the virtual view
    yaw: float = 0.0                 # virtual pan  (deg)
    pitch: float = 0.0               # virtual tilt (deg)
    roll: float = 0.0                # virtual roll (deg)
    use_calibration: bool = True
    # digital zoom
    zoom: float = 1.0
    zoom_x: float = 0.0              # -1 .. 1 (pan within the frame)
    zoom_y: float = 0.0
    # overlays
    timestamp: bool = False
    grid: bool = False
    crosshair: bool = False
    label: str = ""
    overlays_on_capture: bool = True

    def update(self, data: dict[str, Any]) -> None:
        for k, v in data.items():
            if not hasattr(self, k):
                continue
            cur = getattr(self, k)
            if isinstance(cur, bool):
                v = bool(v)
            elif isinstance(cur, int):
                v = int(v)
            elif isinstance(cur, float):
                v = float(v)
            elif isinstance(cur, str):
                v = str(v)
            setattr(self, k, v)
        self.rotate = self.rotate % 360 if self.rotate % 90 == 0 else 0
        if self.dewarp not in ("off", "perspective", "panorama"):
            self.dewarp = "off"
        self.lens_fov = float(np.clip(self.lens_fov, 60, 240))
        self.out_fov = float(np.clip(self.out_fov, 20, 175 if self.dewarp == "perspective" else 360))
        self.zoom = float(np.clip(self.zoom, 1.0, 8.0))
        self.zoom_x = float(np.clip(self.zoom_x, -1, 1))
        self.zoom_y = float(np.clip(self.zoom_y, -1, 1))
        self.label = self.label[:120]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FisheyeCalibration:
    width: int
    height: int
    K: list[list[float]]
    D: list[float]
    rms: float = 0.0
    frames: int = 0
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    def scaled_K(self, w: int, h: int) -> np.ndarray:
        K = np.array(self.K, dtype=np.float64)
        sx, sy = w / self.width, h / self.height
        K[0, 0] *= sx
        K[0, 2] *= sx
        K[1, 1] *= sy
        K[1, 2] *= sy
        return K


def _rotation(yaw: float, pitch: float, roll: float) -> np.ndarray:
    y, p, r = (math.radians(a) for a in (yaw, pitch, roll))
    Ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
    Rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
    Rz = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    return Ry @ Rx @ Rz


def build_dewarp_maps(
    src_w: int, src_h: int, out_w: int, out_h: int, mode: str, lens_fov: float, out_fov: float,
    yaw: float, pitch: float, roll: float, K: Optional[np.ndarray] = None, D: Optional[np.ndarray] = None,
    max_compute_width: int = 640,
) -> tuple[np.ndarray, np.ndarray]:
    """Return remap tables (CV_16SC2 + CV_16UC1) from the fisheye source to the virtual view."""
    scale = min(1.0, max_compute_width / out_w)
    cw, ch = max(2, int(round(out_w * scale))), max(2, int(round(out_h * scale)))

    if K is None:
        diag = math.hypot(src_w, src_h) / 2.0
        f = diag / math.radians(lens_fov / 2.0)
        fx = fy = f
        cx, cy = (src_w - 1) / 2.0, (src_h - 1) / 2.0
        k = np.zeros(4)
    else:
        fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
        k = np.zeros(4) if D is None else np.asarray(D, dtype=np.float64).reshape(-1)[:4]

    u = (np.arange(cw, dtype=np.float64) + 0.5) / scale - 0.5
    v = (np.arange(ch, dtype=np.float64) + 0.5) / scale - 0.5
    uu, vv = np.meshgrid(u - (out_w - 1) / 2.0, v - (out_h - 1) / 2.0)

    if mode == "panorama":
        fc = out_w / math.radians(out_fov)
        lam = uu / fc
        hgt = vv / fc
        rays = np.stack([np.sin(lam), hgt, np.cos(lam)], axis=-1)
    else:
        fo = (out_w / 2.0) / math.tan(math.radians(min(out_fov, 175.0)) / 2.0)
        rays = np.stack([uu / fo, vv / fo, np.ones_like(uu)], axis=-1)

    rays = rays @ _rotation(yaw, pitch, roll).T
    x, y, z = rays[..., 0], rays[..., 1], rays[..., 2]
    rxy = np.hypot(x, y)
    theta = np.arctan2(rxy, z)
    t2 = theta * theta
    theta_d = theta * (1 + k[0] * t2 + k[1] * t2 ** 2 + k[2] * t2 ** 3 + k[3] * t2 ** 4)
    with np.errstate(invalid="ignore", divide="ignore"):
        sc = np.where(rxy > 1e-12, theta_d / rxy, 0.0)
    map_x = (fx * x * sc + cx).astype(np.float32)
    map_y = (fy * y * sc + cy).astype(np.float32)

    if (cw, ch) != (out_w, out_h):
        map_x = cv2.resize(map_x, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
        map_y = cv2.resize(map_y, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    return cv2.convertMaps(map_x, map_y, cv2.CV_16SC2)


class Processor:
    """Applies :class:`ProcessingSettings` to frames, caching dewarp tables."""

    def __init__(self) -> None:
        self.settings = ProcessingSettings()
        self.calibrations: dict[str, FisheyeCalibration] = {}
        self._maps_key: Optional[tuple] = None
        self._maps: Optional[tuple[np.ndarray, np.ndarray]] = None
        self._lock = threading.Lock()
        self._want: Optional[tuple[tuple, tuple]] = None   # (key, build args) requested
        self._builder: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ calibration
    def calibration_for(self, w: int, h: int) -> Optional[FisheyeCalibration]:
        exact = self.calibrations.get(f"{w}x{h}")
        if exact:
            return exact
        for cal in self.calibrations.values():  # same aspect ratio -> scale K
            if abs(cal.width / cal.height - w / h) < 0.01:
                return cal
        return None

    # ------------------------------------------------------------------ geometry
    def _dewarp(self, frame: np.ndarray) -> np.ndarray:
        s = self.settings
        h, w = frame.shape[:2]
        cal = self.calibration_for(w, h) if s.use_calibration else None
        key = (w, h, s.dewarp, round(s.lens_fov, 2), round(s.out_fov, 2), round(s.yaw, 2),
               round(s.pitch, 2), round(s.roll, 2), id(cal))
        K = cal.scaled_K(w, h) if cal else None
        D = np.array(cal.D) if cal else None
        args = (w, h, w, h, s.dewarp, s.lens_fov, s.out_fov, s.yaw, s.pitch, s.roll, K, D)
        with self._lock:
            maps, cur_key = self._maps, self._maps_key
        if maps is None or cur_key is None or cur_key[:3] != key[:3]:
            # first use, new resolution or new projection: build synchronously
            maps = build_dewarp_maps(*args)
            with self._lock:
                self._maps, self._maps_key = maps, key
        elif cur_key != key:
            # parameter tweak (e.g. dragging pan/tilt): rebuild in the background and keep
            # streaming with the previous tables so the video never stalls
            with self._lock:
                self._want = (key, args)
                if self._builder is None or not self._builder.is_alive():
                    self._builder = threading.Thread(target=self._build_loop, name="elpcam-dewarp", daemon=True)
                    self._builder.start()
        m1, m2 = maps
        return cv2.remap(frame, m1, m2, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

    def _build_loop(self) -> None:
        while True:
            with self._lock:
                want = self._want
                self._want = None
            if want is None:
                return
            key, args = want
            maps = build_dewarp_maps(*args)
            with self._lock:
                if self._maps_key is None or self._maps_key[:2] == key[:2]:
                    self._maps, self._maps_key = maps, key

    @staticmethod
    def _zoom(frame: np.ndarray, z: float, zx: float, zy: float) -> np.ndarray:
        h, w = frame.shape[:2]
        cw, ch = int(w / z), int(h / z)
        free_x, free_y = (w - cw) / 2.0, (h - ch) / 2.0
        x0 = int(round(free_x + zx * free_x))
        y0 = int(round(free_y + zy * free_y))
        crop = frame[y0:y0 + ch, x0:x0 + cw]
        return cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)

    def geometry(self, frame: np.ndarray) -> np.ndarray:
        s = self.settings
        out = frame
        if s.dewarp != "off":
            out = self._dewarp(out)
        if s.mirror and s.flip:
            out = cv2.flip(out, -1)
        elif s.mirror:
            out = cv2.flip(out, 1)
        elif s.flip:
            out = cv2.flip(out, 0)
        if s.rotate == 90:
            out = cv2.rotate(out, cv2.ROTATE_90_CLOCKWISE)
        elif s.rotate == 180:
            out = cv2.rotate(out, cv2.ROTATE_180)
        elif s.rotate == 270:
            out = cv2.rotate(out, cv2.ROTATE_90_COUNTERCLOCKWISE)
        if s.zoom > 1.001:
            out = self._zoom(out, s.zoom, s.zoom_x, s.zoom_y)
        return out

    # ------------------------------------------------------------------ overlays
    def has_overlays(self) -> bool:
        s = self.settings
        return bool(s.timestamp or s.grid or s.crosshair or s.label)

    def overlays(self, frame: np.ndarray, info: str = "") -> np.ndarray:
        s = self.settings
        if not self.has_overlays():
            return frame
        out = frame.copy()
        h, w = out.shape[:2]
        th = max(1, w // 640)
        if s.grid:
            for i in (1, 2):
                cv2.line(out, (w * i // 3, 0), (w * i // 3, h), (255, 255, 255), th, cv2.LINE_AA)
                cv2.line(out, (0, h * i // 3), (w, h * i // 3), (255, 255, 255), th, cv2.LINE_AA)
        if s.crosshair:
            L = max(12, w // 30)
            cx, cy = w // 2, h // 2
            for col, t in (((0, 0, 0), th + 2), ((0, 255, 255), th)):
                cv2.line(out, (cx - L, cy), (cx + L, cy), col, t, cv2.LINE_AA)
                cv2.line(out, (cx, cy - L), (cx, cy + L), col, t, cv2.LINE_AA)
                cv2.circle(out, (cx, cy), L // 2, col, t, cv2.LINE_AA)
        texts = []
        if s.timestamp:
            texts.append(time.strftime("%Y-%m-%d %H:%M:%S") + (f"  {info}" if info else ""))
        if s.label:
            texts.append(s.label)
        if texts:
            fs = max(0.5, w / 1600.0)
            ft = max(1, int(round(fs * 2)))
            y = h - int(12 * fs)
            for t in reversed(texts):
                (tw, tht), base = cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, fs, ft)
                x = int(12 * fs)
                cv2.rectangle(out, (x - 6, y - tht - 8), (x + tw + 6, y + base + 2), (0, 0, 0), -1)
                cv2.putText(out, t, (x, y), cv2.FONT_HERSHEY_SIMPLEX, fs, (255, 255, 255), ft, cv2.LINE_AA)
                y -= tht + base + int(16 * fs)
        return out
