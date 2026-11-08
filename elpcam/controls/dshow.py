"""Windows DirectShow access to UVC cameras via comtypes.

This is the same API surface AMCap (ELP's recommended Windows tool) uses:

* ``ICreateDevEnum``      - enumerate video input devices (same order OpenCV's CAP_DSHOW uses)
* ``IAMVideoProcAmp``     - brightness, contrast, hue, saturation, sharpness, gamma,
                            white balance, backlight compensation, gain, power-line frequency
* ``IAMCameraControl``    - exposure, focus, zoom, pan, tilt, roll, iris, low-light compensation
* ``IAMStreamConfig``     - every pixel format / resolution the capture pin offers
* ``IAMVideoControl``     - the discrete frame-rate list for each resolution
* ``ISpecifyPropertyPages`` + ``OleCreatePropertyFrame`` - the driver's own settings dialog

All COM work happens on one dedicated STA thread (``ComThread``) so the objects are
always used from the apartment that created them.
"""

from __future__ import annotations

import ctypes
import queue
import re
import sys
import threading
from ctypes import POINTER, Structure, byref, c_int, c_long, c_longlong, c_ubyte, c_ulong, c_void_p
from ctypes.wintypes import BOOL, DWORD, LONG, RECT, SIZE, ULONG, WORD
from typing import Any, Callable, Optional

from .base import Control, ControlBackend, ControlError, DeviceInfo, VideoMode

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("DirectShow backend is Windows only")

import comtypes  # noqa: E402
from comtypes import COMMETHOD, GUID, HRESULT, IPersist, IUnknown  # noqa: E402
from comtypes.automation import VARIANT  # noqa: E402
from comtypes.persist import IPropertyBag  # noqa: E402

ole32 = ctypes.windll.ole32
oleaut32 = ctypes.windll.oleaut32
ole32.CoTaskMemFree.argtypes = [c_void_p]
ole32.CoTaskMemFree.restype = None
oleaut32.OleCreatePropertyFrame.argtypes = [
    c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_wchar_p, ULONG, POINTER(c_void_p),
    ULONG, POINTER(GUID), DWORD, DWORD, c_void_p,
]
oleaut32.OleCreatePropertyFrame.restype = HRESULT

# --------------------------------------------------------------------------- GUIDs
CLSID_SystemDeviceEnum = GUID("{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}")
CLSID_VideoInputDeviceCategory = GUID("{860BB310-5D01-11D0-BD3B-00A0C911CE86}")
AMPROPSETID_Pin = GUID("{9B00F101-1567-11D1-B3F1-00AA003761C5}")
PIN_CATEGORY_CAPTURE = GUID("{FB6C4281-0353-11D1-905F-0000C0CC16BA}")
PIN_CATEGORY_STILL = GUID("{FB6C428A-0353-11D1-905F-0000C0CC16BA}")
PIN_CATEGORY_PREVIEW = GUID("{FB6C4282-0353-11D1-905F-0000C0CC16BA}")
FORMAT_VideoInfo = GUID("{05589F80-C356-11CE-BF01-00AA0055595A}")
FORMAT_VideoInfo2 = GUID("{F72A76A0-EB0A-11D0-ACE4-0000C0CC16BA}")

PINDIR_OUTPUT = 1
AMPROPERTY_PIN_CATEGORY = 0


# --------------------------------------------------------------------------- structs
class AM_MEDIA_TYPE(Structure):
    _fields_ = [
        ("majortype", GUID),
        ("subtype", GUID),
        ("bFixedSizeSamples", BOOL),
        ("bTemporalCompression", BOOL),
        ("lSampleSize", ULONG),
        ("formattype", GUID),
        ("pUnk", c_void_p),
        ("cbFormat", ULONG),
        ("pbFormat", c_void_p),
    ]


class VIDEO_STREAM_CONFIG_CAPS(Structure):
    _fields_ = [
        ("guid", GUID),
        ("VideoStandard", ULONG),
        ("InputSize", SIZE),
        ("MinCroppingSize", SIZE),
        ("MaxCroppingSize", SIZE),
        ("CropGranularityX", c_int),
        ("CropGranularityY", c_int),
        ("CropAlignX", c_int),
        ("CropAlignY", c_int),
        ("MinOutputSize", SIZE),
        ("MaxOutputSize", SIZE),
        ("OutputGranularityX", c_int),
        ("OutputGranularityY", c_int),
        ("StretchTapsX", c_int),
        ("StretchTapsY", c_int),
        ("ShrinkTapsX", c_int),
        ("ShrinkTapsY", c_int),
        ("MinFrameInterval", c_longlong),
        ("MaxFrameInterval", c_longlong),
        ("MinBitsPerSecond", LONG),
        ("MaxBitsPerSecond", LONG),
    ]


