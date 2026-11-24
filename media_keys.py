"""Zero-dependency native Win32 media keys, Core Audio exact volume, and shutdown timer."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from ru_numbers import format_duration_ru

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

CREATE_NO_WINDOW = 0x08000000
COINIT_MULTITHREADED = 0x0
CLSCTX_ALL = 23
S_OK = 0
S_FALSE = 1
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106 signed 32-bit


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, text: str) -> "GUID":
        cleaned = text.strip().strip("{}")
        parts = cleaned.split("-")
        d1 = int(parts[0], 16)
        d2 = int(parts[1], 16)
        d3 = int(parts[2], 16)
        tail = bytes.fromhex(parts[3] + parts[4])
        return cls(d1, d2, d3, (ctypes.c_ubyte * 8)(*tail))


CLSID_MMDeviceEnumerator = GUID.from_string("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID.from_string("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = GUID.from_string("{5CDF2C82-841E-4546-9722-0CF74078229A}")


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUT_UNION),
    ]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
ERROR_ALREADY_EXISTS = 183

ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(GUID),
    ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long


def acquire_single_instance_mutex(name: str = "Local\\AutonomyVoiceMediaMutex") -> int | None:
    """Return mutex handle if this is the only running instance, else None."""
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


# ---------------------------------------------------------------------------
# Windows Core Audio (exact master volume 0..100%)
# ---------------------------------------------------------------------------
def _com_vtable_fn(obj_ptr: ctypes.c_void_p, index: int, restype: Any, *argtypes: Any):
    vtable = ctypes.cast(obj_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn_addr = vtable[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(fn_addr)


def _com_release(obj_ptr: ctypes.c_void_p | None) -> None:
    if obj_ptr and obj_ptr.value:
        release = _com_vtable_fn(obj_ptr, 2, wintypes.ULONG)
        release(obj_ptr)


def _with_endpoint_volume(fn):
    hr_init = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    should_uninit = hr_init in (S_OK, S_FALSE)
    if hr_init not in (S_OK, S_FALSE, RPC_E_CHANGED_MODE):
        raise OSError(f"CoInitializeEx failed: 0x{hr_init & 0xFFFFFFFF:08X}")

    enumerator = ctypes.c_void_p()
    device = ctypes.c_void_p()
    ep_vol = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_MMDeviceEnumerator),
            None,
            CLSCTX_ALL,
            ctypes.byref(IID_IMMDeviceEnumerator),
            ctypes.byref(enumerator),
        )
        if hr < 0 or not enumerator.value:
            raise OSError(f"CoCreateInstance(MMDeviceEnumerator) failed: 0x{hr & 0xFFFFFFFF:08X}")

        # IMMDeviceEnumerator::GetDefaultAudioEndpoint (index 4): eRender=0, eConsole=0
        get_default = _com_vtable_fn(
            enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
        )
        hr = get_default(enumerator, 0, 0, ctypes.byref(device))
        if hr < 0 or not device.value:
            raise OSError(f"GetDefaultAudioEndpoint failed: 0x{hr & 0xFFFFFFFF:08X}")

        # IMMDevice::Activate (index 3)
        activate = _com_vtable_fn(
            device,
            3,
            ctypes.c_long,
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        hr = activate(device, ctypes.byref(IID_IAudioEndpointVolume), CLSCTX_ALL, None, ctypes.byref(ep_vol))
        if hr < 0 or not ep_vol.value:
            raise OSError(f"IMMDevice::Activate(IAudioEndpointVolume) failed: 0x{hr & 0xFFFFFFFF:08X}")

        return fn(ep_vol)
    finally:
        _com_release(ep_vol)
        _com_release(device)
        _com_release(enumerator)
        if should_uninit:
            ole32.CoUninitialize()


def get_master_volume() -> int:
    """Return current Windows master volume as integer 0..100."""
    def _read(ep: ctypes.c_void_p) -> int:
        # IAudioEndpointVolume::GetMasterVolumeLevelScalar (index 9)
        getter = _com_vtable_fn(ep, 9, ctypes.c_long, ctypes.POINTER(ctypes.c_float))
        val = ctypes.c_float(0.0)
        hr = getter(ep, ctypes.byref(val))
        if hr < 0:
            raise OSError(f"GetMasterVolumeLevelScalar failed: 0x{hr & 0xFFFFFFFF:08X}")
        return int(round(max(0.0, min(1.0, val.value)) * 100))

    return _with_endpoint_volume(_read)


def set_master_volume(percent: int, *, unmute_if_positive: bool = True) -> int:
    """Set Windows master volume to exact integer 0..100 and return applied percent."""
    clamped = max(0, min(100, int(percent)))
    scalar = ctypes.c_float(clamped / 100.0)

    def _write(ep: ctypes.c_void_p) -> int:
        # IAudioEndpointVolume::SetMasterVolumeLevelScalar (index 7)
        setter = _com_vtable_fn(ep, 7, ctypes.c_long, ctypes.c_float, ctypes.c_void_p)
        hr = setter(ep, scalar, None)
        if hr < 0:
            raise OSError(f"SetMasterVolumeLevelScalar failed: 0x{hr & 0xFFFFFFFF:08X}")
        if unmute_if_positive and clamped > 0:
            # IAudioEndpointVolume::SetMute (index 14)
            set_mute = _com_vtable_fn(ep, 14, ctypes.c_long, wintypes.BOOL, ctypes.c_void_p)
            set_mute(ep, False, None)
        return clamped

    return _with_endpoint_volume(_write)


def adjust_master_volume(delta_percent: int) -> int:
    """Adjust Windows master volume by signed delta_percent (e.g. +10 or -10)."""
    current = get_master_volume()
    return set_master_volume(current + int(delta_percent))


def ramp_master_volume(
    target_percent: int,
    *,
    duration: float = 0.12,
    steps: int = 5,
    unmute_if_positive: bool = False,
) -> int:
    """Smoothly ramp Windows master volume to target_percent over a short duration."""
    clamped = max(0, min(100, int(target_percent)))
    try:
        start = get_master_volume()
    except Exception:
        return set_master_volume(clamped, unmute_if_positive=unmute_if_positive)

    if start == clamped or steps <= 1 or duration <= 0:
        return set_master_volume(clamped, unmute_if_positive=unmute_if_positive)

    sleep_s = duration / steps
    for i in range(1, steps + 1):
        val = int(round(start + (clamped - start) * (i / steps)))
        set_master_volume(val, unmute_if_positive=(unmute_if_positive and i == steps))
        if i < steps:
            time.sleep(sleep_s)
    return clamped


class VolumeDuckSession:
    """Manages 50% audio ducking while keeping logical base volume accurate across edits.

    Example:
      - Start at 60% -> start_duck(0.5) lowers live output to 30% (base=60%).
      - User says "тише" (-10%) -> adjust_base(-10) sets base=50%, live preview=25%.
      - User says "тише" (-10%) -> adjust_base(-10) sets base=40%, live preview=20%.
      - Session ends -> end_duck() smoothly restores live output to 40%.
    """

    def __init__(self, factor: float = 0.5) -> None:
        self.factor = max(0.1, min(0.95, float(factor)))
        self.active: bool = False
        self.base_volume: int | None = None

    def _ducked_level(self, base: int) -> int:
        if base <= 0:
            return 0
        return max(1, int(round(base * self.factor)))

    def start_duck(self, factor: float | None = None) -> int | None:
        if factor is not None:
            self.factor = max(0.1, min(0.95, float(factor)))
        if self.active:
            return self.base_volume
        try:
            current = get_master_volume()
        except Exception:
            return None
        self.base_volume = current
        self.active = True
        target = self._ducked_level(current)
        if target != current:
            ramp_master_volume(target, duration=0.10, steps=4, unmute_if_positive=False)
        return current

    def set_base(self, target_percent: int) -> int:
        clamped = max(0, min(100, int(target_percent)))
        if self.active:
            self.base_volume = clamped
            preview = self._ducked_level(clamped)
            set_master_volume(preview, unmute_if_positive=(clamped > 0))
            return clamped
        return set_master_volume(clamped, unmute_if_positive=(clamped > 0))

    def adjust_base(self, delta_percent: int) -> int:
        if self.active and self.base_volume is not None:
            new_base = max(0, min(100, self.base_volume + int(delta_percent)))
            self.base_volume = new_base
            preview = self._ducked_level(new_base)
            set_master_volume(preview, unmute_if_positive=(new_base > 0))
            return new_base
        return adjust_master_volume(delta_percent)

    def end_duck(self) -> int | None:
        if not self.active:
            return None
        target = self.base_volume
        self.active = False
        self.base_volume = None
        if target is not None:
            try:
                ramp_master_volume(target, duration=0.16, steps=5, unmute_if_positive=False)
            except Exception:
                pass
        return target


# ---------------------------------------------------------------------------
# Media keys
# ---------------------------------------------------------------------------
def _tap_vk(vk: int, presses: int = 1, interval: float = 0.14) -> None:
    for idx in range(max(1, presses)):
        down = INPUT(
            type=INPUT_KEYBOARD,
            ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_EXTENDEDKEY, time=0, dwExtraInfo=0),
        )
        up = INPUT(
            type=INPUT_KEYBOARD,
            ki=KEYBDINPUT(
                wVk=vk,
                wScan=0,
                dwFlags=KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP,
                time=0,
                dwExtraInfo=0,
            ),
        )
        arr = (INPUT * 2)(down, up)
        user32.SendInput(2, arr, ctypes.sizeof(INPUT))
        if idx + 1 < presses:
            time.sleep(interval)


# ---------------------------------------------------------------------------
# Sleep / Shutdown timer (Windows shutdown.exe, hidden window)
# ---------------------------------------------------------------------------
def schedule_shutdown(seconds: int) -> dict[str, Any]:
    """Schedule Windows PC shutdown after `seconds` (clamped 30s..86400s)."""
    clamped = max(30, min(86400, int(seconds)))
    # Clear any previously scheduled shutdown so re-scheduling never fails with code 1190
    subprocess.run(
        ["shutdown.exe", "/a"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    proc = subprocess.run(
        ["shutdown.exe", "/s", "/t", str(clamped)],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"shutdown.exe /s /t {clamped} failed with code {proc.returncode}")

    target_dt = datetime.now() + timedelta(seconds=clamped)
    dur_label = format_duration_ru(clamped)
    return {
        "seconds": clamped,
        "duration_label": dur_label,
        "target_time": target_dt.strftime("%H:%M"),
        "target_timestamp": target_dt.timestamp(),
    }


def cancel_shutdown() -> bool:
    """Cancel scheduled Windows PC shutdown. Returns True if a timer was active."""
    proc = subprocess.run(
        ["shutdown.exe", "/a"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    # 0 = cancelled successfully; 1116 = no shutdown in progress
    return proc.returncode == 0


def send_media_action(
    action: str,
    *,
    value: int | None = None,
    prev_double_tap: bool = True,
    volume_step_percent: int = 10,
    duck_session: VolumeDuckSession | None = None,
) -> tuple[str, dict[str, Any]]:
    """Execute a media, exact volume, or shutdown timer command.

    When `duck_session` is active, volume changes update the logical base volume
    and immediately preview the new ducked level, so restoring after the session
    keeps all volume adjustments intact.
    """
    act = action.strip().lower()
    if act == "next":
        _tap_vk(VK_MEDIA_NEXT_TRACK, presses=1)
        return "Следующий трек", {}

    if act == "prev":
        presses = 2 if prev_double_tap else 1
        _tap_vk(VK_MEDIA_PREV_TRACK, presses=presses, interval=0.18)
        return "Предыдущий трек", {}

    if act == "restart":
        _tap_vk(VK_MEDIA_PREV_TRACK, presses=1)
        return "Трек сначала", {}

    if act in {"play_pause", "stop", "resume"}:
        _tap_vk(VK_MEDIA_PLAY_PAUSE, presses=1)
        if act == "stop":
            return "Стоп / Пауза", {}
        if act == "resume":
            return "Продолжить", {}
        return "Пауза / Плей", {}

    if act == "volume_set":
        if value is None:
            raise ValueError("volume_set requires an integer target percent (0..100).")
        if duck_session is not None:
            applied = duck_session.set_base(value)
        else:
            applied = set_master_volume(value)
        return f"Громкость {applied}%", {"volume": applied}

    if act == "volume_up":
        delta = abs(int(value)) if value is not None else max(2, int(volume_step_percent))
        try:
            if duck_session is not None:
                applied = duck_session.adjust_base(+delta)
            else:
                applied = adjust_master_volume(+delta)
            return f"Громче ({applied}%)", {"volume": applied}
        except Exception:
            _tap_vk(VK_VOLUME_UP, presses=max(1, delta // 2), interval=0.03)
            return "Громче", {}

    if act == "volume_down":
        delta = abs(int(value)) if value is not None else max(2, int(volume_step_percent))
        try:
            if duck_session is not None:
                applied = duck_session.adjust_base(-delta)
            else:
                applied = adjust_master_volume(-delta)
            return f"Тише ({applied}%)", {"volume": applied}
        except Exception:
            _tap_vk(VK_VOLUME_DOWN, presses=max(1, delta // 2), interval=0.03)
            return "Тише", {}

    if act == "mute":
        _tap_vk(VK_VOLUME_MUTE, presses=1)
        return "Звук вкл/выкл", {}

    if act == "shutdown_timer":
        if value is None or value <= 0:
            raise ValueError("shutdown_timer requires duration in seconds.")
        info = schedule_shutdown(value)
        return f"Выключение через {info['duration_label']} (в {info['target_time']})", info

    if act == "shutdown_cancel":
        was_active = cancel_shutdown()
        label = "Выключение отменено" if was_active else "Таймер выключения не был активен"
        return label, {"was_active": was_active}

    raise ValueError(f"Unknown action: {action!r}")


# ---------------------------------------------------------------------------
# Windows Startup folder helper
# ---------------------------------------------------------------------------
def get_startup_vbs_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Autonomy Voice Media.vbs"


def is_autostart_enabled() -> bool:
    return get_startup_vbs_path().exists()


def set_autostart(enabled: bool, launcher_vbs: Path) -> bool:
    target = get_startup_vbs_path()
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (
            'Set WshShell = CreateObject("WScript.Shell")\n'
            f'WshShell.Run "wscript.exe ""{launcher_vbs}""", 0, False\n'
        )
        target.write_text(content, encoding="utf-8")
        return True
    if target.exists():
        target.unlink()
    return False
