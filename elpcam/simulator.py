"""A software stand-in for the ELP-USB500W05G-BL170 (``--simulate``).

Renders a synthetic scene through an ideal 170° equidistant fisheye lens using
the ELP's documented video modes, and applies the image controls (brightness,
contrast, saturation, gamma, gain, white balance, exposure …) so the whole
engine and GUI can be exercised without hardware - handy for demos and CI.
Control ranges are typical UVC values, not measured from a real unit.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Optional

import cv2
import numpy as np

from . import camera_info
from .controls.base import AUTO_BY_DEFAULT, Control, ControlBackend, ControlError, DeviceInfo, VideoMode

SIM_PATH = "sim://elp-usb500w05g-bl170"
SIM_DEVICE = DeviceInfo(index=-1, name="ELP USB500W05G-BL170 (simulated)", path=SIM_PATH, is_elp=True)

# id, name, group, type, min, max, step, default, auto_supported, unit
_SPECS = [
    ("brightness", "Brightness", "image", "int", -64, 64, 1, 0, False, ""),
    ("contrast", "Contrast", "image", "int", 0, 64, 1, 32, False, ""),
    ("hue", "Hue", "image", "int", -40, 40, 1, 0, False, ""),
    ("saturation", "Saturation", "image", "int", 0, 128, 1, 64, False, ""),
    ("sharpness", "Sharpness", "image", "int", 0, 6, 1, 3, False, ""),
    ("gamma", "Gamma", "image", "int", 72, 500, 1, 100, False, ""),
    ("white_balance", "White Balance", "image", "int", 2800, 6500, 10, 4600, True, "K"),
    ("backlight_compensation", "Backlight Comp.", "image", "int", 0, 2, 1, 1, False, ""),
    ("gain", "Gain", "image", "int", 0, 100, 1, 0, False, ""),
    ("power_line_frequency", "Power Line Frequency", "image", "menu", 0, 2, 1, 1, False, ""),
    ("exposure", "Exposure", "camera", "int", -13, -1, 1, -6, True, "log2 s"),
    ("low_light_compensation", "Low Light Compensation", "camera", "bool", 0, 1, 1, 0, False, ""),
]


class SimState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.values = {s[0]: s[7] for s in _SPECS}
        self.auto = {s[0]: (s[8] and s[0] in AUTO_BY_DEFAULT) for s in _SPECS}


class SimulatedControls(ControlBackend):
    name = "simulator"
    enumerates_modes = True

    def __init__(self, state: Optional[SimState] = None) -> None:
        self.state = state or SimState()

    def _ctl(self, spec) -> Control:
        cid, name, group, ctype, mn, mx, st, df, auto_ok, unit = spec
        with self.state.lock:
            v, a = self.state.values[cid], self.state.auto[cid]
        if cid == "exposure" and a:
            v = -6
        menu = {0: "Disabled", 1: "50 Hz", 2: "60 Hz"} if ctype == "menu" else None
        return Control(id=cid, name=name, group=group, type=ctype, min=mn, max=mx, step=st, default=df, value=v,
                       auto_supported=auto_ok, auto=a, menu=menu, unit=unit)

    def list_controls(self) -> list[Control]:
        return [self._ctl(s) for s in _SPECS]

    def set_control(self, cid: str, value: Optional[int] = None, auto: Optional[bool] = None) -> Control:
        spec = next((s for s in _SPECS if s[0] == cid), None)
        if spec is None:
            raise ControlError(f"Control '{cid}' is not supported by this camera")
        with self.state.lock:
            if value is not None:
                v = int(np.clip(int(value), spec[4], spec[5]))
                self.state.values[cid] = spec[4] + round((v - spec[4]) / spec[6]) * spec[6]
            if auto is not None and spec[8]:
                self.state.auto[cid] = bool(auto)
        return self._ctl(spec)

    def list_modes(self) -> list[VideoMode]:
        return [VideoMode(m.fourcc, m.width, m.height, list(m.fps)) for m in camera_info.DOCUMENTED_MODES]


def _world(width: int = 2400) -> np.ndarray:
    """Equirectangular synthetic room (360° x 180°)."""
    h = width // 2
    img = np.zeros((h, width, 3), np.uint8)
    lat = np.linspace(90, -90, h)[:, None]
    sky = np.clip(120 + lat * 1.2, 0, 255)
    img[..., 0] = np.where(lat > 0, 200, 70).astype(np.uint8)              # B
    img[..., 1] = np.where(lat > 0, sky, 90).astype(np.uint8)              # G
    img[..., 2] = np.where(lat > 0, sky * 0.6, 110).astype(np.uint8)       # R
    deg = width / 360.0
    for lon in range(-180, 181, 15):
        x = int((lon + 180) * deg)
        cv2.line(img, (x, 0), (x, h), (235, 235, 235) if lon % 45 == 0 else (160, 160, 160), 3 if lon % 45 == 0 else 1)
    for la in range(-75, 76, 15):
        y = int((90 - la) * deg)
        cv2.line(img, (0, y), (width, y), (235, 235, 235) if la == 0 else (160, 160, 160), 3 if la == 0 else 1)
    # colour checker straight ahead
    colors = [(68, 82, 115), (130, 150, 194), (157, 122, 98), (67, 108, 87), (177, 128, 133), (170, 189, 103),
              (44, 126, 214), (166, 91, 80), (99, 90, 193), (108, 60, 94), (64, 188, 157), (46, 163, 224),
              (150, 61, 56), (73, 148, 70), (60, 54, 175), (31, 199, 231), (149, 86, 187), (161, 133, 8),
              (242, 243, 243), (200, 200, 200), (160, 160, 160), (121, 122, 122), (85, 85, 85), (52, 52, 52)]
    cx, cy, sq = width // 2, h // 2, int(5 * deg)
    for i, c in enumerate(colors):
        r, col = divmod(i, 6)
        x0, y0 = cx + (col - 3) * sq, cy + (r - 2) * sq
        cv2.rectangle(img, (x0 + 2, y0 + 2), (x0 + sq - 2, y0 + sq - 2), c, -1)
    for lon, text in ((-90, "WEST"), (0, "ELP USB500W05G-BL170 SIMULATOR"), (90, "EAST"), (180, "BEHIND")):
        x = int((lon + 180) * deg)
        fs = width / 1800
        (tw, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, fs, 3)
        cv2.putText(img, text, (x - tw // 2 if lon != 180 else x - tw, int(h * 0.33)), cv2.FONT_HERSHEY_SIMPLEX, fs,
                    (255, 255, 255), 3, cv2.LINE_AA)
    return img


class SimulatedCapture:
    """Implements the subset of ``cv2.VideoCapture`` the engine uses."""

    _world_img: Optional[np.ndarray] = None
    _base_cache: dict[tuple[int, int], np.ndarray] = {}

    def __init__(self, state: SimState, lens_fov: float = 170.0) -> None:
        self.state = state
        self.lens_fov = lens_fov
        self.fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        self.w, self.h, self.fps = 2592, 1944, 15.0
        self._open = True
        self._dirty = False
        self._last = 0.0
        if SimulatedCapture._world_img is None:
            SimulatedCapture._world_img = _world()

    # --- VideoCapture API -------------------------------------------------
    def isOpened(self) -> bool:
        return self._open

    def release(self) -> None:
        self._open = False

    def set(self, prop: int, value: float) -> bool:
        if prop == cv2.CAP_PROP_FOURCC:
            self.fourcc = int(value)
        elif prop == cv2.CAP_PROP_FRAME_WIDTH:
            self.w = int(value)
        elif prop == cv2.CAP_PROP_FRAME_HEIGHT:
            self.h = int(value)
        elif prop == cv2.CAP_PROP_FPS:
            self.fps = float(value)
        else:
            return False
        self._dirty = True
        return True

    def get(self, prop: int) -> float:
        if self._dirty:
            self._snap_mode()
        return {cv2.CAP_PROP_FRAME_WIDTH: self.w, cv2.CAP_PROP_FRAME_HEIGHT: self.h, cv2.CAP_PROP_FPS: self.fps,
                cv2.CAP_PROP_FOURCC: self.fourcc}.get(prop, -1)

    def _snap_mode(self) -> None:
        self._dirty = False
        fcc = "".join(chr((self.fourcc >> (8 * i)) & 0xFF) for i in range(4))
        modes = [m for m in camera_info.DOCUMENTED_MODES if m.fourcc == fcc] or camera_info.DOCUMENTED_MODES
        best = min(modes, key=lambda m: abs(m.width - self.w) + abs(m.height - self.h))
        self.w, self.h = best.width, best.height
        self.fps = min(self.fps or best.max_fps, best.max_fps)

    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        if not self._open:
            return False, None
        if self._dirty:
            self._snap_mode()
        period = 1.0 / max(1.0, self.fps)
        wait = self._last + period - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        return True, self._render()

    # --- rendering -----------------------------------------------------------
    def _base(self) -> np.ndarray:
        key = (self.w, self.h)
        if key not in self._base_cache:
            w, h = key
            world = self._world_img
            f = math.hypot(w, h) / 2 / math.radians(self.lens_fov / 2)
            xs, ys = np.meshgrid(np.arange(w) - (w - 1) / 2, np.arange(h) - (h - 1) / 2)
            r = np.hypot(xs, ys)
            theta = r / f
            phi = np.arctan2(ys, xs)
            x = np.sin(theta) * np.cos(phi)
            y = np.sin(theta) * np.sin(phi)
            z = np.cos(theta)
            lon = np.degrees(np.arctan2(x, z))
            lat = -np.degrees(np.arcsin(np.clip(y, -1, 1)))
            WH, WW = world.shape[:2]
            mx = ((lon + 180) / 360 * WW).astype(np.float32)
            my = ((90 - lat) / 180 * WH).astype(np.float32)
            img = cv2.remap(world, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
            # natural vignetting of a cheap fisheye
            vig = (1 - 0.35 * (theta / math.radians(self.lens_fov / 2)) ** 2).clip(0.4, 1)[..., None]
            self._base_cache[key] = (img * vig).astype(np.uint8)
        return self._base_cache[key]

    def _render(self) -> np.ndarray:
        img = self._base().copy()
        w, h = self.w, self.h
        t = time.time()
        cx, cy, R = w // 2, int(h * 0.72), int(min(w, h) * 0.09)
        cv2.circle(img, (cx, cy), R, (30, 30, 30), -1, cv2.LINE_AA)
        cv2.circle(img, (cx, cy), R, (240, 240, 240), max(2, R // 20), cv2.LINE_AA)
        a = (t % 60) / 60 * 2 * math.pi
        cv2.line(img, (cx, cy), (int(cx + R * 0.9 * math.sin(a)), int(cy - R * 0.9 * math.cos(a))),
                 (60, 80, 255), max(2, R // 15), cv2.LINE_AA)
        fs = w / 2600
        cv2.putText(img, time.strftime("%H:%M:%S"), (cx - int(180 * fs), cy + R + int(60 * fs)),
                    cv2.FONT_HERSHEY_SIMPLEX, fs * 1.6, (255, 255, 255), max(1, int(3 * fs)), cv2.LINE_AA)
        return self._apply_controls(img)

    def _apply_controls(self, img: np.ndarray) -> np.ndarray:
        with self.state.lock:
            v = dict(self.state.values)
            a = dict(self.state.auto)
        exp = 0.0 if a.get("exposure") else (v["exposure"] + 6) * 0.5    # stops relative to auto
        gain = 1.0 + v["gain"] / 50.0
        mult = (2 ** exp) * gain
        contrast = v["contrast"] / 32.0
        bright = v["brightness"] * 1.5
        gamma = 100.0 / max(1, v["gamma"])
        x = np.arange(256, dtype=np.float32)
        y = np.clip(x * mult, 0, 255)
        y = np.clip((y - 128) * contrast + 128 + bright, 0, 255)
        y = 255 * (y / 255) ** gamma
        wb = 4600 if a.get("white_balance") else v["white_balance"]
        k = (wb - 4600) / 1900.0       # -1 .. +1; a low colour-temperature preset renders bluer, like a real camera
        luts = [np.clip(y * (1 - 0.25 * k), 0, 255), y, np.clip(y * (1 + 0.25 * k), 0, 255)]  # B, G, R
        lut = np.stack(luts, axis=-1).astype(np.uint8).reshape(256, 1, 3)
        out = cv2.LUT(img, lut)
        sat = v["saturation"] / 64.0
        if abs(sat - 1) > 0.01:
            gray = cv2.cvtColor(cv2.cvtColor(out, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
            out = cv2.addWeighted(out, sat, gray, 1 - sat, 0)
        if v["gain"] > 40:
            noise = np.random.randint(-1, 2, out.shape[:2], np.int16)[..., None] * int(v["gain"] / 8)
            out = np.clip(out.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        return out
