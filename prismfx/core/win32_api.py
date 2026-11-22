"""Direct Win32 & DWM OS interop via ctypes — zero external wrapper packages.

Includes zero-swapchain Win32LayeredSurface (UpdateLayeredWindow ULW_ALPHA) for
100% NVIDIA Overlay / MPO flicker-free per-pixel alpha desktop animation overlays.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# DLL handles (strictly loaded from System32 to prevent CWD/AppDir DLL hijacking)
# ---------------------------------------------------------------------------
LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
LOAD_LIBRARY_SEARCH_DEFAULT_DIRS = 0x00001000


def _load_system_dll(name: str) -> ctypes.WinDLL:
    try:
        return ctypes.WinDLL(name, use_last_error=True, winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)
    except TypeError:
        return ctypes.WinDLL(name, use_last_error=True)


user32 = _load_system_dll("user32")
gdi32 = _load_system_dll("gdi32")
dwmapi = _load_system_dll("dwmapi")
kernel32 = _load_system_dll("kernel32")
shell32 = _load_system_dll("shell32")
psapi = _load_system_dll("psapi")
try:
    wtsapi32: Optional[ctypes.WinDLL] = _load_system_dll("wtsapi32")
except OSError:
    wtsapi32 = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_VISIBLE = 0x10000000
WS_MINIMIZE = 0x20000000
WS_MAXIMIZE = 0x01000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000

GA_ROOT = 2
GW_OWNER = 4
WM_QUIT = 0x0012

WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

SPI_GETANIMATION = 0x0048
SPI_SETANIMATION = 0x0049
SPIF_SENDCHANGE = 0x0002

HWND_TOP = 0
HWND_BOTTOM = 1
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SWP_ASYNCWINDOWPOS = 0x4000

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAK = 13
DWMWA_CLOAKED = 14
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MENUSTART = 0x0004
EVENT_SYSTEM_MENUEND = 0x0005
EVENT_SYSTEM_MENUPOPUPSTART = 0x0006
EVENT_SYSTEM_MENUPOPUPEND = 0x0007
EVENT_SYSTEM_MOVESIZESTART = 0x000A
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_OBJECT_CLOAKED = 0x8017
EVENT_OBJECT_UNCLOAKED = 0x8018

WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0
CHILDID_SELF = 0

WH_MOUSE_LL = 14
WM_INPUT = 0x00FF
WM_APP = 0x8000
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_NCHITTEST = 0x0084
HTCAPTION = 2
HTMINBUTTON = 8
HTMAXBUTTON = 9
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17
HTCLOSE = 20
HT_RESIZE_BORDERS = frozenset({
    HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT,
})
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_DISPLAYCHANGE = 0x007E
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0
VK_LBUTTON = 0x01

MSGFLT_ALLOW = 1
IPC_CMD_ACTIVATE = 1
IPC_CMD_QUIT = 2

SEE_MASK_NOCLOSEPROCESS = 0x00000040
ERROR_CANCELLED = 1223
WAIT_OBJECT_0 = 0x00000000
STILL_ACTIVE = 259

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

HWND_MESSAGE = -3
RID_INPUT = 0x10000003
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEMOUSE = 0
RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
RI_MOUSE_LEFT_BUTTON_UP = 0x0002
RI_MOUSE_MIDDLE_BUTTON_DOWN = 0x0010

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183

QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_APP = 7

# ---------------------------------------------------------------------------
# Structs
# ---------------------------------------------------------------------------
class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right - self.left, self.bottom - self.top)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class ANIMATIONINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("iMinAnimate", ctypes.c_int)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class _RAWMOUSE_BUTTONS(ctypes.Structure):
    _fields_ = [
        ("usButtonFlags", wintypes.USHORT),
        ("usButtonData", wintypes.USHORT),
    ]


class _RAWMOUSE_UNION(ctypes.Union):
    _fields_ = [
        ("ulButtons", wintypes.ULONG),
        ("buttons", _RAWMOUSE_BUTTONS),
    ]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [
        ("usFlags", wintypes.USHORT),
        ("_pad", wintypes.USHORT),
        ("_u", _RAWMOUSE_UNION),
        ("ulRawButtons", wintypes.ULONG),
        ("lLastX", wintypes.LONG),
        ("lLastY", wintypes.LONG),
        ("ulExtraInformation", wintypes.ULONG),
    ]


class RAWINPUT_MOUSE(ctypes.Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("mouse", RAWMOUSE),
    ]


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [
        ("fmtid", GUID),
        ("pid", wintypes.DWORD),
    ]


class PROPVARIANT_LPWSTR(ctypes.Structure):
    _fields_ = [
        ("vt", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("pwszVal", ctypes.c_void_p),
        ("_pad", ctypes.c_void_p),
    ]


HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)


# ---------------------------------------------------------------------------
# Prototypes
# ---------------------------------------------------------------------------
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)
HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
ENUMWINDOWSPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
)

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.GetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.COLORREF),
    ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(wintypes.DWORD),
]
user32.GetLayeredWindowAttributes.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.UnhookWinEvent.restype = wintypes.BOOL
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HANDLE
user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.UnregisterClassW.restype = wintypes.BOOL
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.GetRawInputData.argtypes = [
    wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT
]
user32.GetRawInputData.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.EnumWindows.argtypes = [ENUMWINDOWSPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC, ctypes.POINTER(RECT), MONITORENUMPROC, wintypes.LPARAM,
]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long  # Avoid auto-raising PermissionError on cross-proc calls
dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
dwmapi.DwmFlush.argtypes = []
dwmapi.DwmFlush.restype = ctypes.c_long

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
kernel32.GetSystemPowerStatus.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.SetConsoleCtrlHandler.argtypes = [HANDLER_ROUTINE, wintypes.BOOL]
kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.GetTickCount64.argtypes = []
kernel32.GetTickCount64.restype = ctypes.c_uint64
try:
    kernel32.SetDefaultDllDirectories.argtypes = [wintypes.DWORD]
    kernel32.SetDefaultDllDirectories.restype = wintypes.BOOL
except Exception:
    pass

if wtsapi32 is not None:
    try:
        wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
        wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
        wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
        wtsapi32.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
    except Exception:
        wtsapi32 = None

user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
try:
    user32.ChangeWindowMessageFilterEx.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p,
    ]
    user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
except Exception:
    pass

shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
shell32.SHQueryUserNotificationState.restype = ctypes.c_long
shell32.IsUserAnAdmin.argtypes = []
shell32.IsUserAnAdmin.restype = wintypes.BOOL
shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
shell32.ShellExecuteExW.restype = wintypes.BOOL
try:
    shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
    shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
except Exception:
    pass
try:
    shell32.SHGetPropertyStoreForWindow.argtypes = [
        wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
except Exception:
    pass


# ---------------------------------------------------------------------------
# High-level safe helper functions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MonitorRect:
    name: str
    left: int
    top: int
    width: int
    height: int
    work_left: int
    work_top: int
    work_width: int
    work_height: int
    is_primary: bool

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def work_right(self) -> int:
        return self.work_left + self.work_width

    @property
    def work_bottom(self) -> int:
        return self.work_top + self.work_height


def fnv1a_32(text: str) -> str:
    """Deterministic FNV-1a 32-bit hash for isolated profile mutex/socket naming."""
    h = 0x811C9DC5
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def create_single_instance_mutex(profile_id: str) -> tuple[int, bool]:
    name = f"Local\\PrismFX_Instance_{fnv1a_32(profile_id.lower())}"
    handle = kernel32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    return int(handle or 0), err == ERROR_ALREADY_EXISTS


def close_handle(handle: int) -> None:
    if handle:
        kernel32.CloseHandle(handle)


def get_boot_tick_ms() -> int:
    """Return monotonic milliseconds elapsed since system boot (64-bit, never wraps)."""
    try:
        return int(kernel32.GetTickCount64())
    except Exception:
        return 0


def register_session_notification(hwnd: int) -> bool:
    """Register hwnd to receive WM_WTSSESSION_CHANGE notifications for the current session."""
    if not hwnd or wtsapi32 is None:
        return False
    try:
        return bool(wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION))
    except Exception:
        return False


def unregister_session_notification(hwnd: int) -> bool:
    """Unregister hwnd from WM_WTSSESSION_CHANGE notifications."""
    if not hwnd or wtsapi32 is None:
        return False
    try:
        return bool(wtsapi32.WTSUnRegisterSessionNotification(hwnd))
    except Exception:
        return False


def harden_dll_search_order() -> bool:
    """Restrict default Windows DLL search directories to mitigate CWD/PATH DLL hijacking."""
    try:
        return bool(kernel32.SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS))
    except Exception:
        return False


def is_user_admin() -> bool:
    """Return True if the current process is running with elevated Administrator privileges."""
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_process_elevated() -> bool:
    """Return True if the current process is running with elevated Administrator privileges."""
    return is_user_admin()


def _fnv1a_64(text: str) -> int:
    h = 0xCBF29CE484222325
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def register_ipc_message(profile_id: str) -> int:
    """Register a profile-scoped Win32 IPC window message (ACTIVATE)."""
    name = f"PrismFX_IPC_{fnv1a_32(profile_id.lower())}"
    return int(user32.RegisterWindowMessageW(name) or 0)


def register_ipc_quit_message(profile_key: str) -> int:
    """Register a separate profile-scoped Win32 IPC QUIT message (never UIPI-whitelisted)."""
    h = _fnv1a_64(str(profile_key).lower())
    name = f"PrismFX_Quit_{h:016X}"
    return int(user32.RegisterWindowMessageW(name) or 0)


def allow_window_message_uipi(hwnd: int, msg: int) -> bool:
    """Allow lower-integrity processes (e.g. standard Explorer launch) to send msg to an elevated PrismFX."""
    if not hwnd or not msg:
        return False
    try:
        return bool(user32.ChangeWindowMessageFilterEx(hwnd, msg, MSGFLT_ALLOW, None))
    except Exception:
        return False


def find_instance_windows(title_exact: str, exclude_pid: int = 0) -> list[int]:
    """Find all PrismFX Studio top-level windows (even when hidden to tray), skipping exclude_pid."""
    found: list[int] = []

    def _cb(hwnd, _lp):
        if exclude_pid:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == exclude_pid:
                return True
        if get_window_text(hwnd) == title_exact:
            found.append(int(hwnd))
        return True

    user32.EnumWindows(ENUMWINDOWSPROC(_cb), 0)
    return found


def find_instance_window(title_exact: str, exclude_pid: int = 0) -> int:
    """Find an existing PrismFX Studio window (even when hidden to tray), skipping exclude_pid."""
    wins = find_instance_windows(title_exact, exclude_pid=exclude_pid)
    return wins[0] if wins else 0


_CONSOLE_CTRL_REFS: list[HANDLER_ROUTINE] = []


def register_console_ctrl_handler(on_close_fn) -> bool:
    """Install a Win32 console control handler so closing CMD / Ctrl+C / Logoff ALWAYS restores OS state!"""
    @HANDLER_ROUTINE
    def _handler(ctrl_type: int) -> bool:
        if ctrl_type in (CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            try:
                on_close_fn(int(ctrl_type))
            except Exception:
                pass
            return True
        return False

    try:
        if kernel32.SetConsoleCtrlHandler(_handler, True):
            _CONSOLE_CTRL_REFS.append(_handler)
            return True
    except Exception:
        pass
    return False


def shell_execute_elevated(
    file_path: str,
    parameters: str = "",
    cwd: str = "",
    show_cmd: int = SW_SHOWNORMAL,
    wait_timeout_ms: int = 0,
    hwnd: int = 0,
) -> tuple[bool, int, str]:
    """Launch an elevated process via official Windows UAC (`runas`).

    Returns `(ok, exit_code_or_winerr, status)` where status is `"ok"`, `"cancelled"`, or `"error"`.
    """
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd = hwnd or 0
    sei.lpVerb = "runas"
    sei.lpFile = file_path
    sei.lpParameters = parameters or None
    sei.lpDirectory = cwd or None
    sei.nShow = show_cmd

    ctypes.set_last_error(0)
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            return (False, err, "cancelled")
        return (False, err, "error")

    hproc = int(sei.hProcess or 0)
    if not hproc:
        return (True, 0, "ok")
    try:
        if wait_timeout_ms > 0:
            wres = kernel32.WaitForSingleObject(hproc, wait_timeout_ms)
            if wres == WAIT_OBJECT_0:
                code = wintypes.DWORD(0)
                kernel32.GetExitCodeProcess(hproc, ctypes.byref(code))
                return (code.value == 0, int(code.value), "ok" if code.value == 0 else "error")
            return (False, -1, "timeout")
        return (True, 0, "ok")
    finally:
        kernel32.CloseHandle(hproc)


_IID_IPROPERTYSTORE = GUID(
    0x886D8EEB, 0x8CF2, 0x4446, (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99)
)
_FMTID_APPUSERMODEL = GUID(
    0x9F4C2855, 0x9F79, 0x4B39, (ctypes.c_ubyte * 8)(0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3)
)


def set_window_app_user_model_properties(
    hwnd: int,
    app_id: str,
    relaunch_cmd: str = "",
    display_name: str = "",
    icon_resource: str = "",
) -> bool:
    """Bind taskbar identity & pin-to-taskbar relaunch command directly to PrismFX.exe via IPropertyStore."""
    if not hwnd:
        return False
    try:
        store_ptr = ctypes.c_void_p()
        hr = shell32.SHGetPropertyStoreForWindow(
            hwnd, ctypes.byref(_IID_IPROPERTYSTORE), ctypes.byref(store_ptr)
        )
        if hr != 0 or not store_ptr.value:
            return False
        vtable_ptr = ctypes.cast(store_ptr.value, ctypes.POINTER(ctypes.c_void_p)).contents.value
        vtable = ctypes.cast(vtable_ptr, ctypes.POINTER(ctypes.c_void_p))

        _Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        _SetValue = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT_LPWSTR)
        )(vtable[6])
        _Commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])

        try:
            def _set_str(pid: int, text: str) -> None:
                if not text:
                    return
                key = PROPERTYKEY(_FMTID_APPUSERMODEL, pid)
                buf = ctypes.create_unicode_buffer(text)
                pv = PROPVARIANT_LPWSTR()
                pv.vt = 31  # VT_LPWSTR
                pv.pwszVal = ctypes.cast(buf, ctypes.c_void_p).value
                _SetValue(store_ptr.value, ctypes.byref(key), ctypes.byref(pv))

            _set_str(5, app_id)         # System.AppUserModel.ID
            _set_str(2, relaunch_cmd)   # System.AppUserModel.RelaunchCommand
            _set_str(3, icon_resource)  # System.AppUserModel.RelaunchIconResource
            _set_str(4, display_name)   # System.AppUserModel.RelaunchDisplayNameResource
            _Commit(store_ptr.value)
            return True
        finally:
            _Release(store_ptr.value)
    except Exception:
        return False


def get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    n = user32.GetClassNameW(hwnd, buf, 256)
    return buf.value[:n] if n > 0 else ""


def get_process_info(hwnd: int) -> tuple[int, str, str]:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return (0, "", "")
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not hproc:
        return (pid.value, "", "")
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
            full = buf.value
            exe = full.rsplit("\\", 1)[-1]
            return (pid.value, exe, full)
        return (pid.value, "", "")
    finally:
        kernel32.CloseHandle(hproc)


def get_extended_frame_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """Return physical (x, y, w, h) excluding invisible DWM shadow margins."""
    r = RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)
    )
    if hr == 0 and r.width > 0 and r.height > 0:
        return r.as_tuple()
    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return r.as_tuple()
    return (0, 0, 0, 0)


def get_window_rect_size(hwnd: int) -> tuple[int, int]:
    """Ultra-fast kernel-only (w, h) check via GetWindowRect (zero DWM / zero IPC)."""
    r = RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return (r.width, r.height)
    return (0, 0)


def is_window_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD(0)
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return hr == 0 and cloaked.value != 0


def set_window_cloaked(hwnd: int, cloak: bool) -> bool:
    val = wintypes.BOOL(1 if cloak else 0)
    hr = dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_CLOAK, ctypes.byref(val), ctypes.sizeof(val))
    return hr == 0


def dwm_flush() -> None:
    """Wait exactly one DWM composition frame (~6 ms @ 164 Hz) for zero-flash overlay handoffs."""
    try:
        dwmapi.DwmFlush()
    except Exception:
        pass


def get_native_min_animate() -> bool:
    ai = ANIMATIONINFO(cbSize=ctypes.sizeof(ANIMATIONINFO))
    if user32.SystemParametersInfoW(SPI_GETANIMATION, ai.cbSize, ctypes.byref(ai), 0):
        return bool(ai.iMinAnimate)
    return True


def set_native_min_animate(enabled: bool) -> bool:
    ai = ANIMATIONINFO(cbSize=ctypes.sizeof(ANIMATIONINFO), iMinAnimate=1 if enabled else 0)
    return bool(user32.SystemParametersInfoW(SPI_SETANIMATION, ai.cbSize, ctypes.byref(ai), SPIF_SENDCHANGE))


def set_immersive_dark_mode(hwnd: int, enabled: bool = True) -> bool:
    val = wintypes.BOOL(1 if enabled else 0)
    hr = dwmapi.DwmSetWindowAttribute(
        hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(val), ctypes.sizeof(val)
    )
    return hr == 0


def set_window_corner_square(hwnd: int) -> bool:
    val = wintypes.DWORD(1)
    hr = dwmapi.DwmSetWindowAttribute(
        hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(val), ctypes.sizeof(val)
    )
    return hr == 0


def set_click_through_overlay(hwnd: int, click_through: bool = True) -> None:
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if click_through:
        ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    else:
        ex &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
    user32.SetWindowPos(
        hwnd, 0, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )


def get_window_layered_state(hwnd: int) -> tuple[bool, Optional[int]]:
    """Inspect a window's WS_EX_LAYERED state and safe LWA_ALPHA opacity.

    Returns ``(had_layered, alpha_or_none)``:
      - ``(False, None)`` if ``hwnd`` is invalid.
      - ``(False, 255)`` if ``WS_EX_LAYERED`` is not set.
      - ``(True, None)`` if ``WS_EX_LAYERED`` uses per-pixel ``UpdateLayeredWindow``
        (``GetLayeredWindowAttributes`` returns ``False``), ``LWA_COLORKEY``, or lacks
        ``LWA_ALPHA`` — signaling PrismFX MUST NOT mutate this window's alpha or style.
      - ``(True, alpha)`` if ``WS_EX_LAYERED`` uses safe ``LWA_ALPHA``-only mode.
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return (False, None)
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    had_layered = bool(ex & WS_EX_LAYERED)
    if not had_layered:
        return (False, 255)
    cr = wintypes.COLORREF(0)
    alpha = ctypes.c_ubyte(255)
    flags = wintypes.DWORD(0)
    ok = bool(
        user32.GetLayeredWindowAttributes(
            hwnd, ctypes.byref(cr), ctypes.byref(alpha), ctypes.byref(flags)
        )
    )
    if (not ok) or (not (flags.value & LWA_ALPHA)) or (flags.value & LWA_COLORKEY):
        return (True, None)
    return (True, int(alpha.value))


