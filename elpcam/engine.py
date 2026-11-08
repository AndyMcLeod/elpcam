"""The ELPCam engine: device discovery, capture, controls, processing and recording.

Everything the GUI does goes through :class:`CameraEngine`, so the engine can
also be scripted directly::

    from elpcam.engine import CameraEngine
    eng = CameraEngine("elpcam-data")
    eng.open()                                   # picks the ELP camera if present
    eng.set_control("exposure", auto=True)
    eng.snapshot(full_res=True)                  # 2592x1944 still
"""

from __future__ import annotations

import collections
import json
import logging
import queue
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

from . import camera_info
from .calibration import Calibrator
from .controls import ControlError, DeviceInfo, VideoMode, capture_api, list_devices, make_backend
from .diagnostics import camera_privacy_problem
from .processing import Processor
from .simulator import SIM_DEVICE, SIM_PATH, SimulatedCapture, SimulatedControls

log = logging.getLogger("elpcam")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".avi", ".mkv", ".mov"}

#: name: (label, extension, [(OpenCV writer API, FOURCC), ...] tried in order)
CONTAINERS: dict[str, tuple[str, str, list[tuple[int, str]]]] = {
    "mp4-h264": ("MP4 · H.264 (plays in browser)", ".mp4", [(cv2.CAP_MSMF, "H264"), (cv2.CAP_FFMPEG, "avc1")]),
    "mp4": ("MP4 · MPEG-4 (fast, small)", ".mp4", [(cv2.CAP_FFMPEG, "mp4v"), (cv2.CAP_ANY, "mp4v")]),
    "avi": ("AVI · Motion-JPEG (highest quality)", ".avi", [(cv2.CAP_FFMPEG, "MJPG"), (cv2.CAP_ANY, "MJPG")]),
    "avi-xvid": ("AVI · XviD", ".avi", [(cv2.CAP_FFMPEG, "XVID"), (cv2.CAP_ANY, "XVID")]),
}
_available_containers: Optional[list[str]] = None


def available_containers() -> list[str]:
    """Containers whose encoder actually opens on this machine (probed once)."""
    global _available_containers
    if _available_containers is None:
        import tempfile

        ok = []
        prev = cv2.utils.logging.getLogLevel() if hasattr(cv2, "utils") else None
        try:
            if prev is not None:
                cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
            with tempfile.TemporaryDirectory() as d:
                for key, (_label, ext, cands) in CONTAINERS.items():
                    for api, fcc in cands:
                        if key == "mp4-h264" and api == cv2.CAP_FFMPEG:
                            continue  # would need the separately downloaded OpenH264 DLL
                        w = cv2.VideoWriter(str(Path(d) / f"probe_{key}{ext}"), api, cv2.VideoWriter_fourcc(*fcc),
                                            15, (320, 240))
                        good = w.isOpened()
                        w.release()
                        if good:
                            ok.append(key)
                            break
        finally:
            if prev is not None:
                cv2.utils.logging.setLogLevel(prev)
        _available_containers = ok
    return _available_containers


def resolve_container(container: str, width: int) -> str:
    """'auto' -> H.264 when it can keep up (<= 1920 px wide), otherwise fast MPEG-4."""
    avail = available_containers()
    if container == "auto":
        if "mp4-h264" in avail and width <= 1920:
            return "mp4-h264"
        return "mp4" if "mp4" in avail else (avail[0] if avail else "avi")
    return container if container in CONTAINERS else "mp4"


def _stamp() -> str:
    t = time.time()
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(t)) + f"_{int((t % 1) * 1000):03d}"


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w\- .]+", "_", name).strip(" .")
    return name[:64] or "preset"


def _write_image(path: Path, frame: np.ndarray, quality: int = 95) -> None:
    ext = path.suffix.lower()
    params: list[int] = []
    if ext in (".jpg", ".jpeg"):
        params = [cv2.IMWRITE_JPEG_QUALITY, int(quality)]
    elif ext == ".png":
        params = [cv2.IMWRITE_PNG_COMPRESSION, 3]
    ok, buf = cv2.imencode(ext, frame, params)
    if not ok:
        raise RuntimeError(f"Could not encode {ext} image")
    path.write_bytes(buf.tobytes())  # imencode+write handles non-ASCII paths on Windows


class FpsMeter:
    def __init__(self, window: float = 2.0) -> None:
        self.window = window
        self.ts: collections.deque[float] = collections.deque()

    def tick(self, t: float) -> None:
        self.ts.append(t)
        while self.ts and t - self.ts[0] > self.window:
            self.ts.popleft()

    @property
    def fps(self) -> float:
        if len(self.ts) < 2:
            return 0.0
        span = self.ts[-1] - self.ts[0]
        if time.time() - self.ts[-1] > 1.5:
            return 0.0
        return (len(self.ts) - 1) / span if span > 0 else 0.0


