"""Reference data for the ELP-USB500W05G-BL170 (ELP / Ailipu Technology / SVPRO).

Collected from the manufacturer's published spec sheet for the USB500W05G family
(svpro.cc / elpcctv.com) and retailer listings. Used for the Info tab and as a
fallback mode table when the OS cannot enumerate formats.
"""

from __future__ import annotations

from .controls.base import VideoMode

MODEL = "ELP-USB500W05G-BL170"

SPECS: list[tuple[str, str]] = [
    ("Model", MODEL),
    ("Manufacturer", "ELP (Shenzhen Ailipu Technology Co., Ltd.) - also sold as SVPRO"),
    ("Image sensor", "Aptina (onsemi) MI5100 CMOS, 1/2.5\""),
    ("Pixel size", "2.2 µm × 2.2 µm"),
    ("Image area", "5.70 mm × 4.28 mm"),
    ("Max resolution", "2592 × 1944 (5 MP)"),
    ("Lens", "BL170: M12 170° fisheye (wide-angle), fixed focus"),
    ("Output formats", "MJPEG, YUY2 (YUYV)"),
    ("Frame rate", "MJPEG 15 fps @ 2592×1944 … 30 fps @ 1920×1080 and below; YUY2 ~3 fps @ 5 MP"),
    ("Shutter", "Electronic rolling shutter"),
    ("S/N ratio", "38 dB"),
    ("Dynamic range", "70 dB"),
    ("Sensitivity", "1.4 V/lux-sec @ 550 nm"),
    ("Min. illumination", "0.1 lux"),
    ("Adjustable", "Brightness, contrast, saturation, hue, sharpness, gamma, gain, "
                   "white balance, backlight compensation, exposure"),
    ("Auto functions", "AEC (auto exposure), AEB, AGC (auto gain), AWB"),
    ("Interface", "USB 2.0 High Speed, UVC (driver-free), OTG capable"),
    ("Power", "USB bus power, DC 5 V, 150–200 mA"),
    ("Operating temp.", "-10 °C … 70 °C"),
    ("Housing", "Aluminium mini case, ≈ 41 × 41 × 22.6 mm"),
    ("OS support", "Windows XP – 11, Linux (UVC, kernel ≥ 2.6.26), macOS ≥ 10.4.8, Android ≥ 4.0 (UVC)"),
]

#: Mode table from the manufacturer spec sheet (MJPEG); YUY2 rates are typical for USB 2.0.
DOCUMENTED_MODES: list[VideoMode] = [
    VideoMode("MJPG", 2592, 1944, [15.0]),
    VideoMode("MJPG", 2048, 1536, [15.0]),
    VideoMode("MJPG", 1920, 1080, [30.0]),
    VideoMode("MJPG", 1600, 1200, [15.0]),
    VideoMode("MJPG", 1280, 1024, [15.0]),
    VideoMode("MJPG", 1280, 720, [30.0]),
    VideoMode("MJPG", 800, 600, [30.0]),
    VideoMode("MJPG", 640, 480, [30.0]),
    VideoMode("YUY2", 2592, 1944, [3.0]),
    VideoMode("YUY2", 1920, 1080, [6.0]),
    VideoMode("YUY2", 1280, 720, [10.0]),
    VideoMode("YUY2", 640, 480, [30.0]),
]

LINKS: list[tuple[str, str]] = [
    ("ELP / SVPRO downloads (AMCap, 5MP test software, SDKs)", "https://www.svpro.cc/download/"),
    ("ELP USB500W05G spec sheet (MI5100 family)",
     "https://www.svpro.cc/product/elp-5mp-usb-camera-1080p-30fps-wide-angle-lens-face-recognition-webcam-"
     "aptina-mi5100-cmos-sensor-2592x1944-mini-video-camera-for-robot-vision-aerial-image-id-identification-"
     "3d-printer-document-scannin/"),
    ("ELP support / manufacturer site", "https://www.elpcctv.com/"),
    ("UVC 1.5 class specification (usb.org)", "https://www.usb.org/document-library/video-class-v15-document-set"),
    ("DirectShow IAMVideoProcAmp", "https://learn.microsoft.com/windows/win32/api/strmif/nn-strmif-iamvideoprocamp"),
    ("DirectShow IAMCameraControl", "https://learn.microsoft.com/windows/win32/api/strmif/nn-strmif-iamcameracontrol"),
    ("Linux V4L2 camera controls", "https://docs.kernel.org/userspace-api/media/v4l/ext-ctrls-camera.html"),
]