class BITMAPINFOHEADER(Structure):
    _fields_ = [
        ("biSize", DWORD),
        ("biWidth", LONG),
        ("biHeight", LONG),
        ("biPlanes", WORD),
        ("biBitCount", WORD),
        ("biCompression", DWORD),
        ("biSizeImage", DWORD),
        ("biXPelsPerMeter", LONG),
        ("biYPelsPerMeter", LONG),
        ("biClrUsed", DWORD),
        ("biClrImportant", DWORD),
    ]


class VIDEOINFOHEADER(Structure):
    _fields_ = [
        ("rcSource", RECT),
        ("rcTarget", RECT),
        ("dwBitRate", DWORD),
        ("dwBitErrorRate", DWORD),
        ("AvgTimePerFrame", c_longlong),
        ("bmiHeader", BITMAPINFOHEADER),
    ]


class VIDEOINFOHEADER2(Structure):
    _fields_ = [
        ("rcSource", RECT),
        ("rcTarget", RECT),
        ("dwBitRate", DWORD),
        ("dwBitErrorRate", DWORD),
        ("AvgTimePerFrame", c_longlong),
        ("dwInterlaceFlags", DWORD),
        ("dwCopyProtectFlags", DWORD),
        ("dwPictAspectRatioX", DWORD),
        ("dwPictAspectRatioY", DWORD),
        ("dwControlFlags", DWORD),
        ("dwReserved2", DWORD),
        ("bmiHeader", BITMAPINFOHEADER),
    ]


class CAUUID(Structure):
    _fields_ = [("cElems", ULONG), ("pElems", POINTER(GUID))]


# --------------------------------------------------------------------------- interfaces
def _stub(name: str):
    """A vtable slot we never call (keeps method order correct)."""
    return COMMETHOD([], HRESULT, name)


class IPersistStream(IPersist):
    _iid_ = GUID("{00000109-0000-0000-C000-000000000046}")
    _methods_ = [_stub("IsDirty"), _stub("Load"), _stub("Save"), _stub("GetSizeMax")]


class IMoniker(IPersistStream):
    _iid_ = GUID("{0000000F-0000-0000-C000-000000000046}")
    _methods_ = [
        COMMETHOD([], HRESULT, "BindToObject",
                  (["in"], c_void_p, "pbc"), (["in"], c_void_p, "pmkToLeft"),
                  (["in"], POINTER(GUID), "riidResult"), (["out"], POINTER(POINTER(IUnknown)), "ppvResult")),
        COMMETHOD([], HRESULT, "BindToStorage",
                  (["in"], c_void_p, "pbc"), (["in"], c_void_p, "pmkToLeft"),
                  (["in"], POINTER(GUID), "riid"), (["out"], POINTER(POINTER(IUnknown)), "ppvObj")),
        _stub("Reduce"), _stub("ComposeWith"), _stub("Enum"), _stub("IsEqual"), _stub("Hash"),
        _stub("IsRunning"), _stub("GetTimeOfLastChange"), _stub("Inverse"), _stub("CommonPrefixWith"),
        _stub("RelativePathTo"), _stub("GetDisplayName"), _stub("ParseDisplayName"), _stub("IsSystemMoniker"),
    ]


class IEnumMoniker(IUnknown):
    _iid_ = GUID("{00000102-0000-0000-C000-000000000046}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Next", (["in"], ULONG, "celt"),
                  (["out"], POINTER(POINTER(IMoniker)), "rgelt"), (["out"], POINTER(ULONG), "pceltFetched")),
        _stub("Skip"), _stub("Reset"), _stub("Clone"),
    ]


class ICreateDevEnum(IUnknown):
    _iid_ = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
    _methods_ = [
        COMMETHOD([], HRESULT, "CreateClassEnumerator", (["in"], POINTER(GUID), "clsidDeviceClass"),
                  (["out"], POINTER(POINTER(IEnumMoniker)), "ppEnumMoniker"), (["in"], DWORD, "dwFlags")),
    ]


