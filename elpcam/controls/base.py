"""Backend-neutral data model for camera devices, video modes and controls."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class DeviceInfo:
    """A video capture device as seen by the operating system."""

    index: int                      # OpenCV capture index for this backend
    name: str                       # friendly name ("USB Camera", "ELP ...")
    path: str = ""                  # OS device path (DirectShow DevicePath / /dev/videoN)
    vid: Optional[str] = None       # USB vendor id (hex, upper case) if known
    pid: Optional[str] = None       # USB product id (hex, upper case) if known
    is_elp: bool = False            # heuristic: looks like an ELP / Ailipu camera

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VideoMode:
    """One pixel format + frame size the camera can deliver."""

    fourcc: str                     # "MJPG", "YUY2", "NV12", ...
    width: int
    height: int
    fps: list[float] = field(default_factory=list)  # discrete frame rates, highest first

    @property
    def key(self) -> str:
        return f"{self.fourcc}:{self.width}x{self.height}"

    @property
    def max_fps(self) -> float:
        return max(self.fps) if self.fps else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        d["megapixels"] = round(self.width * self.height / 1e6, 2)
        return d


@dataclass
class Control:
    """A single adjustable camera parameter (UVC Processing Unit / Camera Terminal)."""

    id: str                         # stable key, e.g. "brightness"
    name: str                       # display name
    group: str                      # "image" | "camera" | "other"
    type: str = "int"               # "int" | "bool" | "menu"
    min: int = 0
    max: int = 0
    step: int = 1
    default: int = 0
    value: int = 0
    auto_supported: bool = False
    manual_supported: bool = True
    auto: bool = False
    menu: Optional[dict[int, str]] = None
    unit: str = ""                  # display hint: "log2 s", "K", "%", ...
    inactive: bool = False          # true when the driver reports it as currently inactive
    native_id: Any = None           # backend-specific identifier (not serialised)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("native_id", None)
        if self.menu is not None:
            d["menu"] = {str(k): v for k, v in self.menu.items()}
        return d


class ControlError(RuntimeError):
    pass


class ControlBackend(ABC):
    """Reads and writes camera controls for one opened device."""

    name: str = "base"
    #: True if this backend can enumerate exact video modes
    enumerates_modes: bool = False

    @abstractmethod
    def list_controls(self) -> list[Control]:
        ...

    @abstractmethod
    def set_control(self, cid: str, value: Optional[int] = None, auto: Optional[bool] = None) -> Control:
        ...

    def get_control(self, cid: str) -> Control:
        for c in self.list_controls():
            if c.id == cid:
                return c
        raise ControlError(f"Unknown control '{cid}'")

    def list_modes(self) -> list[VideoMode]:
        return []

    def reset_defaults(self) -> list[Control]:
        """Put every control back to the driver default (auto where the default is auto)."""
        out = []
        for c in self.list_controls():
            try:
                if c.type == "int" and c.auto_supported and c.id in AUTO_BY_DEFAULT:
                    out.append(self.set_control(c.id, value=c.default, auto=True))
                else:
                    out.append(self.set_control(c.id, value=c.default, auto=False if c.auto_supported else None))
            except Exception:  # keep going; some drivers refuse certain writes
                pass
        return out

    def open_driver_dialog(self) -> bool:
        """Open the native driver property sheet (Windows only). Returns True if shown."""
        return False

    def close(self) -> None:
        pass


#: Controls whose sensible default is "auto" when the driver supports it.
AUTO_BY_DEFAULT = {"white_balance", "exposure", "focus", "iris"}
