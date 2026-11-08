"""End-to-end API tests against the built-in ELP simulator (no hardware needed).

A real uvicorn server is started on a free port and exercised with urllib only.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from elpcam.engine import CameraEngine
from elpcam.server import create_app
from elpcam.simulator import SIM_PATH


class Resp:
    def __init__(self, status, body):
        self.status_code, self.content = status, body

    def json(self):
        return json.loads(self.content)

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")


class Client:
    def __init__(self, base):
        self.base = base

    def _req(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return Resp(r.status, r.read())
        except urllib.error.HTTPError as e:
            return Resp(e.code, e.read())

    def get(self, path):
        return self._req("GET", path)

    def post(self, path, json=None):
        return self._req("POST", path, {} if json is None else json)

    def delete(self, path):
        return self._req("DELETE", path)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    eng = CameraEngine(tmp_path_factory.mktemp("data"), simulate=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(eng), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    c = Client(f"http://127.0.0.1:{port}")
    r = c.post("/api/open", json={"path": SIM_PATH, "mode": "MJPG:1280x720"})
    assert r.status_code == 200, r.text
    yield c
    server.should_exit = True
    t.join(timeout=10)
    eng.shutdown()


def test_info_and_devices(client):
    info = client.get("/api/info").json()
    assert info["model"] == "ELP-USB500W05G-BL170"
    assert any(c["key"] == "auto" for c in info["containers"])
    devs = client.get("/api/devices").json()
    assert any(d["path"] == SIM_PATH and d["is_elp"] for d in devs)


def test_status_and_modes(client):
    time.sleep(0.5)
    st = client.get("/api/status").json()
    assert st["state"] == "live" and st["frame_size"] == [1280, 720]
    modes = client.get("/api/modes").json()
    assert modes["current"] == "MJPG:1280x720"
    assert any(m["key"] == "MJPG:2592x1944" for m in modes["modes"])


def test_controls_roundtrip(client):
    ctl = {c["id"]: c for c in client.get("/api/controls").json()}
    assert {"brightness", "contrast", "exposure", "white_balance"} <= ctl.keys()
    r = client.post("/api/controls/brightness", json={"value": 20}).json()
    assert r["value"] == 20
    r = client.post("/api/controls/exposure", json={"value": -8, "auto": False}).json()
    assert r["value"] == -8 and r["auto"] is False
    r = client.post("/api/controls/brightness", json={"value": 999}).json()
    assert r["value"] == 64  # clamped to range
    assert client.post("/api/controls/nope", json={"value": 1}).status_code == 400
    client.post("/api/controls-reset")
    assert client.get("/api/controls").json()[0]["value"] == 0


def test_processing_and_frame(client):
    p = client.post("/api/processing", json={"dewarp": "perspective", "yaw": 10, "timestamp": True}).json()
    assert p["dewarp"] == "perspective" and p["yaw"] == 10
    time.sleep(0.3)
    r = client.get("/api/frame.jpg")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    client.post("/api/processing", json={"dewarp": "off", "timestamp": False})


def test_snapshot_record_media(client):
    snap = client.post("/api/snapshot", json={"format": "png"}).json()
    assert snap["file"].endswith(".png") and snap["width"] == 1280
    full = client.post("/api/snapshot", json={"full_res": True}).json()
    assert (full["width"], full["height"]) == (2592, 1944)
    assert client.get("/api/status").json()["frame_size"] == [1280, 720]  # switched back
    client.post("/api/record/start", json={"container": "avi"})
    time.sleep(1.5)
    rec = client.post("/api/record/stop").json()["result"]
    assert rec["frames"] > 10
    names = [m["name"] for m in client.get("/api/media").json()]
    assert snap["file"] in names and rec["file"] in names
    assert client.get(f"/media/{snap['file']}").status_code == 200
    assert client.get(f"/api/thumb/{rec['file']}").content[:2] == b"\xff\xd8"
    assert client.get("/media/..%2Fsettings.json").status_code == 404
    assert client.delete(f"/api/media/{snap['file']}").json()["ok"]


def test_presets(client):
    client.post("/api/controls/saturation", json={"value": 100})
    client.post("/api/presets", json={"name": "Vivid"})
    client.post("/api/controls/saturation", json={"value": 10})
    files = [p["file"] for p in client.get("/api/presets").json()]
    assert "Vivid.json" in files
    client.post("/api/presets/Vivid.json/apply")
    sat = next(c for c in client.get("/api/controls").json() if c["id"] == "saturation")
    assert sat["value"] == 100
    assert client.delete("/api/presets/Vivid.json").json()["ok"]