class IPin(IUnknown):
    _iid_ = GUID("{56A86891-0AD4-11CE-B03A-0020AF0BA770}")
    _methods_ = [
        _stub("Connect"), _stub("ReceiveConnection"), _stub("Disconnect"), _stub("ConnectedTo"),
        _stub("ConnectionMediaType"), _stub("QueryPinInfo"),
        COMMETHOD([], HRESULT, "QueryDirection", (["out"], POINTER(c_int), "pPinDir")),
        _stub("QueryId"), _stub("QueryAccept"), _stub("EnumMediaTypes"), _stub("QueryInternalConnections"),
        _stub("EndOfStream"), _stub("BeginFlush"), _stub("EndFlush"), _stub("NewSegment"),
    ]


class IEnumPins(IUnknown):
    _iid_ = GUID("{56A86892-0AD4-11CE-B03A-0020AF0BA770}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Next", (["in"], ULONG, "cPins"),
                  (["out"], POINTER(POINTER(IPin)), "ppPins"), (["out"], POINTER(ULONG), "pcFetched")),
        _stub("Skip"), _stub("Reset"), _stub("Clone"),
    ]


class IMediaFilter(IPersist):
    _iid_ = GUID("{56A86899-0AD4-11CE-B03A-0020AF0BA770}")
    _methods_ = [_stub("Stop"), _stub("Pause"), _stub("Run"), _stub("GetState"),
                 _stub("SetSyncSource"), _stub("GetSyncSource")]


class IBaseFilter(IMediaFilter):
    _iid_ = GUID("{56A86895-0AD4-11CE-B03A-0020AF0BA770}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumPins", (["out"], POINTER(POINTER(IEnumPins)), "ppEnum")),
        _stub("FindPin"), _stub("QueryFilterInfo"), _stub("JoinFilterGraph"), _stub("QueryVendorInfo"),
    ]


def _range_methods():
    return [
        COMMETHOD([], HRESULT, "GetRange", (["in"], c_long, "Property"),
                  (["in"], POINTER(c_long), "pMin"), (["in"], POINTER(c_long), "pMax"),
                  (["in"], POINTER(c_long), "pSteppingDelta"), (["in"], POINTER(c_long), "pDefault"),
                  (["in"], POINTER(c_long), "pCapsFlags")),
        COMMETHOD([], HRESULT, "Set", (["in"], c_long, "Property"), (["in"], c_long, "lValue"),
                  (["in"], c_long, "Flags")),
        COMMETHOD([], HRESULT, "Get", (["in"], c_long, "Property"), (["in"], POINTER(c_long), "lValue"),
                  (["in"], POINTER(c_long), "Flags")),
    ]


class IAMVideoProcAmp(IUnknown):
    _iid_ = GUID("{C6E13360-30AC-11D0-A18C-00A0C9118956}")
    _methods_ = _range_methods()


class IAMCameraControl(IUnknown):
    _iid_ = GUID("{C6E13370-30AC-11D0-A18C-00A0C9118956}")
    _methods_ = _range_methods()


class IAMStreamConfig(IUnknown):
    _iid_ = GUID("{C6E13340-30AC-11D0-A18C-00A0C9118956}")
    _methods_ = [
        _stub("SetFormat"),
        _stub("GetFormat"),
        COMMETHOD([], HRESULT, "GetNumberOfCapabilities", (["in"], POINTER(c_int), "piCount"),
                  (["in"], POINTER(c_int), "piSize")),
        COMMETHOD([], HRESULT, "GetStreamCaps", (["in"], c_int, "iIndex"),
                  (["in"], POINTER(POINTER(AM_MEDIA_TYPE)), "ppmt"), (["in"], POINTER(c_ubyte), "pSCC")),
    ]


class IAMVideoControl(IUnknown):
    _iid_ = GUID("{6A2E0670-28E4-11D0-A18C-00A0C9118956}")
    _methods_ = [
        _stub("GetCaps"), _stub("SetMode"), _stub("GetMode"), _stub("GetCurrentActualFrameRate"),
        _stub("GetMaxAvailableFrameRate"),
        COMMETHOD([], HRESULT, "GetFrameRateList", (["in"], POINTER(IPin), "pPin"), (["in"], c_long, "iIndex"),
                  (["in"], SIZE, "Dimensions"), (["in"], POINTER(c_long), "ListSize"),
                  (["in"], POINTER(POINTER(c_longlong)), "FrameRates")),
    ]