def _is_safe_lwa_alpha_only(hwnd: int) -> bool:
    """Return True iff an existing WS_EX_LAYERED window uses safe LWA_ALPHA-only mode."""
    cr = wintypes.COLORREF(0)
    alpha = ctypes.c_ubyte(255)
    flags = wintypes.DWORD(0)
    ok = bool(
        user32.GetLayeredWindowAttributes(
            hwnd, ctypes.byref(cr), ctypes.byref(alpha), ctypes.byref(flags)
        )
    )
    return ok and bool(flags.value & LWA_ALPHA) and not bool(flags.value & LWA_COLORKEY)


def set_window_opacity(hwnd: int, alpha_0_to_255: int, had_layered: Optional[bool] = None) -> bool:
    """User-facing opacity setter (safe floor = 20 so users never accidentally vanish a window).

    Refuses to modify per-pixel ``UpdateLayeredWindow`` or ``LWA_COLORKEY`` windows, and only
    strips ``WS_EX_LAYERED`` on restore to 255 when ``had_layered is False``.
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if (ex & WS_EX_LAYERED) and not _is_safe_lwa_alpha_only(hwnd):
        return False
    alpha = max(20, min(255, int(alpha_0_to_255)))
    if alpha >= 255:
        if ex & WS_EX_LAYERED:
            ok = bool(user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA))
            if ok and had_layered is False:
                user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
            return ok
        return True
    if not (ex & WS_EX_LAYERED):
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
    return bool(user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA))


def set_window_hidden_alpha(
    hwnd: int,
    hidden: bool,
    restore_alpha: int = 255,
    had_layered: Optional[bool] = None,
) -> bool:
    """Temporarily hide a cross-process window at alpha=0 during Open/Restore GPU transitions.

    Works on all standard-privilege foreign processes where DWMWA_CLOAK returns E_ACCESSDENIED,
    and still allows PrintWindow(PW_RENDERFULLCONTENT) to capture crisp opaque contents!
    Refuses to mutate per-pixel ``UpdateLayeredWindow`` or ``LWA_COLORKEY`` windows, and only
    strips ``WS_EX_LAYERED`` on restore to 255 when ``had_layered is False``.
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if hidden:
        if (ex & WS_EX_LAYERED) and not _is_safe_lwa_alpha_only(hwnd):
            return False
        if not (ex & WS_EX_LAYERED):
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        return bool(user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA))
    alpha = max(20, min(255, int(restore_alpha)))
    if alpha >= 255:
        if ex & WS_EX_LAYERED:
            if not _is_safe_lwa_alpha_only(hwnd):
                return False
            ok = bool(user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA))
            if ok and had_layered is False:
                user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
            return ok
        return True
    if (ex & WS_EX_LAYERED) and not _is_safe_lwa_alpha_only(hwnd):
        return False
    if not (ex & WS_EX_LAYERED):
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
    return bool(user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA))


