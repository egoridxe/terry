"""Autonomous Liked Tracks launcher for SoundCloud (priority) and Spotify.

Waterfall per service:
1. If desktop app is already open (or in tray) -> bring to front, navigate to Likes, start playback.
2. If desktop app is installed on PC (closed) -> launch app, wait until ready, navigate to Likes, play.
3. If desktop app is NOT installed on PC -> open Liked Tracks directly in the default web browser.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import time
from typing import Any
import webbrowser
import winreg

import numpy as np
from PIL import ImageGrab

log = logging.getLogger("voice_media")


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002

SW_SHOW = 5
SW_RESTORE = 9
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

SOUNDCLOUD_LIKES_URL = "https://soundcloud.com/you/likes"
SPOTIFY_LIKES_URI = "spotify:collection:tracks"
SPOTIFY_LIKES_URL = "https://open.spotify.com/collection/tracks"


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


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
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL

kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32NextW.restype = wintypes.BOOL

dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


# ---------------------------------------------------------------------------
# Win32 window & human-paced click helpers
# ---------------------------------------------------------------------------
def _get_process_path(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(512)
        buf = ctypes.create_unicode_buffer(512)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD(0)
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return hr == 0 and cloaked.value != 0


def find_windows_by_process(process_names: tuple[str, ...]) -> list[dict[str, Any]]:
    wanted = {p.lower() for p in process_names}
    results: list[dict[str, Any]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd: int, _lp: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe_path = _get_process_path(int(pid.value))
        exe_name = os.path.basename(exe_path).lower()
        if exe_name not in wanted:
            return True
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if not user32.IsIconic(hwnd) and (w < 300 or h < 200):
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        results.append(
            {
                "hwnd": int(hwnd),
                "title": buf.value.strip(),
                "pid": int(pid.value),
                "exe_path": exe_path,
                "x": int(rect.left),
                "y": int(rect.top),
                "width": int(w),
                "height": int(h),
                "minimized": bool(user32.IsIconic(hwnd)),
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return results


def focus_hwnd(hwnd: int) -> dict[str, int]:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)

    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_tid = kernel32.GetCurrentThreadId()
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)

    att_fg = False
    att_t = False
    try:
        if fg_tid and fg_tid != cur_tid:
            att_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        if target_tid and target_tid != cur_tid:
            att_t = bool(user32.AttachThreadInput(cur_tid, target_tid, True))
        user32.BringWindowToTop(hwnd)
        if not user32.SetForegroundWindow(hwnd):
            # Harmless Alt tap releases Windows foreground lock
            alt_down = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=0x12, wScan=0, dwFlags=0, time=0, dwExtraInfo=0))
            alt_up = INPUT(
                type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(wVk=0x12, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=0),
            )
            arr = (INPUT * 2)(alt_down, alt_up)
            user32.SendInput(2, arr, ctypes.sizeof(INPUT))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
    finally:
        if att_t:
            user32.AttachThreadInput(cur_tid, target_tid, False)
        if att_fg:
            user32.AttachThreadInput(cur_tid, fg_tid, False)

    time.sleep(0.15)
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "x": int(rect.left),
        "y": int(rect.top),
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top),
    }


def human_click(x: int, y: int, *, hover_seconds: float = 0.08, hold_seconds: float = 0.045) -> None:
    """Move cursor, hover briefly, and send a separate down/up click for Chromium/Electron apps."""
    user32.SetCursorPos(int(x), int(y))
    if hover_seconds > 0:
        time.sleep(hover_seconds)
    down = INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTDOWN, time=0, dwExtraInfo=0),
    )
    up = INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTUP, time=0, dwExtraInfo=0),
    )
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    time.sleep(max(0.02, hold_seconds))
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def _is_process_running(exe_name: str) -> bool:
    """Fast check if a process with matching basename is running (even minimized to tray)."""
    target = exe_name.lower()
    # Enumerate via CreateToolhelp32Snapshot (zero subprocess overhead)
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ULONG_PTR),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (-1, 0, ctypes.c_void_p(-1).value):
        return False
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return False
        while True:
            if entry.szExeFile.lower() == target:
                return True
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
        return False
    finally:
        kernel32.CloseHandle(snap)


# ---------------------------------------------------------------------------
# App discovery (portable across any Windows user profile)
# ---------------------------------------------------------------------------
def find_soundcloud_app(custom_path: str | None = None) -> Path | None:
    if custom_path:
        p = Path(os.path.expandvars(os.path.expanduser(custom_path)))
        if p.exists():
            return p

    # 1. If already open, grab live exe path
    wins = find_windows_by_process(("soundcloud.exe",))
    if wins and wins[0].get("exe_path"):
        p = Path(wins[0]["exe_path"])
        if p.exists():
            return p

    # 2. Standard Windows install locations & shortcuts
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    roaming = Path(os.environ.get("APPDATA", ""))
    home = Path(os.environ.get("USERPROFILE", ""))
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))

    candidates = [
        local / "Programs" / "soundcloud-rpc" / "SoundCloud.exe",
        local / "Programs" / "SoundCloud" / "SoundCloud.exe",
        local / "SoundCloud" / "SoundCloud.exe",
        pf / "SoundCloud" / "SoundCloud.exe",
        pf86 / "SoundCloud" / "SoundCloud.exe",
        home / "Desktop" / "SoundCloud.lnk",
        roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "SoundCloud.lnk",
        roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "SoundCloud" / "SoundCloud.lnk",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def is_spotify_uri_registered() -> bool:
    for root, sub in (
        (winreg.HKEY_CURRENT_USER, r"Software\Classes\spotify\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"spotify\shell\open\command"),
    ):
        try:
            with winreg.OpenKey(root, sub) as key:
                val, _ = winreg.QueryValueEx(key, "")
                if val:
                    return True
        except OSError:
            continue
    return False


def find_spotify_app(custom_path: str | None = None) -> Path | None:
    if custom_path:
        p = Path(os.path.expandvars(os.path.expanduser(custom_path)))
        if p.exists():
            return p

    wins = find_windows_by_process(("spotify.exe",))
    if wins and wins[0].get("exe_path"):
        p = Path(wins[0]["exe_path"])
        if p.exists():
            return p

    roaming = Path(os.environ.get("APPDATA", ""))
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        roaming / "Spotify" / "Spotify.exe",
        local / "Microsoft" / "WindowsApps" / "Spotify.exe",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


# ---------------------------------------------------------------------------
# SoundCloud Liked Tracks automation
# ---------------------------------------------------------------------------
def _wait_for_window(process_names: tuple[str, ...], timeout: float = 12.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wins = find_windows_by_process(process_names)
        if wins:
            return wins[0]
        time.sleep(0.3)
    return None


def play_soundcloud_favorites(custom_path: str | None = None) -> tuple[str, dict[str, Any]]:
    """Open SoundCloud Liked Tracks in desktop app (or browser fallback) and start playback."""
    app_path = find_soundcloud_app(custom_path)
    if app_path is None:
        log.info("SoundCloud desktop app not found; opening browser fallback: %s", SOUNDCLOUD_LIKES_URL)
        webbrowser.open(SOUNDCLOUD_LIKES_URL)
        return "Любимые треки СК (браузер)", {"mode": "browser", "url": SOUNDCLOUD_LIKES_URL}

    was_running = _is_process_running("SoundCloud.exe")
    wins = find_windows_by_process(("soundcloud.exe",))
    if not wins:
        log.info("Launching/unhiding SoundCloud app: %s (was_running=%s)", app_path, was_running)
        os.startfile(str(app_path))  # type: ignore[attr-defined]
        win = _wait_for_window(("soundcloud.exe",), timeout=14.0)
        if not win:
            log.warning("SoundCloud window did not appear; falling back to browser")
            webbrowser.open(SOUNDCLOUD_LIKES_URL)
            return "Любимые треки СК (браузер)", {"mode": "browser", "url": SOUNDCLOUD_LIKES_URL}
        # Cold start needs extra seconds for soundcloud.com to finish loading inside Electron
        time.sleep(1.0 if was_running else 4.5)
    else:
        win = wins[0]

    rect = focus_hwnd(win["hwnd"])
    wx, wy, ww, wh = rect["x"], rect["y"], rect["width"], rect["height"]

    # SoundCloud web layout centers a 1240px container inside the BrowserView (32px Electron header)
    left_margin = max(0, (ww - 1240) // 2)
    cx_base = wx + left_margin
    cy_base = wy + 32

    # 1. Click Library (activate Electron BrowserView + navigate)
    lib_x = cx_base + 232
    lib_y = cy_base + 26
    human_click(lib_x, lib_y, hover_seconds=0.06, hold_seconds=0.045)
    time.sleep(0.18)
    human_click(lib_x, lib_y, hover_seconds=0.05, hold_seconds=0.045)
    time.sleep(0.95)

    # 2. Click Likes sub-tab under Library
    likes_x = cx_base + 154
    likes_y = cy_base + 93
    human_click(likes_x, likes_y, hover_seconds=0.06, hold_seconds=0.045)
    time.sleep(1.15)

    # 3. Hover over first Liked track cover so SoundCloud reveals its circular Play button, then click
    play_x = cx_base + 100
    play_y = cy_base + 297
    human_click(play_x, play_y, hover_seconds=0.35, hold_seconds=0.05)

    log.info("Triggered SoundCloud Likes playback at (%d, %d) inside window %dx%d", play_x, play_y, ww, wh)
    return "Любимые треки SoundCloud", {"mode": "app", "path": str(app_path)}


# ---------------------------------------------------------------------------
# Spotify Liked Songs automation (spotify:collection:tracks + #1ED760 Play detector)
# ---------------------------------------------------------------------------
def detect_spotify_play_button(rect: dict[str, int]) -> tuple[int, int] | None:
    """Locate Spotify's signature green circular Play button (#1ED760) inside the window."""
    wx, wy, ww, wh = rect["x"], rect["y"], rect["width"], rect["height"]
    if ww < 400 or wh < 300:
        return None
    try:
        img = ImageGrab.grab(bbox=(wx, wy, wx + ww, wy + wh), all_screens=True)
    except Exception as exc:
        log.debug("Spotify ImageGrab failed: %s", exc)
        return None

    arr = np.asarray(img, dtype=np.uint8)
    # Action bar sits in y in [22%, 68%], x in [4%, 58%] regardless of sidebar width
    y0, y1 = int(wh * 0.22), int(wh * 0.68)
    x0, x1 = int(ww * 0.04), int(ww * 0.58)
    roi = arr[y0:y1, x0:x1]
    if roi.size == 0:
        return None

    r, g, b = roi[:, :, 0], roi[:, :, 1], roi[:, :, 2]
    # Signature Spotify green (#1ED760 / #1DB954 / hover #3BE477)
    mask = (r < 85) & (g > 170) & (b > 60) & (b < 145) & (g.astype(np.int16) - r.astype(np.int16) > 105)
    ys, xs = np.nonzero(mask)
    if ys.size < 250:
        return None

    # Use median to resist stray green text/icons; verify cluster density around median
    med_x = int(np.median(xs))
    med_y = int(np.median(ys))
    local_count = int(np.sum((np.abs(xs - med_x) <= 38) & (np.abs(ys - med_y) <= 38)))
    if local_count < 250:
        return None

    return wx + x0 + med_x, wy + y0 + med_y


