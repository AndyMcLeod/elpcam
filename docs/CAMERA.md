# ELP-USB500W05G-BL170: camera notes

Research notes collected while building ELPCam: the published specifications, the manufacturer's software and
documentation, and how the camera's controls are exposed on each operating system.

## Identification

* **Model:** ELP-USB500W05G-BL170 (USB500W05G family; the suffix is the lens: `BL170` = 170° fisheye,
  `FD100` / `BH100` = 100° low-distortion, `SFV(5-50)` = varifocal, …)
* **Manufacturer:** ELP, made by Shenzhen Ailipu Technology Co., Ltd., which also sells under the **SVPRO** brand.
* **Class:** USB Video Class (UVC) device, driver-free on Windows, Linux, macOS and Android (OTG).
* On Windows it appears as a DirectShow "video input device". The friendly name depends on firmware
  (often "USB Camera" / "HD USB Camera"). ELPCam marks cameras it recognises as ELP with ★, either by name or
  by USB vendor id (Ailipu modules commonly use `32E4`; older ones `05A3`, `1BCF`, `0C45`, `1E4E`).

## Specifications (manufacturer spec sheet, USB500W05G family)

| Item | Value |
|---|---|
| Sensor | Aptina / onsemi **MI5100** CMOS, 1/2.5″, 2.2 µm pixels, image area 5.70 × 4.28 mm |
| Max resolution | 2592 × 1944 (5 MP) |
| Lens (BL170) | M12 mount, 170° fisheye, fixed focus |
| Formats | MJPEG, YUY2 |
| MJPEG modes | 2592×1944, 2048×1536, 1600×1200, 1280×1024 @ 15 fps · 1920×1080, 1280×720, 800×600, 640×480 @ 30 fps |
| YUY2 | ≈3 fps at 5 MP (USB 2.0 bandwidth limit); 30 fps at 640×480 |
| S/N · dynamic range | 38 dB · 70 dB |
| Sensitivity · min. illumination | 1.4 V/lux-sec @ 550 nm · 0.1 lux |
| Shutter | electronic rolling shutter |
| Adjustable | brightness, contrast, saturation, hue, sharpness, gamma, gain, white balance, backlight compensation, exposure |
| Automatic | AEC (auto exposure), AEB, AGC (auto gain), AWB |
| Interface / power | USB 2.0 High Speed · 5 V bus power, 150–200 mA |
| Temperature | operating −10…70 °C, storage −20…85 °C |
| Housing | aluminium mini case, ≈ 41 × 41 × 22.6 mm, 1/2/3/5 m cable |
| OS | Windows XP–11, Linux (UVC, kernel ≥ 2.6.26), macOS ≥ 10.4.8, Android ≥ 4.0 (UVC) |

