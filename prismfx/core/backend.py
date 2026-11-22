"""Swappable OS backend interface: real Win32/DWM backend + deterministic DemoBackend."""

from __future__ import annotations

import abc
import ctypes
import os
import random
from dataclasses import dataclass
from typing import Optional

from . import win32_api as w32
from .catalog import AppClassification, classify_app, is_protected_window_class


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    exe_name: str
    full_path: str
    x: int
    y: int
    w: int
    h: int
    visible: bool
    minimized: bool
    maximized: bool
    topmost: bool
    opacity: int
    classification: AppClassification


class IWindowSystemBackend(abc.ABC):
    """Abstract OS backend so PrismEngine can hot-swap Live OS <-> Deterministic Demo."""

    @property
    @abc.abstractmethod
    def is_demo(self) -> bool: ...

    @abc.abstractmethod
    def list_monitors(self) -> list[w32.MonitorRect]: ...

    @abc.abstractmethod
    def list_windows(self, own_pid: int = 0) -> list[WindowInfo]: ...

    @abc.abstractmethod
    def get_window_info(self, hwnd: int) -> Optional[WindowInfo]: ...

    @abc.abstractmethod
    def get_foreground_hwnd(self) -> int: ...

    @abc.abstractmethod
    def capture_window_bgra(
        self, hwnd: int, allow_screen_fallback: bool = True
    ) -> Optional[tuple[int, int, bytes]]: ...

    @abc.abstractmethod
    def get_window_layered_state(self, hwnd: int) -> tuple[bool, Optional[int]]: ...

    @abc.abstractmethod
    def set_window_opacity(
        self, hwnd: int, alpha: int, had_layered: Optional[bool] = None
    ) -> bool: ...

    @abc.abstractmethod
    def set_window_hidden_alpha(
        self, hwnd: int, hidden: bool, restore_alpha: int = 255, had_layered: Optional[bool] = None
    ) -> bool: ...

    @abc.abstractmethod
    def is_overlay_window(self, hwnd: int) -> bool: ...

    @abc.abstractmethod
    def get_native_min_animate(self) -> bool: ...

    @abc.abstractmethod
    def set_native_min_animate(self, enabled: bool) -> bool: ...

    @abc.abstractmethod
    def dwm_flush(self) -> None: ...

    @abc.abstractmethod
    def set_window_topmost(self, hwnd: int, topmost: bool) -> bool: ...

    @abc.abstractmethod
    def set_window_cloaked(self, hwnd: int, cloaked: bool) -> bool: ...

    @abc.abstractmethod
    def move_resize_window(self, hwnd: int, x: int, y: int, w: int, h: int) -> bool: ...

    @abc.abstractmethod
    def move_window_only(self, hwnd: int, x: int, y: int) -> bool: ...

    @abc.abstractmethod
    def minimize_window(self, hwnd: int) -> bool: ...

    @abc.abstractmethod
    def maximize_window(self, hwnd: int) -> bool: ...

    @abc.abstractmethod
    def restore_window(self, hwnd: int) -> bool: ...

    @abc.abstractmethod
    def set_desktop_icons_visible(self, visible: bool) -> bool: ...

    @abc.abstractmethod
    def are_desktop_icons_visible(self) -> bool: ...

    @abc.abstractmethod
    def is_on_battery(self) -> bool: ...

    @abc.abstractmethod
    def is_fullscreen_game(self) -> bool: ...

    @abc.abstractmethod
    def is_valid_window(self, hwnd: int) -> bool: ...