class Recorder:
    """Writes frames to a video file at a constant frame rate on its own thread."""

    def __init__(self, path: Path, container: str, fps: float, size: tuple[int, int]) -> None:
        _label, ext, cands = CONTAINERS.get(container, CONTAINERS["mp4"])
        self.container = container
        self.path = path.with_suffix(ext)
        self.fps = max(1.0, float(fps))
        self.size = size
        self.writer = None
        for api, fc in cands:
            if container == "mp4-h264" and api == cv2.CAP_FFMPEG:
                continue
            w = cv2.VideoWriter(str(self.path), api, cv2.VideoWriter_fourcc(*fc), self.fps, size)
            if w.isOpened():
                self.writer, self.codec = w, fc
                break
            w.release()
        if self.writer is None:
            raise RuntimeError(f"No working video encoder for {container}")
        self.q: "queue.Queue[Optional[tuple[np.ndarray, float]]]" = queue.Queue(maxsize=120)
        self.started = time.time()
        self.frames_written = 0
        self.dropped = 0
        self._next_t: Optional[float] = None
        self._t = threading.Thread(target=self._run, name="elpcam-recorder", daemon=True)
        self._t.start()

    def push(self, frame: np.ndarray, t: float) -> None:
        try:
            self.q.put_nowait((frame, t))
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        period = 1.0 / self.fps
        while True:
            item = self.q.get()
            if item is None:
                break
            frame, t = item
            if (frame.shape[1], frame.shape[0]) != self.size:
                frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
            if self._next_t is None:
                self._next_t = t
            if t + period * 0.5 < self._next_t:
                continue  # camera faster than output rate -> drop
            n = 0
            while self._next_t <= t + period * 0.5 and n < int(self.fps * 2):
                self.writer.write(frame)  # repeat frames if the camera slowed down
                self.frames_written += 1
                self._next_t += period
                n += 1
            if n >= int(self.fps * 2):
                self._next_t = t + period
        self.writer.release()

    def stop(self) -> dict[str, Any]:
        self.q.put(None)
        self._t.join(timeout=30)
        return self.info()

    def info(self) -> dict[str, Any]:
        return {"file": self.path.name, "codec": self.codec, "container": self.container, "fps": self.fps,
                "size": list(self.size),
                "elapsed": round(time.time() - self.started, 1), "frames": self.frames_written,
                "dropped": self.dropped}


class Timelapse:
    def __init__(self, engine: "CameraEngine", interval: float, video_fps: float, make_video: bool) -> None:
        self.engine = engine
        self.interval = max(0.2, float(interval))
        self.video_fps = max(1.0, float(video_fps))
        self.make_video = make_video
        self.folder = engine.captures_dir / f"timelapse_{_stamp()}"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self.started = time.time()
        self.writer: Optional[cv2.VideoWriter] = None
        self.wsize: tuple[int, int] = (0, 0)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="elpcam-timelapse", daemon=True)
        self._t.start()

    def _run(self) -> None:
        nxt = time.time()
        while not self._stop.is_set():
            frame = self.engine.capture_frame()
            if frame is not None:
                _write_image(self.folder / f"frame_{self.count:05d}.jpg", frame, 92)
                if self.make_video:
                    if self.writer is None:
                        self.wsize = (frame.shape[1], frame.shape[0])
                        self.writer = cv2.VideoWriter(str(self.folder.with_suffix(".mp4")),
                                                      cv2.VideoWriter_fourcc(*"mp4v"), self.video_fps, self.wsize)
                    if (frame.shape[1], frame.shape[0]) != self.wsize:
                        frame = cv2.resize(frame, self.wsize, interpolation=cv2.INTER_AREA)
                    self.writer.write(frame)
                self.count += 1
            nxt += self.interval
            if nxt < time.time():  # fell behind (e.g. mode switch) - don't burst
                nxt = time.time() + self.interval
            self._stop.wait(max(0.0, nxt - time.time()))
        if self.writer is not None:
            self.writer.release()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._t.join(timeout=10)
        return self.info()

    def info(self) -> dict[str, Any]:
        return {"folder": self.folder.name, "frames": self.count, "interval": self.interval,
                "elapsed": round(time.time() - self.started, 1),
                "video": self.folder.with_suffix(".mp4").name if self.make_video else None}


