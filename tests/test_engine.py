"""Recorder and capture-open details that the simulator end-to-end test doesn't reach."""

import time

import cv2
import numpy as np

from elpcam import engine as eng_mod
from elpcam.controls.base import VideoMode
from elpcam.engine import CameraEngine, Recorder


class FakeWriter:
    """Stands in for cv2.VideoWriter: optionally slow to open, or unable to open."""

    delay = 0.0
    opens = True
    written: list = []

    def __init__(self, *args):
        time.sleep(self.delay)

    def isOpened(self):
        return self.opens

    def write(self, frame):
        FakeWriter.written.append(frame)

    def release(self):
        pass


def _fake(monkeypatch, delay=0.0, opens=True):
    FakeWriter.delay, FakeWriter.opens, FakeWriter.written = delay, opens, []
    monkeypatch.setattr(eng_mod.cv2, "VideoWriter", FakeWriter)


def test_recording_starts_before_slow_encoder_opens(monkeypatch, tmp_path):
    _fake(monkeypatch, delay=0.6)
    t0 = time.time()
    rec = Recorder(tmp_path / "clip", "avi", 10, (64, 48))
    assert time.time() - t0 < 0.3  # returns at once; the encoder opens on the recorder thread
    assert rec.info()["codec"] == "starting"
    frame = np.zeros((48, 64, 3), np.uint8)
    for i in range(10):  # 1 s of 10 fps video, pushed while the encoder is still opening
        rec.push(frame, t0 + i * 0.1)
    info = rec.stop()
    assert info["codec"] == "MJPG" and info["error"] is None
    assert info["frames"] == 10 and info["dropped"] == 0


def test_recording_failure_is_reported(monkeypatch, tmp_path):
    _fake(monkeypatch, opens=False)
    errors = []
    rec = Recorder(tmp_path / "clip", "avi", 10, (64, 48), on_error=lambda r, msg: errors.append((r, msg)))
    rec._t.join(timeout=5)
    rec.push(np.zeros((48, 64, 3), np.uint8), time.time())  # ignored after a failure
    t0 = time.time()
    info = rec.stop()
    assert time.time() - t0 < 1
    assert errors and errors[0][0] is rec and "No working video encoder" in errors[0][1]
    assert info["error"] and info["frames"] == 0


def test_queue_is_bounded_by_memory(monkeypatch, tmp_path):
    _fake(monkeypatch)
    small = Recorder(tmp_path / "a", "avi", 10, (640, 480))
    big = Recorder(tmp_path / "b", "avi", 10, (2592, 1944))
    try:
        assert small.q.maxsize == 120
        assert big.q.maxsize * 2592 * 1944 * 3 <= Recorder.QUEUE_BYTES
    finally:
        small.stop()
        big.stop()


def test_mode_props_put_format_first_with_integer_rate():
    props = CameraEngine._mode_props(VideoMode("MJPG", 2592, 1944, [15.0]), None)
    assert props == [(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG")),
                     (cv2.CAP_PROP_FRAME_WIDTH, 2592), (cv2.CAP_PROP_FRAME_HEIGHT, 1944), (cv2.CAP_PROP_FPS, 15)]
    assert all(isinstance(v, int) for _, v in props)
    assert CameraEngine._mode_props(None, 30) == []