class Win32WindowBackend(IWindowSystemBackend):
    """Live Windows 11 OS backend using direct ctypes Win32 & DWM calls."""

    @property
    def is_demo(self) -> bool:
        return False

    def list_monitors(self) -> list[w32.MonitorRect]:
        mons = w32.enum_monitors()
        if mons:
            return mons
        return [w32.MonitorRect("DISPLAY1", 0, 0, 1920, 1080, 0, 0, 1920, 1040, True)]

    def is_valid_window(self, hwnd: int) -> bool:
        return bool(hwnd and w32.user32.IsWindow(hwnd))

    def get_foreground_hwnd(self) -> int:
        return int(w32.user32.GetForegroundWindow() or 0)

    def get_window_info(self, hwnd: int) -> Optional[WindowInfo]:
        if not self.is_valid_window(hwnd):
            return None
        title = w32.get_window_text(hwnd)
        cls = w32.get_class_name(hwnd)
        pid, exe, path = w32.get_process_info(hwnd)
        x, y, w, h = w32.get_extended_frame_bounds(hwnd)
        return WindowInfo(
            hwnd=hwnd,
            title=title,
            class_name=cls,
            pid=pid,
            exe_name=exe or "unknown.exe",
            full_path=path,
            x=x,
            y=y,
            w=w,
            h=h,
            visible=bool(w32.user32.IsWindowVisible(hwnd)),
            minimized=bool(w32.user32.IsIconic(hwnd)),
            maximized=bool(w32.user32.IsZoomed(hwnd)),
            topmost=w32.is_window_topmost(hwnd),
            opacity=w32.get_window_opacity(hwnd),
            classification=classify_app(exe, cls),
        )

    def list_windows(self, own_pid: int = 0) -> list[WindowInfo]:
        found: list[WindowInfo] = []

        def _cb(hwnd, _lp):
            if not w32.user32.IsWindowVisible(hwnd):
                return True
            if w32.is_window_cloaked(hwnd):
                return True
            ex = w32.user32.GetWindowLongPtrW(hwnd, w32.GWL_EXSTYLE)
            if (ex & w32.WS_EX_TOOLWINDOW) and not (ex & w32.WS_EX_APPWINDOW):
                return True
            title = w32.get_window_text(hwnd)
            if not title.strip():
                return True
            cls = w32.get_class_name(hwnd)
            if is_protected_window_class(cls):
                return True
            pid, exe, path = w32.get_process_info(hwnd)
            if own_pid and pid == own_pid:
                return True
            x, y, w, h = w32.get_extended_frame_bounds(hwnd)
            if w < 80 or h < 60:
                return True
            found.append(
                WindowInfo(
                    hwnd=int(hwnd),
                    title=title,
                    class_name=cls,
                    pid=pid,
                    exe_name=exe or "unknown.exe",
                    full_path=path,
                    x=x,
                    y=y,
                    w=w,
                    h=h,
                    visible=True,
                    minimized=bool(w32.user32.IsIconic(hwnd)),
                    maximized=bool(w32.user32.IsZoomed(hwnd)),
                    topmost=bool(ex & w32.WS_EX_TOPMOST),
                    opacity=w32.get_window_opacity(hwnd),
                    classification=classify_app(exe, cls),
                )
            )
            return True

        w32.user32.EnumWindows(w32.ENUMWINDOWSPROC(_cb), 0)
        return found

    def capture_window_bgra(
        self, hwnd: int, allow_screen_fallback: bool = True
    ) -> Optional[tuple[int, int, bytes]]:
        return w32.capture_window_bgra(hwnd, allow_screen_fallback=allow_screen_fallback)

    def get_window_layered_state(self, hwnd: int) -> tuple[bool, Optional[int]]:
        return w32.get_window_layered_state(hwnd)

    def set_window_opacity(
        self, hwnd: int, alpha: int, had_layered: Optional[bool] = None
    ) -> bool:
        return w32.set_window_opacity(hwnd, alpha, had_layered=had_layered)

    def set_window_hidden_alpha(
        self, hwnd: int, hidden: bool, restore_alpha: int = 255, had_layered: Optional[bool] = None
    ) -> bool:
        return w32.set_window_hidden_alpha(hwnd, hidden, restore_alpha, had_layered=had_layered)

    def is_overlay_window(self, hwnd: int) -> bool:
        return w32.is_overlay_exstyle(hwnd)

    def get_native_min_animate(self) -> bool:
        return w32.get_native_min_animate()

    def set_native_min_animate(self, enabled: bool) -> bool:
        return w32.set_native_min_animate(enabled)

    def dwm_flush(self) -> None:
        w32.dwm_flush()

    def set_window_topmost(self, hwnd: int, topmost: bool) -> bool:
        return w32.set_window_topmost(hwnd, topmost)

    def set_window_cloaked(self, hwnd: int, cloaked: bool) -> bool:
        return w32.set_window_cloaked(hwnd, cloaked)

    def move_resize_window(self, hwnd: int, x: int, y: int, w: int, h: int) -> bool:
        return w32.move_resize_window(hwnd, x, y, w, h)

    def move_window_only(self, hwnd: int, x: int, y: int) -> bool:
        return w32.move_window_visible_pos(hwnd, x, y)

    def minimize_window(self, hwnd: int) -> bool:
        return bool(w32.user32.ShowWindow(hwnd, w32.SW_MINIMIZE))

    def maximize_window(self, hwnd: int) -> bool:
        return bool(w32.user32.ShowWindow(hwnd, w32.SW_SHOWMAXIMIZED))

    def restore_window(self, hwnd: int) -> bool:
        return bool(w32.user32.ShowWindow(hwnd, w32.SW_RESTORE))

    def set_desktop_icons_visible(self, visible: bool) -> bool:
        return w32.set_desktop_icons_visible(visible)

    def are_desktop_icons_visible(self) -> bool:
        return w32.are_desktop_icons_visible()

    def is_on_battery(self) -> bool:
        return w32.is_on_battery_power()

    def is_fullscreen_game(self) -> bool:
        return w32.is_fullscreen_game_active(own_pid=os.getpid())


