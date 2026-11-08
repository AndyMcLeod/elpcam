"""HTTP API + MJPEG live stream + static browser GUI (FastAPI)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, camera_info
from .controls import ControlError
from .engine import CONTAINERS, CameraEngine, available_containers

WEB_DIR = Path(__file__).with_name("web")
BOUNDARY = "elpcamframe"


class OpenReq(BaseModel):
    index: Optional[int] = None
    path: Optional[str] = None
    mode: Optional[str] = None
    fps: Optional[float] = None


class ModeReq(BaseModel):
    mode: str
    fps: Optional[float] = None


class ControlReq(BaseModel):
    value: Optional[int] = None
    auto: Optional[bool] = None


class SnapshotReq(BaseModel):
    format: str = "jpg"
    quality: int = 95
    raw: bool = False
    full_res: bool = False


class RecordReq(BaseModel):
    container: str = "auto"
    fps: Optional[float] = None


class TimelapseReq(BaseModel):
    interval: float = 5.0
    video_fps: float = 30.0
    make_video: bool = True


class PresetReq(BaseModel):
    name: str
    include_mode: bool = True


class CalibReq(BaseModel):
    cols: int = 9
    rows: int = 6
    square_mm: float = 25.0


def _placeholder(text: str) -> bytes:
    img = np.zeros((480, 640, 3), np.uint8)
    img[:] = (24, 22, 20)
    for i, line in enumerate(text.split("\n")):
        (tw, _), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        cv2.putText(img, line, ((640 - tw) // 2, 230 + i * 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (150, 150, 150), 2, cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


def create_app(engine: CameraEngine) -> FastAPI:
    app = FastAPI(title="ELPCam", version=__version__,
                  description=f"Control API for the {camera_info.MODEL} USB camera")

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except (ControlError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, f"Not found: {exc}")
        except TimeoutError as exc:
            raise HTTPException(504, str(exc))

    # ---------------------------------------------------------------- UI
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"  # always revalidate (cheap 304) so upgrades show up
        return resp

    # ---------------------------------------------------------------- info
    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {
            "app": "ELPCam", "version": __version__, "platform": sys.platform,
            "model": camera_info.MODEL, "specs": camera_info.SPECS,
            "documented_modes": [m.to_dict() for m in camera_info.DOCUMENTED_MODES],
            "links": camera_info.LINKS, "data_dir": str(engine.data_dir),
            "opencv": cv2.__version__,
            "containers": [{"key": "auto", "label": "Auto (H.264 up to 1080p, else MPEG-4)"
                            if "mp4-h264" in available_containers() else "Auto (MPEG-4)"}]
            + [{"key": k, "label": CONTAINERS[k][0]} for k in available_containers()],
            "simulate": engine.simulate,
        }

    @app.get("/api/devices")
    def devices() -> list[dict[str, Any]]:
        return [d.to_dict() for d in guard(engine.list_devices)]

    @app.get("/api/status")
    def status(since: int = 0) -> dict[str, Any]:
        return engine.status(since)

    @app.post("/api/open")
    def open_device(req: OpenReq = Body(default=OpenReq())) -> dict[str, Any]:
        return guard(engine.open, req.index, req.path, req.mode, req.fps)

    @app.post("/api/close")
    def close_device() -> dict[str, Any]:
        engine.close()
        return engine.status()

    @app.get("/api/modes")
    def modes() -> dict[str, Any]:
        return {"modes": [m.to_dict() for m in engine.modes], "current": engine.mode.key if engine.mode else None,
                "fps": engine.fps_req, "documented": engine.modes_documented}

    @app.post("/api/mode")
    def set_mode(req: ModeReq) -> dict[str, Any]:
        return guard(engine.set_mode, req.mode, req.fps)

    # ---------------------------------------------------------------- controls
    @app.get("/api/controls")
    def controls() -> list[dict[str, Any]]:
        return guard(engine.list_controls)

    @app.post("/api/controls/{cid}")
    def set_control(cid: str, req: ControlReq) -> dict[str, Any]:
        return guard(engine.set_control, cid, req.value, req.auto)

    @app.post("/api/controls-reset")
    def reset_controls() -> list[dict[str, Any]]:
        return guard(engine.reset_controls)

    @app.post("/api/driver-dialog")
    def driver_dialog() -> dict[str, Any]:
        return {"shown": guard(engine.open_driver_dialog)}

    # ---------------------------------------------------------------- processing
    @app.get("/api/processing")
    def get_processing() -> dict[str, Any]:
        return engine.processing_state()

    @app.post("/api/processing")
    def set_processing(data: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return guard(engine.update_processing, data)

    # ---------------------------------------------------------------- stream & frames
    @app.get("/stream.mjpg", include_in_schema=False)
    def stream() -> StreamingResponse:
        def gen():
            last = -1
            idle = _placeholder("No camera open")
            while True:
                fid, jpg = engine.preview_jpeg(last, timeout=1.0)
                if jpg is None:
                    if fid == last:
                        time.sleep(0.5)
                    jpg = idle
                last = fid
                yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n"
                       .encode() + jpg + b"\r\n")

        return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                                 headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"})

    @app.get("/api/frame.jpg")
    def frame(full: bool = False, raw: bool = False, quality: int = 90) -> Response:
        if full or raw:
            f = engine.capture_frame(raw=raw)
            if f is None:
                raise HTTPException(503, "No frame available")
            jpg = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])[1].tobytes()
        else:
            _, jpg = engine.preview_jpeg(-2, timeout=0)
            if jpg is None:
                raise HTTPException(503, "No frame available")
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- capture
    @app.post("/api/snapshot")
    def snapshot(req: SnapshotReq = Body(default=SnapshotReq())) -> dict[str, Any]:
        return guard(engine.snapshot, req.format, req.quality, req.raw, req.full_res)

    @app.post("/api/record/start")
    def record_start(req: RecordReq = Body(default=RecordReq())) -> dict[str, Any]:
        return guard(engine.start_recording, req.container, req.fps)

    @app.post("/api/record/stop")
    def record_stop() -> dict[str, Any]:
        return {"result": engine.stop_recording()}

    @app.post("/api/timelapse/start")
    def timelapse_start(req: TimelapseReq = Body(default=TimelapseReq())) -> dict[str, Any]:
        return guard(engine.start_timelapse, req.interval, req.video_fps, req.make_video)

    @app.post("/api/timelapse/stop")
    def timelapse_stop() -> dict[str, Any]:
        return {"result": engine.stop_timelapse()}

    # ---------------------------------------------------------------- calibration
    @app.get("/api/calibration")
    def calib_status() -> dict[str, Any]:
        return {"session": engine.calibrator.status(), "current": engine.processing_state()["calibration"],
                "saved": {k: {"rms": v.rms, "frames": v.frames, "created": v.created}
                          for k, v in engine.processor.calibrations.items()}}

    @app.post("/api/calibration/capture")
    def calib_capture(req: CalibReq = Body(default=CalibReq())) -> dict[str, Any]:
        return guard(engine.calibration_capture, req.cols, req.rows, req.square_mm)

    @app.post("/api/calibration/compute")
    def calib_compute() -> dict[str, Any]:
        return guard(engine.calibration_compute)

    @app.post("/api/calibration/clear")
    def calib_clear(all_sizes: bool = False) -> dict[str, Any]:
        engine.calibration_clear(all_sizes)
        return {"ok": True}

    # ---------------------------------------------------------------- presets
    @app.get("/api/presets")
    def presets() -> list[dict[str, Any]]:
        return engine.list_presets()

    @app.post("/api/presets")
    def save_preset(req: PresetReq) -> dict[str, Any]:
        return guard(engine.save_preset, req.name, req.include_mode)

    @app.post("/api/presets/{file}/apply")
    def apply_preset(file: str) -> dict[str, Any]:
        return guard(engine.apply_preset, file)

    @app.delete("/api/presets/{file}")
    def delete_preset(file: str) -> dict[str, Any]:
        guard(engine.delete_preset, file)
        return {"ok": True}

    # ---------------------------------------------------------------- media
    @app.get("/api/media")
    def media() -> list[dict[str, Any]]:
        return engine.list_media()

    @app.get("/media/{rel:path}", include_in_schema=False)
    def media_file(rel: str, download: bool = False) -> FileResponse:
        p = guard(engine.media_path, rel)
        return FileResponse(p, filename=p.name if download else None)

    @app.get("/api/thumb/{rel:path}", include_in_schema=False)
    def thumb(rel: str) -> Response:
        return Response(guard(engine.thumbnail, rel), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=3600"})

    @app.delete("/api/media/{rel:path}")
    def delete_media(rel: str) -> dict[str, Any]:
        guard(engine.delete_media, rel)
        return {"ok": True}

    @app.exception_handler(Exception)
    async def on_error(_req, exc: Exception) -> JSONResponse:  # pragma: no cover
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    return app
