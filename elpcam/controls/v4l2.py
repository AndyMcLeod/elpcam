"""Linux Video4Linux2 backend built on the ``v4l2-ctl`` tool (package ``v4l-utils``).

Using the CLI keeps the backend dependency free and works on Raspberry Pi,
Jetson and desktop Linux alike.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from typing import Optional

from .base import Control, ControlBackend, ControlError, DeviceInfo, VideoMode

_CTRL_RE = re.compile(r"^\s*(\w+)\s+0x([0-9a-f]+)\s+\((\w+)\)\s*:\s*(.*)$")
_MENU_RE = re.compile(r"^\s+(\d+):\s+(.*)$")
_KV_RE = re.compile(r"(\w+)=(-?\w+)")
_FMT_RE = re.compile(r"\[\d+\]:\s+'(\w+)'")
_SIZE_RE = re.compile(r"Size:\s+\w+\s+(\d+)x(\d+)")
_FPS_RE = re.compile(r"\(([\d.]+)\s+fps\)")

ELP_VENDOR_IDS = {"32E4", "05A3", "1BCF", "0C45", "1E4E"}

#: v4l2 control name -> (stable key, display name, group)
NAMES = {
    "brightness": ("brightness", "Brightness", "image"),
    "contrast": ("contrast", "Contrast", "image"),
    "saturation": ("saturation", "Saturation", "image"),
    "hue": ("hue", "Hue", "image"),
    "gamma": ("gamma", "Gamma", "image"),
    "gain": ("gain", "Gain", "image"),
    "sharpness": ("sharpness", "Sharpness", "image"),
    "backlight_compensation": ("backlight_compensation", "Backlight Comp.", "image"),
    "power_line_frequency": ("power_line_frequency", "Power Line Frequency", "image"),
    "white_balance_temperature": ("white_balance", "White Balance", "image"),
    "white_balance_temperature_auto": ("white_balance_auto", "Auto White Balance", "image"),
    "white_balance_automatic": ("white_balance_auto", "Auto White Balance", "image"),
    "hue_auto": ("hue_auto", "Auto Hue", "image"),
    "exposure_auto": ("exposure_mode", "Exposure Mode", "camera"),
    "auto_exposure": ("exposure_mode", "Exposure Mode", "camera"),
    "exposure_absolute": ("exposure", "Exposure (100 µs)", "camera"),
    "exposure_time_absolute": ("exposure", "Exposure (100 µs)", "camera"),
    "exposure_auto_priority": ("low_light_compensation", "Low Light Compensation", "camera"),
    "exposure_dynamic_framerate": ("low_light_compensation", "Low Light Compensation", "camera"),
    "focus_absolute": ("focus", "Focus", "camera"),
    "focus_auto": ("focus_auto", "Auto Focus", "camera"),
    "focus_automatic_continuous": ("focus_auto", "Auto Focus", "camera"),
    "zoom_absolute": ("zoom", "Zoom", "camera"),
    "pan_absolute": ("pan", "Pan", "camera"),
    "tilt_absolute": ("tilt", "Tilt", "camera"),
    "iris_absolute": ("iris", "Iris", "camera"),
}


def _run(args: list[str]) -> str:
    exe = shutil.which("v4l2-ctl")
    if not exe:
        raise ControlError("v4l2-ctl not found - install it with: sudo apt install v4l-utils")
    res = subprocess.run([exe, *args], capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        raise ControlError(res.stderr.strip() or f"v4l2-ctl {' '.join(args)} failed")
    return res.stdout


def _usb_ids(node: str) -> tuple[Optional[str], Optional[str]]:
    base = f"/sys/class/video4linux/{os.path.basename(node)}/device"
    for up in ("..", "../.."):
        vp = os.path.join(base, up, "idVendor")
        pp = os.path.join(base, up, "idProduct")
        if os.path.exists(vp) and os.path.exists(pp):
            with open(vp) as f1, open(pp) as f2:
                return f1.read().strip().upper(), f2.read().strip().upper()
    return None, None


def list_devices() -> list[DeviceInfo]:
    out = []
    for node in sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0)):
        idx = int(re.sub(r"\D", "", node) or 0)
        name_file = f"/sys/class/video4linux/{os.path.basename(node)}/name"
        try:
            with open(name_file) as f:
                name = f.read().strip()
        except OSError:
            name = node
        # Skip metadata-only nodes that UVC exposes alongside each capture node
        try:
            caps = _run(["-d", node, "--all"])
            if "Video Capture" not in caps.split("Device Caps")[-1]:
                continue
        except ControlError:
            pass
        vid, pid = _usb_ids(node)
        is_elp = "elp" in name.lower() or (vid in ELP_VENDOR_IDS if vid else False)
        out.append(DeviceInfo(index=idx, name=name, path=node, vid=vid, pid=pid, is_elp=is_elp))
    return out


class V4L2Controls(ControlBackend):
    name = "v4l2"
    enumerates_modes = True

    def __init__(self, device: DeviceInfo) -> None:
        self.device = device
        self.node = device.path or f"/dev/video{device.index}"
        self._native: dict[str, str] = {}

    def list_controls(self) -> list[Control]:
        text = _run(["-d", self.node, "--list-ctrls-menus"])
        controls: list[Control] = []
        cur: Optional[Control] = None
        for line in text.splitlines():
            m = _CTRL_RE.match(line)
            if m:
                vname, _hex, vtype, rest = m.groups()
                kv = dict(_KV_RE.findall(rest))
                key, label, group = NAMES.get(vname, (vname, vname.replace("_", " ").title(), "other"))
                ctype = {"bool": "bool", "menu": "menu", "intmenu": "menu"}.get(vtype, "int")
                if vtype not in ("int", "bool", "menu", "intmenu"):
                    cur = None
                    continue
                cur = Control(
                    id=key, name=label, group=group, type=ctype,
                    min=int(kv.get("min", 0)), max=int(kv.get("max", 1)), step=int(kv.get("step", 1)),
                    default=int(kv.get("default", 0)), value=int(kv.get("value", 0)),
                    menu={} if ctype == "menu" else None,
                    inactive="inactive" in kv.get("flags", ""), native_id=vname,
                )
                if key == "exposure":
                    cur.unit = "100 µs"
                if key == "white_balance":
                    cur.unit = "K"
                self._native[key] = vname
                controls.append(cur)
                continue
            mm = _MENU_RE.match(line)
            if mm and cur is not None and cur.menu is not None:
                cur.menu[int(mm.group(1))] = mm.group(2).strip()
        return controls

    def set_control(self, cid: str, value: Optional[int] = None, auto: Optional[bool] = None) -> Control:
        if cid not in self._native:
            self.list_controls()
        if cid not in self._native:
            raise ControlError(f"Control '{cid}' is not supported by this camera")
        if value is not None:
            _run(["-d", self.node, f"--set-ctrl={self._native[cid]}={int(value)}"])
        return self.get_control(cid)

    def list_modes(self) -> list[VideoMode]:
        text = _run(["-d", self.node, "--list-formats-ext"])
        modes: list[VideoMode] = []
        fourcc = None
        cur: Optional[VideoMode] = None
        for line in text.splitlines():
            fm = _FMT_RE.search(line)
            if fm:
                fourcc = fm.group(1)
                if fourcc == "YUYV":
                    fourcc = "YUY2"
                continue
            sm = _SIZE_RE.search(line)
            if sm and fourcc:
                cur = VideoMode(fourcc=fourcc, width=int(sm.group(1)), height=int(sm.group(2)), fps=[])
                modes.append(cur)
                continue
            pm = _FPS_RE.search(line)
            if pm and cur is not None:
                cur.fps.append(round(float(pm.group(1)), 3))
        for m in modes:
            m.fps = sorted(set(m.fps), reverse=True)
        return sorted(modes, key=lambda m: (m.fourcc != "MJPG", -m.width * m.height))