class DemoWindowBackend(IWindowSystemBackend):
    """Deterministic seeded virtual desktop backend — zero OS side effects!"""

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self._icons_visible = True
        self._battery = False
        self._fullscreen = False
        self._foreground = 1001
        self._monitors = [
            w32.MonitorRect("\\\\.\\DISPLAY1", 0, 0, 1920, 1080, 0, 0, 1920, 1032, True),
            w32.MonitorRect("\\\\.\\DISPLAY2", 1920, 0, 2560, 1440, 1920, 0, 2560, 1392, False),
        ]
        specs = [
            (1001, "Visual Studio Code — PrismFX", "Chrome_WidgetWin_1", 4120, "Code.exe", 120, 80, 1180, 760),
            (1002, "Google Chrome — Shader Gallery", "Chrome_WidgetWin_1", 5840, "chrome.exe", 260, 110, 1240, 820),
            (1003, "Windows Terminal — PowerShell 7", "CASCADIA_HOSTING_WINDOW_CLASS", 6204, "WindowsTerminal.exe", 340, 170, 960, 620),
            (1004, "Spotify Premium — Lo-Fi Focus", "Chrome_WidgetWin_1", 7310, "Spotify.exe", 510, 180, 980, 680),
            (1005, "File Explorer — Projects", "CabinetWClass", 2140, "explorer.exe", 180, 140, 920, 640),
            (1006, "OBS Studio 30.2 (Recording)", "Qt662QWindowIcon", 8912, "obs64.exe", 80, 90, 1024, 680),
            (1007, "VALORANT (Protected Game)", "UnrealWindow", 9404, "VALORANT-Win64-Shipping.exe", 0, 0, 1920, 1080),
            (1008, "Notepad — Architecture Notes.md", "Notepad", 9812, "Notepad.exe", 640, 220, 720, 520),
        ]
        self._windows: dict[int, WindowInfo] = {}
        self._window_styles: dict[int, tuple[int, int]] = {}
        self._layered_hwnds: set[int] = set()
        self._per_pixel_layered_hwnds: set[int] = set()
        default_style = w32.WS_VISIBLE | w32.WS_CAPTION | w32.WS_THICKFRAME | w32.WS_SYSMENU
        borderless_style = w32.WS_VISIBLE | w32.WS_POPUP
        for hwnd, title, cls, pid, exe, x, y, w, h in specs:
            self._windows[hwnd] = WindowInfo(
                hwnd=hwnd,
                title=title,
                class_name=cls,
                pid=pid,
                exe_name=exe,
                full_path=f"C:\\Program Files\\Demo\\{exe}",
                x=x,
                y=y,
                w=w,
                h=h,
                visible=True,
                minimized=False,
                maximized=False,
                topmost=False,
                opacity=255,
                classification=classify_app(exe, cls),
            )
            self._window_styles[hwnd] = (borderless_style if hwnd == 1007 else default_style, 0)

    @property
    def is_demo(self) -> bool:
        return True

    def list_monitors(self) -> list[w32.MonitorRect]:
        return list(self._monitors)

    def list_windows(self, own_pid: int = 0) -> list[WindowInfo]:
        return list(self._windows.values())

    def get_window_info(self, hwnd: int) -> Optional[WindowInfo]:
        return self._windows.get(hwnd)

    def get_foreground_hwnd(self) -> int:
        return self._foreground

    def is_valid_window(self, hwnd: int) -> bool:
        return hwnd in self._windows

    def capture_window_bgra(
        self, hwnd: int, allow_screen_fallback: bool = True
    ) -> Optional[tuple[int, int, bytes]]:
        info = self._windows.get(hwnd)
        if not info:
            return None
        # Generate a deterministic synthetic BGRA gradient card (256x160)
        w, h = 256, 160
        buf = bytearray(w * h * 4)
        seed_tint = (hwnd * 73) & 0xFF
        for yy in range(h):
            for xx in range(w):
                idx = (yy * w + xx) * 4
                is_bar = yy < 24
                if is_bar:
                    buf[idx:idx + 4] = bytes((38, 28, 24, 255))
                else:
                    buf[idx] = (40 + (xx * 80 // w) + (seed_tint // 3)) & 0xFF
                    buf[idx + 1] = (24 + (yy * 40 // h)) & 0xFF
                    buf[idx + 2] = (22 + ((xx + yy) * 20 // (w + h))) & 0xFF
                    buf[idx + 3] = 255
        return (w, h, bytes(buf))

    def get_window_layered_state(self, hwnd: int) -> tuple[bool, Optional[int]]:
        w = self._windows.get(hwnd)
        if not w:
            return (False, None)
        if hwnd in self._per_pixel_layered_hwnds:
            return (True, None)
        return (hwnd in self._layered_hwnds, w.opacity)

    def set_window_opacity(
        self, hwnd: int, alpha: int, had_layered: Optional[bool] = None
    ) -> bool:
        if hwnd not in self._windows or hwnd in self._per_pixel_layered_hwnds:
            return False
        clamped = max(20, min(255, int(alpha)))
        self._windows[hwnd].opacity = clamped
        if clamped < 255:
            self._layered_hwnds.add(hwnd)
        elif had_layered is False:
            self._layered_hwnds.discard(hwnd)
        return True

    def set_window_hidden_alpha(
        self, hwnd: int, hidden: bool, restore_alpha: int = 255, had_layered: Optional[bool] = None
    ) -> bool:
        if hwnd not in self._windows or hwnd in self._per_pixel_layered_hwnds:
            return False
        if hidden:
            self._windows[hwnd].opacity = 0
            self._layered_hwnds.add(hwnd)
            return True
        clamped = max(20, min(255, int(restore_alpha)))
        self._windows[hwnd].opacity = clamped
        if clamped < 255:
            self._layered_hwnds.add(hwnd)
        elif had_layered is False:
            self._layered_hwnds.discard(hwnd)
        return True

    def is_overlay_window(self, hwnd: int) -> bool:
        return False

    def get_native_min_animate(self) -> bool:
        return True

    def set_native_min_animate(self, enabled: bool) -> bool:
        return True

    def dwm_flush(self) -> None:
        pass

    def set_window_topmost(self, hwnd: int, topmost: bool) -> bool:
        if hwnd in self._windows:
            self._windows[hwnd].topmost = topmost
            return True
        return False

    def set_window_cloaked(self, hwnd: int, cloaked: bool) -> bool:
        return hwnd in self._windows

    def move_resize_window(self, hwnd: int, x: int, y: int, w: int, h: int) -> bool:
        if hwnd in self._windows:
            win = self._windows[hwnd]
            win.x, win.y, win.w, win.h = x, y, w, h
            return True
        return False

    def move_window_only(self, hwnd: int, x: int, y: int) -> bool:
        if hwnd in self._windows:
            win = self._windows[hwnd]
            win.x, win.y = x, y
            return True
        return False

    def minimize_window(self, hwnd: int) -> bool:
        if hwnd in self._windows:
            self._windows[hwnd].minimized = True
            return True
        return False

    def maximize_window(self, hwnd: int) -> bool:
        if hwnd in self._windows:
            self._windows[hwnd].maximized = True
            return True
        return False

    def restore_window(self, hwnd: int) -> bool:
        if hwnd in self._windows:
            self._windows[hwnd].minimized = False
            self._windows[hwnd].maximized = False
            return True
        return False

    def set_desktop_icons_visible(self, visible: bool) -> bool:
        self._icons_visible = visible
        return True

    def are_desktop_icons_visible(self) -> bool:
        return self._icons_visible

    def is_on_battery(self) -> bool:
        return self._battery

    def set_foreground_hwnd(self, hwnd: int) -> None:
        self._foreground = int(hwnd)

    def set_window_style(self, hwnd: int, style: int, ex_style: int = 0) -> None:
        self._window_styles[int(hwnd)] = (int(style), int(ex_style))

    def is_fullscreen_game(self) -> bool:
        if self._fullscreen:
            return True
        fg = self._windows.get(self._foreground)
        if not fg or not fg.visible or fg.minimized:
            return False
        cx = fg.x + fg.w // 2
        cy = fg.y + fg.h // 2
        mon: Optional[w32.MonitorRect] = None
        for m in self._monitors:
            if m.left <= cx < m.right and m.top <= cy < m.bottom:
                mon = m
                break
        if mon is None:
            return False
        style, ex_style = self._window_styles.get(
            fg.hwnd, (w32.WS_VISIBLE | w32.WS_CAPTION | w32.WS_THICKFRAME | w32.WS_SYSMENU, 0)
        )
        return w32.is_borderless_fullscreen_candidate(
            class_name=fg.class_name,
            exe_name=fg.exe_name,
            style=style,
            ex_style=ex_style,
            rect=(fg.x, fg.y, fg.w, fg.h),
            monitor_rect=mon,
            is_cloaked=False,
            is_iconic=fg.minimized,
            is_zoomed=fg.maximized,
        )