def play_spotify_favorites(custom_path: str | None = None) -> tuple[str, dict[str, Any]]:
    """Open Spotify Liked Songs via spotify:collection:tracks (or browser fallback) and click Play."""
    has_uri = is_spotify_uri_registered()
    app_path = find_spotify_app(custom_path)

    if not has_uri and app_path is None:
        log.info("Spotify desktop app not found; opening browser fallback: %s", SPOTIFY_LIKES_URL)
        webbrowser.open(SPOTIFY_LIKES_URL)
        return "Любимые треки Spotify (браузер)", {"mode": "browser", "url": SPOTIFY_LIKES_URL}

    was_running = _is_process_running("Spotify.exe")
    if has_uri:
        log.info("Opening Spotify Liked Songs via URI: %s (was_running=%s)", SPOTIFY_LIKES_URI, was_running)
        os.startfile(SPOTIFY_LIKES_URI)  # type: ignore[attr-defined]
    else:
        os.startfile(str(app_path))  # type: ignore[attr-defined]

    win = _wait_for_window(("spotify.exe",), timeout=14.0)
    if not win:
        log.warning("Spotify window did not appear; falling back to browser")
        webbrowser.open(SPOTIFY_LIKES_URL)
        return "Любимые треки Spotify (браузер)", {"mode": "browser", "url": SPOTIFY_LIKES_URL}

    # Wait briefly for Liked Songs view to render, then focus and detect green Play circle
    time.sleep(1.0 if was_running else 2.5)
    rect = focus_hwnd(win["hwnd"])

    btn: tuple[int, int] | None = None
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        btn = detect_spotify_play_button(rect)
        if btn is not None:
            break
        time.sleep(0.35)
        rect = focus_hwnd(win["hwnd"])

    if btn is not None:
        human_click(btn[0], btn[1], hover_seconds=0.10, hold_seconds=0.05)
        log.info("Clicked Spotify Liked Songs green Play button at (%d, %d)", btn[0], btn[1])
    else:
        # Fallback: standard position of Play button when left sidebar is collapsed
        fx = rect["x"] + 127
        fy = rect["y"] + 374
        human_click(fx, fy, hover_seconds=0.10, hold_seconds=0.05)
        log.info("Green Play button not detected; clicked fallback (%d, %d)", fx, fy)

    return "Любимые треки Spotify", {"mode": "app", "uri": SPOTIFY_LIKES_URI}


def play_favorites(service: str = "soundcloud", *, config: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    cfg = config or {}
    svc = service.strip().lower()
    if svc == "spotify":
        return play_spotify_favorites(custom_path=cfg.get("spotify_app_path"))
    return play_soundcloud_favorites(custom_path=cfg.get("soundcloud_app_path"))