Note: some retailer listings for the BL170 name an **OV5640** sensor, and others the **MI5100**. Both are 5 MP, 2592×1944
and 15 fps in MJPEG. The modes and controls the driver actually reports (shown in ELPCam's Info tab) take precedence
over any listing.

## Official software & documentation

The ELP/SVPRO download page is <https://www.svpro.cc/download/>. It provides:

| Item | Platform | Purpose |
|---|---|---|
| **AMCap** (universal testing software) + *Amcap Operation Guide* | Windows | ELP's recommended viewer: preview, format/resolution selection, driver property pages |
| **5MP / 8MP+ USB Camera Testing Software** | Windows | Viewer for the high-resolution models (5 MP stills) |
| H.264 USB Camera Testing Software | Windows | For H.264-encoding models (not this camera) |
| **ELP USB Camera SDK** | Windows, Android, Linux | Reference SDKs from Ailipu |

ELP also lists third-party software that works with these UVC cameras: Blue Iris, ContaCam, Yawcam, iSpy,
MotionEyeOS, ZoneMinder, VLC, guvcview / V4L2 (Linux), CameraFi (Android), QuickTime (macOS).

ELPCam replaces AMCap and the testing tool. It reads the same driver interfaces and adds dewarping, recording,
presets and an API. AMCap's *Video Capture Filter* property sheet is still available through the
**Driver dialog** button.

Other references:

* Spec sheet (USB500W05G / MI5100 family): <https://www.svpro.cc/product/elp-5mp-usb-camera-1080p-30fps-wide-angle-lens-face-recognition-webcam-aptina-mi5100-cmos-sensor-2592x1944-mini-video-camera-for-robot-vision-aerial-image-id-identification-3d-printer-document-scannin/>
* Manufacturer site: <https://www.elpcctv.com/>
* UVC 1.5 class specification: <https://www.usb.org/document-library/video-class-v15-document-set>
* DirectShow `IAMVideoProcAmp`: <https://learn.microsoft.com/windows/win32/api/strmif/nn-strmif-iamvideoprocamp>
* DirectShow `IAMCameraControl`: <https://learn.microsoft.com/windows/win32/api/strmif/nn-strmif-iamcameracontrol>
* Linux V4L2 camera controls: <https://docs.kernel.org/userspace-api/media/v4l/ext-ctrls-camera.html>

## How the controls map

| ELPCam id | UVC unit / selector | Windows DirectShow | Linux V4L2 (`v4l2-ctl`) |
|---|---|---|---|
| `brightness` | PU · BRIGHTNESS | `VideoProcAmp_Brightness` (0) | `brightness` |
| `contrast` | PU · CONTRAST | `VideoProcAmp_Contrast` (1) | `contrast` |
| `hue` | PU · HUE | `VideoProcAmp_Hue` (2) | `hue` |
| `saturation` | PU · SATURATION | `VideoProcAmp_Saturation` (3) | `saturation` |
| `sharpness` | PU · SHARPNESS | `VideoProcAmp_Sharpness` (4) | `sharpness` |
| `gamma` | PU · GAMMA | `VideoProcAmp_Gamma` (5) | `gamma` |
| `white_balance` (+auto) | PU · WHITE_BALANCE_TEMPERATURE(_AUTO) | `VideoProcAmp_WhiteBalance` (7) + auto flag | `white_balance_temperature` + `white_balance_automatic` |
| `backlight_compensation` | PU · BACKLIGHT_COMPENSATION | `VideoProcAmp_BacklightCompensation` (8) | `backlight_compensation` |
| `gain` | PU · GAIN | `VideoProcAmp_Gain` (9) | `gain` |
| `power_line_frequency` | PU · POWER_LINE_FREQUENCY | KS property 13 | `power_line_frequency` |
| `exposure` (+auto) | CT · EXPOSURE_TIME_ABSOLUTE / AE_MODE | `CameraControl_Exposure` (4), **log2 seconds** (−6 = 1/64 s) + auto flag | `exposure_time_absolute` (100 µs units) + `auto_exposure` |
| `low_light_compensation` | CT · AE_PRIORITY | KS property 19 | `exposure_dynamic_framerate` |
| `focus`, `zoom`, `pan`, `tilt`, `roll`, `iris` | CT | `CameraControl_*` | `*_absolute` |

The BL170 has a fixed-focus lens, so focus/zoom/pan/tilt usually don't appear. ELPCam shows exactly what the driver
reports, with the driver's own min/max/step/default.

## Practical tips

* **Use MJPEG for high resolutions.** USB 2.0 cannot carry uncompressed YUY2 5 MP faster than ≈3 fps.
* **Power-line frequency** must match your mains (50 Hz Europe/Asia, 60 Hz Americas) or fluorescent/LED lighting
  will flicker or band.
* **Low-light compensation** lets auto exposure lower the frame rate in dim light, which gives a brighter but slower image.
* **Exposure** on Windows is a power of two: −1 = 1/2 s, −6 = 1/64 s, −10 ≈ 1/1000 s. Longer exposures cap the
  frame rate.
* Plug the camera directly into the computer (or a powered hub). The module draws up to 200 mA.
* Only one application can stream from the camera at a time. Close AMCap/Teams/Zoom before starting ELPCam.