class CameraEngine:
    """Owns one camera at a time and exposes every operation the GUI needs."""

    def __init__(self, data_dir: str | Path = "elpcam-data", simulate: bool = False) -> None:
        self.simulate = simulate
        self.data_dir = Path(data_dir).resolve()
        self.captures_dir = self.data_dir / "captures"
        self.presets_dir = self.data_dir / "presets"
        self.thumbs_dir = self.captures_dir / ".thumbs"
        for d in (self.captures_dir, self.presets_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._settings_path = self.data_dir / "settings.json"
        self.settings: dict[str, Any] = self._load_settings()

        self.processor = Processor()
        self.processor.settings.update(self.settings.get("processing", {}))
        self.calibrator = Calibrator(self.data_dir / "calibration.json")
        self.processor.calibrations = self.calibrator.load()

        self.preview_width = int(self.settings.get("preview_width", 1280))
        self.jpeg_quality = int(self.settings.get("jpeg_quality", 80))

        self.device: Optional[DeviceInfo] = None
        self.backend = None
        self.modes: list[VideoMode] = []
        self.modes_documented = False
        self.mode: Optional[VideoMode] = None
        self.fps_req: Optional[float] = None
        self.actual_size: Optional[tuple[int, int]] = None
        self.state = "closed"
        self.last_error = ""

        self._cap: Optional[cv2.VideoCapture] = None
        self._gen = 0
        self._thread: Optional[threading.Thread] = None
        self._op = threading.RLock()
        self._cond = threading.Condition()
        self._raw: Optional[np.ndarray] = None
        self._frame: Optional[np.ndarray] = None
        self._frame_id = 0
        self._frame_t = 0.0
        self._jpeg_lock = threading.Lock()
        self._jpeg: tuple[int, Optional[bytes]] = (-1, None)
        self._fps = FpsMeter()
        self._proc_ms = 0.0
        self.frames_total = 0

        self.recorder: Optional[Recorder] = None
        self.timelapse: Optional[Timelapse] = None
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=200)
        self._event_id = 0

    # ================================================================ settings / events
    def _load_settings(self) -> dict[str, Any]:
        try:
            return json.loads(self._settings_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_settings(self) -> None:
        self.settings["processing"] = self.processor.settings.to_dict()
        self.settings["preview_width"] = self.preview_width
        self.settings["jpeg_quality"] = self.jpeg_quality
        try:
            tmp = self._settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
            tmp.replace(self._settings_path)
        except Exception as exc:  # pragma: no cover
            log.warning("Could not save settings: %s", exc)

    def event(self, level: str, msg: str) -> None:
        self._event_id += 1
        self._events.append({"id": self._event_id, "t": time.time(), "level": level, "msg": msg})
        getattr(log, "warning" if level == "warning" else "error" if level == "error" else "info")(msg)

    def events_since(self, since: int) -> list[dict[str, Any]]:
        return [e for e in list(self._events) if e["id"] > since]

    # ================================================================ devices
    def list_devices(self) -> list[DeviceInfo]:
        try:
            devs = list_devices()
        except Exception as exc:
            log.warning("Device enumeration failed: %s", exc)
            devs = []
        if self.simulate:
            devs.append(SIM_DEVICE)
        return devs

    def _make_backend(self, dev: DeviceInfo):
        if dev.path == SIM_PATH:
            return SimulatedControls()
        return make_backend(dev, lambda: self._cap)

    def _pick_device(self, index: Optional[int], path: Optional[str]) -> DeviceInfo:
        devs = self.list_devices()
        if not devs:
            raise ControlError(camera_privacy_problem()
                               or "No cameras found. Check the USB cable and the OS camera privacy settings.")
        if path:
            for d in devs:
                if d.path == path:
                    return d
        if index is not None:
            for d in devs:
                if d.index == index:
                    return d
            raise ControlError(f"No camera with index {index}")
        saved = self.settings.get("device_path")
        for d in devs:
            if saved and d.path == saved:
                return d
        for d in devs:
            if d.is_elp:
                return d
        return devs[0]

    @staticmethod
    def _default_mode(modes: list[VideoMode]) -> Optional[VideoMode]:
        if not modes:
            return None
        mjpg = [m for m in modes if m.fourcc == "MJPG"] or modes
        return max(mjpg, key=lambda m: (m.width * m.height, m.max_fps))

    def _find_mode(self, key: Optional[str]) -> Optional[VideoMode]:
        if not key:
            return None
        for m in self.modes:
            if m.key == key:
                return m
        mm = re.match(r"(\w+):(\d+)x(\d+)", key)
        if mm:  # allow arbitrary modes when the OS could not enumerate
            return VideoMode(mm.group(1), int(mm.group(2)), int(mm.group(3)), [])
        return None

    def max_mode(self) -> Optional[VideoMode]:
        return self._default_mode(self.modes)

    # ================================================================ open / close
    def open(self, index: Optional[int] = None, path: Optional[str] = None,
             mode_key: Optional[str] = None, fps: Optional[float] = None) -> dict[str, Any]:
        with self._op:
            self.close()
            self.state = "opening"
            try:
                dev = self._pick_device(index, path)
                self.device = dev
                self.backend = self._make_backend(dev)
                try:
                    self.modes = self.backend.list_modes()
                except Exception as exc:
                    self.event("warning", f"Could not enumerate modes: {exc}")
                    self.modes = []
                self.modes_documented = False
                if not self.modes and dev.is_elp:
                    self.modes = [VideoMode(m.fourcc, m.width, m.height, list(m.fps))
                                  for m in camera_info.DOCUMENTED_MODES]
                    self.modes_documented = True
                same_dev = self.settings.get("device_path") == dev.path
                mode = (self._find_mode(mode_key)
                        or (self._find_mode(self.settings.get("mode_key")) if same_dev else None)
                        or self._default_mode(self.modes))
                if fps is None and same_dev and mode is not None and self.settings.get("mode_key") == mode.key:
                    fps = self.settings.get("fps")
                self._start_capture(mode, fps)
                self.settings["device_path"] = dev.path
                self.settings["device_name"] = dev.name
                if self.settings.get("restore_controls", True):
                    self._restore_controls()
                self.save_settings()
                self.event("info", f"Opened {dev.name} ({self.describe_mode()})")
            except Exception as exc:
                self._release_cap()
                if self.backend is not None:
                    try:
                        self.backend.close()
                    except Exception:
                        pass
                self.backend = None
                self.device = None
                self.modes = []
                self.mode = None
                self.state = "error"
                self.last_error = str(exc)
                self.event("error", f"Open failed: {exc}")
                raise
            return self.status()

    def close(self) -> None:
        with self._op:
            if self.recorder:
                self.stop_recording()
            if self.timelapse:
                self.stop_timelapse()
            self._stop_capture()
            if self.backend is not None:
                try:
                    self.backend.close()
                except Exception:
                    pass
            if self.device is not None and self.state != "error":
                self.event("info", f"Closed {self.device.name}")
            self.backend = None
            self.device = None
            self.modes = []
            self.mode = None
            self.actual_size = None
            self.state = "closed"
            with self._cond:
                self._raw = self._frame = None
                self._frame_id += 1
                self._cond.notify_all()

    def _open_cap(self, mode: Optional[VideoMode], fps: Optional[float]) -> cv2.VideoCapture:
        assert self.device is not None
        if self.device.path == SIM_PATH:
            state = self.backend.state if isinstance(self.backend, SimulatedControls) else None
            cap = SimulatedCapture(state or SimulatedControls().state)
        else:
            cap = cv2.VideoCapture(self.device.index, capture_api())
        if not cap.isOpened():
            cap.release()
            reason = camera_privacy_problem()
            if reason:
                raise ControlError(reason)
            raise ControlError(f"Could not open '{self.device.name}'. Is another application using it, "
                               "or is camera access blocked in the OS privacy settings?")
        if mode is not None:
            if mode.fourcc and len(mode.fourcc) == 4:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*mode.fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode.height)
            want = fps or mode.max_fps
            if want:
                cap.set(cv2.CAP_PROP_FPS, float(want))
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return cap

    def _start_capture(self, mode: Optional[VideoMode], fps: Optional[float]) -> None:
        cap = self._open_cap(mode, fps)
        ok, frame = False, None
        t0 = time.time()
        while time.time() - t0 < 5.0:
            ok, frame = cap.read()
            if ok and frame is not None:
                break
            time.sleep(0.05)
        if not ok or frame is None:
            cap.release()
            raise ControlError("Camera opened but delivered no frames (try a lower resolution or another USB port).")
        self._cap = cap
        self.mode = mode
        self.fps_req = fps or (mode.max_fps if mode else None)
        self.actual_size = (frame.shape[1], frame.shape[0])
        if mode and (mode.width, mode.height) != self.actual_size:
            self.event("warning", f"Requested {mode.width}x{mode.height}, camera delivers "
                                  f"{self.actual_size[0]}x{self.actual_size[1]}")
        self._fps = FpsMeter()
        self._gen += 1
        gen = self._gen
        self.state = "live"
        self.last_error = ""
        self._publish(frame, time.time())
        self._thread = threading.Thread(target=self._loop, args=(gen,), name="elpcam-capture", daemon=True)
        self._thread.start()

    def _stop_capture(self) -> None:
        self._gen += 1
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None
        self._release_cap()

    def _release_cap(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._cap = None

    def _reconfigure(self, mode: Optional[VideoMode], fps: Optional[float]) -> None:
        with self._op:
            self._stop_capture()
            try:
                self._start_capture(mode, fps)
            except Exception:
                self.state = "error"
                raise

    # ================================================================ capture loop
    def _publish(self, raw: np.ndarray, t: float) -> None:
        t0 = time.perf_counter()
        try:
            proc = self.processor.geometry(raw)
        except Exception as exc:  # never kill the stream because of a processing error
            proc = raw
            if not getattr(self, "_proc_err_logged", False):
                self.event("warning", f"Processing error: {exc}")
                self._proc_err_logged = True
        self._proc_ms = self._proc_ms * 0.9 + (time.perf_counter() - t0) * 1000 * 0.1
        with self._cond:
            self._raw = raw
            self._frame = proc
            self._frame_id += 1
            self._frame_t = t
            self._cond.notify_all()
        rec = self.recorder
        if rec is not None:
            out = self.processor.overlays(proc, self._overlay_info()) \
                if self.processor.settings.overlays_on_capture else proc
            rec.push(out, t)

    def _loop(self, gen: int) -> None:
        fails = 0
        while gen == self._gen:
            cap = self._cap
            if cap is None:
                break
            ok, frame = cap.read()
            if gen != self._gen:
                break
            if not ok or frame is None:
                fails += 1
                if fails == 20:
                    self.event("warning", "Camera stopped delivering frames - waiting…")
                if fails >= 60:
                    self._reconnect(gen)
                    return
                time.sleep(0.05)
                continue
            fails = 0
            t = time.time()
            self._fps.tick(t)
            self.frames_total += 1
            self._publish(frame, t)

    def _reconnect(self, gen: int) -> None:
        """Camera unplugged or stalled: keep trying to reopen it."""
        self.state = "reconnecting"
        self.event("warning", "Camera lost - trying to reconnect (re-plug the USB cable if needed)")
        self._release_cap()
        dev = self.device
        while gen == self._gen and dev is not None:
            time.sleep(2.0)
            try:
                devs = self.list_devices()
                match = next((d for d in devs if d.path and d.path == dev.path), None)
                if match is None:
                    continue
                self.device = match
                cap = self._open_cap(self.mode, self.fps_req)
                ok, frame = cap.read()
                if not ok:
                    cap.release()
                    continue
                if gen != self._gen:
                    cap.release()
                    return
                self._cap = cap
                self.state = "live"
                self.event("info", "Camera reconnected")
                try:
                    if self.backend is not None:
                        self.backend.close()
                    self.backend = self._make_backend(match)
                except Exception:
                    pass
                self._loop(gen)
                return
            except Exception:
                continue

    # ================================================================ frames
    def wait_frame(self, last_id: int, timeout: float = 2.0) -> tuple[int, Optional[np.ndarray]]:
        with self._cond:
            if self._frame_id == last_id:
                self._cond.wait(timeout)
            return self._frame_id, self._frame

    def _overlay_info(self) -> str:
        if self.actual_size is None:
            return ""
        return f"{self.actual_size[0]}x{self.actual_size[1]}"

    def preview_jpeg(self, last_id: int, timeout: float = 2.0) -> tuple[int, Optional[bytes]]:
        fid, frame = self.wait_frame(last_id, timeout)
        if frame is None:
            return fid, None
        with self._jpeg_lock:
            if self._jpeg[0] == fid:
                return self._jpeg
            img = frame
            pw = self.preview_width
            if pw and img.shape[1] > pw:
                img = cv2.resize(img, (pw, int(round(img.shape[0] * pw / img.shape[1]))),
                                 interpolation=cv2.INTER_AREA)
            img = self.processor.overlays(img, self._overlay_info())
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
            self._jpeg = (fid, buf.tobytes() if ok else None)
            return self._jpeg

    def capture_frame(self, raw: bool = False) -> Optional[np.ndarray]:
        """Latest frame at full resolution (processed + overlays unless ``raw``)."""
        with self._cond:
            r, f = self._raw, self._frame
        if raw:
            return None if r is None else r.copy()
        if f is None:
            return None
        if self.processor.settings.overlays_on_capture:
            return self.processor.overlays(f, self._overlay_info())
        return f.copy()

    # ================================================================ stills
    def snapshot(self, fmt: str = "jpg", quality: int = 95, raw: bool = False,
                 full_res: bool = False) -> dict[str, Any]:
        fmt = fmt.lower().lstrip(".")
        if fmt not in ("jpg", "png", "bmp", "tif"):
            fmt = "jpg"
        if self.state != "live":
            raise ControlError("Camera is not streaming")
        if full_res:
            best = self.max_mode()
            if best is not None and self.mode is not None and best.key != self.mode.key:
                return self._full_res_still(best, fmt, quality, raw)
        frame = self.capture_frame(raw=raw)
        if frame is None:
            raise ControlError("No frame available yet")
        return self._save_still(frame, fmt, quality, raw)

    def _save_still(self, frame: np.ndarray, fmt: str, quality: int, raw: bool) -> dict[str, Any]:
        path = self.captures_dir / f"IMG_{_stamp()}{'_raw' if raw else ''}.{fmt}"
        _write_image(path, frame, quality)
        h, w = frame.shape[:2]
        self.event("info", f"Saved {path.name} ({w}x{h})")
        return {"file": path.name, "width": w, "height": h, "size": path.stat().st_size}

    def _full_res_still(self, best: VideoMode, fmt: str, quality: int, raw: bool) -> dict[str, Any]:
        with self._op:
            if self.recorder:
                raise ControlError("Stop recording before taking a full-resolution still")
            orig_mode, orig_fps = self.mode, self.fps_req
            self.event("info", f"Switching to {best.width}x{best.height} for still…")
            try:
                self._reconfigure(best, best.max_fps)
                last = self._frame_id
                got = 0
                t0 = time.time()
                while got < 6 and time.time() - t0 < 6:  # let auto exposure settle
                    fid, _ = self.wait_frame(last, 1.0)
                    if fid != last:
                        got += 1
                        last = fid
                frame = self.capture_frame(raw=raw)
                if frame is None:
                    raise ControlError("No frame at full resolution")
                return self._save_still(frame, fmt, quality, raw)
            finally:
                self._reconfigure(orig_mode, orig_fps)

    # ================================================================ mode
    def set_mode(self, mode_key: str, fps: Optional[float] = None) -> dict[str, Any]:
        with self._op:
            if self.device is None:
                raise ControlError("No camera open")
            if self.recorder:
                raise ControlError("Stop recording before changing the video mode")
            mode = self._find_mode(mode_key)
            if mode is None:
                raise ControlError(f"Unknown mode {mode_key}")
            if fps is not None and mode.fps and fps not in mode.fps:
                fps = min(mode.fps, key=lambda f: abs(f - fps))
            self._reconfigure(mode, fps)
            self.settings["mode_key"] = mode.key
            self.settings["fps"] = self.fps_req
            self.save_settings()
            self.event("info", f"Mode {self.describe_mode()}")
            return self.status()

    def describe_mode(self) -> str:
        if self.mode is None:
            return "driver default"
        fps = f"@{self.fps_req:g}fps" if self.fps_req else ""
        return f"{self.mode.fourcc} {self.mode.width}x{self.mode.height}{fps}"

    # ================================================================ controls
    def list_controls(self) -> list[dict[str, Any]]:
        if self.backend is None:
            return []
        return [c.to_dict() for c in self.backend.list_controls()]

    def _device_key(self) -> str:
        return self.device.path or self.device.name if self.device else ""

    def set_control(self, cid: str, value: Optional[int] = None, auto: Optional[bool] = None) -> dict[str, Any]:
        if self.backend is None:
            raise ControlError("No camera open")
        c = self.backend.set_control(cid, value, auto)
        store = self.settings.setdefault("controls", {}).setdefault(self._device_key(), {})
        store[cid] = {"value": c.value, "auto": c.auto}
        self._save_soon()
        return c.to_dict()

    def reset_controls(self) -> list[dict[str, Any]]:
        if self.backend is None:
            raise ControlError("No camera open")
        out = [c.to_dict() for c in self.backend.reset_defaults()]
        self.settings.setdefault("controls", {}).pop(self._device_key(), None)
        self.save_settings()
        self.event("info", "Controls reset to driver defaults")
        return out

    def _restore_controls(self) -> None:
        saved = self.settings.get("controls", {}).get(self._device_key(), {})
        self._apply_controls(saved)

    def _apply_controls(self, values: dict[str, Any]) -> int:
        if not values or self.backend is None:
            return 0
        n = 0
        # auto flags first so manual values are not overridden by the auto loop
        for cid, v in sorted(values.items(), key=lambda kv: not kv[1].get("auto", False)):
            try:
                self.backend.set_control(cid, v.get("value"), v.get("auto"))
                n += 1
            except Exception:
                pass
        return n

    def open_driver_dialog(self) -> bool:
        if self.backend is None:
            raise ControlError("No camera open")
        return self.backend.open_driver_dialog()

    _save_timer: Optional[threading.Timer] = None

    def _save_soon(self) -> None:
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(1.0, self.save_settings)
        self._save_timer.daemon = True
        self._save_timer.start()

    # ================================================================ processing
    def update_processing(self, data: dict[str, Any]) -> dict[str, Any]:
        self.processor.settings.update(data)
        if "preview_width" in data:
            self.preview_width = max(0, int(data["preview_width"]))
        if "jpeg_quality" in data:
            self.jpeg_quality = int(np.clip(int(data["jpeg_quality"]), 30, 100))
        with self._jpeg_lock:
            self._jpeg = (-1, None)
        # re-run processing on the last raw frame so a paused/slow camera updates immediately
        with self._cond:
            raw = self._raw
        if raw is not None and self.state == "live" and self.fps_actual() < 2:
            self._publish(raw, time.time())
        self._save_soon()
        return self.processing_state()

    def processing_state(self) -> dict[str, Any]:
        d = self.processor.settings.to_dict()
        d["preview_width"] = self.preview_width
        d["jpeg_quality"] = self.jpeg_quality
        size = self.actual_size
        cal = self.processor.calibration_for(*size) if size else None
        d["calibration"] = None if cal is None else {
            "size": f"{cal.width}x{cal.height}", "rms": round(cal.rms, 3), "frames": cal.frames,
            "created": cal.created}
        return d

    # ================================================================ calibration
    def calibration_capture(self, cols: int, rows: int, square_mm: float) -> dict[str, Any]:
        self.calibrator.configure(cols, rows, square_mm)
        frame = self.capture_frame(raw=True)
        if frame is None:
            raise ControlError("No frame available")
        return self.calibrator.add_frame(frame)

    def calibration_compute(self) -> dict[str, Any]:
        cal = self.calibrator.compute()
        self.processor.calibrations[f"{cal.width}x{cal.height}"] = cal
        self.calibrator.save(self.processor.calibrations)
        self.event("info", f"Fisheye calibration saved for {cal.width}x{cal.height} (RMS {cal.rms:.3f}px)")
        return {"size": f"{cal.width}x{cal.height}", "rms": cal.rms, "frames": cal.frames,
                "K": cal.K, "D": cal.D}

    def calibration_clear(self, all_sizes: bool = False) -> None:
        self.calibrator.reset()
        if all_sizes:
            self.processor.calibrations.clear()
        elif self.actual_size:
            self.processor.calibrations.pop(f"{self.actual_size[0]}x{self.actual_size[1]}", None)
        self.calibrator.save(self.processor.calibrations)

    # ================================================================ recording
    def start_recording(self, container: str = "auto", fps: Optional[float] = None) -> dict[str, Any]:
        with self._op:
            if self.recorder:
                return self.recorder.info()
            if self.state != "live":
                raise ControlError("Camera is not streaming")
            with self._cond:
                f = self._frame
            if f is None:
                raise ControlError("No frame available")
            # nominal rate; the recorder repeats/drops frames so playback stays real-time
            rate = fps or self.fps_req or round(self._fps.fps) or 15
            size = (f.shape[1], f.shape[0])
            path = self.captures_dir / f"VID_{_stamp()}"
            self.recorder = Recorder(path, resolve_container(container, size[0]), float(rate), size)
            self.settings["record_container"] = container
            self.event("info", f"Recording {self.recorder.path.name} ({size[0]}x{size[1]} @ {rate:g} fps, "
                               f"{self.recorder.codec})")
            return self.recorder.info()

    def stop_recording(self) -> Optional[dict[str, Any]]:
        rec = self.recorder
        if rec is None:
            return None
        self.recorder = None
        info = rec.stop()
        self.event("info", f"Saved {info['file']} ({info['elapsed']} s, {info['frames']} frames)")
        return info

    def start_timelapse(self, interval: float = 5.0, video_fps: float = 30.0, make_video: bool = True) -> dict:
        if self.timelapse:
            return self.timelapse.info()
        if self.state != "live":
            raise ControlError("Camera is not streaming")
        self.timelapse = Timelapse(self, interval, video_fps, make_video)
        self.event("info", f"Time-lapse started: one frame every {self.timelapse.interval:g} s")
        return self.timelapse.info()

    def stop_timelapse(self) -> Optional[dict[str, Any]]:
        tl = self.timelapse
        if tl is None:
            return None
        self.timelapse = None
        info = tl.stop()
        self.event("info", f"Time-lapse finished: {info['frames']} frames in {info['folder']}")
        return info

    # ================================================================ presets
    def list_presets(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.presets_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                out.append({"name": d.get("name", p.stem), "file": p.name, "saved": d.get("saved"),
                            "mode": d.get("mode"), "controls": len(d.get("controls", {}))})
            except Exception:
                continue
        return out

    def save_preset(self, name: str, include_mode: bool = True) -> dict[str, Any]:
        controls = {c["id"]: {"value": c["value"], "auto": c["auto"]} for c in self.list_controls()}
        data = {
            "name": name, "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
            "camera": self.device.name if self.device else None,
            "controls": controls, "processing": self.processor.settings.to_dict(),
            "mode": self.mode.key if (include_mode and self.mode) else None,
            "fps": self.fps_req if include_mode else None,
        }
        path = self.presets_dir / f"{_safe_name(name)}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.event("info", f"Preset '{name}' saved")
        return {"name": name, "file": path.name}

    def apply_preset(self, file: str) -> dict[str, Any]:
        path = self._preset_path(file)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("mode") and self.device is not None and (self.mode is None or data["mode"] != self.mode.key):
            try:
                self.set_mode(data["mode"], data.get("fps"))
            except Exception as exc:
                self.event("warning", f"Preset mode not applied: {exc}")
        n = self._apply_controls(data.get("controls", {}))
        if data.get("processing"):
            self.update_processing(data["processing"])
        self.event("info", f"Preset '{data.get('name', path.stem)}' applied ({n} controls)")
        return self.status()

    def delete_preset(self, file: str) -> None:
        self._preset_path(file).unlink()

    def _preset_path(self, file: str) -> Path:
        p = (self.presets_dir / Path(file).name).resolve()
        if p.parent != self.presets_dir.resolve() or not p.exists():
            raise FileNotFoundError(file)
        return p

    # ================================================================ media
    def media_path(self, rel: str) -> Path:
        p = (self.captures_dir / rel).resolve()
        root = self.captures_dir.resolve()
        if root not in p.parents or not p.is_file():
            raise FileNotFoundError(rel)
        return p

    def list_media(self) -> list[dict[str, Any]]:
        items = []
        root = self.captures_dir
        for p in root.iterdir():
            if p.name.startswith("."):
                continue
            if p.is_dir():
                frames = sorted(p.glob("*.jpg"))
                st = p.stat()
                items.append({"name": p.name, "kind": "folder", "size": sum(f.stat().st_size for f in frames),
                              "mtime": st.st_mtime, "count": len(frames),
                              "cover": f"{p.name}/{frames[-1].name}" if frames else None})
                continue
            ext = p.suffix.lower()
            kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None
            if kind:
                st = p.stat()
                items.append({"name": p.name, "kind": kind, "size": st.st_size, "mtime": st.st_mtime})
        items.sort(key=lambda i: i["mtime"], reverse=True)
        return items

    def thumbnail(self, rel: str, width: int = 360) -> bytes:
        src = self.media_path(rel)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)
        key = re.sub(r"[^\w.-]", "_", rel) + f"_{int(src.stat().st_mtime)}_{width}.jpg"
        thumb = self.thumbs_dir / key
        if thumb.exists():
            return thumb.read_bytes()
        img = None
        if src.suffix.lower() in VIDEO_EXT:
            cap = cv2.VideoCapture(str(src))
            ok, img = cap.read()
            cap.release()
            if not ok:
                img = None
        else:
            data = np.frombuffer(src.read_bytes(), np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_REDUCED_COLOR_4)
        if img is None:
            img = np.full((width * 3 // 4, width, 3), 40, np.uint8)
        h, w = img.shape[:2]
        img = cv2.resize(img, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        thumb.write_bytes(buf.tobytes())
        return buf.tobytes()

    def delete_media(self, rel: str) -> None:
        p = (self.captures_dir / rel).resolve()
        root = self.captures_dir.resolve()
        if root not in p.parents:
            raise FileNotFoundError(rel)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.is_file():
            p.unlink()
        else:
            raise FileNotFoundError(rel)
        self.event("info", f"Deleted {rel}")

    # ================================================================ status
    def fps_actual(self) -> float:
        return self._fps.fps

    def status(self, since: int = 0) -> dict[str, Any]:
        with self._cond:
            f = self._frame
        return {
            "state": self.state,
            "error": self.last_error,
            "device": self.device.to_dict() if self.device else None,
            "backend": self.backend.name if self.backend else None,
            "mode": self.mode.to_dict() if self.mode else None,
            "modes_documented": self.modes_documented,
            "fps_requested": self.fps_req,
            "fps": round(self.fps_actual(), 2),
            "frame_size": list(self.actual_size) if self.actual_size else None,
            "output_size": [f.shape[1], f.shape[0]] if f is not None else None,
            "processing_ms": round(self._proc_ms, 1),
            "frames": self.frames_total,
            "recording": self.recorder.info() if self.recorder else None,
            "timelapse": self.timelapse.info() if self.timelapse else None,
            "events": self.events_since(since),
            "time": time.time(),
        }

    def shutdown(self) -> None:
        try:
            self.close()
        finally:
            self.save_settings()