def get_window_opacity(hwnd: int) -> int:
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if not (ex & WS_EX_LAYERED):
        return 255
    cr = wintypes.COLORREF(0)
    alpha = ctypes.c_ubyte(255)
    flags = wintypes.DWORD(0)
    if user32.GetLayeredWindowAttributes(hwnd, ctypes.byref(cr), ctypes.byref(alpha), ctypes.byref(flags)):
        if flags.value & LWA_ALPHA:
            return int(alpha.value)
    return 255


def set_window_topmost(hwnd: int, topmost: bool) -> bool:
    insert_after = HWND_TOPMOST if topmost else HWND_NOTOPMOST
    return bool(
        user32.SetWindowPos(
            hwnd, insert_after, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    )


def is_window_topmost(hwnd: int) -> bool:
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    return bool(ex & WS_EX_TOPMOST)


def is_overlay_exstyle(hwnd: int) -> bool:
    """Return True for non-interactive/overlay windows (e.g. NVIDIA Overlay, toasts, tooltips)."""
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if ex & (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE):
        return True
    if (ex & WS_EX_TOPMOST) and (ex & WS_EX_TOOLWINDOW):
        return True
    return False


def move_resize_window(hwnd: int, x: int, y: int, w: int, h: int, activate: bool = False) -> bool:
    flags = SWP_NOZORDER | (0 if activate else SWP_NOACTIVATE)
    return bool(user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), flags))