class IKsPropertySet(IUnknown):
    _iid_ = GUID("{31EFAC30-515C-11D0-A9AA-00AA0061BE93}")
    _methods_ = [
        _stub("Set"),
        COMMETHOD([], HRESULT, "Get", (["in"], POINTER(GUID), "guidPropSet"), (["in"], DWORD, "dwPropID"),
                  (["in"], c_void_p, "pInstanceData"), (["in"], DWORD, "cbInstanceData"),
                  (["in"], c_void_p, "pPropData"), (["in"], DWORD, "cbPropData"),
                  (["in"], POINTER(DWORD), "pcbReturned")),
        _stub("QuerySupported"),
    ]


class ISpecifyPropertyPages(IUnknown):
    _iid_ = GUID("{B196B28B-BAB4-101A-B69C-00AA00341D07}")
    _methods_ = [COMMETHOD([], HRESULT, "GetPages", (["in"], POINTER(CAUUID), "pPages"))]


# --------------------------------------------------------------------------- property tables
FLAG_AUTO = 0x1
FLAG_MANUAL = 0x2

#: (native id, key, display name, unit)
VIDEO_PROC_AMP_PROPS = [
    (0, "brightness", "Brightness", ""),
    (1, "contrast", "Contrast", ""),
    (2, "hue", "Hue", ""),
    (3, "saturation", "Saturation", ""),
    (4, "sharpness", "Sharpness", ""),
    (5, "gamma", "Gamma", ""),
    (6, "color_enable", "Color Enable", ""),
    (7, "white_balance", "White Balance", "K"),
    (8, "backlight_compensation", "Backlight Comp.", ""),
    (9, "gain", "Gain", ""),
    (10, "digital_multiplier", "Digital Multiplier", ""),
    (11, "digital_multiplier_limit", "Digital Mult. Limit", ""),
    (13, "power_line_frequency", "Power Line Frequency", ""),
]

CAMERA_CONTROL_PROPS = [
    (4, "exposure", "Exposure", "log2 s"),
    (5, "iris", "Iris", ""),
    (6, "focus", "Focus", ""),
    (3, "zoom", "Zoom", ""),
    (0, "pan", "Pan", "°"),
    (1, "tilt", "Tilt", "°"),
    (2, "roll", "Roll", "°"),
    (19, "low_light_compensation", "Low Light Compensation", ""),
]

POWER_LINE_MENU = {0: "Disabled", 1: "50 Hz", 2: "60 Hz", 3: "Auto"}


def _subtype_to_fourcc(g: GUID) -> str:
    s = str(g).upper()
    known = {
        "{E436EB7E-524F-11CE-9F53-0020AF0BA770}": "RGB24",
        "{E436EB7D-524F-11CE-9F53-0020AF0BA770}": "RGB565",
        "{E436EB7C-524F-11CE-9F53-0020AF0BA770}": "RGB555",
        "{E436EB7F-524F-11CE-9F53-0020AF0BA770}": "RGB32",
    }
    if s in known:
        return known[s]
    # FOURCC-based subtypes: XXXXXXXX-0000-0010-8000-00AA00389B71
    m = re.match(r"\{([0-9A-F]{8})-0000-0010-8000-00AA00389B71\}", s)
    if m:
        v = int(m.group(1), 16)
        raw = bytes([(v >> (8 * i)) & 0xFF for i in range(4)])
        try:
            txt = raw.decode("ascii")
            if txt.isprintable():
                return txt.strip()
        except UnicodeDecodeError:
            pass
        return f"0x{v:08X}"
    return s


def _free_media_type(pmt: POINTER(AM_MEDIA_TYPE)) -> None:
    if not pmt:
        return
    mt = pmt.contents
    if mt.cbFormat and mt.pbFormat:
        ole32.CoTaskMemFree(mt.pbFormat)
    if mt.pUnk:
        IUnknown.Release(ctypes.cast(mt.pUnk, POINTER(IUnknown)))  # pragma: no cover
    ole32.CoTaskMemFree(ctypes.cast(pmt, c_void_p))


