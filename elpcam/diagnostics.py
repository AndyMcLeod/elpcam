"""Explain *why* a camera cannot be opened (Windows camera privacy is the usual culprit)."""

from __future__ import annotations

import re
import sys
from typing import Optional

_BASE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"


def _reg_value(root, sub: str) -> Optional[str]:
    import winreg

    try:
        with winreg.OpenKey(root, sub) as k:
            return str(winreg.QueryValueEx(k, "Value")[0])
    except OSError:
        return None


def store_python_package() -> Optional[str]:
    """Package family name if we run on Microsoft Store Python (e.g. PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0)."""
    m = re.search(r"WindowsApps[\\/](PythonSoftwareFoundation\.Python\.[\d.]+)_[^\\/]*__(\w+)", sys.base_prefix)
    return f"{m.group(1)}_{m.group(2)}" if m else None


def camera_privacy_problem() -> Optional[str]:
    """Human readable reason when Windows privacy settings block camera access for this Python, else None."""
    if sys.platform != "win32":
        return None
    import winreg

    if _reg_value(winreg.HKEY_LOCAL_MACHINE, _BASE) == "Deny":
        return ("Windows camera access is turned off for this PC: Settings > Privacy & security > Camera > "
                "turn on 'Camera access'.")
    if _reg_value(winreg.HKEY_CURRENT_USER, _BASE) == "Deny":
        return "Windows blocks apps from the camera: Settings > Privacy & security > Camera > turn on 'Let apps access your camera'."
    pkg = store_python_package()
    if pkg:
        if _reg_value(winreg.HKEY_CURRENT_USER, f"{_BASE}\\{pkg}") == "Deny":
            ver = pkg.split("_")[0].replace("PythonSoftwareFoundation.", "")
            return (f"Windows blocks camera access for Microsoft Store {ver.replace('.', ' ', 1)}: Settings > Privacy & security > "
                    f"Camera > turn on '{ver.replace('.', ' ', 1)}' (or run ELPCam with a python.org install of Python).")
    elif _reg_value(winreg.HKEY_CURRENT_USER, f"{_BASE}\\NonPackaged") == "Deny":
        return ("Windows blocks desktop apps from the camera: Settings > Privacy & security > Camera > turn on "
                "'Let desktop apps access your camera'.")
    return None