def move_window_visible_pos(hwnd: int, vis_x: int, vis_y: int) -> bool:
    """Move a window so its DWM visible bounds land at (vis_x, vis_y) WITHOUT resizing!

    Compensates for Windows 11's invisible ~7px DWM drop-shadow margins and uses SWP_NOSIZE
    so Momentum Drag glides smoothly without ever shrinking or jumping the window!
    """
    r_win = RECT()
    r_ext = RECT()
    off_x, off_y = 0, 0
    if (
        user32.GetWindowRect(hwnd, ctypes.byref(r_win))
        and dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r_ext), ctypes.sizeof(r_ext)
        ) == 0
        and r_ext.width > 0
        and r_ext.height > 0
    ):
        off_x = r_ext.left - r_win.left
        off_y = r_ext.top - r_win.top
    flags = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
    return bool(user32.SetWindowPos(hwnd, 0, int(vis_x) - off_x, int(vis_y) - off_y, 0, 0, flags))


def place_behind_window(overlay_hwnd: int, target_hwnd: int, x: int, y: int, w: int, h: int) -> bool:
    return bool(
        user32.SetWindowPos(
            overlay_hwnd,
            target_hwnd,
            int(x), int(y), int(w), int(h),
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    )


def enum_monitors() -> list[MonitorRect]:
    monitors: list[MonitorRect] = []

    def _cb(hmon, hdc, lprc, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            m = info.rcMonitor
            w = info.rcWork
            monitors.append(
                MonitorRect(
                    name=info.szDevice,
                    left=m.left,
                    top=m.top,
                    width=m.width,
                    height=m.height,
                    work_left=w.left,
                    work_top=w.top,
                    work_width=w.width,
                    work_height=w.height,
                    is_primary=bool(info.dwFlags & 1),
                )
            )
        return True

    user32.EnumDisplayMonitors(0, None, MONITORENUMPROC(_cb), 0)
    return monitors


def get_cursor_pos() -> tuple[int, int]:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def is_lbutton_down() -> bool:
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def window_from_point(x: int, y: int) -> int:
    return int(user32.WindowFromPoint(POINT(int(x), int(y))) or 0)


def hit_test_window(hwnd: int, screen_x: int, screen_y: int, timeout_ms: int = 20) -> int:
    lparam = ((screen_y & 0xFFFF) << 16) | (screen_x & 0xFFFF)
    res = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(hwnd, WM_NCHITTEST, 0, lparam, 0x0002, timeout_ms, ctypes.byref(res))
    return int(res.value) if ok else 0


def hit_test_caption(hwnd: int, screen_x: int, screen_y: int) -> bool:
    return hit_test_window(hwnd, screen_x, screen_y, timeout_ms=40) == HTCAPTION


def is_on_battery_power() -> bool:
    status = SYSTEM_POWER_STATUS()
    if kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return status.ACLineStatus == 0
    return False


def is_fullscreen_game_active() -> bool:
    state = ctypes.c_int(0)
    hr = shell32.SHQueryUserNotificationState(ctypes.byref(state))
    if hr == 0:
        # Exclude QUNS_BUSY (2): Win11 returns QUNS_BUSY for Focus Assist / NVIDIA ShadowPlay / normal full windows!
        return state.value in (QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)
    return False


def find_desktop_listview_hwnd() -> int:
    progman = user32.FindWindowW("Progman", None)
    if progman:
        defview = user32.FindWindowExW(progman, 0, "SHELLDLL_DefView", None)
        if defview:
            return int(defview)
    found = [0]

    def _cb(hwnd, _lp):
        if get_class_name(hwnd) == "WorkerW":
            dv = user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
            if dv:
                found[0] = int(dv)
                return False
        return True

    user32.EnumWindows(ENUMWINDOWSPROC(_cb), 0)
    return found[0]


def set_desktop_icons_visible(visible: bool) -> bool:
    defview = find_desktop_listview_hwnd()
    if not defview:
        return False
    listview = user32.FindWindowExW(defview, 0, "SysListView32", None)
    target = int(listview or defview)
    user32.ShowWindow(target, SW_SHOW if visible else SW_HIDE)
    return True


def are_desktop_icons_visible() -> bool:
    defview = find_desktop_listview_hwnd()
    if not defview:
        return True
    listview = user32.FindWindowExW(defview, 0, "SysListView32", None)
    target = int(listview or defview)
    return bool(user32.IsWindowVisible(target))


def _is_window_hidden_for_screen_blt(hwnd: int) -> bool:
    """Return True if hwnd is invisible, cloaked, or alpha==0 so BitBlt(hdc_scr) would capture background."""
    if not user32.IsWindowVisible(hwnd):
        return True
    if is_window_cloaked(hwnd):
        return True
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if ex & WS_EX_LAYERED:
        cr = wintypes.COLORREF(0)
        alpha = ctypes.c_ubyte(255)
        flags = wintypes.DWORD(0)
        if user32.GetLayeredWindowAttributes(
            hwnd, ctypes.byref(cr), ctypes.byref(alpha), ctypes.byref(flags)
        ):
            if (flags.value & LWA_ALPHA) and alpha.value == 0:
                return True
    return False


def capture_window_bgra(hwnd: int, allow_screen_fallback: bool = True) -> Optional[tuple[int, int, bytes]]:
    """Capture a window's exact visible DWM frame contents as 1:1 cropped top-down BGRA bytes.

    Crops out Windows 11's invisible 7px left/right/bottom DWM drop-shadow borders so the
    rendered overlay matches DWMWA_EXTENDED_FRAME_BOUNDS pixel-for-pixel (zero snap/flash!).
    """
    if not user32.IsWindow(hwnd):
        return None
    r_win = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r_win)):
        return None
    w, h = r_win.width, r_win.height
    if w < 16 or h < 16 or w > 7680 or h > 4320:
        return None

    # Compute exact DWM extended frame crop relative to GetWindowRect
    r_ext = RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r_ext), ctypes.sizeof(r_ext)
    )
    if hr == 0 and r_ext.width >= 16 and r_ext.height >= 16:
        crop_x = max(0, min(w - 16, r_ext.left - r_win.left))
        crop_y = max(0, min(h - 16, r_ext.top - r_win.top))
        crop_w = max(16, min(w - crop_x, r_ext.width))
        crop_h = max(16, min(h - crop_y, r_ext.height))
    else:
        crop_x, crop_y, crop_w, crop_h = 0, 0, w, h

    hdc_scr = user32.GetDC(0)
    if not hdc_scr:
        return None
    hdc_full = gdi32.CreateCompatibleDC(hdc_scr)
    if not hdc_full:
        user32.ReleaseDC(0, hdc_scr)
        return None

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    bits_ptr = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(
        hdc_full, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
    )
    if not hbmp or not bits_ptr.value:
        if hbmp:
            gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_full)
        user32.ReleaseDC(0, hdc_scr)
        return None

    old_obj = gdi32.SelectObject(hdc_full, hbmp)
    try:
        ok = user32.PrintWindow(hwnd, hdc_full, PW_RENDERFULLCONTENT)
        if not ok:
            ok = user32.PrintWindow(hwnd, hdc_full, 0)
        if not ok:
            if (not allow_screen_fallback) or _is_window_hidden_for_screen_blt(hwnd):
                return None
            gdi32.BitBlt(hdc_full, 0, 0, w, h, hdc_scr, r_win.left, r_win.top, SRCCOPY)

        # Fast C-level crop when DWM shadow margins exist
        if crop_x == 0 and crop_y == 0 and crop_w == w and crop_h == h:
            raw = ctypes.string_at(bits_ptr.value, w * h * 4)
            return (w, h, raw)

        hdc_crop = gdi32.CreateCompatibleDC(hdc_scr)
        if not hdc_crop:
            return (w, h, ctypes.string_at(bits_ptr.value, w * h * 4))
        bmi_c = BITMAPINFO()
        bmi_c.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi_c.bmiHeader.biWidth = crop_w
        bmi_c.bmiHeader.biHeight = -crop_h
        bmi_c.bmiHeader.biPlanes = 1
        bmi_c.bmiHeader.biBitCount = 32
        bmi_c.bmiHeader.biCompression = BI_RGB
        crop_ptr = ctypes.c_void_p()
        hbmp_c = gdi32.CreateDIBSection(
            hdc_crop, ctypes.byref(bmi_c), DIB_RGB_COLORS, ctypes.byref(crop_ptr), None, 0
        )
        if not hbmp_c or not crop_ptr.value:
            if hbmp_c:
                gdi32.DeleteObject(hbmp_c)
            gdi32.DeleteDC(hdc_crop)
            raw = ctypes.string_at(bits_ptr.value, w * h * 4)
            return (w, h, raw)
        old_c = gdi32.SelectObject(hdc_crop, hbmp_c)
        try:
            gdi32.BitBlt(hdc_crop, 0, 0, crop_w, crop_h, hdc_full, crop_x, crop_y, SRCCOPY)
            raw = ctypes.string_at(crop_ptr.value, crop_w * crop_h * 4)
            return (crop_w, crop_h, raw)
        finally:
            gdi32.SelectObject(hdc_crop, old_c)
            gdi32.DeleteObject(hbmp_c)
            gdi32.DeleteDC(hdc_crop)
    finally:
        gdi32.SelectObject(hdc_full, old_obj)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_full)
        user32.ReleaseDC(0, hdc_scr)


