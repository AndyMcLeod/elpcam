"""Checkerboard calibration of the fisheye lens (OpenCV ``cv2.fisheye`` model).

Print a checkerboard (default 9×6 inner corners), show it to the camera from
10–20 different angles/positions covering the whole field of view, capture a
frame each time, then compute. The resulting K / D are stored per resolution
and used by the dewarper.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

from .processing import FisheyeCalibration


class Calibrator:
    def __init__(self, store: Path) -> None:
        self.store = store
        self.cols = 9
        self.rows = 6
        self.square_mm = 25.0
        self._img_pts: list[np.ndarray] = []
        self._size: Optional[tuple[int, int]] = None
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- persistence
    def load(self) -> dict[str, FisheyeCalibration]:
        if not self.store.exists():
            return {}
        try:
            raw = json.loads(self.store.read_text(encoding="utf-8"))
            return {k: FisheyeCalibration(**v) for k, v in raw.items()}
        except Exception:
            return {}

    def save(self, cals: dict[str, FisheyeCalibration]) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps({k: v.__dict__ for k, v in cals.items()}, indent=2), encoding="utf-8")

    # ----------------------------------------------------------------- capture
    def configure(self, cols: int, rows: int, square_mm: float) -> None:
        with self._lock:
            if (cols, rows) != (self.cols, self.rows):
                self._img_pts.clear()
            self.cols, self.rows, self.square_mm = int(cols), int(rows), float(square_mm)

    def reset(self) -> None:
        with self._lock:
            self._img_pts.clear()
            self._size = None

    def status(self) -> dict[str, Any]:
        return {"frames": len(self._img_pts), "cols": self.cols, "rows": self.rows,
                "square_mm": self.square_mm, "size": list(self._size) if self._size else None}

    def add_frame(self, frame: np.ndarray) -> dict[str, Any]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        h, w = gray.shape[:2]
        with self._lock:
            if self._size and self._size != (w, h):
                self._img_pts.clear()
            self._size = (w, h)
        scale = min(1.0, 1280.0 / w)
        small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
        pattern = (self.cols, self.rows)
        found, corners = False, None
        if hasattr(cv2, "findChessboardCornersSB"):
            found, corners = cv2.findChessboardCornersSB(small, pattern, flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
        if not found:
            found, corners = cv2.findChessboardCorners(
                small, pattern, flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
        if not found or corners is None:
            return {"found": False, **self.status()}
        corners = (corners.reshape(-1, 1, 2) / scale).astype(np.float32)
        win = max(3, int(round(5 / scale)))
        cv2.cornerSubPix(gray, corners, (win, win), (-1, -1),
                         (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 0.01))
        with self._lock:
            self._img_pts.append(corners)
        return {"found": True, **self.status()}

    # ----------------------------------------------------------------- solve
    def compute(self) -> FisheyeCalibration:
        with self._lock:
            pts = list(self._img_pts)
            size = self._size
        if size is None or len(pts) < 5:
            raise ValueError("Capture at least 5 checkerboard views first (10–20 recommended).")
        objp = np.zeros((1, self.cols * self.rows, 3), np.float64)
        objp[0, :, :2] = np.mgrid[0:self.cols, 0:self.rows].T.reshape(-1, 2) * self.square_mm
        obj = [objp.copy() for _ in pts]
        img = [p.reshape(1, -1, 2).astype(np.float64) for p in pts]
        K = np.zeros((3, 3))
        D = np.zeros((4, 1))
        crit = (cv2.TERM_CRITERIA_COUNT + cv2.TERM_CRITERIA_EPS, 100, 1e-6)
        base = cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC | cv2.fisheye.CALIB_FIX_SKEW
        last_err: Optional[Exception] = None
        for flags in (base | cv2.fisheye.CALIB_CHECK_COND, base):
            try:
                rms, K, D, _r, _t = cv2.fisheye.calibrate(obj, img, size, K, D, flags=flags, criteria=crit)
                cal = FisheyeCalibration(width=size[0], height=size[1], K=K.tolist(),
                                         D=[float(x) for x in D.reshape(-1)], rms=float(rms), frames=len(pts))
                return cal
            except cv2.error as exc:
                last_err = exc
                K = np.zeros((3, 3))
                D = np.zeros((4, 1))
        raise ValueError(f"Calibration failed - capture more varied views ({last_err})")
