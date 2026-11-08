import cv2
import numpy as np

from elpcam.calibration import Calibrator


def _checkerboard(cols=9, rows=6, sq=60, margin=80):
    w, h = (cols + 1) * sq + 2 * margin, (rows + 1) * sq + 2 * margin
    img = np.full((h, w), 255, np.uint8)
    for r in range(rows + 1):
        for c in range(cols + 1):
            if (r + c) % 2 == 0:
                y, x = margin + r * sq, margin + c * sq
                img[y:y + sq, x:x + sq] = 0
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def test_detects_checkerboard(tmp_path):
    cal = Calibrator(tmp_path / "cal.json")
    board = _checkerboard()
    res = cal.add_frame(board)
    assert res["found"] and res["frames"] == 1
    res = cal.add_frame(np.zeros_like(board))           # same size, no board -> rejected
    assert not res["found"] and res["frames"] == 1
    res = cal.add_frame(np.zeros((480, 640, 3), np.uint8))  # new resolution restarts the session
    assert not res["found"] and res["frames"] == 0


def test_recovers_known_fisheye(tmp_path):
    """Project a board through a known fisheye model at many poses; calibration must recover K."""
    w, h = 1296, 972
    K = np.array([[420.0, 0, w / 2], [0, 420.0, h / 2], [0, 0, 1]])
    D = np.array([[0.05], [-0.02], [0.01], [-0.003]])
    cal = Calibrator(tmp_path / "cal.json")
    cal.configure(9, 6, 25.0)
    objp = np.zeros((1, 54, 3), np.float64)
    objp[0, :, :2] = np.mgrid[0:9, 0:6].T.reshape(-1, 2) * 25.0
    objp[0, :, :2] -= objp[0, :, :2].mean(axis=0)
    rng = np.random.default_rng(1)
    for _ in range(14):
        rvec = rng.uniform(-0.6, 0.6, 3).reshape(3, 1)
        tvec = np.array([[rng.uniform(-120, 120)], [rng.uniform(-80, 80)], [rng.uniform(250, 420)]])
        img_pts, _ = cv2.fisheye.projectPoints(objp, rvec, tvec, K, D)
        cal._img_pts.append(img_pts.reshape(-1, 1, 2).astype(np.float32))
    cal._size = (w, h)
    result = cal.compute()
    Kr = np.array(result.K)
    assert result.rms < 0.05
    assert abs(Kr[0, 0] - 420) < 2 and abs(Kr[0, 2] - w / 2) < 2
    # persistence round trip
    cal.save({"1296x972": result})
    assert "1296x972" in cal.load()