# --------------------------------------------------------------------------- COM thread
class ComThread:
    """Runs callables on a single COM single-threaded apartment."""

    _instance: Optional["ComThread"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._q: "queue.Queue[tuple[Callable[[], Any], queue.Queue]]" = queue.Queue()
        self._t = threading.Thread(target=self._run, name="elpcam-com", daemon=True)
        self._t.start()

    @classmethod
    def get(cls) -> "ComThread":
        with cls._lock:
            if cls._instance is None:
                cls._instance = ComThread()
            return cls._instance

    def _run(self) -> None:
        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        while True:
            fn, reply = self._q.get()
            try:
                reply.put((True, fn()))
            except BaseException as exc:  # noqa: BLE001 - propagate to caller
                reply.put((False, exc))

    def call(self, fn: Callable[[], Any], timeout: float = 15.0) -> Any:
        if threading.current_thread() is self._t:
            return fn()
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._q.put((fn, reply))
        ok, val = reply.get(timeout=timeout)
        if ok:
            return val
        raise val


# --------------------------------------------------------------------------- enumeration helpers
_VIDPID_RE = re.compile(r"vid_([0-9a-f]{4}).*?pid_([0-9a-f]{4})", re.I)
#: USB vendor ids seen on ELP / Ailipu camera modules
ELP_VENDOR_IDS = {"32E4", "05A3", "1BCF", "0C45", "1E4E"}


def _monikers() -> list[Any]:
    dev_enum = comtypes.CoCreateInstance(CLSID_SystemDeviceEnum, interface=ICreateDevEnum,
                                         clsctx=comtypes.CLSCTX_INPROC_SERVER)
    enum = dev_enum.CreateClassEnumerator(byref(CLSID_VideoInputDeviceCategory), 0)
    out = []
    if not enum:
        return out
    while True:
        mon, fetched = enum.Next(1)
        if not fetched or not mon:
            break
        out.append(mon)
    return out


def _bag_read(bag: Any, key: str) -> str:
    try:
        v = bag.Read(key, VARIANT(), None)
        return "" if v is None else str(v)
    except Exception:
        return ""


def _enumerate_devices() -> list[DeviceInfo]:
    devices = []
    for i, mon in enumerate(_monikers()):
        try:
            bag = mon.BindToStorage(None, None, byref(IPropertyBag._iid_)).QueryInterface(IPropertyBag)
            name = _bag_read(bag, "FriendlyName") or f"Camera {i}"
            path = _bag_read(bag, "DevicePath")
        except Exception:
            name, path = f"Camera {i}", ""
        vid = pid = None
        m = _VIDPID_RE.search(path)
        if m:
            vid, pid = m.group(1).upper(), m.group(2).upper()
        is_elp = ("elp" in name.lower()) or (vid in ELP_VENDOR_IDS if vid else False)
        devices.append(DeviceInfo(index=i, name=name, path=path, vid=vid, pid=pid, is_elp=is_elp))
    return devices


def list_devices() -> list[DeviceInfo]:
    """All DirectShow video input devices, in OpenCV CAP_DSHOW index order."""
    return ComThread.get().call(_enumerate_devices)


def _bind_filter(index: int, path: str = "") -> Any:
    mons = _monikers()
    target = None
    if path:
        for mon in mons:
            try:
                bag = mon.BindToStorage(None, None, byref(IPropertyBag._iid_)).QueryInterface(IPropertyBag)
                if _bag_read(bag, "DevicePath") == path:
                    target = mon
                    break
            except Exception:
                continue
    if target is None:
        if index < 0 or index >= len(mons):
            raise ControlError(f"No DirectShow video device at index {index}")
        target = mons[index]
    unk = target.BindToObject(None, None, byref(IBaseFilter._iid_))
    return unk.QueryInterface(IBaseFilter)


def _pin_category(pin: Any) -> Optional[GUID]:
    try:
        ks = pin.QueryInterface(IKsPropertySet)
    except Exception:
        return None
    cat = GUID()
    returned = DWORD(0)
    try:
        ks.Get(byref(AMPROPSETID_Pin), AMPROPERTY_PIN_CATEGORY, None, 0,
               ctypes.cast(byref(cat), c_void_p), ctypes.sizeof(GUID), byref(returned))
    except Exception:
        return None
    return cat


def _output_pins(flt: Any) -> list[tuple[Any, Optional[GUID]]]:
    pins = []
    enum = flt.EnumPins()
    while True:
        pin, fetched = enum.Next(1)
        if not fetched or not pin:
            break
        try:
            if pin.QueryDirection() == PINDIR_OUTPUT:
                pins.append((pin, _pin_category(pin)))
        except Exception:
            continue
    return pins


# --------------------------------------------------------------------------- backend
class DirectShowControls(ControlBackend):
    """UVC controls + mode enumeration for one DirectShow device."""

    name = "directshow"
    enumerates_modes = True

    def __init__(self, device: DeviceInfo) -> None:
        self.device = device
        self._com = ComThread.get()
        self._com.call(self._init)

    # ---- runs on COM thread -------------------------------------------------
    def _init(self) -> None:
        self._filter = _bind_filter(self.device.index, self.device.path)
        self._amp = self._qi(self._filter, IAMVideoProcAmp)
        self._cam = self._qi(self._filter, IAMCameraControl)
        self._vctl = self._qi(self._filter, IAMVideoControl)
        self._pins = _output_pins(self._filter)
        self._ranges: dict[str, tuple] = {}
        for iface, props, group in ((self._amp, VIDEO_PROC_AMP_PROPS, "image"),
                                    (self._cam, CAMERA_CONTROL_PROPS, "camera")):
            if iface is None:
                continue
            for pid, key, label, unit in props:
                mn, mx, st, df, caps = c_long(), c_long(), c_long(), c_long(), c_long()
                try:
                    iface.GetRange(pid, byref(mn), byref(mx), byref(st), byref(df), byref(caps))
                except Exception:
                    continue
                if mx.value < mn.value:
                    continue
                self._ranges[key] = (iface, pid, label, unit, group, mn.value, mx.value,
                                     max(1, st.value), df.value, caps.value)

    @staticmethod
    def _qi(obj: Any, iface: Any) -> Any:
        try:
            return obj.QueryInterface(iface)
        except Exception:
            return None

    def _read(self, key: str) -> Control:
        iface, pid, label, unit, group, mn, mx, st, df, caps = self._ranges[key]
        val, flags = c_long(df), c_long(0)
        inactive = False
        try:
            iface.Get(pid, byref(val), byref(flags))
        except Exception:
            inactive = True
        auto_ok = bool(caps & FLAG_AUTO)
        manual_ok = bool(caps & FLAG_MANUAL) or not auto_ok
        ctype = "int"
        menu = None
        if key == "power_line_frequency":
            ctype = "menu"
            menu = {k: v for k, v in POWER_LINE_MENU.items() if mn <= k <= mx}
        elif key in ("color_enable", "low_light_compensation") or (mn == 0 and mx == 1):
            ctype = "bool"
        return Control(
            id=key, name=label, group=group, type=ctype, min=mn, max=mx, step=st, default=df,
            value=val.value, auto_supported=auto_ok, manual_supported=manual_ok,
            auto=bool(flags.value & FLAG_AUTO), menu=menu, unit=unit, inactive=inactive, native_id=pid,
        )

    def _list(self) -> list[Control]:
        return [self._read(k) for k in self._ranges]

    def _set(self, key: str, value: Optional[int], auto: Optional[bool]) -> Control:
        if key not in self._ranges:
            raise ControlError(f"Control '{key}' is not supported by this camera")
        cur = self._read(key)
        iface, pid, *_ = self._ranges[key]
        v = cur.value if value is None else int(value)
        v = max(cur.min, min(cur.max, v))
        if cur.step > 1:
            v = cur.min + round((v - cur.min) / cur.step) * cur.step
        want_auto = cur.auto if auto is None else bool(auto)
        if want_auto and not cur.auto_supported:
            want_auto = False
        flags = FLAG_AUTO if want_auto else FLAG_MANUAL
        try:
            iface.Set(pid, v, flags)
        except Exception as exc:
            raise ControlError(f"Driver rejected {cur.name}={v} ({'auto' if want_auto else 'manual'}): {exc}")
        return self._read(key)

    def _modes(self) -> list[VideoMode]:
        modes: dict[str, VideoMode] = {}
        pins = sorted(self._pins, key=lambda p: 0 if p[1] == PIN_CATEGORY_CAPTURE else 1)
        for pin, cat in pins:
            if cat is not None and cat not in (PIN_CATEGORY_CAPTURE,):
                continue
            cfg = self._qi(pin, IAMStreamConfig)
            if cfg is None:
                continue
            count, size = c_int(0), c_int(0)
            try:
                cfg.GetNumberOfCapabilities(byref(count), byref(size))
            except Exception:
                continue
            buf = (c_ubyte * max(size.value, ctypes.sizeof(VIDEO_STREAM_CONFIG_CAPS)))()
            for i in range(count.value):
                pmt = POINTER(AM_MEDIA_TYPE)()
                try:
                    cfg.GetStreamCaps(i, byref(pmt), buf)
                except Exception:
                    continue
                try:
                    mt = pmt.contents
                    fourcc = _subtype_to_fourcc(mt.subtype)
                    if mt.formattype == FORMAT_VideoInfo and mt.cbFormat >= ctypes.sizeof(VIDEOINFOHEADER):
                        vih = ctypes.cast(mt.pbFormat, POINTER(VIDEOINFOHEADER)).contents
                    elif mt.formattype == FORMAT_VideoInfo2 and mt.cbFormat >= ctypes.sizeof(VIDEOINFOHEADER2):
                        vih = ctypes.cast(mt.pbFormat, POINTER(VIDEOINFOHEADER2)).contents
                    else:
                        continue
                    w, h = vih.bmiHeader.biWidth, abs(vih.bmiHeader.biHeight)
                    caps = VIDEO_STREAM_CONFIG_CAPS.from_buffer(buf)
                    fps = self._frame_rates(pin, i, w, h)
                    if not fps:
                        cand = [vih.AvgTimePerFrame, caps.MinFrameInterval, caps.MaxFrameInterval]
                        fps = sorted({round(1e7 / t, 3) for t in cand if t and t > 0}, reverse=True)
                    key = f"{fourcc}:{w}x{h}"
                    if key in modes:
                        modes[key].fps = sorted(set(modes[key].fps) | set(fps), reverse=True)
                    else:
                        modes[key] = VideoMode(fourcc=fourcc, width=w, height=h, fps=fps)
                finally:
                    _free_media_type(pmt)
            if modes:
                break
        return sorted(modes.values(), key=lambda m: (m.fourcc != "MJPG", -m.width * m.height, m.fourcc))

    def _frame_rates(self, pin: Any, index: int, w: int, h: int) -> list[float]:
        if self._vctl is None:
            return []
        n = c_long(0)
        arr = POINTER(c_longlong)()
        try:
            self._vctl.GetFrameRateList(pin, index, SIZE(w, h), byref(n), byref(arr))
        except Exception:
            return []
        try:
            vals = [arr[i] for i in range(n.value)] if arr else []
        finally:
            if arr:
                ole32.CoTaskMemFree(ctypes.cast(arr, c_void_p))
        return sorted({round(1e7 / v, 3) for v in vals if v > 0}, reverse=True)

    def _dialog(self) -> bool:
        spp = self._qi(self._filter, ISpecifyPropertyPages)
        if spp is None:
            return False
        pages = CAUUID()
        spp.GetPages(byref(pages))
        try:
            unk = ctypes.cast(self._filter, c_void_p)
            arr = (c_void_p * 1)(unk.value)
            oleaut32.OleCreatePropertyFrame(None, 0, 0, self.device.name, 1, arr,
                                            pages.cElems, pages.pElems, 0, 0, None)
        finally:
            if pages.pElems:
                ole32.CoTaskMemFree(ctypes.cast(pages.pElems, c_void_p))
        return True

    def _release(self) -> None:
        self._amp = self._cam = self._vctl = None
        self._pins = []
        self._filter = None

    # ---- public API (thread safe) ------------------------------------------
    def list_controls(self) -> list[Control]:
        return self._com.call(self._list)

    def set_control(self, cid: str, value: Optional[int] = None, auto: Optional[bool] = None) -> Control:
        return self._com.call(lambda: self._set(cid, value, auto))

    def get_control(self, cid: str) -> Control:
        def _g():
            if cid not in self._ranges:
                raise ControlError(f"Unknown control '{cid}'")
            return self._read(cid)
        return self._com.call(_g)

    def list_modes(self) -> list[VideoMode]:
        return self._com.call(self._modes)

    def open_driver_dialog(self) -> bool:
        """Show the driver's property sheet (the same dialog AMCap opens).

        Runs on its own STA thread with its own filter instance so the modal
        dialog does not block the control thread or the video stream.
        """
        dev = self.device

        def _worker() -> None:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
            try:
                tmp = DirectShowControls.__new__(DirectShowControls)
                tmp.device = dev
                tmp._filter = _bind_filter(dev.index, dev.path)
                tmp._dialog()
                tmp._filter = None
            except Exception:
                pass
            finally:
                comtypes.CoUninitialize()

        threading.Thread(target=_worker, name="elpcam-propsheet", daemon=True).start()
        return True

    def close(self) -> None:
        try:
            self._com.call(self._release)
        except Exception:
            pass