def list_iconic_top_windows(own_pid: int = 0) -> list[int]:
    """Return top-level minimized (iconic) app windows for zero-flash Restore pre-hiding."""
    found: list[int] = []

    def _cb(hwnd, _lp):
        if not user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            return True
        if is_window_cloaked(hwnd):
            return True
        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if (ex & WS_EX_TOOLWINDOW) and not (ex & WS_EX_APPWINDOW):
            return True
        if ex & (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE):
            return True
        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        if (style & WS_CHILD) or not (style & WS_CAPTION):
            return True
        if own_pid:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own_pid:
                return True
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        found.append(int(hwnd))
        return True

    user32.EnumWindows(ENUMWINDOWSPROC(_cb), 0)
    return found


_OVERLAY_CLASS_NAME = "PrismFX_LayeredOverlay"
_OVERLAY_CLASS_REGISTERED = False
_OVERLAY_WNDPROC_REF: Optional[WNDPROC] = None


def _ensure_overlay_window_class() -> str:
    """Register a null-brush Win32 window class once so showing never paints a black/white flash."""
    global _OVERLAY_CLASS_REGISTERED, _OVERLAY_WNDPROC_REF
    if _OVERLAY_CLASS_REGISTERED:
        return _OVERLAY_CLASS_NAME

    @WNDPROC
    def _wndproc(hwnd, msg, wp, lp):
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    _OVERLAY_WNDPROC_REF = _wndproc
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = 0
    wc.lpfnWndProc = _OVERLAY_WNDPROC_REF
    wc.hInstance = hinst
    wc.hbrBackground = 0
    wc.lpszClassName = _OVERLAY_CLASS_NAME
    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if atom or ctypes.get_last_error() == 1410:  # ERROR_CLASS_ALREADY_EXISTS
        _OVERLAY_CLASS_REGISTERED = True
        return _OVERLAY_CLASS_NAME
    return "Static"


