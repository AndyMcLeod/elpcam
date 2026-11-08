"""Command line entry point.

    python -m elpcam                     # start the browser GUI (default)
    python -m elpcam serve --port 8080 --host 0.0.0.0
    python -m elpcam --simulate          # try everything without a camera
    python -m elpcam list                # cameras and their video modes
    python -m elpcam controls            # every UVC control with range/default/value
    python -m elpcam set exposure -7     # set a control  (--auto to enable auto mode)
    python -m elpcam snapshot out.jpg --mode MJPG:2592x1944
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__, camera_info


def _engine(args):
    from .engine import CameraEngine

    return CameraEngine(args.data_dir, simulate=args.simulate)


def cmd_serve(args) -> int:
    import uvicorn

    from .server import create_app

    eng = _engine(args)
    if not args.no_open_camera:
        try:
            eng.open(index=args.device, mode_key=args.mode)
        except Exception as exc:  # the GUI shows the error and lets the user retry
            print(f"[elpcam] camera not opened: {exc}", file=sys.stderr)
    app = create_app(eng)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print(f"[elpcam] ELPCam {__version__} - open {url}")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        eng.shutdown()
    return 0


def cmd_list(args) -> int:
    from .controls import list_devices, make_backend

    devs = list_devices()
    if not devs:
        print("No cameras found.")
        return 1
    for d in devs:
        tag = "  [ELP]" if d.is_elp else ""
        ids = f"  VID:PID {d.vid}:{d.pid}" if d.vid else ""
        print(f"#{d.index}  {d.name}{ids}{tag}")
        try:
            b = make_backend(d, lambda: None)
            modes = b.list_modes()
            for m in modes:
                print(f"      {m.fourcc:5s} {m.width:>4d}x{m.height:<4d}  " + ", ".join(f"{f:g}" for f in m.fps) + " fps")
            if not modes:
                print("      (driver did not report modes" + (f" - {_why()}" if _why() else "") + ")")
            b.close()
        except Exception as exc:
            print(f"      (modes unavailable: {exc})")
    return 0


def _why() -> str:
    from .diagnostics import camera_privacy_problem

    return camera_privacy_problem() or ""


def _backend_for(args):
    from .controls import list_devices, make_backend

    devs = list_devices()
    if not devs:
        raise SystemExit("No cameras found.")
    dev = next((d for d in devs if d.index == args.device), None) if args.device is not None else \
        next((d for d in devs if d.is_elp), devs[0])
    if dev is None:
        raise SystemExit(f"No camera #{args.device}")
    return dev, make_backend(dev, lambda: None)


def cmd_controls(args) -> int:
    dev, b = _backend_for(args)
    print(f"{dev.name}  (backend: {b.name})")
    if b.name == "opencv" and _why():
        print(f"  {_why()}")
        return 1
    for c in b.list_controls():
        auto = (" auto=" + ("on" if c.auto else "off")) if c.auto_supported else ""
        menu = f"  {c.menu}" if c.menu else ""
        print(f"  {c.id:26s} {c.value:>6d}   [{c.min} .. {c.max} step {c.step}, default {c.default}]{auto}{menu}")
    return 0


def cmd_set(args) -> int:
    _dev, b = _backend_for(args)
    auto = True if args.auto else (False if args.manual else None)
    c = b.set_control(args.control, args.value, auto)
    print(f"{c.id} = {c.value}" + (f" (auto={'on' if c.auto else 'off'})" if c.auto_supported else ""))
    return 0


def cmd_snapshot(args) -> int:
    eng = _engine(args)
    eng.open(index=args.device, mode_key=args.mode)
    try:
        time.sleep(args.warmup)
        info = eng.snapshot(fmt=Path(args.output).suffix.lstrip(".") or "jpg", raw=args.raw)
        src = eng.captures_dir / info["file"]
        out = Path(args.output)
        out.write_bytes(src.read_bytes())
        src.unlink()
        print(f"Saved {out} ({info['width']}x{info['height']})")
    finally:
        eng.shutdown()
    return 0


def cmd_info(_args) -> int:
    print(camera_info.MODEL)
    for k, v in camera_info.SPECS:
        print(f"  {k:18s} {v}")
    print("\nDocumentation & official software:")
    for k, v in camera_info.LINKS:
        print(f"  {k}\n    {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="elpcam", description=f"ELPCam {__version__} - {camera_info.MODEL} control")
    p.add_argument("--data-dir", default="elpcam-data", help="captures, presets and settings (default: ./elpcam-data)")
    p.add_argument("--device", type=int, default=None, help="camera index (default: auto-detect ELP)")
    p.add_argument("--simulate", action="store_true",
                   help="add a simulated ELP-USB500W05G-BL170 (no hardware needed)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="start the browser GUI (default)")
    s.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 to allow other machines)")
    s.add_argument("--port", type=int, default=8750)
    s.add_argument("--mode", default=None, help="initial mode, e.g. MJPG:1920x1080")
    s.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    s.add_argument("--no-open-camera", action="store_true", help="start without opening a camera")
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("list", help="list cameras and video modes").set_defaults(fn=cmd_list)
    sub.add_parser("controls", help="list camera controls").set_defaults(fn=cmd_controls)
    sub.add_parser("info", help="print ELP-USB500W05G-BL170 specs and documentation links").set_defaults(fn=cmd_info)

    st = sub.add_parser("set", help="set a camera control")
    st.add_argument("control")
    st.add_argument("value", type=int, nargs="?")
    g = st.add_mutually_exclusive_group()
    g.add_argument("--auto", action="store_true")
    g.add_argument("--manual", action="store_true")
    st.set_defaults(fn=cmd_set)

    sn = sub.add_parser("snapshot", help="grab one still to a file")
    sn.add_argument("output", nargs="?", default=f"elp_{time.strftime('%Y%m%d_%H%M%S')}.jpg")
    sn.add_argument("--mode", default=None, help="e.g. MJPG:2592x1944")
    sn.add_argument("--raw", action="store_true", help="skip dewarp/orientation processing")
    sn.add_argument("--warmup", type=float, default=1.5, help="seconds to let auto exposure settle")
    sn.set_defaults(fn=cmd_snapshot)

    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # legacy Windows consoles can't print µ, ×, ≈ ...
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="[elpcam] %(levelname)s %(message)s")
    if not getattr(args, "fn", None):
        args = p.parse_args([*(argv or sys.argv[1:]), "serve"])
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