class Win32LayeredSurface:
    """Zero-swapchain, pure Win32 UpdateLayeredWindow(ULW_ALPHA) click-through overlay surface.

    Supports both:
      - Zero-copy bottom-up DIB (biHeight = +h) for direct OpenGL glReadPixels(GL_BGRA) (~2.3 ms @ 1540x860)
      - Instant top-down opaque Frame-0 blit (present_opaque_bgra, ~1.2 ms @ 1540x860, zero GL wait!)
    Creates NO OpenGL/DXGI swapchain HWND -> 100% immune to NVIDIA Overlay (Alt+Z) / MPO flicker!
    """

    def __init__(self) -> None:
        cls_name = _ensure_overlay_window_class()
        hinst = kernel32.GetModuleHandleW(None)
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        self.hwnd: int = int(
            user32.CreateWindowExW(ex, cls_name, "", WS_POPUP, 0, 0, 1, 1, 0, 0, hinst, None) or 0
        )
        self._hdc_scr: int = 0
        self._hdc_mem: int = 0
        self._hbmp: int = 0
        self._old_obj: int = 0
        self._bits_ptr = ctypes.c_void_p()
        self._dib_w: int = 0
        self._dib_h: int = 0
        self._dib_top_down: bool = True
        self._visible: bool = False
        self._parked: bool = False
        self._hdc_park: int = 0
        self._hbmp_park: int = 0
        self._old_park: int = 0
        self._blend_alpha = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        self._blend_opaque = BLENDFUNCTION(AC_SRC_OVER, 0, 255, 0)
        self._pt_src = POINT(0, 0)

    @property
    def dib_byte_size(self) -> int:
        return (self._dib_w * self._dib_h * 4) if (self._hbmp and self._bits_ptr.value) else 0

    def _ensure_park_dib(self) -> bool:
        if self._hbmp_park and self._hdc_park:
            return True
        hdc_scr = self._hdc_scr or int(user32.GetDC(0) or 0)
        if not hdc_scr:
            return False
        own_scr = hdc_scr != self._hdc_scr
        hdc_p = int(gdi32.CreateCompatibleDC(hdc_scr) or 0)
        if own_scr:
            user32.ReleaseDC(0, hdc_scr)
        if not hdc_p:
            return False
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = 1
        bmi.bmiHeader.biHeight = -1
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        ptr = ctypes.c_void_p()
        hbmp = int(gdi32.CreateDIBSection(hdc_p, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(ptr), None, 0) or 0)
        if not hbmp or not ptr.value:
            gdi32.DeleteDC(hdc_p)
            return False
        old_p = int(gdi32.SelectObject(hdc_p, hbmp) or 0)
        ctypes.memset(ptr.value, 0, 4)
        self._hdc_park = hdc_p
        self._hbmp_park = hbmp
        self._old_park = old_p
        return True

    def _ensure_dib(self, w: int, h: int, top_down: bool = True) -> bool:
        if self._hbmp and self._dib_w == w and self._dib_h == h and self._dib_top_down == top_down:
            return True
        self._free_dib()
        self._hdc_scr = int(user32.GetDC(0) or 0)
        if not self._hdc_scr:
            return False
        self._hdc_mem = int(gdi32.CreateCompatibleDC(self._hdc_scr) or 0)
        if not self._hdc_mem:
            user32.ReleaseDC(0, self._hdc_scr)
            self._hdc_scr = 0
            return False

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h if top_down else h  # -h = top-down (GDI/QImage), +h = bottom-up (glReadPixels!)
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        self._bits_ptr = ctypes.c_void_p()
        self._hbmp = int(
            gdi32.CreateDIBSection(
                self._hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(self._bits_ptr), None, 0
            )
            or 0
        )
        if not self._hbmp or not self._bits_ptr.value:
            self._free_dib()
            return False
        self._old_obj = int(gdi32.SelectObject(self._hdc_mem, self._hbmp) or 0)
        self._dib_w = w
        self._dib_h = h
        self._dib_top_down = top_down
        return True

    def _commit_ulw(self, x: int, y: int, w: int, h: int, blend: BLENDFUNCTION) -> bool:
        pt_dst = POINT(int(x), int(y))
        sz = SIZE(int(w), int(h))
        ok = bool(
            user32.UpdateLayeredWindow(
                self.hwnd, self._hdc_scr, ctypes.byref(pt_dst), ctypes.byref(sz),
                self._hdc_mem, ctypes.byref(self._pt_src), 0, ctypes.byref(blend), ULW_ALPHA,
            )
        )
        if ok:
            if not self._visible:
                # Reveal atomically AFTER pixels are committed — zero blank/black first frame!
                user32.SetWindowPos(
                    self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                )
                self._visible = True
            elif self._parked:
                # Surface is already live on DWM -> just ensure topmost Z-order without SWP_SHOWWINDOW!
                user32.SetWindowPos(
                    self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                )
            self._parked = False
        return ok

    def park(self) -> bool:
        """Park surface offscreen as a 1x1 0-alpha pixel while keeping DWM surface & main DIB warm!

        Eliminates the 1-2 VSync DWM surface-recreation stall caused by SW_HIDE -> SWP_SHOWWINDOW,
        cutting Frame-0 presentation latency to ~2.5 ms at 1540x860.
        """
        if not self.hwnd:
            return False
        if not self._ensure_park_dib():
            self.hide()
            return False
        hdc_scr = self._hdc_scr or int(user32.GetDC(0) or 0)
        if not hdc_scr:
            self.hide()
            return False
        own_scr = hdc_scr != self._hdc_scr
        pt_park = POINT(-30000, -30000)
        sz_park = SIZE(1, 1)
        ok = bool(
            user32.UpdateLayeredWindow(
                self.hwnd, hdc_scr, ctypes.byref(pt_park), ctypes.byref(sz_park),
                self._hdc_park, ctypes.byref(self._pt_src), 0, ctypes.byref(self._blend_alpha), ULW_ALPHA,
            )
        )
        if own_scr:
            user32.ReleaseDC(0, hdc_scr)
        if ok:
            if not self._visible:
                user32.SetWindowPos(
                    self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                )
                self._visible = True
            self._parked = True
        return ok

    def acquire_bottom_up_dib(self, w: int, h: int) -> int:
        """Allocate/reuse a bottom-up 32-bit BGRA DIB and return its raw pixel address for glReadPixels."""
        if not self.hwnd or w <= 0 or h <= 0:
            return 0
        if not self._ensure_dib(w, h, top_down=False):
            return 0
        return int(self._bits_ptr.value or 0)

    def commit_premul_dib(self, x: int, y: int, w: int, h: int) -> bool:
        """Present pixels already written directly into this surface's DIB (zero CPU copies!)."""
        if not self.hwnd or not self._hbmp or self._dib_w != w or self._dib_h != h:
            return False
        return self._commit_ulw(x, y, w, h, self._blend_alpha)

    def present_opaque_bgra(self, x: int, y: int, w: int, h: int, bgra_bytes: bytes | memoryview) -> bool:
        """Ultra-fast (~1.2 ms) opaque Frame-0 blit straight from top-down GDI BGRA capture (zero GL wait!).

        Uses AlphaFormat=0 (constant alpha 255), so bogus 0x00 alpha bytes from GDI PrintWindow
        can NEVER appear transparent or black!
        """
        if not self.hwnd or w <= 0 or h <= 0:
            return False
        if not self._ensure_dib(w, h, top_down=True):
            return False
        expected = w * h * 4
        if len(bgra_bytes) < expected:
            return False
        src = bgra_bytes if isinstance(bgra_bytes, bytes) else bytes(bgra_bytes)
        ctypes.memmove(self._bits_ptr.value, src, expected)
        return self._commit_ulw(x, y, w, h, self._blend_opaque)

    def present_premul_bgra(self, x: int, y: int, w: int, h: int, bgra_bytes: bytes | memoryview) -> bool:
        """Atomically upload top-down premultiplied BGRA pixels & position in physical screen coordinates."""
        if not self.hwnd or w <= 0 or h <= 0:
            return False
        if not self._ensure_dib(w, h, top_down=True):
            return False
        expected = w * h * 4
        if len(bgra_bytes) < expected:
            return False
        src = bgra_bytes if isinstance(bgra_bytes, bytes) else bytes(bgra_bytes)
        ctypes.memmove(self._bits_ptr.value, src, expected)
        return self._commit_ulw(x, y, w, h, self._blend_alpha)

    def hide(self) -> None:
        if self.hwnd and self._visible:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self._visible = False
        self._parked = False

    def _free_park_dib(self) -> None:
        if self._hdc_park and self._old_park:
            gdi32.SelectObject(self._hdc_park, self._old_park)
            self._old_park = 0
        if self._hbmp_park:
            gdi32.DeleteObject(self._hbmp_park)
            self._hbmp_park = 0
        if self._hdc_park:
            gdi32.DeleteDC(self._hdc_park)
            self._hdc_park = 0

    def _free_dib(self) -> None:
        if self._hdc_mem and self._old_obj:
            gdi32.SelectObject(self._hdc_mem, self._old_obj)
            self._old_obj = 0
        if self._hbmp:
            gdi32.DeleteObject(self._hbmp)
            self._hbmp = 0
        if self._hdc_mem:
            gdi32.DeleteDC(self._hdc_mem)
            self._hdc_mem = 0
        if self._hdc_scr:
            user32.ReleaseDC(0, self._hdc_scr)
            self._hdc_scr = 0
        self._bits_ptr = ctypes.c_void_p()
        self._dib_w = 0
        self._dib_h = 0

    def destroy(self) -> None:
        self.hide()
        self._free_dib()
        self._free_park_dib()
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = 0
