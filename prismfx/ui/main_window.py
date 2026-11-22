"""PrismFX Frameless Architectural Studio Window — 5 Complete Control Pages + System Tray."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import pathlib
import queue
import threading
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .. import __app_name__, __version__
from ..core import startup as win_startup
from ..core import win32_api as w32
from ..core.catalog import is_protected_window_class
from ..core.config import BUILTIN_PRESETS, PrismConfig, SafetyConfig
from ..core.effects import CATEGORIES, EASING_CURVES, TRIGGER_TYPES, EffectSpec
from ..core.engine import PrismEngine, SpotlightState, TileRequest
from ..core.logging_setup import get_logger
from ..core.physics import SNAP_PRESETS
from ..render.gl_renderer import OffscreenGpuEngine
from ..render.overlay import (
    DesktopFxOverlay,
    FloatingTileWindow,
    QuickControlPopup,
    SpotlightOverlay,
    clamp_to_virtual_screens,
    physical_to_qt_logical,
)
from ..render.preview_widget import ShaderPreviewWidget
from .dialogs import ExcludedExesDialog, RandomPoolDialog
from .theme import ATLAS, TOKENS, build_stylesheet
from .widgets import (
    ArchToggle,
    EffectCard,
    GlobalToastBanner,
    MetricTile,
    PanelCard,
    PresetItemWidget,
    ScanlineContainer,
    SectionHeader,
    SliderRow,
)

_log = get_logger("prismfx.ui.main_window")

ACCENT_SWATCHES = ["#2D81FF", "#74AAFF", "#38BDF8", "#34D399", "#F59E0B", "#F97316", "#F43F5E", "#A855F7"]
SPOTLIGHT_SWATCHES = ["#06080E", "#0A0D18", "#120B18", "#071412", "#140D08"]


class MainWindow(QtWidgets.QMainWindow):
    """Frameless architectural desktop studio controlling PrismEngine & GPU overlays."""

    startupStatusReady = QtCore.Signal(object)
    spotlightStateChanged = QtCore.Signal(object)
    pauseStateChanged = QtCore.Signal(str)

    def __init__(
        self,
        engine: PrismEngine,
        enable_overlays: bool = True,
        enable_tray: bool = True,
        icon_path: Optional[pathlib.Path] = None,
        ipc_msg: int = 0,
        repo_root: Optional[pathlib.Path] = None,
        profile_arg: str = "",
        ipc_quit_msg: int = 0,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.cfg: PrismConfig = engine.config
        self._lang = self.cfg.language
        self._ipc_msg = int(ipc_msg or 0)
        self._ipc_quit_msg = int(ipc_quit_msg or 0)
        self._repo_root = (repo_root or pathlib.Path(__file__).resolve().parents[2]).resolve()
        self._profile_arg = profile_arg
        self._shutdown_started = False
        self._startup_user_modified = False
        self._is_admin = False
        # Fast non-blocking initial startup snapshot (avoids 4s sync schtasks /Query in __init__)
        self._startup_status = self._make_fast_initial_startup_status()
        self._is_admin = self._startup_status.is_admin
        if not self.engine.backend.is_demo:
            self.cfg.start_with_windows = self._startup_status.enabled
            self.cfg.start_with_windows_admin = (self._startup_status.mode == "elevated_task")
        self.startupStatusReady.connect(self._apply_startup_status, QtCore.Qt.QueuedConnection)
        self._native_bound = False
        self._wts_registered = False
        self._hotkeys_registered: dict[int, bool] = {}
        self._fallback_shortcuts: list[QtGui.QShortcut] = []
        self._tray_minimize_notified = False
        self._drag_pos: Optional[QtCore.QPoint] = None
        self._active_page = "home"
        self._active_trigger = "open"
        self._active_category = "all"
        self._search_text = ""
        self._home_preview_effect_id: str = self.cfg.triggers["close"].effect_id
        self._custom_duration_triggers: set[str] = set()
        self._custom_accent_triggers: set[str] = set()
        self._section_headers: list[SectionHeader] = []
        self._home_quick_btns: list[tuple[QtWidgets.QPushButton, str, str, str]] = []
        self._accent_swatch_btns: dict[str, QtWidgets.QPushButton] = {}
        self._spot_swatch_btns: dict[str, QtWidgets.QPushButton] = {}
        self._snap_edge_labels: dict[str, tuple[QtWidgets.QLabel, str, str]] = {}
        self._effect_cards: dict[str, EffectCard] = {}
        self._nav_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._trigger_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._cat_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._tile_windows: dict[int, FloatingTileWindow] = {}
        self._last_snapshot_sig: Optional[tuple[bool, str, bool, int, int, str]] = None
        self._current_fps = 0.0
        self._current_frame_ms = 0.0
        self._act_tray_open: Optional[QtGui.QAction] = None
        self._act_tray_toggle_engine: Optional[QtGui.QAction] = None
        self._act_tray_quick: Optional[QtGui.QAction] = None
        self._act_tray_icons: Optional[QtGui.QAction] = None
        self._act_tray_autostart: Optional[QtGui.QAction] = None
        self._act_tray_admin: Optional[QtGui.QAction] = None
        self._act_tray_restore: Optional[QtGui.QAction] = None
        self._act_tray_quit: Optional[QtGui.QAction] = None

        self.setWindowTitle(f"{__app_name__} Studio v{__version__}")
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.Window)
        self.setMinimumSize(1120, 720)
        self._icon_path: Optional[pathlib.Path] = pathlib.Path(icon_path) if icon_path else None
        self._app_icon = self._load_icon(self._icon_path)
        self.setWindowIcon(self._app_icon)

        # Restore persisted bounds safely (preserving negative multi-monitor coordinates)
        gx, gy, gw, gh = self.cfg.window_geometry
        rx, ry, rw, rh = clamp_to_virtual_screens(gx, gy, max(1120, gw), max(720, gh))
        self.setGeometry(rx, ry, rw, rh)

        # Desktop GPU overlays (optional so headless/cloaked probes stay ultra-clean)
        self.fx_overlay: Optional[DesktopFxOverlay] = DesktopFxOverlay(engine) if enable_overlays else None
        self.spotlight_overlay: Optional[SpotlightOverlay] = SpotlightOverlay(engine) if enable_overlays else None
        self.quick_bar = QuickControlPopup(engine)

        if self.spotlight_overlay is not None:
            self.spotlightStateChanged.connect(
                self.spotlight_overlay.apply_state, QtCore.Qt.QueuedConnection
            )
            engine.on_spotlight_changed = self._dispatch_spotlight_from_thread

        self.pauseStateChanged.connect(
            self._on_engine_pause_changed, QtCore.Qt.QueuedConnection
        )
        engine.on_pause_state_changed = self._dispatch_pause_from_thread
        self._on_engine_pause_changed(engine._cached_paused_reason)

        self._build_shell()
        self._apply_target_fps()
        self._wire_hotkeys()
        self._tray: Optional[QtWidgets.QSystemTrayIcon] = None
        if enable_tray and QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self._setup_tray()

        # Lock-free UI telemetry refresh (reads atomic EngineFrame snapshot)
        self._ui_timer = QtCore.QTimer(self)
        self._ui_timer.setInterval(250)
        self._ui_timer.timeout.connect(self._poll_engine_snapshot)
        self._ui_timer.start()

        # Sync initial selections & dependent control states
        self._select_page("home")
        self._refresh_animations_grid()
        self._sync_trigger_controls()
        self._sync_dependent_controls()
        self._sync_focus_swatch_highlight()
        self._sync_battery_banner(self.engine.paused_reason())
        self._sync_startup_ui()
        self._refresh_apps_table()
        self._retranslate_ui()
        self._start_async_startup_query()

        # Notify user if config.json was corrupt and backed up to config.json.corrupt.bak
        if getattr(self.engine.store, "last_load_recovered_corrupt", False):
            self.show_toast(
                self._tr(
                    "พบไฟล์ config.json เสียหาย จึงสำรองไว้ที่ config.json.corrupt.bak และโหลดค่าเริ่มต้นที่ปลอดภัยแล้ว",
                    "Corrupt config.json was backed up to config.json.corrupt.bak and reset to safe defaults.",
                ),
                kind="warn",
                duration_ms=8000,
            )

    # ------------------------------------------------------------------
    # Localization helper
    # ------------------------------------------------------------------
    def _qt_esc(self, text: str) -> str:
        """Escape literal '&' as '&&' for Qt QPushButton/QCheckBox mnemonic handling."""
        return text.replace("&", "&&")

    def _tr(self, th: str, en: str) -> str:
        return th if self._lang == "th" else en

    def _tr_btn(self, th: str, en: str) -> str:
        return self._qt_esc(self._tr(th, en))

    def _load_icon(self, icon_path: Optional[pathlib.Path]) -> QtGui.QIcon:
        if icon_path and icon_path.exists():
            return QtGui.QIcon(str(icon_path))
        assets = pathlib.Path(__file__).resolve().parents[2] / "assets"
        for candidate in (assets / "prismfx.ico", assets / "prismfx_256.png"):
            if candidate.exists():
                return QtGui.QIcon(str(candidate))
        pm = QtGui.QPixmap(32, 32)
        pm.fill(QtGui.QColor("#121316"))
        p = QtGui.QPainter(pm)
        p.setPen(QtGui.QPen(QtGui.QColor("#2D81FF"), 2))
        p.drawRect(3, 3, 25, 25)
        p.fillRect(9, 9, 14, 14, QtGui.QColor("#2D81FF"))
        p.fillRect(13, 13, 6, 6, QtGui.QColor("#74AAFF"))
        p.end()
        return QtGui.QIcon(pm)

    def _is_process_elevated(self) -> bool:
        """Return True when the current process runs elevated (fail-closed for UIPI guard)."""
        fn = getattr(w32, "is_process_elevated", None) or getattr(w32, "is_user_admin", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return True
        return bool(self._is_admin)

    def _make_fast_initial_startup_status(self) -> win_startup.StartupStatus:
        """Build an instant, non-blocking StartupStatus without spawning schtasks.exe."""
        is_admin = self._is_process_elevated()
        try:
            exe, _args, _native = win_startup.resolve_launcher(self._repo_root)
            launcher_path = str(exe)
        except Exception:
            launcher_path = ""

        elev_allowed = False
        elev_reason = ""
        can_elev = getattr(win_startup, "can_enable_elevated_task", None)
        if callable(can_elev):
            try:
                elev_allowed, elev_reason = can_elev(self._repo_root)
            except Exception:
                elev_allowed, elev_reason = False, ""

        enabled = False
        mode = "none"
        cmd_line = ""
        approved_disabled = False
        if not self.engine.backend.is_demo:
            read_reg = getattr(win_startup, "_read_registry_run", None)
            if callable(read_reg):
                try:
                    reg_cmd, approved_disabled = read_reg()
                    if reg_cmd:
                        enabled = not approved_disabled
                        mode = "registry"
                        cmd_line = reg_cmd
                except Exception:
                    pass

        base_kwargs = dict(
            enabled=enabled,
            mode=mode,
            is_admin=is_admin,
            launcher_path=launcher_path,
            command_line=cmd_line,
            approved_disabled=approved_disabled,
        )
        try:
            return win_startup.StartupStatus(
                **base_kwargs,
                elevated_task_allowed=elev_allowed,
                elevated_task_block_reason=elev_reason,
            )
        except TypeError:
            return win_startup.StartupStatus(**base_kwargs)

    def _start_async_startup_query(self) -> None:
        """Query full Task Scheduler + Registry startup state on a daemon thread."""
        repo_root = self._repo_root
        dry_run = self.engine.backend.is_demo

        def _worker() -> None:
            try:
                st = win_startup.query_startup_status(repo_root, dry_run=dry_run)
            except Exception:
                return
            if self._shutdown_started or self._startup_user_modified:
                return
            try:
                self.startupStatusReady.emit(st)
            except RuntimeError:
                pass

        threading.Thread(target=_worker, name="PrismFX-StartupQuery", daemon=True).start()

    @QtCore.Slot(object)
    def _apply_startup_status(self, st: object) -> None:
        """Apply asynchronously queried StartupStatus safely on the Qt GUI thread."""
        if (
            self._shutdown_started
            or self._startup_user_modified
            or not isinstance(st, win_startup.StartupStatus)
        ):
            return
        self._startup_status = st
        self._is_admin = st.is_admin
        if not self.engine.backend.is_demo:
            self.cfg.start_with_windows = st.enabled
            self.cfg.start_with_windows_admin = (st.mode == "elevated_task")
        self._sync_startup_ui()

    def ensure_native_handle(self) -> int:
        """Realize native Win32 HWND even when starting hidden in tray (for IPC, UIPI, Global Hotkeys & taskbar pinning)."""
        hwnd = int(self.winId())
        if hwnd:
            self.engine.register_own_hwnd(hwnd)
            if not self._native_bound:
                self._native_bound = True
                if not self._wts_registered:
                    self._wts_registered = bool(w32.register_session_notification(hwnd))
                if not self.engine.backend.is_demo:
                    self._register_global_hotkeys(hwnd)
                if self._ipc_msg:
                    # UIPI hardening: whitelist ONLY the Activate message, NEVER self._ipc_quit_msg
                    w32.allow_window_message_uipi(hwnd, self._ipc_msg)
                exe, _args, is_native = win_startup.resolve_launcher(self._repo_root)
                ico = (
                    self._icon_path
                    if (self._icon_path is not None and self._icon_path.exists())
                    else (self._repo_root / "assets" / "prismfx.ico")
                )
                relaunch = f'"{exe}"' if is_native else ""
                icon_res = f"{ico},0" if ico.exists() else ""
                w32.set_window_app_user_model_properties(
                    hwnd,
                    app_id="PrismFX.Studio",
                    relaunch_cmd=relaunch,
                    display_name="PrismFX Studio",
                    icon_resource=icon_res,
                )
        return hwnd

    def _register_global_hotkeys(self, hwnd: int) -> dict[int, bool]:
        """Register system-wide Win32 hotkeys (Ctrl+Alt+W Quick Bar, Ctrl+Alt+Shift+R Panic Restore & Pause)."""
        if not hwnd:
            return dict(self._hotkeys_registered)
        specs = (
            (
                w32.HOTKEY_ID_QUICK_BAR,
                w32.MOD_CONTROL | w32.MOD_ALT | w32.MOD_NOREPEAT,
                w32.VK_W,
                "Ctrl+Alt+W",
            ),
            (
                w32.HOTKEY_ID_PANIC_RESTORE,
                w32.MOD_CONTROL | w32.MOD_ALT | w32.MOD_SHIFT | w32.MOD_NOREPEAT,
                w32.VK_R,
                "Ctrl+Alt+Shift+R",
            ),
        )
        for hk_id, mods, vk, label in specs:
            if self._hotkeys_registered.get(hk_id):
                continue
            ok = w32.register_global_hotkey(hwnd, hk_id, mods, vk)
            self._hotkeys_registered[hk_id] = ok
            if not ok:
                _log.warning("Global hotkey registration failed for %s (id=%d)", label, hk_id)
        self._sync_fallback_shortcuts()
        return dict(self._hotkeys_registered)

    def _unregister_global_hotkeys(self) -> None:
        if not self._hotkeys_registered:
            return
        try:
            hwnd = int(self.winId())
        except Exception:
            hwnd = 0
        for hk_id, was_ok in list(self._hotkeys_registered.items()):
            if was_ok and hwnd:
                try:
                    w32.unregister_global_hotkey(hwnd, hk_id)
                except Exception as exc:
                    _log.warning("UnregisterHotKey failed for id=%d: %s", hk_id, exc)
        self._hotkeys_registered.clear()

    def _sync_fallback_shortcuts(self) -> None:
        """Disable local QShortcut fallbacks when global Win32 hotkeys are active (avoid double-fire)."""
        for sc in self._fallback_shortcuts:
            hk_id = int(sc.property("hotkey_id") or 0)
            sc.setEnabled(not bool(self._hotkeys_registered.get(hk_id, False)))

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        hwnd = self.ensure_native_handle()
        if hwnd:
            w32.set_immersive_dark_mode(hwnd, True)
            w32.set_window_corner_square(hwnd)

    def nativeEvent(self, eventType, message):  # type: ignore[override]
        try:
            if eventType == b"windows_generic_MSG" and message is not None:
                addr = int(message)
                if addr:
                    msg = wintypes.MSG.from_address(addr)
                    if (
                        self._ipc_quit_msg
                        and self._ipc_quit_msg != self._ipc_msg
                        and msg.message == self._ipc_quit_msg
                    ):
                        if msg.wParam == w32.IPC_CMD_QUIT:
                            QtCore.QTimer.singleShot(0, self.full_shutdown)
                            return (True, 1)
                    elif self._ipc_msg and msg.message == self._ipc_msg:
                        if msg.wParam == w32.IPC_CMD_ACTIVATE:
                            QtCore.QTimer.singleShot(0, self._restore_from_tray)
                            return (True, 1)
                        if msg.wParam == w32.IPC_CMD_QUIT:
                            # Reject IPC_CMD_QUIT over the UIPI-whitelisted Activate channel when elevated
                            if not self._is_process_elevated():
                                QtCore.QTimer.singleShot(0, self.full_shutdown)
                                return (True, 1)
                            return (True, 0)
                    elif msg.message == w32.WM_HOTKEY:
                        hk_id = int(msg.wParam)
                        if hk_id == w32.HOTKEY_ID_QUICK_BAR:
                            QtCore.QTimer.singleShot(0, self._open_quick_controls)
                            return (True, 0)
                        if hk_id == w32.HOTKEY_ID_PANIC_RESTORE:
                            QtCore.QTimer.singleShot(0, self._on_global_panic_restore)
                            return (True, 0)
                    elif msg.message == w32.WM_NCHITTEST and not self.isMaximized():
                        x, y = w32.unpack_lparam_signed_xy(int(msg.lParam))
                        rc = w32.RECT()
                        if w32.user32.GetWindowRect(msg.hWnd, ctypes.byref(rc)):
                            if rc.left <= x < rc.right and rc.top <= y < rc.bottom:
                                border = max(6, int(round(6.0 * max(1.0, float(self.devicePixelRatioF())))))
                                left = (x - rc.left) < border
                                right = (rc.right - x) <= border
                                top = (y - rc.top) < border
                                bottom = (rc.bottom - y) <= border
                                if top and left:
                                    return (True, w32.HTTOPLEFT)
                                if top and right:
                                    return (True, w32.HTTOPRIGHT)
                                if bottom and left:
                                    return (True, w32.HTBOTTOMLEFT)
                                if bottom and right:
                                    return (True, w32.HTBOTTOMRIGHT)
                                if left:
                                    return (True, w32.HTLEFT)
                                if right:
                                    return (True, w32.HTRIGHT)
                                if top:
                                    return (True, w32.HTTOP)
                                if bottom:
                                    return (True, w32.HTBOTTOM)
                    elif msg.message in (w32.WM_QUERYENDSESSION, w32.WM_ENDSESSION):
                        # Synchronously flush journal & restore all hidden/pre-hidden windows before logoff/shutdown
                        self.engine.journal.flush_sync()
                        self.engine.stop()
                    elif msg.message == w32.WM_POWERBROADCAST:
                        if msg.wParam == w32.PBT_APMSUSPEND:
                            if self.fx_overlay is not None:
                                self.fx_overlay.cancel_all_active()
                            self.engine.on_system_suspend()
                        elif msg.wParam in (w32.PBT_APMRESUMEAUTOMATIC, w32.PBT_APMRESUMESUSPEND):
                            QtCore.QTimer.singleShot(0, self._on_system_wake_or_display_change)
                    elif msg.message == w32.WM_DISPLAYCHANGE:
                        QtCore.QTimer.singleShot(0, self._on_system_wake_or_display_change)
                    elif msg.message == w32.WM_WTSSESSION_CHANGE:
                        if msg.wParam == w32.WTS_SESSION_LOCK:
                            if self.fx_overlay is not None:
                                self.fx_overlay.cancel_all_active()
                            self.engine.on_system_suspend()
                        elif msg.wParam == w32.WTS_SESSION_UNLOCK:
                            QtCore.QTimer.singleShot(0, self._on_system_wake_or_display_change)
        except Exception as exc:
            _log.debug("nativeEvent exception: %s", exc)
        return super().nativeEvent(eventType, message)

    def _on_system_wake_or_display_change(self) -> None:
        if self._shutdown_started:
            return
        if self.fx_overlay is not None:
            self.fx_overlay.cancel_all_active()
        self.engine.on_system_resume_or_display_change()
        OffscreenGpuEngine.shared().validate_context()

    def _apply_target_fps(self) -> None:
        fps = max(30, min(360, int(self.cfg.target_fps)))
        if hasattr(self, "home_stage"):
            self.home_stage.set_target_fps(fps)
        if hasattr(self, "lab_stage"):
            self.lab_stage.set_target_fps(fps)
        if hasattr(self, "wobble_stage"):
            self.wobble_stage.set_target_fps(fps)
        if self.fx_overlay is not None:
            self.fx_overlay.set_target_fps(fps)

    def _push_config(self) -> None:
        self.engine.update_config(self.cfg)
        self._apply_target_fps()

    def _pause_all_stages(self) -> None:
        if hasattr(self, "home_stage"):
            self.home_stage.pause_clock()
        if hasattr(self, "lab_stage"):
            self.lab_stage.pause_clock()
        if hasattr(self, "wobble_stage"):
            self.wobble_stage.pause_clock()

    def _wake_active_stage(self) -> None:
        if not self.isVisible() or self.isMinimized():
            self._pause_all_stages()
            return
        if hasattr(self, "home_stage"):
            if self._active_page == "home":
                self.home_stage.notify_interaction()
            else:
                self.home_stage.pause_clock()
        if hasattr(self, "lab_stage"):
            if self._active_page == "animations":
                self.lab_stage.notify_interaction()
            else:
                self.lab_stage.pause_clock()
        if hasattr(self, "wobble_stage"):
            if self._active_page == "physics":
                self.wobble_stage.notify_interaction()
            else:
                self.wobble_stage.pause_clock()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._persist_geometry()
        if self._tray and self._tray.isVisible() and not self.engine.backend.is_demo:
            event.ignore()
            self.hide()
            self._pause_all_stages()
            self._notify_minimized_to_tray_once()
        else:
            self.full_shutdown()
            event.accept()

    def _notify_minimized_to_tray_once(self) -> None:
        if self._tray_minimize_notified or self._tray is None or not self._tray.isVisible():
            return
        self._tray_minimize_notified = True
        try:
            self._tray.showMessage(
                self._tr("PrismFX Studio ย่อลง System Tray แล้ว", "PrismFX Studio Minimized to Tray"),
                self._tr(
                    "โปรแกรมยังทำงานอยู่เบื้องหลัง กด Ctrl+Alt+W เพื่อเปิดแถบควบคุมด่วน หรือ Ctrl+Alt+Shift+R เพื่อกู้คืนและหยุดฉุกเฉิน",
                    "Still running in the background. Press Ctrl+Alt+W for Quick Bar, or Ctrl+Alt+Shift+R for Panic Restore & Pause.",
                ),
                QtWidgets.QSystemTrayIcon.Information,
                4500,
            )
        except Exception as exc:
            _log.debug("Tray minimize notification failed: %s", exc)

    def show_toast(self, message: str, kind: str = "info", duration_ms: int = 4500) -> None:
        """Display the app-wide Toast Banner visible across every Studio page."""
        if hasattr(self, "toast_banner") and self.toast_banner is not None:
            self.toast_banner.show_toast(message, kind=kind, duration_ms=duration_ms)

    def event(self, event: QtCore.QEvent) -> bool:
        ev_type = event.type()
        if ev_type == QtCore.QEvent.WindowDeactivate:
            self._pause_all_stages()
        elif ev_type == QtCore.QEvent.WindowActivate:
            self._wake_active_stage()
        return super().event(event)

    def changeEvent(self, event: QtCore.QEvent) -> None:
        ev_type = event.type()
        if ev_type == QtCore.QEvent.WindowStateChange:
            if self.isMinimized():
                # Phase 5 / Board Resolution #2: pause GPU preview clocks when minimized
                self._pause_all_stages()
            elif self.isVisible() and self.isActiveWindow():
                self._wake_active_stage()
        elif ev_type == QtCore.QEvent.ActivationChange:
            if not self.isActiveWindow():
                self._pause_all_stages()
            elif self.isVisible() and not self.isMinimized():
                self._wake_active_stage()
        super().changeEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self._pause_all_stages()
        super().hideEvent(event)

    # ------------------------------------------------------------------
    # Frameless Shell Construction
    # ------------------------------------------------------------------
    def _build_shell(self) -> None:
        root_widget = ScanlineContainer(TOKENS["bg_base"])
        self.setCentralWidget(root_widget)
        outer = QtWidgets.QVBoxLayout(root_widget)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        # 1) Custom Architectural Titlebar
        self._titlebar = self._build_titlebar()
        outer.addWidget(self._titlebar)

        sep_top = QtWidgets.QFrame()
        sep_top.setFixedHeight(1)
        sep_top.setStyleSheet(f"background: {TOKENS['line']};")
        outer.addWidget(sep_top)

        # 2) Body (Sidebar + Right Content Area with Global Toast Banner + Stacked Pages)
        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._sidebar = self._build_sidebar()
        body.addWidget(self._sidebar)

        sep_v = QtWidgets.QFrame()
        sep_v.setFixedWidth(1)
        sep_v.setStyleSheet(f"background: {TOKENS['line']};")
        body.addWidget(sep_v)

        content_col = QtWidgets.QVBoxLayout()
        content_col.setContentsMargins(0, 0, 0, 0)
        content_col.setSpacing(0)

        # App-wide Global Toast Banner (visible on all 5 pages!)
        toast_wrap = QtWidgets.QWidget()
        toast_wrap.setStyleSheet("background: transparent;")
        tw_lay = QtWidgets.QVBoxLayout(toast_wrap)
        tw_lay.setContentsMargins(14, 8, 14, 0)
        tw_lay.setSpacing(0)
        self.toast_banner = GlobalToastBanner()
        tw_lay.addWidget(self.toast_banner)
        self._toast_wrap = toast_wrap
        content_col.addWidget(toast_wrap)

        self._stack = QtWidgets.QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        self._page_map: dict[str, int] = {}

        for key, widget in [
            ("home", self._build_home_page()),
            ("animations", self._build_animations_page()),
            ("physics", self._build_physics_page()),
            ("focus", self._build_focus_page()),
            ("apps", self._build_apps_page()),
        ]:
            idx = self._stack.addWidget(widget)
            self._page_map[key] = idx

        content_col.addWidget(self._stack, 1)
        body.addLayout(content_col, 1)
        outer.addLayout(body, 1)

        # 3) Bottom Telemetry Status Bar (+ QSizeGrip)
        sep_bot = QtWidgets.QFrame()
        sep_bot.setFixedHeight(1)
        sep_bot.setStyleSheet(f"background: {TOKENS['line']};")
        outer.addWidget(sep_bot)

        self._statusbar = self._build_statusbar()
        outer.addWidget(self._statusbar)

    def _build_titlebar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(46)
        bar.setStyleSheet(f"background-color: {TOKENS['bg_recessed']};")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 8, 0)
        lay.setSpacing(10)

        # Brand mark (real multi-res PrismFX icon!)
        mark = QtWidgets.QLabel()
        mark.setFixedSize(24, 24)
        mark.setStyleSheet("background: transparent;")
        pm = self._app_icon.pixmap(24, 24)
        if not pm.isNull():
            mark.setPixmap(pm)
        else:
            mark.setText("▪")
            mark.setStyleSheet(f"color: {TOKENS['accent']}; font-size: 18px; background: transparent;")
        lay.addWidget(mark)

        lbl_brand = QtWidgets.QLabel(f"PRISMFX STUDIO")
        lbl_brand.setFont(ATLAS.font_for("display", 11.0, bold=True))
        lbl_brand.setStyleSheet(f"color: {TOKENS['ink_primary']}; letter-spacing: 1.5px; background: transparent;")
        lay.addWidget(lbl_brand)

        self.lbl_ver = QtWidgets.QLabel(f"v{__version__} // 42 GPU SHADERS")
        self.lbl_ver.setFont(ATLAS.font_for("mono", 8.5))
        self.lbl_ver.setStyleSheet(
            f"color: {TOKENS['accent_hi']}; background: {TOKENS['bg_elevated']}; "
            f"border: 1px solid {TOKENS['line']}; padding: 2px 7px;"
        )
        lay.addWidget(self.lbl_ver)

        if self.engine.backend.is_demo:
            demo_tag = QtWidgets.QLabel("DEMO MODE")
            demo_tag.setFont(ATLAS.font_for("mono", 8.5, bold=True))
            demo_tag.setStyleSheet(
                f"color: {TOKENS['warn']}; background: #261E0F; "
                f"border: 1px solid {TOKENS['warn']}; padding: 2px 7px;"
            )
            lay.addWidget(demo_tag)

        lay.addStretch(1)

        # Quick Controls Button
        self.btn_quick_ctrl = QtWidgets.QPushButton(self._tr("ควบคุมด่วน (Ctrl+Alt+W)", "Quick Bar (Ctrl+Alt+W)"))
        self.btn_quick_ctrl.setObjectName("btn_quick_ctrl")
        self.btn_quick_ctrl.setFixedHeight(28)
        self.btn_quick_ctrl.setStyleSheet("padding: 2px 10px;")
        self.btn_quick_ctrl.setFont(ATLAS.font_for("body", 9.0, text=self.btn_quick_ctrl.text()))
        self.btn_quick_ctrl.clicked.connect(self._open_quick_controls)
        lay.addWidget(self.btn_quick_ctrl)

        # Bilingual Switch
        self.btn_lang = QtWidgets.QPushButton("TH / EN")
        self.btn_lang.setObjectName("btn_lang")
        self.btn_lang.setFixedHeight(28)
        self.btn_lang.setStyleSheet("padding: 2px 10px;")
        self.btn_lang.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        self.btn_lang.clicked.connect(self._toggle_language)
        lay.addWidget(self.btn_lang)

        # Master Engine Switch
        self.master_toggle = ArchToggle(
            self.cfg.enabled,
            self._tr("เปิดทำงาน", "ENGINE ON"),
            self._tr("หยุดชั่วคราว", "ENGINE OFF"),
        )
        self.master_toggle.setObjectName("master_toggle")
        self.master_toggle.toggled_state.connect(self._on_master_toggled)
        lay.addWidget(self.master_toggle)

        # Window Chrome Buttons
        for obj_name, glyph, cb, is_close in [
            ("btn_min", "─", self.showMinimized, False),
            ("btn_max", "□", self._toggle_maximize, False),
            ("btn_close", "✕", self._on_close_button, True),
        ]:
            b = QtWidgets.QPushButton(glyph)
            b.setObjectName(obj_name)
            b.setFixedSize(36, 30)
            b.setFont(ATLAS.font_for("mono", 9.5))
            if is_close:
                b.setStyleSheet(
                    f"QPushButton {{ background: transparent; border: none; color: {TOKENS['ink_secondary']}; }}"
                    f"QPushButton:hover {{ background: {TOKENS['danger']}; color: #FFFFFF; }}"
                )
            else:
                b.setStyleSheet(
                    f"QPushButton {{ background: transparent; border: none; color: {TOKENS['ink_secondary']}; }}"
                    f"QPushButton:hover {{ background: {TOKENS['bg_hover']}; color: {TOKENS['ink_primary']}; }}"
                )
            b.clicked.connect(cb)
            lay.addWidget(b)

        bar.mousePressEvent = self._title_mouse_press  # type: ignore[method-assign]
        bar.mouseMoveEvent = self._title_mouse_move    # type: ignore[method-assign]
        bar.mouseDoubleClickEvent = lambda _e: self._toggle_maximize()  # type: ignore[method-assign]
        return bar

    def _build_sidebar(self) -> QtWidgets.QWidget:
        side = QtWidgets.QWidget()
        side.setFixedWidth(214)
        side.setStyleSheet(f"background-color: {TOKENS['bg_recessed']};")
        lay = QtWidgets.QVBoxLayout(side)
        lay.setContentsMargins(0, 12, 0, 12)
        lay.setSpacing(2)

        self.lbl_nav = QtWidgets.QLabel(self._tr("  เมนูหลัก", "  MODULES"))
        self.lbl_nav.setFont(ATLAS.font_for("mono", 8.0, text=self.lbl_nav.text(), bold=True))
        self.lbl_nav.setStyleSheet(f"color: {TOKENS['ink_muted']}; padding: 4px 12px;")
        lay.addWidget(self.lbl_nav)

        nav_items = [
            ("home", "01", "หน้าหลักสตูดิโอ", "Studio Home"),
            ("animations", "02", "คลังแอนิเมชัน 42+", "Animations Lab"),
            ("physics", "03", "ฟิสิกส์และการปัด", "Physics & Gestures"),
            ("focus", "04", "โฟกัสและเดสก์ท็อป", "Focus & Desktop"),
            ("apps", "05", "แอปและความปลอดภัย", "Apps & Safety"),
        ]
        for key, num, th, en in nav_items:
            raw_text = f"{num}  {self._tr(th, en)}"
            btn = QtWidgets.QPushButton(self._qt_esc(raw_text))
            btn.setObjectName(f"nav_{key}")
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setFixedHeight(42)
            btn.setFont(ATLAS.font_for("body", 10.5, text=raw_text, bold=True))
            btn.clicked.connect(lambda _=False, k=key: self._select_page(k))
            self._nav_buttons[key] = btn
            lay.addWidget(btn)

        lay.addStretch(1)

        # Sidebar Quick Startup & Privileges Card (visible on every page!)
        start_card = PanelCard(recessed=False)
        stl = QtWidgets.QVBoxLayout(start_card)
        stl.setContentsMargins(10, 9, 10, 9)
        stl.setSpacing(6)

        st_hdr = QtWidgets.QHBoxLayout()
        st_hdr.setSpacing(4)
        self.lbl_st_hdr = QtWidgets.QLabel(self._tr("เปิดพร้อมเครื่อง", "STARTUP"))
        self.lbl_st_hdr.setFont(ATLAS.font_for("mono", 7.6, text=self.lbl_st_hdr.text(), bold=True))
        self.lbl_st_hdr.setStyleSheet(f"color: {TOKENS['accent_hi']}; border: none; background: transparent;")
        self.lbl_side_priv = QtWidgets.QLabel("● ADMIN" if self._is_admin else "◐ STANDARD")
        self.lbl_side_priv.setFont(ATLAS.font_for("mono", 7.3, bold=True))
        self.lbl_side_priv.setStyleSheet(
            f"color: {TOKENS['ok'] if self._is_admin else TOKENS['warn']}; border: none; background: transparent;"
        )
        st_hdr.addWidget(self.lbl_st_hdr)
        st_hdr.addStretch(1)
        st_hdr.addWidget(self.lbl_side_priv)
        stl.addLayout(st_hdr)

        self.chk_side_autostart = QtWidgets.QCheckBox(self._tr_btn("เปิดพร้อม Windows", "Start with Windows"))
        self.chk_side_autostart.setObjectName("chk_side_autostart")
        self.chk_side_autostart.setFont(ATLAS.font_for("body", 8.8, text=self.chk_side_autostart.text()))
        self.chk_side_autostart.setChecked(self.cfg.start_with_windows)
        self.chk_side_autostart.toggled.connect(self._on_side_autostart_toggled)
        stl.addWidget(self.chk_side_autostart)

        self.btn_side_admin = QtWidgets.QPushButton(
            self._tr_btn("✓ สิทธิ์ผู้ดูแลพร้อมใช้", "✓ Admin Mode Active")
            if self._is_admin
            else self._tr_btn("ยกระดับสิทธิ์ Admin", "Run as Admin")
        )
        self.btn_side_admin.setObjectName("btn_side_admin")
        self.btn_side_admin.setFixedHeight(32)
        self.btn_side_admin.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_side_admin.setFont(ATLAS.font_for("body", 9.0, text=self.btn_side_admin.text(), bold=True))
        self.btn_side_admin.clicked.connect(self._on_side_admin_clicked)
        stl.addWidget(self.btn_side_admin)

        wrap_st = QtWidgets.QHBoxLayout()
        wrap_st.setContentsMargins(10, 0, 10, 8)
        wrap_st.addWidget(start_card)
        lay.addLayout(wrap_st)

        # Sidebar Engine Architecture Card
        card = PanelCard(recessed=False)
        cl = QtWidgets.QVBoxLayout(card)
        cl.setContentsMargins(10, 10, 10, 10)
        cl.setSpacing(4)
        self.lbl_arch = QtWidgets.QLabel(self._tr("ทำงานนอกโปรเซส", "OUT-OF-PROCESS ENGINE"))
        self.lbl_arch.setFont(ATLAS.font_for("mono", 7.8, text=self.lbl_arch.text(), bold=True))
        self.lbl_arch.setStyleSheet(f"color: {TOKENS['ok']}; border: none; background: transparent;")
        self.lbl_arch_desc = QtWidgets.QLabel(
            self._tr(
                "ปลอดภัย 100% ไม่ฉีด DLL\nไม่ชน Anti-Cheat / เกม",
                "Zero DLL Injection.\nAnti-Cheat & Win11 Safe.",
            )
        )
        self.lbl_arch_desc.setFont(ATLAS.font_for("body", 8.8, text=self.lbl_arch_desc.text()))
        self.lbl_arch_desc.setStyleSheet(f"color: {TOKENS['ink_secondary']}; border: none; background: transparent;")
        self.lbl_arch_desc.setWordWrap(True)
        self.lbl_arch_desc.setToolTip(
            self._tr(
                "PrismFX ใช้ระบบดักเหตุการณ์ภายนอก (Out-of-process) โดยไม่ฉีดไฟล์เข้าไปในเกมหรือโปรแกรมอื่น จึงปลอดภัยต่อระบบ Anti-Cheat",
                "PrismFX runs 100% out-of-process with zero DLL injection, staying safe with games and Anti-Cheat.",
            )
        )
        cl.addWidget(self.lbl_arch)
        cl.addWidget(self.lbl_arch_desc)

        wrap = QtWidgets.QHBoxLayout()
        wrap.setContentsMargins(10, 0, 10, 0)
        wrap.addWidget(card)
        lay.addLayout(wrap)
        return side

    def _build_statusbar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(28)
        bar.setStyleSheet(f"background-color: {TOKENS['bg_recessed']};")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 2, 0)
        lay.setSpacing(14)

        mono = ATLAS.font_for("mono", 8.5)
        self.lbl_st_engine = QtWidgets.QLabel("● ENGINE: ACTIVE")
        self.lbl_st_engine.setFont(mono)
        self.lbl_st_engine.setStyleSheet(f"color: {TOKENS['ok']}; background: transparent;")

        self.lbl_st_fps = QtWidgets.QLabel("STAGE: 165.0 FPS (6.06 ms)")
        self.lbl_st_fps.setFont(mono)
        self.lbl_st_fps.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent;")

        self.lbl_st_gpu = QtWidgets.QLabel("GPU: OpenGL 3.3 Core")
        self.lbl_st_gpu.setFont(mono)
        self.lbl_st_gpu.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent;")

        self.lbl_st_journal = QtWidgets.QLabel("JOURNAL: 0 MUTATIONS")
        self.lbl_st_journal.setFont(mono)
        self.lbl_st_journal.setStyleSheet(f"color: {TOKENS['ink_muted']}; background: transparent;")

        self.size_grip = QtWidgets.QSizeGrip(self)
        self.size_grip.setObjectName("window_size_grip")
        self.size_grip.setFixedSize(16, 16)

        lay.addWidget(self.lbl_st_engine)
        lay.addWidget(self.lbl_st_fps)
        lay.addWidget(self.lbl_st_gpu, 1)
        lay.addWidget(self.lbl_st_journal)
        lay.addWidget(self.size_grip, 0, QtCore.Qt.AlignBottom | QtCore.Qt.AlignRight)
        return bar

    # ------------------------------------------------------------------
    # Page 1: Studio Home
    # ------------------------------------------------------------------
    def _build_home_page(self) -> QtWidgets.QWidget:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        page = QtWidgets.QWidget()
        page.setStyleSheet("background: transparent;")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(16, 10, 16, 12)
        lay.setSpacing(9)

        self._sec_home = SectionHeader(
            "PRISMFX // NEXT-GEN WINDOWS 11 DESKTOP MOTION",
            self._tr("ศูนย์ควบคุมแอนิเมชันและฟิสิกส์หน้าต่าง 165 Hz", "165 Hz GPU Window Motion & Physics Studio"),
            self._tr(
                "ออกแบบใหม่หมดจดด้วย OpenGL 3.3 Shaders (42+ เอฟเฟกต์) แทนที่ระบบ DX10 เก่า — ปราศจากการฉีด DLL และกู้คืนสถานะอัตโนมัติ",
                "Clean-room OpenGL 3.3 Core engine with 42+ GPU shaders, closed-form spring physics, and crash-safe restoration.",
            ),
        )
        self._section_headers.append(self._sec_home)
        lay.addWidget(self._sec_home)

        # Battery Auto-Pause Banner (Board Resolution #5: visible whenever paused on battery)
        self.battery_banner = PanelCard()
        self.battery_banner.setObjectName("battery_pause_banner")
        self.battery_banner.setStyleSheet(
            f"PanelCard {{ background-color: #241C0F; border: 1px solid {TOKENS['warn']}; }}"
        )
        bbl = QtWidgets.QHBoxLayout(self.battery_banner)
        bbl.setContentsMargins(12, 7, 10, 7)
        bbl.setSpacing(10)
        self.lbl_battery_banner = QtWidgets.QLabel(
            self._tr(
                "⚡ หยุดเอฟเฟกต์อัตโนมัติเพราะกำลังใช้แบตเตอรี่เพื่อประหยัดพลังงาน — คุณสามารถเปิดใช้งานบนแบตเตอรี่ชั่วคราวได้",
                "⚡ Effects auto-paused on Battery power to save energy — you can temporarily enable effects on battery.",
            )
        )
        self.lbl_battery_banner.setWordWrap(True)
        self.lbl_battery_banner.setFont(ATLAS.font_for("body", 9.2, text=self.lbl_battery_banner.text(), bold=True))
        self.lbl_battery_banner.setStyleSheet(f"color: #FDE68A; background: transparent; border: none;")
        self.btn_battery_override = QtWidgets.QPushButton(
            self._tr_btn("เปิดใช้บนแบตเตอรี่ชั่วคราว", "Enable on Battery Temporarily")
        )
        self.btn_battery_override.setObjectName("btn_battery_override")
        self.btn_battery_override.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_battery_override.setFont(
            ATLAS.font_for("body", 9.0, text=self.btn_battery_override.text(), bold=True)
        )
        self.btn_battery_override.setStyleSheet(
            f"QPushButton {{ background: {TOKENS['warn']}; border: 1px solid #FBBF24; color: #121316; padding: 4px 10px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: #FBBF24; }}"
        )
        self.btn_battery_override.clicked.connect(self._on_battery_override_clicked)
        bbl.addWidget(self.lbl_battery_banner, 1)
        bbl.addWidget(self.btn_battery_override)
        self.battery_banner.hide()
        lay.addWidget(self.battery_banner)

        # 4 Architectural Telemetry Tiles
        kpi = QtWidgets.QHBoxLayout()
        kpi.setSpacing(10)
        self.tile_shaders = MetricTile(
            "GPU SHADER LIBRARY",
            f"{self.engine.registry.shader_effect_count()}",
            self._tr("เชดเดอร์ GLSL แท้ + โหลดเพิ่มได้", "Native GLSL + Custom Plugins"),
            TOKENS["accent"],
        )
        self.tile_fps = MetricTile(
            "LIVE STAGE PACING",
            "165.0 FPS",
            "6.06 ms target budget",
            TOKENS["ok"],
        )
        self.tile_played = MetricTile(
            "TRANSITIONS PLAYED",
            "0",
            self._tr("ทำงานนอกโปรเซส (Zero Injection)", "Out-of-process WinEvent hooks"),
            TOKENS["accent_hi"],
        )
        self.tile_journal = MetricTile(
            "CRASH RECOVERY JOURNAL",
            "CLEAN",
            self._tr("คืนค่าหน้าต่างอัตโนมัติหากปิดฉุกเฉิน", "0 pending OS mutations"),
            TOKENS["warn"],
        )
        self.tile_journal.setToolTip(
            self._tr(
                "บันทึกรายการแก้ไขหน้าต่างชั่วคราวลงไฟล์ เพื่อกู้คืนสถานะเดิมอัตโนมัติหากโปรแกรมปิดกะทันหัน",
                "Crash-safe recovery journal that automatically reverts temporary window mutations after any crash.",
            )
        )
        for t in (self.tile_shaders, self.tile_fps, self.tile_played, self.tile_journal):
            kpi.addWidget(t, 1)
        lay.addLayout(kpi)

        # Split Row: Interactive GPU Stage (left) + Curated Presets (right)
        split = QtWidgets.QHBoxLayout()
        split.setSpacing(12)

        # Left: Live GPU Stage Panel
        stage_card = PanelCard()
        sl = QtWidgets.QVBoxLayout(stage_card)
        sl.setContentsMargins(10, 9, 10, 9)
        sl.setSpacing(7)

        sh = QtWidgets.QHBoxLayout()
        sh.setSpacing(6)
        self.lbl_stage = QtWidgets.QLabel(
            self._tr("เวทีทดสอบเชดเดอร์สด (คลิกเพื่อเล่นซ้ำ)", "LIVE GPU STAGE (CLICK TO REPLAY)")
        )
        self.lbl_stage.setMinimumWidth(0)
        self.lbl_stage.setFont(ATLAS.font_for("mono", 8.3, text=self.lbl_stage.text(), bold=True))
        self.lbl_stage.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        sh.addWidget(self.lbl_stage, 1)

        self.btn_home_replay = QtWidgets.QPushButton(self._tr("เล่นซ้ำ", "Replay"))
        self.btn_home_replay.setObjectName("btn_home_replay")
        self.btn_home_replay.setFixedHeight(28)
        self.btn_home_replay.setStyleSheet("padding: 2px 10px;")
        self.btn_home_replay.clicked.connect(lambda: self.home_stage.replay())
        self.btn_home_test_real = QtWidgets.QPushButton(self._tr("ทดสอบบนหน้าต่างจริง", "Test on Real Window"))
        self.btn_home_test_real.setObjectName("btn_home_test_real")
        self.btn_home_test_real.setFixedHeight(28)
        self.btn_home_test_real.setProperty("accent", "true")
        self.btn_home_test_real.setStyleSheet("padding: 2px 10px;")
        self.btn_home_test_real.clicked.connect(self._test_home_preview_on_real_window)
        sh.addWidget(self.btn_home_replay)
        sh.addWidget(self.btn_home_test_real)
        sl.addLayout(sh)

        self.home_stage = ShaderPreviewWidget()
        self.home_stage.setMinimumHeight(200)
        self.home_stage.fps_updated.connect(self._on_stage_fps)
        self._home_preview_effect_id = self.cfg.triggers["close"].effect_id
        self.home_stage.set_effect(self.engine.registry.get(self._home_preview_effect_id))
        sl.addWidget(self.home_stage, 1)

        # Quick Showcase Strip (compact 2-row grid guaranteed fully visible at 1120×720!)
        strip = QtWidgets.QGridLayout()
        strip.setContentsMargins(0, 0, 0, 0)
        strip.setSpacing(5)
        quick_effects = [
            ("ember_burn", "เผาไหม้ไฟ", "Burn"),
            ("genie_lamp", "ตะเกียง Genie", "Genie"),
            ("cyber_glitch", "กลิตช์ RGB", "Glitch"),
            ("black_hole", "หลุมดำวน", "Black Hole"),
            ("frost_shatter", "แช่แข็งแตก", "Cryo Frost"),
            ("doors_3d", "ประตู 3 มิติ", "3D Doors"),
            ("crt_tv_off", "ทีวี CRT", "CRT Off"),
            ("hex_shield", "รังผึ้ง Hex", "Hex Shield"),
        ]
        self._home_quick_btns.clear()
        for idx, (eid, th_s, en_s) in enumerate(quick_effects):
            lbl = self._tr(th_s, en_s)
            b = QtWidgets.QPushButton(lbl)
            b.setObjectName(f"quick_fx_{eid}")
            b.setFixedHeight(28)
            b.setFont(ATLAS.font_for("body", 8.8, text=lbl))
            b.setProperty("pill", "true")
            b.setProperty("selected", "true" if eid == self._home_preview_effect_id else "false")
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, target_eid=eid: self._select_home_showcase_effect(target_eid))
            self._home_quick_btns.append((b, eid, th_s, en_s))
            strip.addWidget(b, idx // 4, idx % 4)
        sl.addLayout(strip)
        split.addWidget(stage_card, 3)

        # Right: Curated Presets Panel
        preset_card = PanelCard()
        preset_card.setMinimumWidth(305)
        preset_card.setMaximumWidth(370)
        pl = QtWidgets.QVBoxLayout(preset_card)
        pl.setContentsMargins(10, 9, 10, 9)
        pl.setSpacing(7)

        ph = QtWidgets.QHBoxLayout()
        ph.setSpacing(5)
        self.lbl_pr = QtWidgets.QLabel(self._tr("ชุดพรีเซตพร้อมใช้", "CURATED PRESETS"))
        self.lbl_pr.setFont(ATLAS.font_for("mono", 8.2, text=self.lbl_pr.text(), bold=True))
        self.lbl_pr.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        self.btn_save_pr = QtWidgets.QPushButton(self._tr("+ บันทึก", "+ Save"))
        self.btn_save_pr.setObjectName("btn_save_preset")
        self.btn_save_pr.setFixedHeight(26)
        self.btn_save_pr.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_save_pr.setStyleSheet("padding: 2px 8px;")
        self.btn_save_pr.setToolTip(self._tr("บันทึกการตั้งค่าปัจจุบันเป็นพรีเซตใหม่", "Save current settings as a custom preset"))
        self.btn_save_pr.clicked.connect(self._save_custom_preset_dialog)
        self.btn_reset_defaults = QtWidgets.QPushButton(self._tr("รีเซ็ต", "Reset"))
        self.btn_reset_defaults.setObjectName("btn_reset_defaults")
        self.btn_reset_defaults.setFixedHeight(26)
        self.btn_reset_defaults.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_reset_defaults.setStyleSheet("padding: 2px 8px;")
        self.btn_reset_defaults.setToolTip(self._tr("รีเซ็ตการตั้งค่าทั้งหมดกลับเป็นค่าเริ่มต้น", "Reset all settings to safe defaults"))
        self.btn_reset_defaults.clicked.connect(self._reset_all_defaults)
        ph.addWidget(self.lbl_pr, 1)
        ph.addWidget(self.btn_save_pr)
        ph.addWidget(self.btn_reset_defaults)
        pl.addLayout(ph)

        # Scrollable preset container keeps Home strictly within 1120×720 even with many user presets
        preset_scroll = QtWidgets.QScrollArea()
        preset_scroll.setWidgetResizable(True)
        preset_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        preset_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        preset_host = QtWidgets.QWidget()
        preset_host.setStyleSheet("background: transparent;")
        self._preset_list_layout = QtWidgets.QVBoxLayout(preset_host)
        self._preset_list_layout.setContentsMargins(0, 0, 2, 0)
        self._preset_list_layout.setSpacing(5)
        preset_scroll.setWidget(preset_host)
        pl.addWidget(preset_scroll, 1)

        # Global Speed Scale Slider
        self.sl_global_speed = SliderRow(
            self._tr("ความเร็วแอนิเมชันรวม (Global Speed)", "Global Animation Speed"),
            50, 200, int(round(self.cfg.global_speed_scale * 100)),
            formatter=lambda v: f"{v / 100.0:.2f}×",
        )
        self.sl_global_speed.setContentsMargins(0, 0, 4, 0)
        self.sl_global_speed.value_changed.connect(self._on_global_speed)
        pl.addWidget(self.sl_global_speed)

        split.addWidget(preset_card, 2)
        lay.addLayout(split, 1)
        self._rebuild_preset_buttons()

        scroll.setWidget(page)
        return scroll

    def _rebuild_preset_buttons(self) -> None:
        while self._preset_list_layout.count():
            item = self._preset_list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        del_tip = self._tr("ลบพรีเซตที่บันทึกไว้", "Delete saved preset")
        user_desc = self._tr("พรีเซตที่ผู้ใช้บันทึกไว้", "User saved preset")
        for name, is_builtin in self.engine.store.list_presets():
            meta = BUILTIN_PRESETS.get(name)
            desc = (meta["desc_th"] if self._lang == "th" else meta["desc_en"]) if meta else user_desc
            active = name == self.cfg.active_preset
            card = PresetItemWidget(
                preset_name=name,
                desc=desc,
                is_builtin=is_builtin,
                active=active,
                delete_tooltip=del_tip,
            )
            card.applied.connect(self._apply_preset)
            if not is_builtin:
                card.delete_requested.connect(self._delete_user_preset)
            self._preset_list_layout.addWidget(card)
        self._preset_list_layout.addStretch(1)

    # ------------------------------------------------------------------
    # Page 2: Animations Lab (42+ Effects + Custom GLSL Loader)
    # ------------------------------------------------------------------
    def _build_animations_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page.setStyleSheet("background: transparent;")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(16, 10, 16, 12)
        lay.setSpacing(8)

        self._sec_animations = SectionHeader(
            "ANIMATIONS LAB // 42+ GPU SHADERS",
            self._tr("ปรับแต่งแอนิเมชันแยกตามเหตุการณ์ + เชดเดอร์กำหนดเอง", "Per-Trigger GPU Shaders & Custom GLSL Lab"),
        )
        self._section_headers.append(self._sec_animations)
        lay.addWidget(self._sec_animations)

        # Trigger Strip (6 triggers + Random Pool + Custom GLSL)
        t_row = QtWidgets.QHBoxLayout()
        t_row.setSpacing(4)
        for tid, en, th in TRIGGER_TYPES:
            b = QtWidgets.QPushButton(self._tr_btn(th, en))
            b.setObjectName(f"trigger_{tid}")
            b.setProperty("pill", "true")
            b.setFont(ATLAS.font_for("body", 8.6, text=self._tr(th, en)))
            b.setStyleSheet("padding: 3px 7px;")
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=tid: self._select_trigger(k))
            self._trigger_buttons[tid] = b
            t_row.addWidget(b)
        t_row.addStretch(1)

        self.btn_random_pool = QtWidgets.QPushButton(self._tr_btn("รายการสุ่ม", "Random Pool"))
        self.btn_random_pool.setObjectName("btn_random_pool")
        self.btn_random_pool.setFixedHeight(27)
        self.btn_random_pool.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_random_pool.setStyleSheet("padding: 2px 8px;")
        self.btn_random_pool.clicked.connect(self._open_random_pool_dialog)
        t_row.addWidget(self.btn_random_pool)

        self.btn_new_glsl = QtWidgets.QPushButton(self._tr_btn("+ GLSL", "+ GLSL"))
        self.btn_new_glsl.setObjectName("btn_new_glsl")
        self.btn_new_glsl.setFixedHeight(27)
        self.btn_new_glsl.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_new_glsl.setStyleSheet("padding: 2px 8px;")
        self.btn_new_glsl.setToolTip(self._tr("สร้างไฟล์เชดเดอร์ GLSL ตัวอย่าง", "Create a sample custom GLSL shader"))
        self.btn_new_glsl.clicked.connect(self._create_sample_custom_shader)
        t_row.addWidget(self.btn_new_glsl)
        lay.addLayout(t_row)

        # Category Filter + Search Bar (tuned to fit cleanly at 1120×720 in BOTH TH and EN!)
        f_row = QtWidgets.QHBoxLayout()
        f_row.setSpacing(4)
        for cid, (en, th) in CATEGORIES.items():
            b = QtWidgets.QPushButton(self._tr_btn(th, en))
            b.setObjectName(f"cat_{cid}")
            b.setProperty("pill", "true")
            b.setFont(ATLAS.font_for("body", 8.5, text=self._tr(th, en)))
            b.setStyleSheet("padding: 4px 7px;")
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=cid: self._select_category(k))
            self._cat_buttons[cid] = b
            f_row.addWidget(b)
        self.ed_search = QtWidgets.QLineEdit()
        self.ed_search.setObjectName("ed_anim_search")
        self.ed_search.setPlaceholderText(self._tr("ค้นหาแอนิเมชัน...", "Search 42+ shaders..."))
        self.ed_search.setMinimumWidth(120)
        self.ed_search.setMaximumWidth(185)
        self.ed_search.textChanged.connect(self._on_search_changed)
        f_row.addWidget(self.ed_search, 1)
        lay.addLayout(f_row)

        # Split: Left Scrollable Grid (2 balanced columns, zero horizontal clipping!) | Right Inspector
        split = QtWidgets.QHBoxLayout()
        split.setSpacing(12)

        self._anim_scroll = QtWidgets.QScrollArea()
        self._anim_scroll.setWidgetResizable(True)
        self._anim_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._anim_grid_host = QtWidgets.QWidget()
        self._anim_grid_host.setStyleSheet("background: transparent;")
        self._anim_grid = QtWidgets.QGridLayout(self._anim_grid_host)
        self._anim_grid.setContentsMargins(0, 0, 6, 0)
        self._anim_grid.setSpacing(8)
        self._anim_grid.setColumnStretch(0, 1)
        self._anim_grid.setColumnStretch(1, 1)
        self._anim_scroll.setWidget(self._anim_grid_host)
        split.addWidget(self._anim_scroll, 1)

        # Right Inspector Panel (scrollable vertically so nothing clips even when GLSL Error Log is shown)
        insp = PanelCard()
        insp.setFixedWidth(326)
        insp_outer = QtWidgets.QVBoxLayout(insp)
        insp_outer.setContentsMargins(0, 0, 0, 0)
        insp_outer.setSpacing(0)

        insp_scroll = QtWidgets.QScrollArea()
        insp_scroll.setWidgetResizable(True)
        insp_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        insp_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        insp_inner = QtWidgets.QWidget()
        insp_inner.setStyleSheet("background: transparent;")
        il = QtWidgets.QVBoxLayout(insp_inner)
        il.setContentsMargins(9, 8, 9, 8)
        il.setSpacing(5)

        top_i = QtWidgets.QHBoxLayout()
        top_i.setSpacing(6)
        self.chk_trig_enabled = QtWidgets.QCheckBox(self._tr("เปิดใช้งานเหตุการณ์นี้", "Enable this trigger"))
        self.chk_trig_enabled.toggled.connect(self._on_trigger_params_changed)
        top_i.addWidget(self.chk_trig_enabled, 1)
        self.btn_lab_dir = QtWidgets.QPushButton(self._tr("ทิศทาง: เปิด (OPEN) ⇄", "DIR: OPEN ⇄"))
        self.btn_lab_dir.setObjectName("btn_lab_dir")
        self.btn_lab_dir.setFixedHeight(25)
        self.btn_lab_dir.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_lab_dir.setStyleSheet(
            f"QPushButton {{ padding: 2px 8px; font-size: 11px; background: {TOKENS['bg_recessed']}; "
            f"border: 1px solid {TOKENS['line_strong']}; }}"
            f"QPushButton:hover {{ border-color: {TOKENS['accent']}; }}"
        )
        self.btn_lab_dir.clicked.connect(self._flip_lab_direction)
        top_i.addWidget(self.btn_lab_dir)
        il.addLayout(top_i)

        self.lab_stage = ShaderPreviewWidget()
        self.lab_stage.setMinimumHeight(134)
        self.lab_stage.setFixedHeight(138)
        il.addWidget(self.lab_stage)

        eff_hdr = QtWidgets.QHBoxLayout()
        eff_hdr.setSpacing(6)
        self.lbl_lab_effect = QtWidgets.QLabel("Prism Glide")
        self.lbl_lab_effect.setFont(ATLAS.font_for("display", 11.5, bold=True))
        self.lbl_lab_effect.setStyleSheet(f"color: {TOKENS['ink_primary']}; background: transparent; border: none;")
        self.lbl_lab_effect.setMinimumWidth(0)
        self.btn_use_effect_defaults = QtWidgets.QPushButton(self._tr("ค่าแนะนำ", "Defaults"))
        self.btn_use_effect_defaults.setObjectName("btn_use_effect_defaults")
        self.btn_use_effect_defaults.setFixedHeight(24)
        self.btn_use_effect_defaults.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_use_effect_defaults.setStyleSheet("padding: 1px 7px; font-size: 11px;")
        self.btn_use_effect_defaults.setToolTip(
            self._tr("รีเซ็ตเวลา/สี/ความแรงกลับเป็นค่าแนะนำของเชดเดอร์นี้", "Reset duration, intensity, and accent to this shader's defaults")
        )
        self.btn_use_effect_defaults.clicked.connect(self._apply_effect_recommended_defaults)
        eff_hdr.addWidget(self.lbl_lab_effect, 1)
        eff_hdr.addWidget(self.btn_use_effect_defaults)
        il.addLayout(eff_hdr)

        self.sl_trig_dur = SliderRow(self._tr("ระยะเวลาแอนิเมชัน", "Duration"), 80, 1200, 320, " ms")
        self.sl_trig_dur.value_changed.connect(self._on_trigger_duration_changed)
        il.addWidget(self.sl_trig_dur)

        self.sl_trig_int = SliderRow(
            self._tr("ความแรงเอฟเฟกต์", "Shader Intensity"),
            30, 180, 100, formatter=lambda v: f"{v}%",
        )
        self.sl_trig_int.value_changed.connect(self._on_trigger_params_changed)
        il.addWidget(self.sl_trig_int)

        self.lbl_ease = QtWidgets.QLabel(self._tr("เส้นโค้งสปริง (Easing Curve)", "Motion Spring Curve"))
        self.lbl_ease.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
        self.lbl_ease.setToolTip(
            self._tr("กำหนดลักษณะอัตราเร่งและการหยุดของแอนิเมชัน (เช่น สปริงนุ่มนวล หรือ เด้งยืดหยุ่น)", "Controls acceleration and settle curve of the transition.")
        )
        il.addWidget(self.lbl_ease)
        self.cmb_easing = QtWidgets.QComboBox()
        for cid, en, th in EASING_CURVES:
            self.cmb_easing.addItem(self._tr(f"{th} ({en})", en), cid)
        self.cmb_easing.currentIndexChanged.connect(self._on_trigger_params_changed)
        il.addWidget(self.cmb_easing)

        # Accent Color Swatches (highlighted active selection!)
        self.lbl_sw_accent = QtWidgets.QLabel(self._tr("สีแสงเอฟเฟกต์ (Shader Accent)", "Shader Accent Tint"))
        self.lbl_sw_accent.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
        il.addWidget(self.lbl_sw_accent)
        sw_row = QtWidgets.QHBoxLayout()
        sw_row.setSpacing(4)
        self._accent_swatch_btns.clear()
        for hx in ACCENT_SWATCHES:
            sb = QtWidgets.QPushButton()
            sb.setFixedSize(28, 22)
            sb.setCursor(QtCore.Qt.PointingHandCursor)
            sb.setToolTip(hx)
            sb.setStyleSheet(f"background: {hx}; border: 1px solid #444B5E;")
            sb.clicked.connect(lambda _=False, c=hx: self._set_trigger_accent(c))
            self._accent_swatch_btns[hx.upper()] = sb
            sw_row.addWidget(sb)
        sw_row.addStretch(1)
        il.addLayout(sw_row)

        self.btn_try = QtWidgets.QPushButton(self._tr("▶ ทดสอบเชดเดอร์นี้บนหน้าต่างจริง", "▶ Fire Live Desktop Preview"))
        self.btn_try.setObjectName("btn_lab_test_real")
        self.btn_try.setProperty("accent", "true")
        self.btn_try.setFixedHeight(30)
        self.btn_try.setStyleSheet("padding: 3px 10px;")
        self.btn_try.clicked.connect(self._test_active_effect_on_real_window)
        il.addWidget(self.btn_try)

        # Custom GLSL Tools Row (Board Resolution #4: Open Shaders Folder + Reload Shaders)
        glsl_row = QtWidgets.QHBoxLayout()
        glsl_row.setSpacing(6)
        self.btn_open_shaders_dir = QtWidgets.QPushButton(
            self._tr("เปิดโฟลเดอร์ Shaders", "Open Shaders Folder")
        )
        self.btn_open_shaders_dir.setObjectName("btn_open_shaders_dir")
        self.btn_open_shaders_dir.setFixedHeight(26)
        self.btn_open_shaders_dir.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_open_shaders_dir.setStyleSheet("padding: 2px 8px; font-size: 11px;")
        self.btn_open_shaders_dir.clicked.connect(self._open_shaders_folder)

        self.btn_reload_shaders = QtWidgets.QPushButton(
            self._tr("รีโหลดเชดเดอร์", "Reload Shaders")
        )
        self.btn_reload_shaders.setObjectName("btn_reload_shaders")
        self.btn_reload_shaders.setFixedHeight(26)
        self.btn_reload_shaders.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_reload_shaders.setStyleSheet("padding: 2px 8px; font-size: 11px;")
        self.btn_reload_shaders.clicked.connect(self._reload_custom_shaders)
        glsl_row.addWidget(self.btn_open_shaders_dir, 1)
        glsl_row.addWidget(self.btn_reload_shaders, 1)
        il.addLayout(glsl_row)

        # GLSL Compile / Sandbox Error Log Panel (hidden unless custom shader has error)
        self.glsl_error_panel = PanelCard(recessed=True)
        self.glsl_error_panel.setObjectName("glsl_error_panel")
        self.glsl_error_panel.setStyleSheet(
            f"PanelCard {{ background-color: #261419; border: 1px solid {TOKENS['danger']}; }}"
        )
        ge_lay = QtWidgets.QVBoxLayout(self.glsl_error_panel)
        ge_lay.setContentsMargins(8, 6, 8, 6)
        ge_lay.setSpacing(4)
        self.lbl_glsl_err_hdr = QtWidgets.QLabel(self._tr("⚠ บันทึกข้อผิดพลาด GLSL", "⚠ GLSL COMPILE ERROR LOG"))
        self.lbl_glsl_err_hdr.setFont(ATLAS.font_for("mono", 8.0, text=self.lbl_glsl_err_hdr.text(), bold=True))
        self.lbl_glsl_err_hdr.setStyleSheet("color: #FDA4AF; background: transparent; border: none;")
        ge_lay.addWidget(self.lbl_glsl_err_hdr)
        self.txt_glsl_error = QtWidgets.QPlainTextEdit()
        self.txt_glsl_error.setObjectName("txt_glsl_error")
        self.txt_glsl_error.setReadOnly(True)
        self.txt_glsl_error.setFixedHeight(76)
        self.txt_glsl_error.setFont(ATLAS.font_for("mono", 8.0))
        self.txt_glsl_error.setStyleSheet(
            f"QPlainTextEdit {{ background: {TOKENS['bg_recessed']}; color: #FDE68A; border: 1px solid {TOKENS['line']}; }}"
        )
        ge_lay.addWidget(self.txt_glsl_error)
        self.glsl_error_panel.hide()
        il.addWidget(self.glsl_error_panel)

        il.addStretch(1)
        insp_scroll.setWidget(insp_inner)
        insp_outer.addWidget(insp_scroll)

        split.addWidget(insp, 0)
        lay.addLayout(split, 1)
        return page

    # ------------------------------------------------------------------
    # Page 3: Physics & Gestures (Live Wobbly Sandbox + Momentum + Flicks + Edge Snap)
    # ------------------------------------------------------------------
    def _build_physics_page(self) -> QtWidgets.QWidget:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        page = QtWidgets.QWidget()
        page.setStyleSheet("background: transparent;")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 16)
        lay.setSpacing(12)

        self._sec_physics = SectionHeader(
            "PHYSICS & GESTURES // COMPIZ SPRING ENGINE",
            self._tr("หน้าต่างเยลลี่, แรงเฉื่อยตอนลาก, ปัดสแนป และการควบคุมแถบหัวเรื่อง", "Wobbly Windows, Momentum Inertia, Flicks & Smart Edge Snap"),
        )
        self._section_headers.append(self._sec_physics)
        lay.addWidget(self._sec_physics)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(12)

        # Left: Interactive Wobbly Window Sandbox
        wob_card = PanelCard()
        wl = QtWidgets.QVBoxLayout(wob_card)
        wl.setContentsMargins(12, 10, 12, 10)
        wl.setSpacing(7)

        wh = QtWidgets.QHBoxLayout()
        self.chk_wobbly = QtWidgets.QCheckBox(self._tr("เปิดใช้หน้าต่างเยลลี่ (Wobbly Windows)", "Enable Wobbly Windows (Compiz Physics)"))
        self.chk_wobbly.setChecked(self.cfg.physics.wobbly_enabled)
        self.chk_wobbly.toggled.connect(self._save_physics_ui)
        wh.addWidget(self.chk_wobbly, 1)
        self.lbl_wob_hint = QtWidgets.QLabel(self._tr("คลิกลากเขย่าหน้าต่างด้านล่างเพื่อทดสอบ!", "GRAB & SHAKE CARD BELOW"))
        self.lbl_wob_hint.setFont(ATLAS.font_for("mono", 8.0, text=self.lbl_wob_hint.text(), bold=True))
        self.lbl_wob_hint.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        wh.addWidget(self.lbl_wob_hint)
        wl.addLayout(wh)

        self.wobble_stage = ShaderPreviewWidget()
        self.wobble_stage.setMinimumHeight(210)
        self.wobble_stage.set_wobble_sandbox(True)
        self.wobble_stage.configure_wobble(
            self.cfg.physics.wobbly_stiffness,
            self.cfg.physics.wobbly_damping,
            self.cfg.physics.wobbly_amplitude,
        )
        wl.addWidget(self.wobble_stage)

        self.sl_wob_stiff = SliderRow(self._tr("ความตึงสปริง (Stiffness)", "Spring Stiffness"), 10, 42, int(self.cfg.physics.wobbly_stiffness))
        self.sl_wob_damp = SliderRow(self._tr("ความหน่วงเยลลี่ (Damping)", "Spring Damping"), 32, 92, int(self.cfg.physics.wobbly_damping * 100), "%")
        self.sl_wob_amp = SliderRow(self._tr("ความแรงการสั่น (Amplitude)", "Wobble Amplitude"), 30, 200, int(self.cfg.physics.wobbly_amplitude * 100), "%")
        for s in (self.sl_wob_stiff, self.sl_wob_damp, self.sl_wob_amp):
            s.value_changed.connect(self._save_physics_ui)
            wl.addWidget(s)

        top.addWidget(wob_card, 3)

        # Right: Momentum Dragging + Drag Transparency
        mom_card = PanelCard()
        ml = QtWidgets.QVBoxLayout(mom_card)
        ml.setContentsMargins(14, 12, 14, 12)
        ml.setSpacing(8)

        self.lbl_m = QtWidgets.QLabel(self._tr("ฟิสิกส์แรงเฉื่อยและการลาก", "MOMENTUM & DRAG PHYSICS"))
        self.lbl_m.setFont(ATLAS.font_for("mono", 8.5, text=self.lbl_m.text(), bold=True))
        self.lbl_m.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        ml.addWidget(self.lbl_m)

        self.chk_momentum = QtWidgets.QCheckBox(self._tr("แรงเฉื่อยเมื่อโยนหน้าต่าง (Momentum Drag)", "Enable Momentum / Inertia Window Glide"))
        self.chk_momentum.setChecked(self.cfg.physics.momentum_enabled)
        self.chk_bounce = QtWidgets.QCheckBox(self._tr("เด้งกลับเมื่อชนขอบจอ (Elastic Edge Bounce)", "Elastic Bounce off Monitor Borders"))
        self.chk_bounce.setChecked(self.cfg.physics.momentum_bounce)
        self.chk_clamp = QtWidgets.QCheckBox(self._tr("จำกัดหน้าต่างไม่ให้หลุดขอบจอ (Strict Clamp)", "Strictly Clamp Windows to Screen Limits"))
        self.chk_clamp.setChecked(self.cfg.physics.momentum_strict_clamp)
        self.chk_drag_alpha = QtWidgets.QCheckBox(self._tr("โปร่งใสอัตโนมัติขณะลากย้ายหน้าต่าง", "Semi-Transparent While Moving Window"))
        self.chk_drag_alpha.setChecked(self.cfg.physics.drag_transparency_enabled)

        for c in (self.chk_momentum, self.chk_bounce, self.chk_clamp):
            c.toggled.connect(self._save_physics_ui)
            ml.addWidget(c)

        self.lbl_clamp_note = QtWidgets.QLabel(
            self._tr(
                "ℹ โหมด Strict Clamp จะปิดการเด้งกลับขอบจอ (Elastic Bounce) โดยอัตโนมัติ",
                "ℹ Strict Clamp automatically disables Elastic Edge Bounce.",
            )
        )
        self.lbl_clamp_note.setObjectName("lbl_clamp_note")
        self.lbl_clamp_note.setWordWrap(True)
        self.lbl_clamp_note.setFont(ATLAS.font_for("body", 8.4, text=self.lbl_clamp_note.text()))
        self.lbl_clamp_note.setStyleSheet(f"color: {TOKENS['warn']}; background: transparent; border: none; padding-left: 22px;")
        self.lbl_clamp_note.setVisible(False)
        ml.addWidget(self.lbl_clamp_note)

        self.chk_drag_alpha.toggled.connect(self._save_physics_ui)
        ml.addWidget(self.chk_drag_alpha)

        self.sl_friction = SliderRow(
            self._tr("แรงเสียดทานตอนไหล (Friction)", "Glide Friction"),
            25, 120, int(self.cfg.physics.momentum_friction * 10),
            formatter=lambda v: f"{v / 10.0:.1f}",
        )
        self.sl_drag_op = SliderRow(
            self._tr("ความทึบแสงขณะลาก", "Drag Opacity"), 40, 98, self.cfg.physics.drag_opacity_pct, "%"
        )
        self.sl_friction.value_changed.connect(self._save_physics_ui)
        self.sl_drag_op.value_changed.connect(self._save_physics_ui)
        ml.addWidget(self.sl_friction)
        ml.addWidget(self.sl_drag_op)

        # Titlebar Middle-Click Action
        self.lbl_tb = QtWidgets.QLabel(self._tr("คลิกปุ่มกลางที่แถบหัวเรื่อง (Middle-Click Titlebar)", "Titlebar Middle-Click Action"))
        self.lbl_tb.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
        ml.addWidget(self.lbl_tb)
        self.cmb_mid_click = QtWidgets.QComboBox()
        for val, th, en in [
            ("rollup", "ม้วนเก็บเหลือแค่แถบหัวเรื่อง (Window Shade Roll-Up)", "Roll-Up / Window Shade"),
            ("minimize", "ย่อเก็บลงทาสก์บาร์ (Minimize)", "Minimize Window"),
            ("pin", "ปักหมุดให้อยู่บนสุดเสมอ (Always on Top)", "Toggle Always on Top"),
            ("tile", "ย่อเป็นไทล์ลอยขนาดเล็ก (Minimize to Mini-Tile)", "Minimize to Floating Mini-Tile"),
            ("none", "ปิดใช้งาน (None)", "Disabled"),
        ]:
            self.cmb_mid_click.addItem(self._tr(th, en), val)
        idx = self.cmb_mid_click.findData(self.cfg.physics.middle_click_titlebar)
        if idx >= 0:
            self.cmb_mid_click.setCurrentIndex(idx)
        self.cmb_mid_click.currentIndexChanged.connect(self._save_physics_ui)
        ml.addWidget(self.cmb_mid_click)
        ml.addStretch(1)

        top.addWidget(mom_card, 2)
        lay.addLayout(top)

        # Bottom: Window Flicks & 4-Edge Smart Snap Matrix
        snap_card = PanelCard()
        spl = QtWidgets.QVBoxLayout(snap_card)
        spl.setContentsMargins(14, 12, 14, 12)
        spl.setSpacing(9)

        sr = QtWidgets.QHBoxLayout()
        self.chk_flicks = QtWidgets.QCheckBox(self._tr("ปัดเมาส์เร็วเพื่อจัดวางหน้าต่าง (Window Flicks)", "Enable Window Flicks (Gesture Throw)"))
        self.chk_flicks.setChecked(self.cfg.physics.flicks_enabled)
        self.chk_flicks.setToolTip(
            self._tr("ปิดเป็นค่าเริ่มต้นเพื่อป้องกันการขยาย/ย่อหน้าต่างโดยไม่ตั้งใจเวลาลากเร็ว", "Off by default to prevent accidental maximize/minimize during fast drags.")
        )
        self.chk_edge_snap = QtWidgets.QCheckBox(self._tr_btn("ลากชนขอบ/มุมจอเพื่อแบ่งสัดส่วนอัจฉริยะ (Smart Edge Snap)", "Enable Monitor Edge & Corner Smart Snap"))
        self.chk_edge_snap.setChecked(self.cfg.physics.edge_snap_enabled)
        self.chk_flicks.toggled.connect(self._save_physics_ui)
        self.chk_edge_snap.toggled.connect(self._save_physics_ui)
        sr.addWidget(self.chk_flicks)
        sr.addWidget(self.chk_edge_snap)
        sr.addStretch(1)
        spl.addLayout(sr)

        # Smooth Maximize / Restore-Down / Edge-Snap Transition (compact secondary control!)
        mr = QtWidgets.QHBoxLayout()
        mr.setSpacing(14)
        self.chk_smooth_max = QtWidgets.QCheckBox(
            self._tr_btn(
                "แอนิเมชันลื่นไหลตอนขยายเต็มจอ / ชนขอบจอ (Smooth Maximize & Snap)",
                "Smooth Transition on Maximize / Restore-Down / Edge Snap",
            )
        )
        self.chk_smooth_max.setChecked(self.cfg.physics.smooth_maximize_enabled)
        self.chk_smooth_max.toggled.connect(self._save_physics_ui)
        self.sl_smooth_max_ms = SliderRow(
            self._tr("ระยะเวลาขยายเต็มจอ", "Morph Duration"),
            120, 380, int(self.cfg.physics.smooth_maximize_ms), " ms",
        )
        self.sl_smooth_max_ms.value_changed.connect(self._save_physics_ui)
        mr.addWidget(self.chk_smooth_max, 2)
        mr.addWidget(self.sl_smooth_max_ms, 1)
        spl.addLayout(mr)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(10)
        self._snap_combos: dict[str, QtWidgets.QComboBox] = {}
        self._snap_edge_labels.clear()
        edges = [
            ("snap_left_zone", "ขอบซ้าย / ปัดซ้าย", "Left Edge / Flick", self.cfg.physics.snap_left_zone),
            ("snap_right_zone", "ขอบขวา / ปัดขวา", "Right Edge / Flick", self.cfg.physics.snap_right_zone),
            ("snap_top_zone", "ขอบบน / ปัดขึ้น", "Top Edge / Flick", self.cfg.physics.snap_top_zone),
            ("snap_bottom_zone", "ขอบล่าง / ปัดลง", "Bottom Edge / Flick", self.cfg.physics.snap_bottom_zone),
        ]
        for col, (attr, lbl_th, lbl_en, cur) in enumerate(edges):
            v = QtWidgets.QVBoxLayout()
            l = QtWidgets.QLabel(self._tr(lbl_th, lbl_en))
            l.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
            self._snap_edge_labels[attr] = (l, lbl_th, lbl_en)
            cb = QtWidgets.QComboBox()
            for zk, (zen, zth) in SNAP_PRESETS.items():
                cb.addItem(self._tr(f"{zth} ({zen})", zen), zk)
            ci = cb.findData(cur)
            if ci >= 0:
                cb.setCurrentIndex(ci)
            cb.currentIndexChanged.connect(self._save_physics_ui)
            self._snap_combos[attr] = cb
            v.addWidget(l)
            v.addWidget(cb)
            grid.addLayout(v, 0, col)
        spl.addLayout(grid)
        lay.addWidget(snap_card)
        lay.addStretch(1)

        scroll.setWidget(page)
        return scroll

    # ------------------------------------------------------------------
    # Page 4: Focus, Spotlight, Mini-Tiles & Desktop Icons
    # ------------------------------------------------------------------
    def _build_focus_page(self) -> QtWidgets.QWidget:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        page = QtWidgets.QWidget()
        page.setStyleSheet("background: transparent;")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 16)
        lay.setSpacing(12)

        self._sec_focus = SectionHeader(
            "FOCUS, ATMOSPHERE & DESKTOP CONTROLS",
            self._tr("ไฮไลต์หน้าต่างที่ใช้งาน, ย่ออัตโนมัติ, ไทล์ลอย และจัดการไอคอนเดสก์ท็อป", "Spotlight Dimmer, Inactive Fade, Window Tiles & Desktop Icons"),
        )
        self._section_headers.append(self._sec_focus)
        lay.addWidget(self._sec_focus)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(12)

        # Left: Spotlight & Inactive Focus
        sp_card = PanelCard()
        sl = QtWidgets.QVBoxLayout(sp_card)
        sl.setContentsMargins(14, 14, 14, 14)
        sl.setSpacing(10)

        self.lbl_sp = QtWidgets.QLabel(self._tr("สปอตไลต์โฟกัสหน้าต่างปัจจุบัน", "ACTIVE WINDOW SPOTLIGHT"))
        self.lbl_sp.setFont(ATLAS.font_for("mono", 8.5, text=self.lbl_sp.text(), bold=True))
        self.lbl_sp.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        sl.addWidget(self.lbl_sp)

        self.chk_spotlight = QtWidgets.QCheckBox(self._tr("หรี่แสงหน้าต่างเบื้องหลังเพื่อโฟกัสหน้าต่างปัจจุบัน (Spotlight)", "Darken Inactive Windows Behind Active Window"))
        self.chk_spotlight.setChecked(self.cfg.focus.spotlight_enabled)
        self.chk_spotlight.toggled.connect(self._save_focus_ui)
        sl.addWidget(self.chk_spotlight)

        self.cmb_spot_mode = QtWidgets.QComboBox()
        for val, th, en in [
            ("halo", "วงแสงรัศมีรอบหน้าต่างปัจจุบัน (Radial Window Halo)", "Radial Window Halo"),
            ("cursor", "ไฟฉายสปอตไลต์ตามเมาส์ (Cursor Spotlight)", "Cursor Radial Spotlight"),
            ("flat", "หรี่แสงพื้นหลังเรียบทั่วทั้งจอ (Flat Dimmer)", "Flat Fullscreen Dimmer"),
        ]:
            self.cmb_spot_mode.addItem(self._tr(th, en), val)
        mi = self.cmb_spot_mode.findData(self.cfg.focus.spotlight_mode)
        if mi >= 0:
            self.cmb_spot_mode.setCurrentIndex(mi)
        self.cmb_spot_mode.currentIndexChanged.connect(self._save_focus_ui)
        sl.addWidget(self.cmb_spot_mode)

        self.sl_spot_op = SliderRow(self._tr("ความเข้มการหรี่แสง", "Dimmer Darkness"), 12, 82, self.cfg.focus.spotlight_opacity_pct, "%")
        self.sl_spot_op.value_changed.connect(self._save_focus_ui)
        sl.addWidget(self.sl_spot_op)

        sw = QtWidgets.QHBoxLayout()
        sw.setSpacing(6)
        self._spot_swatch_btns.clear()
        for hx in SPOTLIGHT_SWATCHES:
            b = QtWidgets.QPushButton()
            b.setFixedSize(32, 22)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setToolTip(hx)
            b.setStyleSheet(f"background: {hx}; border: 1px solid #475066;")
            b.clicked.connect(lambda _=False, c=hx: self._set_spotlight_color(c))
            self._spot_swatch_btns[hx.upper()] = b
            sw.addWidget(b)
        sw.addStretch(1)
        sl.addLayout(sw)

        line = QtWidgets.QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {TOKENS['line']};")
        sl.addWidget(line)

        self.chk_inactive_fade = QtWidgets.QCheckBox(self._tr("ทำให้หน้าต่างที่ไม่ได้ใช้งานโปร่งแสงอัตโนมัติ", "Make Inactive Windows Semi-Transparent"))
        self.chk_inactive_fade.setChecked(self.cfg.focus.inactive_fade_enabled)
        self.chk_inactive_fade.toggled.connect(self._save_focus_ui)
        sl.addWidget(self.chk_inactive_fade)

        self.sl_inactive_op = SliderRow(self._tr("ความทึบของหน้าต่างที่ไม่โฟกัส", "Inactive Opacity"), 45, 95, self.cfg.focus.inactive_opacity_pct, "%")
        self.sl_inactive_op.value_changed.connect(self._save_focus_ui)
        sl.addWidget(self.sl_inactive_op)

        self.chk_automin = QtWidgets.QCheckBox(self._tr("ย่อหน้าต่างที่ไม่ได้ใช้งานนานเกินกำหนดอัตโนมัติ", "Auto-Minimize Idle Inactive Windows"))
        self.chk_automin.setChecked(self.cfg.focus.auto_minimize_enabled)
        self.chk_automin.toggled.connect(self._save_focus_ui)
        sl.addWidget(self.chk_automin)

        self.sl_automin_min = SliderRow(self._tr("ระยะเวลาว่างก่อนย่ออัตโนมัติ", "Idle Timeout"), 1, 30, self.cfg.focus.auto_minimize_minutes, " min")
        self.sl_automin_min.value_changed.connect(self._save_focus_ui)
        sl.addWidget(self.sl_automin_min)

        row.addWidget(sp_card, 1)

        # Right: Desktop Icons & Floating Mini-Tiles
        dk_card = PanelCard()
        dl = QtWidgets.QVBoxLayout(dk_card)
        dl.setContentsMargins(14, 14, 14, 14)
        dl.setSpacing(10)

        self.lbl_d = QtWidgets.QLabel(self._tr("ไอคอนเดสก์ท็อปและไทล์หน้าต่างลอย", "DESKTOP ICONS & FLOATING WINDOW TILES"))
        self.lbl_d.setFont(ATLAS.font_for("mono", 8.5, text=self.lbl_d.text(), bold=True))
        self.lbl_d.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        dl.addWidget(self.lbl_d)

        self.chk_dbl_icons = QtWidgets.QCheckBox(self._tr("ดับเบิลคลิกพื้นว่างบนเดสก์ท็อปเพื่อซ่อน/แสดงไอคอน", "Double-Click Desktop to Toggle Icons Visibility"))
        self.chk_dbl_icons.setChecked(self.cfg.physics.double_click_desktop_icons)
        self.chk_dbl_icons.toggled.connect(self._save_physics_ui)
        dl.addWidget(self.chk_dbl_icons)

        self.chk_autohide_icons = QtWidgets.QCheckBox(self._tr("ซ่อนไอคอนเดสก์ท็อปอัตโนมัติเมื่อไม่ได้คลิกเดสก์ท็อป", "Auto-Hide Desktop Icons Unless Desktop Focused"))
        self.chk_autohide_icons.setChecked(self.cfg.focus.desktop_icons_autohide)
        self.chk_autohide_icons.toggled.connect(self._save_focus_ui)
        dl.addWidget(self.chk_autohide_icons)

        self.btn_toggle_icons_now = QtWidgets.QPushButton(self._tr("ซ่อน / แสดงไอคอนเดสก์ท็อปทันที", "Toggle Desktop Icons Now"))
        self.btn_toggle_icons_now.setObjectName("btn_toggle_icons_now")
        self.btn_toggle_icons_now.clicked.connect(lambda: self.engine.toggle_desktop_icons())
        dl.addWidget(self.btn_toggle_icons_now)

        line2 = QtWidgets.QFrame()
        line2.setFixedHeight(1)
        line2.setStyleSheet(f"background: {TOKENS['line']};")
        dl.addWidget(line2)

        self.chk_tiles_top = QtWidgets.QCheckBox(self._tr("ให้ไทล์หน้าต่างลอยอยู่บนสุดเสมอ", "Keep Floating Mini-Tiles Always on Top"))
        self.chk_tiles_top.setChecked(self.cfg.focus.tiles_topmost)
        self.chk_tiles_top.toggled.connect(self._save_focus_ui)
        dl.addWidget(self.chk_tiles_top)

        self.sl_tile_size = SliderRow(self._tr("ขนาดไทล์หน้าต่างลอย", "Mini-Tile Width"), 140, 300, self.cfg.focus.tile_size_px, " px")
        self.sl_tile_size.value_changed.connect(self._save_focus_ui)
        dl.addWidget(self.sl_tile_size)

        self.btn_restore_tiles = QtWidgets.QPushButton(self._tr("คืนค่าไทล์หน้าต่างทั้งหมด", "Restore All Floating Tiles"))
        self.btn_restore_tiles.clicked.connect(self._restore_all_tiles)
        dl.addWidget(self.btn_restore_tiles)
        dl.addStretch(1)

        row.addWidget(dk_card, 1)
        lay.addLayout(row)
        lay.addStretch(1)

        scroll.setWidget(page)
        return scroll

    # ------------------------------------------------------------------
    # Page 5: Apps Catalog, Exclusions & Crash Safety
    # ------------------------------------------------------------------
    def _build_apps_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page.setStyleSheet("background: transparent;")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(10)

        self._sec_apps = SectionHeader(
            "APP CATALOG, EXCLUSIONS & CRASH RECOVERY",
            self._tr("ตรวจสอบหน้าต่างที่เปิดอยู่, สิทธิ์ระบบ/เปิดพร้อมเครื่อง และระบบกู้คืนฉุกเฉิน", "Live App Catalog, Windows Startup & Privileges, Smart Exclusions"),
        )
        self._section_headers.append(self._sec_apps)
        lay.addWidget(self._sec_apps)

        # Windows Startup & Administrator Privileges Card (WindowFX-style boot & UIPI elevation)
        boot_card = PanelCard()
        bl = QtWidgets.QVBoxLayout(boot_card)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(8)

        bh = QtWidgets.QHBoxLayout()
        bh.setSpacing(10)
        self.lbl_boot_hdr = QtWidgets.QLabel(self._tr("การเปิดพร้อมเครื่องและสิทธิ์ระบบ", "WINDOWS STARTUP & PRIVILEGES"))
        self.lbl_boot_hdr.setFont(ATLAS.font_for("mono", 8.3, text=self.lbl_boot_hdr.text(), bold=True))
        self.lbl_boot_hdr.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")

        self.lbl_priv_badge = QtWidgets.QLabel()
        self.lbl_priv_badge.setFont(ATLAS.font_for("mono", 7.8, bold=True))

        self.lbl_startup_status = QtWidgets.QLabel()
        self.lbl_startup_status.setFont(ATLAS.font_for("body", 8.8, text="เปิดอัตโนมัติ"))
        self.lbl_startup_status.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")

        self.btn_elevate_admin = QtWidgets.QPushButton()
        self.btn_elevate_admin.setObjectName("btn_elevate_admin")
        self.btn_elevate_admin.setFixedHeight(32)
        self.btn_elevate_admin.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_elevate_admin.clicked.connect(self._on_relaunch_as_admin)

        bh.addWidget(self.lbl_boot_hdr)
        bh.addWidget(self.lbl_priv_badge)
        bh.addWidget(self.lbl_startup_status, 1)
        bh.addWidget(self.btn_elevate_admin)
        bl.addLayout(bh)

        br = QtWidgets.QHBoxLayout()
        br.setSpacing(14)
        self.chk_autostart = QtWidgets.QCheckBox(
            self._tr_btn("เปิดโปรแกรมทันทีหลังเปิดเครื่อง (Start with Windows)", "Start Automatically with Windows")
        )
        self.chk_autostart.setObjectName("chk_autostart")
        self.chk_autostart.setChecked(self.cfg.start_with_windows)
        self.chk_autostart.toggled.connect(self._on_autostart_toggled)

        self.chk_autostart_min = QtWidgets.QCheckBox(
            self._tr_btn("เริ่มแบบย่อลง System Tray ทันที", "Start Minimized to Tray on Boot")
        )
        self.chk_autostart_min.setObjectName("chk_autostart_min")
        self.chk_autostart_min.setChecked(self.cfg.autostart_minimized)
        self.chk_autostart_min.toggled.connect(self._on_autostart_min_toggled)

        self.chk_autostart_admin = QtWidgets.QCheckBox(
            self._tr_btn("ใช้สิทธิ์ผู้ดูแลตอนเปิดเครื่อง (ไม่เด้ง UAC — คุมหน้าต่าง Admin ได้)", "Elevated Boot Task (Zero-UAC Admin Startup)")
        )
        self.chk_autostart_admin.setObjectName("chk_autostart_admin")
        self.chk_autostart_admin.setChecked(self.cfg.start_with_windows_admin)
        self.chk_autostart_admin.toggled.connect(self._on_autostart_admin_toggled)

        br.addWidget(self.chk_autostart)
        br.addWidget(self.chk_autostart_min)
        br.addWidget(self.chk_autostart_admin, 1)
        bl.addLayout(br)
        lay.addWidget(boot_card)

        # Top Safety Switches Card
        safe_card = PanelCard()
        sl = QtWidgets.QHBoxLayout(safe_card)
        sl.setContentsMargins(14, 9, 14, 9)
        sl.setSpacing(14)

        self.chk_pause_fs = QtWidgets.QCheckBox(self._tr("หยุดอัตโนมัติเมื่อเล่นเกมเต็มจอ (Fullscreen / Borderless)", "Auto-Pause on Fullscreen Games"))
        self.chk_pause_fs.setChecked(self.cfg.safety.pause_on_fullscreen_game)
        self.chk_pause_bat = QtWidgets.QCheckBox(self._tr("หยุดอัตโนมัติเมื่อใช้แบตเตอรี่", "Auto-Pause on Battery"))
        self.chk_pause_bat.setChecked(self.cfg.safety.pause_on_battery)
        self.chk_auto_game = QtWidgets.QCheckBox(self._tr_btn("ยกเว้นเกมและ Anti-Cheat อัตโนมัติ", "Auto-Exclude Games & Anti-Cheat"))
        self.chk_auto_game.setChecked(self.cfg.safety.auto_exclude_games)
        self.chk_suppress_min = QtWidgets.QCheckBox(self._tr("ปิดแอนิเมชันย่อ/เปิดเดิมของ Windows (กันกระพริบ)", "Suppress Native Win11 Min/Restore Zoom (Zero-Flash)"))
        self.chk_suppress_min.setChecked(self.cfg.safety.suppress_native_min_anim)

        grid_safe = QtWidgets.QGridLayout()
        grid_safe.setSpacing(8)
        for idx, c in enumerate((self.chk_pause_fs, self.chk_pause_bat, self.chk_auto_game, self.chk_suppress_min)):
            c.toggled.connect(self._save_safety_ui)
            grid_safe.addWidget(c, idx // 2, idx % 2)
        sl.addLayout(grid_safe, 1)

        self.btn_emergency = QtWidgets.QPushButton(self._tr("กู้คืนทุกหน้าต่างทันที", "Emergency Restore All"))
        self.btn_emergency.setObjectName("btn_emergency_restore")
        self.btn_emergency.setProperty("danger", "true")
        self.btn_emergency.setToolTip(
            self._tr("คืนค่าความโปร่งใส/ปักหมุด/ม้วนเก็บของทุกหน้าต่างทันที (คีย์ลัดฉุกเฉิน: Ctrl+Alt+Shift+R)", "Restore all window opacity/pin/rollup states immediately (Global Panic Key: Ctrl+Alt+Shift+R).")
        )
        self.btn_emergency.clicked.connect(self._on_emergency_restore)
        sl.addWidget(self.btn_emergency)
        lay.addWidget(safe_card)

        # Running Windows Table Header + Actions (including Offline Exclusions Manager)
        hdr = QtWidgets.QHBoxLayout()
        hdr.setSpacing(8)
        self.lbl_tbl = QtWidgets.QLabel(self._tr("หน้าต่างที่กำลังทำงานอยู่ (จำแนกหมวดหมู่อัตโนมัติ)", "RUNNING WINDOWS & DOMAIN VERDICTS"))
        self.lbl_tbl.setFont(ATLAS.font_for("mono", 8.5, text=self.lbl_tbl.text(), bold=True))
        self.lbl_tbl.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent;")
        self.btn_manage_excluded = QtWidgets.QPushButton(
            self._tr("จัดการรายการยกเว้น...", "Manage Exclusions...")
        )
        self.btn_manage_excluded.setObjectName("btn_manage_excluded")
        self.btn_manage_excluded.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_manage_excluded.clicked.connect(self._open_excluded_exes_dialog)
        self.btn_ref = QtWidgets.QPushButton(self._tr("รีเฟรชรายการ", "Refresh Windows"))
        self.btn_ref.setObjectName("btn_refresh_apps")
        self.btn_ref.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_ref.clicked.connect(self._refresh_apps_table)
        hdr.addWidget(self.lbl_tbl, 1)
        hdr.addWidget(self.btn_manage_excluded)
        hdr.addWidget(self.btn_ref)
        lay.addLayout(hdr)

        self.apps_table = QtWidgets.QTableWidget(0, 6)
        self._apply_apps_table_headers()
        self.apps_table.setAlternatingRowColors(True)
        self.apps_table.verticalHeader().setVisible(False)
        self.apps_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.apps_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        hh = self.apps_table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QtWidgets.QHeaderView.Fixed)
        self.apps_table.setColumnWidth(5, 296)
        lay.addWidget(self.apps_table, 1)
        return page

    def _apply_apps_table_headers(self) -> None:
        if self._lang == "th":
            headers = ["แอปพลิเคชัน", "โปรเซส", "หมวดหมู่", "คำแนะนำ FX", "สถานะ", "คำสั่งด่วน"]
        else:
            headers = ["APPLICATION", "PROCESS", "CATEGORY", "FX VERDICT", "STATUS", "QUICK ACTIONS"]
        self.apps_table.setHorizontalHeaderLabels(headers)

    # ------------------------------------------------------------------
    # Navigation, Reactivity & Actions
    # ------------------------------------------------------------------
    def _select_page(self, key: str) -> None:
        self._active_page = key
        self._stack.setCurrentIndex(self._page_map[key])
        for k, btn in self._nav_buttons.items():
            active = k == key
            btn.setStyleSheet(
                f"QPushButton {{ text-align: left; padding-left: 16px; border: none; "
                f"border-left: 3px solid {TOKENS['accent'] if active else 'transparent'}; "
                f"background: {TOKENS['bg_selected'] if active else 'transparent'}; "
                f"color: {TOKENS['ink_primary'] if active else TOKENS['ink_secondary']}; }}"
                f"QPushButton:hover {{ background: {TOKENS['bg_hover']}; color: {TOKENS['ink_primary']}; }}"
            )
        # Pause inactive GPU stages & keep unfocused window at 0 FPS!
        if self.isVisible() and not self.isMinimized() and self.isActiveWindow():
            self._wake_active_stage()
        else:
            self._pause_all_stages()
        if key == "apps":
            self._refresh_apps_table()

    def _select_trigger(self, tid: str) -> None:
        self._active_trigger = tid
        self._sync_trigger_controls()
        self._refresh_animations_grid()

    def _select_category(self, cid: str) -> None:
        self._active_category = cid
        for k, b in self._cat_buttons.items():
            b.setProperty("selected", "true" if k == cid else "false")
            b.style().unpolish(b)
            b.style().polish(b)
        self._refresh_animations_grid()

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text.strip().lower()
        self._refresh_animations_grid()

    def _sync_trigger_controls(self) -> None:
        for k, b in self._trigger_buttons.items():
            b.setProperty("selected", "true" if k == self._active_trigger else "false")
            b.style().unpolish(b)
            b.style().polish(b)

        tc = self.cfg.triggers[self._active_trigger]
        self.chk_trig_enabled.blockSignals(True)
        self.chk_trig_enabled.setChecked(tc.enabled)
        self.chk_trig_enabled.blockSignals(False)

        self.sl_trig_dur.set_value(tc.duration_ms)
        self.sl_trig_int.set_value(int(round(tc.intensity * 100)))

        self.cmb_easing.blockSignals(True)
        ei = self.cmb_easing.findData(tc.easing)
        if ei >= 0:
            self.cmb_easing.setCurrentIndex(ei)
        self.cmb_easing.blockSignals(False)

        spec = self.engine.registry.get(tc.effect_id)
        eff_txt = f"{spec.name_th} // {spec.name_en}" if self._lang == "th" else spec.name_en
        self.lbl_lab_effect.setText(eff_txt)
        self.lbl_lab_effect.setFont(ATLAS.font_for("display", 11.5, text=eff_txt, bold=True))
        self.lab_stage.set_effect(
            spec,
            duration_ms=tc.duration_ms,
            easing=tc.easing,
            intensity=tc.intensity,
            accent_hex=tc.accent_hex,
            reverse=(self._active_trigger in ("open", "restore", "menu", "popup")),
        )
        self._update_lab_dir_button()
        self._sync_accent_swatch_highlight(tc.accent_hex)
        self._sync_dependent_controls()
        self._sync_glsl_error_log(spec)

    def _sync_accent_swatch_highlight(self, active_hex: str) -> None:
        target = (active_hex or "").strip().upper()
        for hx, btn in self._accent_swatch_btns.items():
            if hx == target:
                btn.setStyleSheet(f"background: {hx}; border: 2px solid #FFFFFF;")
            else:
                btn.setStyleSheet(f"background: {hx}; border: 1px solid #444B5E;")

    def _sync_focus_swatch_highlight(self) -> None:
        target = (self.cfg.focus.spotlight_color_hex or "").strip().upper()
        for hx, btn in self._spot_swatch_btns.items():
            if hx == target:
                btn.setStyleSheet(f"background: {hx}; border: 2px solid #FFFFFF;")
            else:
                btn.setStyleSheet(f"background: {hx}; border: 1px solid #475066;")

    def _sync_glsl_error_log(self, spec: Optional[EffectSpec] = None) -> None:
        if not hasattr(self, "glsl_error_panel"):
            return
        if spec is None:
            spec = self.engine.registry.get(self.cfg.triggers[self._active_trigger].effect_id)
        if spec.is_custom and spec.custom_frag_path:
            err = OffscreenGpuEngine.shared().compile_check_custom_shader(spec.custom_frag_path)
            if err:
                self.txt_glsl_error.setPlainText(err)
                self.glsl_error_panel.show()
                return
        self.txt_glsl_error.clear()
        self.glsl_error_panel.hide()

    def _sync_dependent_controls(self) -> None:
        """Disable child sliders/checkboxes when parent switches are OFF and enforce Strict Clamp vs Bounce (PFX-M09)."""
        # Page 02 Animations Lab trigger sub-controls
        trig_on = self.chk_trig_enabled.isChecked()
        self.sl_trig_dur.setEnabled(trig_on)
        self.sl_trig_int.setEnabled(trig_on)
        self.cmb_easing.setEnabled(trig_on)
        for sb in self._accent_swatch_btns.values():
            sb.setEnabled(trig_on)

        # Page 03 Physics dependent controls
        wob_on = self.chk_wobbly.isChecked()
        for s in (self.sl_wob_stiff, self.sl_wob_damp, self.sl_wob_amp):
            s.setEnabled(wob_on)

        mom_on = self.chk_momentum.isChecked()
        strict_clamp = self.chk_clamp.isChecked()
        self.chk_clamp.setEnabled(mom_on)
        self.sl_friction.setEnabled(mom_on)
        self.chk_bounce.setEnabled(mom_on and not strict_clamp)
        self.lbl_clamp_note.setVisible(mom_on and strict_clamp)

        self.sl_drag_op.setEnabled(self.chk_drag_alpha.isChecked())
        self.sl_smooth_max_ms.setEnabled(self.chk_smooth_max.isChecked())

        snap_any = self.chk_flicks.isChecked() or self.chk_edge_snap.isChecked()
        for cb in self._snap_combos.values():
            cb.setEnabled(snap_any)

        # Page 04 Focus dependent controls
        spot_on = self.chk_spotlight.isChecked()
        self.cmb_spot_mode.setEnabled(spot_on)
        self.sl_spot_op.setEnabled(spot_on)
        for sb in self._spot_swatch_btns.values():
            sb.setEnabled(spot_on)
        self.sl_inactive_op.setEnabled(self.chk_inactive_fade.isChecked())
        self.sl_automin_min.setEnabled(self.chk_automin.isChecked())

    def _refresh_animations_grid(self) -> None:
        while self._anim_grid.count():
            item = self._anim_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self._effect_cards.clear()

        current_eid = self.cfg.triggers[self._active_trigger].effect_id
        items = self.engine.registry.by_category(self._active_category)
        if self._search_text:
            q = self._search_text
            items = [
                e for e in items
                if (
                    q in e.name_en.lower()
                    or q in e.name_th.lower()
                    or q in e.desc_en.lower()
                    or q in e.desc_th.lower()
                    or q in e.id.lower()
                )
            ]

        for idx, spec in enumerate(items):
            card = EffectCard(spec, lang=self._lang, is_selected=(spec.id == current_eid))
            card.selected.connect(self._on_effect_card_selected)
            self._effect_cards[spec.id] = card
            self._anim_grid.addWidget(card, idx // 2, idx % 2)

        self._anim_grid.setRowStretch((len(items) + 1) // 2 + 1, 1)

    def _on_effect_card_selected(self, effect_id: str) -> None:
        tc = self.cfg.triggers[self._active_trigger]
        old_spec = self.engine.registry.get(tc.effect_id)
        spec = self.engine.registry.get(effect_id)
        tc.effect_id = effect_id
        # PFX-M08: preserve user-tuned Duration and Accent when comparing shaders!
        if (
            self._active_trigger not in self._custom_duration_triggers
            and tc.duration_ms == old_spec.default_ms
        ):
            tc.duration_ms = spec.default_ms
        if (
            self._active_trigger not in self._custom_accent_triggers
            and tc.accent_hex.strip().upper() == old_spec.accent_hex.strip().upper()
        ):
            tc.accent_hex = spec.accent_hex
        self._push_config()
        for eid, card in self._effect_cards.items():
            card.set_selected(eid == effect_id)
        self._sync_trigger_controls()

    def _apply_effect_recommended_defaults(self) -> None:
        tc = self.cfg.triggers[self._active_trigger]
        spec = self.engine.registry.get(tc.effect_id)
        tc.duration_ms = spec.default_ms
        tc.accent_hex = spec.accent_hex
        tc.intensity = 1.0
        self._custom_duration_triggers.discard(self._active_trigger)
        self._custom_accent_triggers.discard(self._active_trigger)
        self._push_config()
        self._sync_trigger_controls()
        self.show_toast(
            self._tr(
                f"รีเซ็ตค่าแนะนำสำหรับ '{spec.name_th}' แล้ว ({spec.default_ms} ms)",
                f"Applied recommended defaults for '{spec.name_en}' ({spec.default_ms} ms).",
            ),
            kind="info",
            duration_ms=3000,
        )

    def _on_trigger_duration_changed(self, *_args) -> None:
        self._custom_duration_triggers.add(self._active_trigger)
        self._on_trigger_params_changed()

    def _on_trigger_params_changed(self, *_args) -> None:
        tc = self.cfg.triggers[self._active_trigger]
        tc.enabled = self.chk_trig_enabled.isChecked()
        tc.duration_ms = self.sl_trig_dur.slider.value()
        tc.intensity = self.sl_trig_int.slider.value() / 100.0
        tc.easing = str(self.cmb_easing.currentData() or "spring_smooth")
        self._push_config()
        self._sync_dependent_controls()
        spec = self.engine.registry.get(tc.effect_id)
        self.lab_stage.set_effect(
            spec,
            duration_ms=tc.duration_ms,
            easing=tc.easing,
            intensity=tc.intensity,
            accent_hex=tc.accent_hex,
            reverse=(self._active_trigger in ("open", "restore", "menu", "popup")),
        )
        self._update_lab_dir_button()

    def _set_trigger_accent(self, hex_color: str) -> None:
        self._custom_accent_triggers.add(self._active_trigger)
        self.cfg.triggers[self._active_trigger].accent_hex = hex_color
        self._push_config()
        self._sync_trigger_controls()

    def _update_lab_dir_button(self) -> None:
        txt = (
            self._tr("ทิศทาง: เปิด (OPEN) ⇄", "DIR: OPEN ⇄")
            if self.lab_stage._reverse
            else self._tr("ทิศทาง: ปิด (CLOSE) ⇄", "DIR: CLOSE ⇄")
        )
        self.btn_lab_dir.setText(txt)
        self.btn_lab_dir.setFont(ATLAS.font_for("body", 8.5, text=txt, bold=True))

    def _flip_lab_direction(self) -> None:
        self.lab_stage.set_direction(not self.lab_stage._reverse)
        self._update_lab_dir_button()

    def _on_master_toggled(self, enabled: bool) -> None:
        self.cfg.enabled = enabled
        self._push_config()
        self._sync_tray_pause_action()
        self._sync_battery_banner(self.engine.paused_reason())

    def _on_tray_toggle_engine(self) -> None:
        new_state = not self.cfg.enabled
        self.cfg.enabled = new_state
        self.master_toggle.set_on(new_state, emit=False)
        self._push_config()
        self._sync_tray_pause_action()
        self._sync_battery_banner(self.engine.paused_reason())
        self.show_toast(
            self._tr(
                "เปิดใช้งานเอฟเฟกต์ PrismFX แล้ว" if new_state else "หยุดเอฟเฟกต์ PrismFX ชั่วคราวแล้ว",
                "PrismFX effects enabled." if new_state else "PrismFX effects paused.",
            ),
            kind="ok" if new_state else "warn",
            duration_ms=3200,
        )

    def _sync_tray_pause_action(self) -> None:
        if self._act_tray_toggle_engine is None:
            return
        if self.cfg.enabled:
            self._act_tray_toggle_engine.setText(self._tr("หยุดเอฟเฟกต์ชั่วคราว (Pause Effects)", "Pause Effects"))
        else:
            self._act_tray_toggle_engine.setText(self._tr("เปิดใช้งานเอฟเฟกต์ (Enable Effects)", "Enable Effects"))

    def _on_global_speed(self, val: int) -> None:
        self.cfg.global_speed_scale = val / 100.0
        self._push_config()

    def _select_home_showcase_effect(self, effect_id: str) -> None:
        spec = self.engine.registry.get(effect_id)
        self._home_preview_effect_id = spec.id
        self.home_stage.set_effect(spec)
        for btn, eid, _th, _en in self._home_quick_btns:
            btn.setProperty("selected", "true" if eid == spec.id else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _apply_preset(self, name: str) -> None:
        self.cfg = self.engine.store.apply_preset(self.cfg, name)
        self._custom_duration_triggers.clear()
        self._custom_accent_triggers.clear()
        self._push_config()
        self._sync_all_controls_from_cfg()
        self._rebuild_preset_buttons()
        self._sync_trigger_controls()
        self._refresh_animations_grid()
        self._select_home_showcase_effect(self.cfg.triggers["close"].effect_id)

    def _save_custom_preset_dialog(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(
            self,
            self._tr("บันทึกพรีเซตใหม่", "Save Custom Preset"),
            self._tr("ตั้งชื่อพรีเซตของคุณ:", "Preset name:"),
        )
        if ok and name.strip():
            clean = name.strip()
            builtin_lower = {b.lower() for b in BUILTIN_PRESETS}
            if clean.lower() in builtin_lower:
                self.show_toast(
                    self._tr(
                        f"ไม่สามารถใช้ชื่อพรีเซตระบบ '{clean}' ได้ กรุณาตั้งชื่ออื่น",
                        f"Cannot overwrite built-in preset name '{clean}'. Please choose a different name.",
                    ),
                    kind="warn",
                    duration_ms=5000,
                )
                return
            out = self.engine.store.save_user_preset(clean, self.cfg)
            if out is not None:
                saved_name = out.name.removesuffix(".prismpreset.json")
                self.cfg.active_preset = saved_name
                self._push_config()
                self._rebuild_preset_buttons()
                self.show_toast(
                    self._tr(f"บันทึกพรีเซต '{saved_name}' เรียบร้อยแล้ว", f"Saved preset '{saved_name}'."),
                    kind="ok",
                )
            else:
                self.show_toast(
                    self._tr(
                        f"ไม่สามารถบันทึกพรีเซต '{clean}' ได้ (อาจซ้ำกับชื่อระบบหรือมีอักขระไม่รองรับ)",
                        f"Failed to save preset '{clean}' (reserved name or invalid path).",
                    ),
                    kind="warn",
                    duration_ms=5000,
                )

    def _delete_user_preset(self, name: str) -> None:
        ans = QtWidgets.QMessageBox.question(
            self,
            self._tr("ยืนยันการลบพรีเซต", "Delete Saved Preset"),
            self._tr(
                f"ต้องการลบพรีเซต '{name}' ใช่หรือไม่?",
                f"Are you sure you want to delete preset '{name}'?",
            ),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if ans != QtWidgets.QMessageBox.Yes:
            return
        if self.engine.store.delete_user_preset(name):
            if self.cfg.active_preset == name:
                self.cfg.active_preset = "Prism Studio Default"
                self._push_config()
            self._rebuild_preset_buttons()
            self.show_toast(
                self._tr(f"ลบพรีเซต '{name}' เรียบร้อยแล้ว", f"Deleted preset '{name}'."),
                kind="info",
            )
        else:
            self.show_toast(
                self._tr(f"ไม่สามารถลบพรีเซต '{name}' ได้", f"Could not delete preset '{name}'."),
                kind="warn",
            )

    def _reset_all_defaults(self) -> None:
        ans = QtWidgets.QMessageBox.question(
            self,
            self._tr("รีเซ็ตการตั้งค่าทั้งหมด", "Reset All Settings to Defaults"),
            self._tr(
                "รีเซ็ตแอนิเมชัน ฟิสิกส์ โฟกัส และรายการยกเว้นทั้งหมดกลับเป็นค่าเริ่มต้นที่ปลอดภัยใช่หรือไม่? (ภาษาและขนาดหน้าต่างจะคงไว้)",
                "Reset all animations, physics, focus, and exclusions to safe defaults? (Language and window size are preserved.)",
            ),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if ans != QtWidgets.QMessageBox.Yes:
            return
        fresh = PrismConfig()
        fresh.language = self._lang
        fresh.window_geometry = list(self.cfg.window_geometry)
        fresh.start_with_windows = self.cfg.start_with_windows
        fresh.start_with_windows_admin = self.cfg.start_with_windows_admin
        fresh.autostart_minimized = self.cfg.autostart_minimized
        self.cfg = fresh
        self._custom_duration_triggers.clear()
        self._custom_accent_triggers.clear()
        self._push_config()
        self._sync_all_controls_from_cfg()
        self._rebuild_preset_buttons()
        self._sync_trigger_controls()
        self._refresh_animations_grid()
        self._refresh_apps_table()
        self._select_home_showcase_effect(self.cfg.triggers["close"].effect_id)
        self.show_toast(
            self._tr("รีเซ็ตการตั้งค่าทั้งหมดกลับเป็นค่าเริ่มต้นแล้ว", "All settings have been reset to safe defaults."),
            kind="ok",
        )

    def _sync_all_controls_from_cfg(self) -> None:
        """Synchronize all UI checkboxes, sliders, and combos from `self.cfg` without signal loops."""
        self.master_toggle.set_on(self.cfg.enabled, emit=False)
        self.sl_global_speed.set_value(int(round(self.cfg.global_speed_scale * 100)))

        ph = self.cfg.physics
        for cb, val in (
            (self.chk_wobbly, ph.wobbly_enabled),
            (self.chk_momentum, ph.momentum_enabled),
            (self.chk_bounce, ph.momentum_bounce),
            (self.chk_clamp, ph.momentum_strict_clamp),
            (self.chk_drag_alpha, ph.drag_transparency_enabled),
            (self.chk_flicks, ph.flicks_enabled),
            (self.chk_edge_snap, ph.edge_snap_enabled),
            (self.chk_smooth_max, ph.smooth_maximize_enabled),
            (self.chk_dbl_icons, ph.double_click_desktop_icons),
        ):
            cb.blockSignals(True)
            cb.setChecked(bool(val))
            cb.blockSignals(False)
        self.sl_wob_stiff.set_value(int(round(ph.wobbly_stiffness)))
        self.sl_wob_damp.set_value(int(round(ph.wobbly_damping * 100)))
        self.sl_wob_amp.set_value(int(round(ph.wobbly_amplitude * 100)))
        self.sl_friction.set_value(int(round(ph.momentum_friction * 10)))
        self.sl_drag_op.set_value(int(ph.drag_opacity_pct))
        self.sl_smooth_max_ms.set_value(int(ph.smooth_maximize_ms))
        self.wobble_stage.configure_wobble(ph.wobbly_stiffness, ph.wobbly_damping, ph.wobbly_amplitude)

        self.cmb_mid_click.blockSignals(True)
        mi = self.cmb_mid_click.findData(ph.middle_click_titlebar)
        if mi >= 0:
            self.cmb_mid_click.setCurrentIndex(mi)
        self.cmb_mid_click.blockSignals(False)
        for attr, cb in self._snap_combos.items():
            cb.blockSignals(True)
            si = cb.findData(getattr(ph, attr, "left_50"))
            if si >= 0:
                cb.setCurrentIndex(si)
            cb.blockSignals(False)

        fc = self.cfg.focus
        for cb, val in (
            (self.chk_spotlight, fc.spotlight_enabled),
            (self.chk_inactive_fade, fc.inactive_fade_enabled),
            (self.chk_automin, fc.auto_minimize_enabled),
            (self.chk_autohide_icons, fc.desktop_icons_autohide),
            (self.chk_tiles_top, fc.tiles_topmost),
        ):
            cb.blockSignals(True)
            cb.setChecked(bool(val))
            cb.blockSignals(False)
        self.cmb_spot_mode.blockSignals(True)
        smi = self.cmb_spot_mode.findData(fc.spotlight_mode)
        if smi >= 0:
            self.cmb_spot_mode.setCurrentIndex(smi)
        self.cmb_spot_mode.blockSignals(False)
        self.sl_spot_op.set_value(int(fc.spotlight_opacity_pct))
        self.sl_inactive_op.set_value(int(fc.inactive_opacity_pct))
        self.sl_automin_min.set_value(int(fc.auto_minimize_minutes))
        self.sl_tile_size.set_value(int(fc.tile_size_px))
        self._sync_focus_swatch_highlight()

        sf = self.cfg.safety
        for cb, val in (
            (self.chk_pause_fs, sf.pause_on_fullscreen_game),
            (self.chk_pause_bat, sf.pause_on_battery),
            (self.chk_auto_game, sf.auto_exclude_games),
            (self.chk_suppress_min, sf.suppress_native_min_anim),
        ):
            cb.blockSignals(True)
            cb.setChecked(bool(val))
            cb.blockSignals(False)

        self._sync_dependent_controls()
        self._sync_tray_pause_action()
        self._sync_battery_banner(self.engine.paused_reason())

    def _open_random_pool_dialog(self) -> None:
        dlg = RandomPoolDialog(self.engine.registry, self.cfg.random_pool, lang=self._lang, parent=self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            pool = dlg.selected_pool()
            if pool:
                self.cfg.random_pool = pool
                self._push_config()
                self.show_toast(
                    self._tr(
                        f"อัปเดตรายการสุ่มแอนิเมชันแล้ว ({len(pool)} เอฟเฟกต์)",
                        f"Updated Smart Random Pool ({len(pool)} effects).",
                    ),
                    kind="ok",
                )

    def _open_excluded_exes_dialog(self) -> None:
        dlg = ExcludedExesDialog(
            self.cfg.safety.excluded_exes,
            SafetyConfig().excluded_exes,
            lang=self._lang,
            parent=self,
        )
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.cfg.safety.excluded_exes = dlg.excluded_exes()
            self._push_config()
            self._refresh_apps_table()
            self.show_toast(
                self._tr(
                    f"อัปเดตรายการแอปที่ยกเว้นแล้ว ({len(self.cfg.safety.excluded_exes)} รายการ)",
                    f"Updated excluded executables ({len(self.cfg.safety.excluded_exes)} entries).",
                ),
                kind="ok",
            )

    def _open_shaders_folder(self) -> None:
        sdir = self.engine.store.shaders_dir
        try:
            sdir.mkdir(parents=True, exist_ok=True)
            if not self.engine.backend.is_demo:
                os.startfile(str(sdir))  # type: ignore[attr-defined]
            self.show_toast(
                self._tr(f"โฟลเดอร์เชดเดอร์: {sdir}", f"Shaders folder: {sdir}"),
                kind="info",
                duration_ms=5500,
            )
        except OSError as exc:
            _log.warning("Failed to open shaders folder %s: %s", sdir, exc)
            self.show_toast(
                self._tr(f"ไม่สามารถเปิดโฟลเดอร์ได้: {sdir}", f"Could not open folder: {sdir}"),
                kind="danger",
            )

    def _reload_custom_shaders(self) -> None:
        """Invalidate cached custom GLSL programs, rescan shader folders, and display any compile errors."""
        OffscreenGpuEngine.shared().invalidate_custom_shaders()
        user_shaders_dir = self.engine.store.shaders_dir
        shader_dirs: list[pathlib.Path] = []
        # Preserve existing bundle/startup shader dirs first, keeping user shaders_dir LAST so user overrides win
        for existing_d in getattr(self.engine.registry, "_custom_dirs", []):
            if existing_d != user_shaders_dir and existing_d not in shader_dirs:
                shader_dirs.append(existing_d)
        assets_shaders = self._repo_root / "assets" / "shaders"
        if assets_shaders.is_dir() and assets_shaders != user_shaders_dir and assets_shaders not in shader_dirs:
            shader_dirs.append(assets_shaders)
        shader_dirs.append(user_shaders_dir)

        loaded = self.engine.registry.reload_custom_dirs(shader_dirs)
        self.tile_shaders.set_value(f"{self.engine.registry.shader_effect_count()}")
        self._refresh_animations_grid()
        self._sync_trigger_controls()

        # Check all custom shaders for compile/sandbox errors so user gets immediate feedback
        err_shaders: list[tuple[str, str]] = []
        gpu = OffscreenGpuEngine.shared()
        for spec in self.engine.registry.by_category("custom"):
            if spec.custom_frag_path:
                err = gpu.compile_check_custom_shader(spec.custom_frag_path)
                if err:
                    err_shaders.append((spec.name_en, err))

        if err_shaders:
            combined = "\n\n".join(f"[{name}]\n{err}" for name, err in err_shaders)
            self.txt_glsl_error.setPlainText(combined)
            self.glsl_error_panel.show()
            self.show_toast(
                self._tr(
                    f"รีโหลดเชดเดอร์แล้ว ({loaded} รายการ) — พบข้อผิดพลาด GLSL {len(err_shaders)} ไฟล์",
                    f"Reloaded {loaded} custom shader(s) — {len(err_shaders)} GLSL error(s) found.",
                ),
                kind="warn",
                duration_ms=6500,
            )
        else:
            self._sync_glsl_error_log()
            self.show_toast(
                self._tr(
                    f"รีโหลดเชดเดอร์กำหนดเองสำเร็จ ({loaded} รายการ)",
                    f"Reloaded {loaded} custom shader(s) cleanly.",
                ),
                kind="ok",
            )

    def _create_sample_custom_shader(self) -> None:
        """Scaffold a ready-to-edit custom GLSL shader plugin into user shaders folder and reload."""
        sdir = self.engine.store.shaders_dir
        meta = sdir / "prism_wave.prismfx.json"
        glsl = sdir / "prism_wave.glsl"
        created_new = not glsl.exists()
        try:
            sdir.mkdir(parents=True, exist_ok=True)
            if created_new:
                glsl.write_text(
                    "void main() {\n"
                    "    float p = clamp(u_progress, 0.0, 1.0);\n"
                    "    float wave = sin(v_uv.y * 24.0 + p * 12.0) * 0.04 * p * u_intensity;\n"
                    "    vec4 col = texture(u_tex, v_uv + vec2(wave, 0.0));\n"
                    "    col.rgb += u_accent * abs(wave) * 14.0;\n"
                    "    fragColor = vec4(col.rgb, col.a * (1.0 - p));\n"
                    "}\n",
                    encoding="utf-8",
                )
                meta.write_text(
                    '{\n  "id": "prism_wave",\n  "name_en": "Custom Prism Wave",\n'
                    '  "name_th": "คลื่นปริซึมกำหนดเอง",\n  "fragment": "prism_wave.glsl",\n'
                    '  "default_ms": 380,\n  "accent_hex": "#38BDF8"\n}\n',
                    encoding="utf-8",
                )
        except OSError as exc:
            _log.warning("Failed to create sample custom shader in %s: %s", sdir, exc)
            self.show_toast(
                self._tr("ไม่สามารถสร้างไฟล์เชดเดอร์ตัวอย่างได้", "Failed to create sample custom shader."),
                kind="danger",
            )
            return
        self.engine.registry.scan_custom_dir(sdir)
        self.tile_shaders.set_value(f"{self.engine.registry.shader_effect_count()}")
        self._select_category("custom")
        self.show_toast(
            self._tr(
                f"{'สร้าง' if created_new else 'พร้อมใช้งาน'}เชดเดอร์ตัวอย่างที่: {glsl}",
                f"{'Created' if created_new else 'Ready'} sample shader at: {glsl}",
            ),
            kind="ok",
            duration_ms=6000,
        )

    def _save_physics_ui(self, *_args) -> None:
        ph = self.cfg.physics
        ph.wobbly_enabled = self.chk_wobbly.isChecked()
        ph.wobbly_stiffness = float(self.sl_wob_stiff.slider.value())
        ph.wobbly_damping = self.sl_wob_damp.slider.value() / 100.0
        ph.wobbly_amplitude = self.sl_wob_amp.slider.value() / 100.0
        ph.momentum_enabled = self.chk_momentum.isChecked()
        ph.momentum_bounce = self.chk_bounce.isChecked()
        ph.momentum_strict_clamp = self.chk_clamp.isChecked()
        ph.momentum_friction = self.sl_friction.slider.value() / 10.0
        ph.drag_transparency_enabled = self.chk_drag_alpha.isChecked()
        ph.drag_opacity_pct = self.sl_drag_op.slider.value()
        ph.flicks_enabled = self.chk_flicks.isChecked()
        ph.edge_snap_enabled = self.chk_edge_snap.isChecked()
        ph.smooth_maximize_enabled = self.chk_smooth_max.isChecked()
        ph.smooth_maximize_ms = int(self.sl_smooth_max_ms.slider.value())
        ph.middle_click_titlebar = str(self.cmb_mid_click.currentData() or "rollup")
        ph.double_click_desktop_icons = self.chk_dbl_icons.isChecked()
        for attr, cb in self._snap_combos.items():
            setattr(ph, attr, str(cb.currentData() or "left_50"))
        self.wobble_stage.configure_wobble(ph.wobbly_stiffness, ph.wobbly_damping, ph.wobbly_amplitude)
        self._sync_dependent_controls()
        self._push_config()

    def _save_focus_ui(self, *_args) -> None:
        fc = self.cfg.focus
        fc.spotlight_enabled = self.chk_spotlight.isChecked()
        fc.spotlight_mode = str(self.cmb_spot_mode.currentData() or "halo")
        fc.spotlight_opacity_pct = self.sl_spot_op.slider.value()
        fc.inactive_fade_enabled = self.chk_inactive_fade.isChecked()
        fc.inactive_opacity_pct = self.sl_inactive_op.slider.value()
        fc.auto_minimize_enabled = self.chk_automin.isChecked()
        fc.auto_minimize_minutes = self.sl_automin_min.slider.value()
        fc.desktop_icons_autohide = self.chk_autohide_icons.isChecked()
        fc.tiles_topmost = self.chk_tiles_top.isChecked()
        fc.tile_size_px = self.sl_tile_size.slider.value()
        self._sync_dependent_controls()
        self._push_config()

    def _set_spotlight_color(self, hx: str) -> None:
        self.cfg.focus.spotlight_color_hex = hx
        self._sync_focus_swatch_highlight()
        self._push_config()

    def _save_safety_ui(self, *_args) -> None:
        sf = self.cfg.safety
        was_pause_bat = bool(sf.pause_on_battery)
        sf.pause_on_fullscreen_game = self.chk_pause_fs.isChecked()
        sf.pause_on_battery = self.chk_pause_bat.isChecked()
        sf.auto_exclude_games = self.chk_auto_game.isChecked()
        sf.suppress_native_min_anim = self.chk_suppress_min.isChecked()
        if (not was_pause_bat) and sf.pause_on_battery:
            self.engine.set_battery_pause_override(False)
        self._push_config()
        self._sync_battery_banner(self.engine.paused_reason())
        self._refresh_apps_table()

    def _sync_battery_banner(self, reason: Optional[str] = None) -> None:
        if not hasattr(self, "battery_banner"):
            return
        cur_reason = self.engine.paused_reason() if reason is None else str(reason or "")
        self.battery_banner.setVisible(cur_reason == "battery")

    def _on_battery_override_clicked(self) -> None:
        self.engine.set_battery_pause_override(True)
        self._sync_battery_banner(self.engine.paused_reason())
        self.show_toast(
            self._tr(
                "เปิดใช้งานเอฟเฟกต์บนแบตเตอรี่ชั่วคราวแล้ว (จะรีเซ็ตเมื่อเสียบปลั๊กไฟ)",
                "Temporarily enabled effects on Battery power (auto-resets when AC returns).",
            ),
            kind="ok",
            duration_ms=4500,
        )

    @staticmethod
    def _format_app_display_name(friendly_name: str, win_title: str, exe_name: str) -> str:
        """Avoid repeating identical app prefix in Page 05 APPLICATION column (PFX-P02)."""
        fn = (friendly_name or "").strip()
        wt = (win_title or "").strip()
        if not wt:
            return fn or exe_name
        if fn:
            if wt.lower().startswith(fn.lower()):
                return wt[:48]
            return f"{fn} — {wt[:32]}"
        return wt[:48]

    def _refresh_apps_table(self) -> None:
        # Explicitly detach & hide previous cell widgets before rebuilding rows (prevents Qt orphan paint at 0,0)
        for r in range(self.apps_table.rowCount()):
            old_w = self.apps_table.cellWidget(r, 5)
            if old_w is not None:
                self.apps_table.removeCellWidget(r, 5)
                old_w.hide()
                old_w.setParent(None)
                old_w.deleteLater()
        self.apps_table.clearContents()
        self.apps_table.setRowCount(0)

        wins = self.engine.backend.list_windows(own_pid=self.engine.own_pid)
        self.apps_table.setRowCount(len(wins))
        for row, win in enumerate(wins):
            c = win.classification
            excluded = self.engine.is_excluded(win)
            is_pinned = self.engine.is_window_pinned(win.hwnd)
            is_rolled = self.engine.is_window_rolled_up(win.hwnd)
            self.apps_table.setRowHeight(row, 46)

            app_text = self._format_app_display_name(c.friendly_name, win.title, win.exe_name)
            app_item = QtWidgets.QTableWidgetItem(app_text)
            app_item.setToolTip(win.title or c.friendly_name)
            self.apps_table.setItem(row, 0, app_item)
            self.apps_table.setItem(row, 1, QtWidgets.QTableWidgetItem(win.exe_name))
            self.apps_table.setItem(row, 2, QtWidgets.QTableWidgetItem(self._tr(c.category_th, c.category)))
            self.apps_table.setItem(row, 3, QtWidgets.QTableWidgetItem(self._tr(c.reason_th, c.reason_en)))

            st_txt = self._tr("ยกเว้น", "EXCLUDED") if excluded else self._tr("ทำงาน FX", "ACTIVE FX")
            st_item = QtWidgets.QTableWidgetItem(st_txt)
            st_item.setForeground(QtGui.QColor(TOKENS["warn"] if excluded else TOKENS["ok"]))
            self.apps_table.setItem(row, 4, st_item)

            act_w = QtWidgets.QWidget()
            act_w.setStyleSheet("background: transparent;")
            al = QtWidgets.QHBoxLayout(act_w)
            al.setContentsMargins(4, 3, 4, 3)
            al.setSpacing(4)

            compact_btn_css = (
                f"QPushButton {{ padding: 2px 7px; font-size: 11px; background: {TOKENS['bg_elevated']}; "
                f"border: 1px solid {TOKENS['line_strong']}; color: {TOKENS['ink_primary']}; }}"
                f"QPushButton:hover {{ border-color: {TOKENS['accent']}; background: {TOKENS['bg_hover']}; }}"
            )
            active_btn_css = (
                f"QPushButton {{ padding: 2px 7px; font-size: 11px; background: {TOKENS['bg_selected']}; "
                f"border: 1px solid {TOKENS['accent']}; color: {TOKENS['accent_hi']}; font-weight: 600; }}"
                f"QPushButton:hover {{ border-color: {TOKENS['accent_hi']}; }}"
            )
            btn_ex = QtWidgets.QPushButton(self._tr("อนุญาต", "Allow") if excluded else self._tr("ยกเว้น", "Exclude"))
            btn_ex.setFixedHeight(30)
            btn_ex.setFont(ATLAS.font_for("body", 8.5, text=btn_ex.text()))
            btn_ex.setStyleSheet(compact_btn_css)
            btn_ex.clicked.connect(lambda _=False, exe=win.exe_name: self._toggle_exclude_exe(exe))

            btn_pin = QtWidgets.QPushButton(self._tr("ปักหมุด", "Pin"))
            btn_pin.setFixedHeight(30)
            btn_pin.setFont(ATLAS.font_for("body", 8.5, text=btn_pin.text()))
            btn_pin.setStyleSheet(active_btn_css if is_pinned else compact_btn_css)
            btn_pin.clicked.connect(lambda _=False, h=win.hwnd: self._on_table_pin_clicked(h))

            btn_roll = QtWidgets.QPushButton(self._tr("ม้วนเก็บ", "Roll"))
            btn_roll.setFixedHeight(30)
            btn_roll.setFont(ATLAS.font_for("body", 8.5, text=btn_roll.text()))
            btn_roll.setStyleSheet(active_btn_css if is_rolled else compact_btn_css)
            btn_roll.clicked.connect(lambda _=False, h=win.hwnd: self._on_table_roll_clicked(h))

            btn_fx = QtWidgets.QPushButton(self._tr("ทดสอบ ▶", "FX ▶"))
            btn_fx.setFixedHeight(30)
            btn_fx.setFont(ATLAS.font_for("body", 8.5, text=btn_fx.text()))
            btn_fx.setStyleSheet(compact_btn_css)
            btn_fx.clicked.connect(lambda _=False, h=win.hwnd: self.engine.trigger_effect_for_window(h, "preview"))

            for b in (btn_ex, btn_pin, btn_roll, btn_fx):
                al.addWidget(b)
            self.apps_table.setCellWidget(row, 5, act_w)

    def _on_table_pin_clicked(self, hwnd: int) -> None:
        self.engine.toggle_pin_window(hwnd)
        self._refresh_apps_table()

    def _on_table_roll_clicked(self, hwnd: int) -> None:
        self.engine.toggle_rollup_window(hwnd)
        self._refresh_apps_table()

    def _toggle_exclude_exe(self, exe: str) -> None:
        low = exe.lower()
        existing = [x for x in self.cfg.safety.excluded_exes if x.lower() == low]
        if existing:
            self.cfg.safety.excluded_exes = [x for x in self.cfg.safety.excluded_exes if x.lower() != low]
        else:
            self.cfg.safety.excluded_exes.append(exe)
        self._push_config()
        self._refresh_apps_table()

    def _pick_real_target_hwnd(self, allow_self_fallback: bool = False) -> int:
        own_hwnd = int(self.winId())
        own_pid = int(getattr(self.engine, "own_pid", 0) or 0)

        def _is_valid_target(hwnd: int) -> bool:
            if not hwnd or hwnd == own_hwnd or self.engine.is_own_hwnd(hwnd):
                return False
            info = self.engine.backend.get_window_info(hwnd)
            if info is None or not info.visible:
                return False
            if own_pid and info.pid == own_pid:
                return False
            if is_protected_window_class(info.class_name):
                return False
            return True

        fg = int(self.engine.backend.get_foreground_hwnd() or 0)
        if _is_valid_target(fg):
            return fg
        last_fg = int(getattr(self.engine, "_last_real_fg_hwnd", 0) or 0)
        if _is_valid_target(last_fg):
            return last_fg
        wins = self.engine.backend.list_windows(own_pid=own_pid)
        for w in wins:
            if w.hwnd != own_hwnd and (not self.engine.is_own_hwnd(w.hwnd)) and (not is_protected_window_class(w.class_name)):
                return w.hwnd
        return own_hwnd if allow_self_fallback else 0

    def _test_effect_on_real_window(self, eid: str) -> None:
        if eid in ("none", "random"):
            eid = "ember_burn"
        target = self._pick_real_target_hwnd(allow_self_fallback=True)
        self.engine.trigger_effect_for_window(target, "preview", override_effect_id=eid, reverse_override=False)

    def _test_home_preview_on_real_window(self) -> None:
        """Play the exact effect currently displayed on Home Stage (PFX-H13 / UX-03)."""
        eid = self._home_preview_effect_id or self.cfg.triggers["close"].effect_id
        self._test_effect_on_real_window(eid)

    def _test_active_effect_on_real_window(self) -> None:
        eid = self.cfg.triggers[self._active_trigger].effect_id
        self._test_effect_on_real_window(eid)

    def _open_quick_controls(self) -> None:
        target = self._pick_real_target_hwnd(allow_self_fallback=False)
        if target:
            self.quick_bar.retranslate(self._lang)
            self.quick_bar.show_for_window(target)
        else:
            self.show_toast(
                self._tr("ไม่พบหน้าต่างเป้าหมายสำหรับแถบควบคุมด่วน", "No target window available for Quick Bar."),
                kind="info",
                duration_ms=3000,
            )

    def _on_emergency_restore(self) -> None:
        if self.fx_overlay is not None:
            self.fx_overlay.cancel_all_active()
        if self.spotlight_overlay is not None:
            self.spotlight_overlay.hide()
        self._restore_all_tiles()
        restored = self.engine.restore_all_os_state()
        failed = int(getattr(self.engine, "last_restore_failed_count", 0) or 0)
        self.tile_journal.set_value(
            self._tr("กู้คืนแล้ว", "RESTORED"),
            self._tr(f"คืนค่า {restored} รายการเรียบร้อย", f"{restored} items reset cleanly"),
        )
        if failed > 0:
            self.show_toast(
                self._tr(
                    f"กู้คืนหน้าต่างสำเร็จ {restored} รายการ (ล้มเหลว {failed} รายการ — บางหน้าต่างอาจต้องใช้สิทธิ์ Admin)",
                    f"Restored {restored} item(s), {failed} failed (elevated windows may require Run as Admin).",
                ),
                kind="warn",
                duration_ms=6500,
            )
        else:
            self.show_toast(
                self._tr(
                    f"กู้คืนทุกหน้าต่างเรียบร้อยแล้ว ({restored} รายการ)",
                    f"Emergency Restore complete ({restored} item(s) restored cleanly).",
                ),
                kind="ok",
                duration_ms=4500,
            )

    def _on_global_panic_restore(self) -> None:
        """Global Panic Hotkey (Ctrl+Alt+Shift+R): Restore ALL windows immediately + pause engine + notify (PFX-C10)."""
        _log.warning("Global Panic Restore & Pause triggered (Ctrl+Alt+Shift+R)")
        if self.fx_overlay is not None:
            self.fx_overlay.cancel_all_active()
        if self.spotlight_overlay is not None:
            self.spotlight_overlay.hide()
        self._restore_all_tiles()
        restored = self.engine.restore_all_os_state()
        failed = int(getattr(self.engine, "last_restore_failed_count", 0) or 0)

        self.cfg.enabled = False
        self.master_toggle.set_on(False, emit=False)
        self._push_config()
        self._sync_tray_pause_action()
        self._sync_battery_banner(self.engine.paused_reason())

        self.tile_journal.set_value(
            self._tr("หยุดฉุกเฉิน", "PANIC PAUSED"),
            self._tr(f"คืนค่า {restored} รายการและหยุด Engine แล้ว", f"Restored {restored} item(s) & paused"),
        )

        if failed > 0:
            msg = self._tr(
                f"กู้คืนฉุกเฉิน {restored} รายการและหยุดเอฟเฟกต์แล้ว (ล้มเหลว {failed} รายการ — อาจต้องใช้สิทธิ์ Admin)",
                f"Panic Restore recovered {restored} item(s) & paused engine ({failed} failed — Admin may be required).",
            )
            toast_kind = "warn"
        else:
            msg = self._tr(
                f"กู้คืนทุกหน้าต่าง ({restored} รายการ) และหยุดเอฟเฟกต์ชั่วคราวเรียบร้อยแล้ว",
                f"Panic Restore complete ({restored} item(s)) — PrismFX engine paused.",
            )
            toast_kind = "ok"

        self.show_toast(msg, kind=toast_kind, duration_ms=6500)
        if self._tray is not None and self._tray.isVisible():
            try:
                self._tray.showMessage(
                    self._tr("PrismFX: กู้คืนฉุกเฉินและหยุดเอฟเฟกต์แล้ว", "PrismFX: Panic Restore & Pause"),
                    msg,
                    QtWidgets.QSystemTrayIcon.Warning if failed > 0 else QtWidgets.QSystemTrayIcon.Information,
                    5000,
                )
            except Exception as exc:
                _log.debug("Tray panic notification failed: %s", exc)

    # ------------------------------------------------------------------
    # Windows Startup & Administrator Privileges
    # ------------------------------------------------------------------
    def _sync_startup_ui(self, status_note: str = "") -> None:
        st = self._startup_status
        self._is_admin = st.is_admin
        elev_allowed = bool(getattr(st, "elevated_task_allowed", False))
        elev_reason = str(getattr(st, "elevated_task_block_reason", "") or "")

        # Sync checkboxes without feedback loops
        for cb, val in (
            (self.chk_autostart, self.cfg.start_with_windows),
            (self.chk_side_autostart, self.cfg.start_with_windows),
            (self.chk_autostart_min, self.cfg.autostart_minimized),
            (self.chk_autostart_admin, self.cfg.start_with_windows_admin),
        ):
            cb.blockSignals(True)
            cb.setChecked(bool(val))
            cb.blockSignals(False)

        # PFX-C01 (Board Decision #3): disallow enabling Zero-UAC Elevated Boot Task in user-writable/Portable dirs
        if not elev_allowed and st.mode != "elevated_task":
            self.chk_autostart_admin.setEnabled(False)
            tip = self._tr(
                "โหมด Zero-UAC Admin ต้องติดตั้งแบบ Standalone EXE ในโฟลเดอร์ที่ป้องกันสิทธิ์ (เช่น Program Files) — โหมด Portable ใช้ได้เฉพาะ Standard Auto-Start",
                "Zero-UAC Admin Boot requires a Standalone EXE installed in an Admin-protected folder (e.g. Program Files). Portable mode supports Standard Auto-Start only.",
            )
            if elev_reason:
                tip = f"{tip}\n({elev_reason})"
            self.chk_autostart_admin.setToolTip(tip)
        else:
            self.chk_autostart_admin.setEnabled(True)
            if not elev_allowed and st.mode == "elevated_task":
                self.chk_autostart_admin.setToolTip(
                    self._tr(
                        "พบ Elevated Task เดิมในโหมด Portable — สามารถยกเลิกเพื่อสลับไปใช้ Standard Auto-Start ที่ปลอดภัยได้",
                        "Existing elevated task detected in Portable mode — uncheck to migrate to safe Standard Auto-Start.",
                    )
                )
            else:
                self.chk_autostart_admin.setToolTip(
                    self._tr(
                        "เปิดโปรแกรมอัตโนมัติพร้อมสิทธิ์ผู้ดูแลผ่าน Task Scheduler (ไม่เด้ง UAC)",
                        "Start automatically with Administrator privileges via Task Scheduler (Zero-UAC).",
                    )
                )

        if self._act_tray_autostart is not None:
            self._act_tray_autostart.blockSignals(True)
            self._act_tray_autostart.setChecked(bool(self.cfg.start_with_windows))
            self._act_tray_autostart.blockSignals(False)

        # Privilege badges
        if self._is_admin:
            self.lbl_side_priv.setText("● ADMIN")
            self.lbl_side_priv.setStyleSheet(f"color: {TOKENS['ok']}; border: none; background: transparent;")
            self.lbl_priv_badge.setText("● ADMIN / ELEVATED")
            self.lbl_priv_badge.setStyleSheet(
                f"color: {TOKENS['ok']}; background: #0E2219; border: 1px solid {TOKENS['ok']}; padding: 1px 6px;"
            )
            side_txt = self._tr_btn("✓ สิทธิ์ผู้ดูแลพร้อมใช้", "✓ Admin Mode Active")
            self.btn_side_admin.setText(side_txt)
            self.btn_side_admin.setFont(ATLAS.font_for("body", 8.6, text=side_txt, bold=True))
            self.btn_side_admin.setProperty("accent", "false")
            self.btn_side_admin.setStyleSheet(
                f"QPushButton {{ background: #0E2219; border: 1px solid {TOKENS['ok']}; color: {TOKENS['ok']}; padding: 2px 6px; }}"
                f"QPushButton:hover {{ background: #133024; }}"
            )
            adm_txt = self._tr_btn("✓ กำลังทำงานด้วยสิทธิ์ผู้ดูแลระบบ", "✓ Running as Administrator")
            self.btn_elevate_admin.setText(adm_txt)
            self.btn_elevate_admin.setFont(ATLAS.font_for("body", 9.0, text=adm_txt, bold=True))
            self.btn_elevate_admin.setEnabled(False)
            self.btn_elevate_admin.setProperty("accent", "false")
            self.btn_elevate_admin.setStyleSheet(
                f"QPushButton {{ background: #0E2219; border: 1px solid {TOKENS['ok']}; color: {TOKENS['ok']}; padding: 3px 10px; }}"
            )
        else:
            self.lbl_side_priv.setText("◐ STANDARD")
            self.lbl_side_priv.setStyleSheet(f"color: {TOKENS['warn']}; border: none; background: transparent;")
            self.lbl_priv_badge.setText("◐ STANDARD USER")
            self.lbl_priv_badge.setStyleSheet(
                f"color: {TOKENS['warn']}; background: #261E0F; border: 1px solid {TOKENS['warn']}; padding: 1px 6px;"
            )
            side_txt = self._tr_btn("ยกระดับสิทธิ์ Admin", "Run as Admin")
            self.btn_side_admin.setText(side_txt)
            self.btn_side_admin.setFont(ATLAS.font_for("body", 9.0, text=side_txt, bold=True))
            self.btn_side_admin.setStyleSheet(
                f"QPushButton {{ background: {TOKENS['accent']}; border: 1px solid {TOKENS['accent_hi']}; "
                f"color: #FFFFFF; padding: 3px 8px; font-weight: 600; }}"
                f"QPushButton:hover {{ background: #3B8BFF; }}"
            )

            adm_txt = self._tr_btn("ยกระดับสิทธิ์ผู้ดูแลระบบ (Run as Admin)", "Restart as Administrator")
            self.btn_elevate_admin.setText(adm_txt)
            self.btn_elevate_admin.setFont(ATLAS.font_for("body", 9.2, text=adm_txt, bold=True))
            self.btn_elevate_admin.setEnabled(True)
            self.btn_elevate_admin.setStyleSheet(
                f"QPushButton {{ background: {TOKENS['accent']}; border: 1px solid {TOKENS['accent_hi']}; "
                f"color: #FFFFFF; padding: 4px 12px; font-weight: 600; }}"
                f"QPushButton:hover {{ background: #3B8BFF; }}"
            )

        # Startup mode detail label
        if status_note:
            st_text = status_note
        elif self.cfg.start_with_windows:
            if self.cfg.start_with_windows_admin:
                st_text = self._tr(
                    "สถานะ: เปิดอัตโนมัติพร้อมสิทธิ์ Admin (Task Scheduler — ไม่เด้ง UAC)",
                    "Active: Elevated Logon Task (Zero-UAC Admin Boot)",
                )
            else:
                st_text = self._tr(
                    "สถานะ: เปิดอัตโนมัติพร้อม Windows (HKCU Run)",
                    "Active: Standard Windows Startup (HKCU Run)",
                )
        else:
            st_text = self._tr("สถานะ: ปิดการเปิดอัตโนมัติหลังเปิดเครื่อง", "Disabled: Manual launch only")
        self.lbl_startup_status.setText(st_text)
        self.lbl_startup_status.setFont(ATLAS.font_for("body", 8.8, text=st_text))

    def _apply_startup_change(self, enable: bool, elevated_task: bool) -> None:
        self._startup_user_modified = True
        dry_run = self.engine.backend.is_demo
        hwnd = int(self.winId()) if self.isVisible() else 0
        ok, code, _msg = win_startup.apply_startup_settings(
            self._repo_root,
            enable=enable,
            elevated_task=elevated_task,
            hwnd=hwnd,
            dry_run=dry_run,
        )
        note = ""
        if ok:
            self.cfg.start_with_windows = bool(enable)
            self.cfg.start_with_windows_admin = bool(enable and elevated_task)
            self._push_config()
            self._startup_status = win_startup.query_startup_status(self._repo_root, dry_run=dry_run)
            self.show_toast(
                self._tr(
                    "เปิดการเริ่มต้นพร้อม Windows แล้ว" if enable else "ปิดการเริ่มต้นพร้อม Windows แล้ว",
                    "Windows Auto-Start enabled." if enable else "Windows Auto-Start disabled.",
                ),
                kind="ok",
                duration_ms=3500,
            )
        else:
            # Refresh real OS state so toggles stay 100% honest if user cancelled UAC
            self._startup_status = win_startup.query_startup_status(self._repo_root, dry_run=dry_run)
            if not dry_run:
                self.cfg.start_with_windows = self._startup_status.enabled
                self.cfg.start_with_windows_admin = (self._startup_status.mode == "elevated_task")
            if code == "cancelled":
                note = self._tr("ยกเลิกการขอสิทธิ์ผู้ดูแลระบบ (UAC) — คงค่าเดิมไว้", "UAC cancelled — previous startup setting kept")
            elif code == "blocked_insecure_path":
                note = self._tr(
                    "โหมด Zero-UAC Admin ต้องติดตั้งในโฟลเดอร์ที่ป้องกันสิทธิ์ (เช่น Program Files) — โหมด Portable ใช้ได้เฉพาะ Standard Auto-Start",
                    "Zero-UAC Admin Boot requires an Admin-protected folder (e.g. Program Files) — Portable uses Standard Auto-Start only",
                )
            else:
                note = self._tr("ไม่สามารถอัปเดตการเปิดอัตโนมัติได้ กรุณาลองอีกครั้ง", "Could not update Windows startup entry")
            self.show_toast(note, kind="warn", duration_ms=5500)
        self._sync_startup_ui(status_note=note)

    def _on_side_autostart_toggled(self, checked: bool) -> None:
        elev = bool(checked and self.cfg.start_with_windows_admin and getattr(self._startup_status, "elevated_task_allowed", False))
        self._apply_startup_change(enable=checked, elevated_task=elev)

    def _on_autostart_toggled(self, checked: bool) -> None:
        elev = bool(checked and self.cfg.start_with_windows_admin and getattr(self._startup_status, "elevated_task_allowed", False))
        self._apply_startup_change(enable=checked, elevated_task=elev)

    def _on_autostart_min_toggled(self, checked: bool) -> None:
        self.cfg.autostart_minimized = bool(checked)
        self._push_config()

    def _on_autostart_admin_toggled(self, checked: bool) -> None:
        # Turning ON elevated boot automatically enables startup in one click
        enable = True if checked else self.cfg.start_with_windows
        self._apply_startup_change(enable=enable, elevated_task=checked)

    def _on_side_admin_clicked(self) -> None:
        if self._is_admin:
            self._select_page("apps")
        else:
            self._on_relaunch_as_admin()

    def _on_relaunch_as_admin(self) -> None:
        if self._is_admin:
            return
        extra = ["--profile", self._profile_arg] if self._profile_arg else []
        hwnd = int(self.winId()) if self.isVisible() else 0
        ok, status = win_startup.relaunch_as_admin(
            self._repo_root,
            extra_args=extra,
            hwnd=hwnd,
            dry_run=self.engine.backend.is_demo,
        )
        if not ok:
            note = (
                self._tr("ยกเลิกการยกระดับสิทธิ์ (UAC) — ยังทำงานต่อในโหมดปกติ", "UAC cancelled — continuing in Standard mode")
                if status == "cancelled"
                else self._tr("ไม่สามารถเปิดโหมดผู้ดูแลระบบได้", "Failed to relaunch as Administrator")
            )
            self._sync_startup_ui(status_note=note)
            self.show_toast(note, kind="warn", duration_ms=5000)

    def _restore_all_tiles(self) -> None:
        for hwnd, tw in list(self._tile_windows.items()):
            self.engine.restore_from_tile(hwnd)
            tw.close()
        self._tile_windows.clear()

    def _toggle_language(self) -> None:
        self._lang = "en" if self._lang == "th" else "th"
        self.cfg.language = self._lang
        self._push_config()
        self._retranslate_ui()

    def _retranslate_combo_preserve_data(
        self, combo: QtWidgets.QComboBox, items: list[tuple[str, str]]
    ) -> None:
        """Replace combo box labels cleanly while preserving the currently selected userData."""
        cur_data = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for label, data_val in items:
            combo.addItem(label, data_val)
        idx = combo.findData(cur_data)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _set_btn_text_font(
        self,
        btn: QtWidgets.QAbstractButton,
        th: str,
        en: str,
        pt: float = 9.2,
        bold: bool = False,
        escape_amp: bool = True,
    ) -> None:
        raw = self._tr(th, en)
        btn.setText(self._qt_esc(raw) if escape_amp else raw)
        btn.setFont(ATLAS.font_for("body", pt, text=raw, bold=bold))

    def _set_lbl_text_font(
        self,
        lbl: QtWidgets.QLabel,
        th: str,
        en: str,
        role: str = "body",
        pt: float = 9.0,
        bold: bool = False,
    ) -> None:
        txt = self._tr(th, en)
        lbl.setText(txt)
        lbl.setFont(ATLAS.font_for(role, pt, text=txt, bold=bold))

    def _retranslate_ui(self) -> None:
        """Retranslate 100% of Studio widgets, SectionHeaders, Tray Menu, Quick Bar, and Tiles live (PFX-H13)."""
        # 1) Titlebar
        self._set_btn_text_font(
            self.btn_quick_ctrl, "ควบคุมด่วน (Ctrl+Alt+W)", "Quick Bar (Ctrl+Alt+W)", pt=9.0
        )
        self.master_toggle.set_texts(
            self._tr("เปิดทำงาน", "ENGINE ON"),
            self._tr("หยุดชั่วคราว", "ENGINE OFF"),
        )

        # 2) Sidebar
        self._set_lbl_text_font(self.lbl_nav, "  เมนูหลัก", "  MODULES", role="mono", pt=8.0, bold=True)
        nav_labels = {
            "home": ("01", "หน้าหลักสตูดิโอ", "Studio Home"),
            "animations": ("02", "คลังแอนิเมชัน 42+", "Animations Lab"),
            "physics": ("03", "ฟิสิกส์และการปัด", "Physics & Gestures"),
            "focus": ("04", "โฟกัสและเดสก์ท็อป", "Focus & Desktop"),
            "apps": ("05", "แอปและความปลอดภัย", "Apps & Safety"),
        }
        for k, (n, th, en) in nav_labels.items():
            txt = f"{n}  {self._tr(th, en)}"
            self._nav_buttons[k].setText(self._qt_esc(txt))
            self._nav_buttons[k].setFont(ATLAS.font_for("body", 10.5, text=txt, bold=True))

        self._set_lbl_text_font(self.lbl_st_hdr, "เปิดพร้อมเครื่อง", "STARTUP", role="mono", pt=7.6, bold=True)
        self._set_btn_text_font(self.chk_side_autostart, "เปิดพร้อม Windows", "Start with Windows", pt=8.8)
        self._set_lbl_text_font(self.lbl_arch, "ทำงานนอกโปรเซส", "OUT-OF-PROCESS ENGINE", role="mono", pt=7.8, bold=True)
        self._set_lbl_text_font(
            self.lbl_arch_desc,
            "ปลอดภัย 100% ไม่ฉีด DLL\nไม่ชน Anti-Cheat / เกม",
            "Zero DLL Injection.\nAnti-Cheat & Win11 Safe.",
            role="body",
            pt=8.8,
        )
        self.lbl_arch_desc.setToolTip(
            self._tr(
                "PrismFX ใช้ระบบดักเหตุการณ์ภายนอก (Out-of-process) โดยไม่ฉีดไฟล์เข้าไปในเกมหรือโปรแกรมอื่น จึงปลอดภัยต่อระบบ Anti-Cheat",
                "PrismFX runs 100% out-of-process with zero DLL injection, staying safe with games and Anti-Cheat.",
            )
        )

        # 3) All 5 SectionHeaders
        self._sec_home.set_texts(
            "PRISMFX // NEXT-GEN WINDOWS 11 DESKTOP MOTION",
            self._tr("ศูนย์ควบคุมแอนิเมชันและฟิสิกส์หน้าต่าง 165 Hz", "165 Hz GPU Window Motion & Physics Studio"),
            self._tr(
                "ออกแบบใหม่หมดจดด้วย OpenGL 3.3 Shaders (42+ เอฟเฟกต์) แทนที่ระบบ DX10 เก่า — ปราศจากการฉีด DLL และกู้คืนสถานะอัตโนมัติ",
                "Clean-room OpenGL 3.3 Core engine with 42+ GPU shaders, closed-form spring physics, and crash-safe restoration.",
            ),
        )
        self._sec_animations.set_texts(
            "ANIMATIONS LAB // 42+ GPU SHADERS",
            self._tr("ปรับแต่งแอนิเมชันแยกตามเหตุการณ์ + เชดเดอร์กำหนดเอง", "Per-Trigger GPU Shaders & Custom GLSL Lab"),
        )
        self._sec_physics.set_texts(
            "PHYSICS & GESTURES // COMPIZ SPRING ENGINE",
            self._tr(
                "หน้าต่างเยลลี่, แรงเฉื่อยตอนลาก, ปัดสแนป และการควบคุมแถบหัวเรื่อง",
                "Wobbly Windows, Momentum Inertia, Flicks & Smart Edge Snap",
            ),
        )
        self._sec_focus.set_texts(
            "FOCUS, ATMOSPHERE & DESKTOP CONTROLS",
            self._tr(
                "ไฮไลต์หน้าต่างที่ใช้งาน, ย่ออัตโนมัติ, ไทล์ลอย และจัดการไอคอนเดสก์ท็อป",
                "Spotlight Dimmer, Inactive Fade, Window Tiles & Desktop Icons",
            ),
        )
        self._sec_apps.set_texts(
            "APP CATALOG, EXCLUSIONS & CRASH RECOVERY",
            self._tr(
                "ตรวจสอบหน้าต่างที่เปิดอยู่, สิทธิ์ระบบ/เปิดพร้อมเครื่อง และระบบกู้คืนฉุกเฉิน",
                "Live App Catalog, Windows Startup & Privileges, Smart Exclusions",
            ),
        )

        # 4) Page 01: Home
        self._set_lbl_text_font(
            self.lbl_battery_banner,
            "⚡ หยุดเอฟเฟกต์อัตโนมัติเพราะกำลังใช้แบตเตอรี่เพื่อประหยัดพลังงาน — คุณสามารถเปิดใช้งานบนแบตเตอรี่ชั่วคราวได้",
            "⚡ Effects auto-paused on Battery power to save energy — you can temporarily enable effects on battery.",
            pt=9.2,
            bold=True,
        )
        self._set_btn_text_font(
            self.btn_battery_override, "เปิดใช้บนแบตเตอรี่ชั่วคราว", "Enable on Battery Temporarily", pt=9.0, bold=True
        )
        self.tile_shaders.set_label(self._tr("คลังเชดเดอร์ GPU", "GPU SHADER LIBRARY"))
        self.tile_shaders.set_value(
            f"{self.engine.registry.shader_effect_count()}",
            self._tr("เชดเดอร์ GLSL แท้ + โหลดเพิ่มได้", "Native GLSL + Custom Plugins"),
        )
        self.tile_fps.set_label(self._tr("เฟรมเรตเวทีทดสอบ", "LIVE STAGE PACING"))
        self.tile_played.set_label(self._tr("แอนิเมชันที่เล่นแล้ว", "TRANSITIONS PLAYED"))
        self.tile_played.set_value(
            f"{self.engine.latest_frame().total_animations_played}",
            self._tr("ทำงานนอกโปรเซส (Zero Injection)", "Out-of-process WinEvent hooks"),
        )
        self.tile_journal.set_label(self._tr("ระบบกู้คืนฉุกเฉิน", "CRASH RECOVERY"))
        frame = self.engine.latest_frame()
        self.tile_journal.set_value(
            self._tr("ปกติ", "CLEAN") if frame.journal_pending == 0 else f"{frame.journal_pending}",
            self._tr("พร้อมกู้คืนสถานะอัตโนมัติ", "Auto-recovery armed"),
        )
        self.tile_journal.setToolTip(
            self._tr(
                "บันทึกรายการแก้ไขหน้าต่างชั่วคราวลงไฟล์ เพื่อกู้คืนสถานะเดิมอัตโนมัติหากโปรแกรมปิดกะทันหัน",
                "Crash-safe recovery journal that automatically reverts temporary window mutations after any crash.",
            )
        )
        self._set_lbl_text_font(
            self.lbl_stage,
            "เวทีทดสอบเชดเดอร์สด (คลิกเพื่อเล่นซ้ำ)",
            "LIVE GPU STAGE (CLICK TO REPLAY)",
            role="mono",
            pt=8.3,
            bold=True,
        )
        self._set_btn_text_font(self.btn_home_replay, "เล่นซ้ำ", "Replay", pt=9.0)
        self._set_btn_text_font(self.btn_home_test_real, "ทดสอบบนหน้าต่างจริง", "Test on Real Window", pt=9.0, bold=True)
        for btn, _eid, th_s, en_s in self._home_quick_btns:
            self._set_btn_text_font(btn, th_s, en_s, pt=8.8)
        self._set_lbl_text_font(self.lbl_pr, "ชุดพรีเซตพร้อมใช้", "CURATED PRESETS", role="mono", pt=8.2, bold=True)
        self._set_btn_text_font(self.btn_save_pr, "+ บันทึก", "+ Save", pt=8.6)
        self.btn_save_pr.setToolTip(self._tr("บันทึกการตั้งค่าปัจจุบันเป็นพรีเซตใหม่", "Save current settings as a custom preset"))
        self._set_btn_text_font(self.btn_reset_defaults, "รีเซ็ต", "Reset", pt=8.6)
        self.btn_reset_defaults.setToolTip(self._tr("รีเซ็ตการตั้งค่าทั้งหมดกลับเป็นค่าเริ่มต้น", "Reset all settings to safe defaults"))
        self.sl_global_speed.set_label(self._tr("ความเร็วแอนิเมชันรวม (Global Speed)", "Global Animation Speed"))
        self._rebuild_preset_buttons()

        # 5) Page 02: Animations Lab
        for tid, en, th in TRIGGER_TYPES:
            if tid in self._trigger_buttons:
                self._set_btn_text_font(self._trigger_buttons[tid], th, en, pt=8.6)
        self._set_btn_text_font(self.btn_random_pool, "รายการสุ่ม", "Random Pool", pt=8.6)
        self._set_btn_text_font(self.btn_new_glsl, "+ GLSL", "+ GLSL", pt=8.6)
        self.btn_new_glsl.setToolTip(self._tr("สร้างไฟล์เชดเดอร์ GLSL ตัวอย่าง", "Create a sample custom GLSL shader"))
        for cid, (en, th) in CATEGORIES.items():
            if cid in self._cat_buttons:
                self._set_btn_text_font(self._cat_buttons[cid], th, en, pt=8.5)
        self.ed_search.setPlaceholderText(self._tr("ค้นหาแอนิเมชัน...", "Search 42+ shaders..."))
        self._set_btn_text_font(self.chk_trig_enabled, "เปิดใช้งานเหตุการณ์นี้", "Enable this trigger", pt=9.2)
        self._set_btn_text_font(self.btn_use_effect_defaults, "ค่าแนะนำ", "Defaults", pt=8.4)
        self.btn_use_effect_defaults.setToolTip(
            self._tr(
                "รีเซ็ตเวลา/สี/ความแรงกลับเป็นค่าแนะนำของเชดเดอร์นี้",
                "Reset duration, intensity, and accent to this shader's defaults",
            )
        )
        self.sl_trig_dur.set_label(self._tr("ระยะเวลาแอนิเมชัน", "Duration"))
        self.sl_trig_int.set_label(self._tr("ความแรงเอฟเฟกต์", "Shader Intensity"))
        self._set_lbl_text_font(self.lbl_ease, "เส้นโค้งสปริง (Easing Curve)", "Motion Spring Curve", pt=9.0)
        self.lbl_ease.setToolTip(
            self._tr(
                "กำหนดลักษณะอัตราเร่งและการหยุดของแอนิเมชัน (เช่น สปริงนุ่มนวล หรือ เด้งยืดหยุ่น)",
                "Controls acceleration and settle curve of the transition.",
            )
        )
        self._retranslate_combo_preserve_data(
            self.cmb_easing,
            [(self._tr(f"{th} ({en})", en), cid) for cid, en, th in EASING_CURVES],
        )
        self._set_lbl_text_font(self.lbl_sw_accent, "สีแสงเอฟเฟกต์ (Shader Accent)", "Shader Accent Tint", pt=9.0)
        self._set_btn_text_font(self.btn_try, "▶ ทดสอบเชดเดอร์นี้บนหน้าต่างจริง", "▶ Fire Live Desktop Preview", pt=9.2, bold=True)
        self._set_btn_text_font(self.btn_open_shaders_dir, "เปิดโฟลเดอร์ Shaders", "Open Shaders Folder", pt=8.5)
        self._set_btn_text_font(self.btn_reload_shaders, "รีโหลดเชดเดอร์", "Reload Shaders", pt=8.5)
        self._set_lbl_text_font(
            self.lbl_glsl_err_hdr, "⚠ บันทึกข้อผิดพลาด GLSL", "⚠ GLSL COMPILE ERROR LOG", role="mono", pt=8.0, bold=True
        )
        self._refresh_animations_grid()
        self._sync_trigger_controls()

        # 6) Page 03: Physics & Gestures
        self._set_btn_text_font(
            self.chk_wobbly, "เปิดใช้หน้าต่างเยลลี่ (Wobbly Windows)", "Enable Wobbly Windows (Compiz Physics)", pt=9.5
        )
        self._set_lbl_text_font(
            self.lbl_wob_hint,
            "คลิกลากเขย่าหน้าต่างด้านล่างเพื่อทดสอบ!",
            "GRAB & SHAKE CARD BELOW",
            role="mono",
            pt=8.0,
            bold=True,
        )
        self.sl_wob_stiff.set_label(self._tr("ความตึงสปริง (Stiffness)", "Spring Stiffness"))
        self.sl_wob_damp.set_label(self._tr("ความหน่วงเยลลี่ (Damping)", "Spring Damping"))
        self.sl_wob_amp.set_label(self._tr("ความแรงการสั่น (Amplitude)", "Wobble Amplitude"))
        self._set_lbl_text_font(
            self.lbl_m, "ฟิสิกส์แรงเฉื่อยและการลาก", "MOMENTUM & DRAG PHYSICS", role="mono", pt=8.5, bold=True
        )
        self._set_btn_text_font(
            self.chk_momentum, "แรงเฉื่อยเมื่อโยนหน้าต่าง (Momentum Drag)", "Enable Momentum / Inertia Window Glide", pt=9.2
        )
        self._set_btn_text_font(
            self.chk_bounce, "เด้งกลับเมื่อชนขอบจอ (Elastic Edge Bounce)", "Elastic Bounce off Monitor Borders", pt=9.2
        )
        self._set_btn_text_font(
            self.chk_clamp, "จำกัดหน้าต่างไม่ให้หลุดขอบจอ (Strict Clamp)", "Strictly Clamp Windows to Screen Limits", pt=9.2
        )
        self._set_lbl_text_font(
            self.lbl_clamp_note,
            "ℹ โหมด Strict Clamp จะปิดการเด้งกลับขอบจอ (Elastic Bounce) โดยอัตโนมัติ",
            "ℹ Strict Clamp automatically disables Elastic Edge Bounce.",
            pt=8.4,
        )
        self._set_btn_text_font(
            self.chk_drag_alpha, "โปร่งใสอัตโนมัติขณะลากย้ายหน้าต่าง", "Semi-Transparent While Moving Window", pt=9.2
        )
        self.sl_friction.set_label(self._tr("แรงเสียดทานตอนไหล (Friction)", "Glide Friction"))
        self.sl_drag_op.set_label(self._tr("ความทึบแสงขณะลาก", "Drag Opacity"))
        self._set_lbl_text_font(
            self.lbl_tb, "คลิกปุ่มกลางที่แถบหัวเรื่อง (Middle-Click Titlebar)", "Titlebar Middle-Click Action", pt=9.0
        )
        self._retranslate_combo_preserve_data(
            self.cmb_mid_click,
            [
                (self._tr("ม้วนเก็บเหลือแค่แถบหัวเรื่อง (Window Shade Roll-Up)", "Roll-Up / Window Shade"), "rollup"),
                (self._tr("ย่อเก็บลงทาสก์บาร์ (Minimize)", "Minimize Window"), "minimize"),
                (self._tr("ปักหมุดให้อยู่บนสุดเสมอ (Always on Top)", "Toggle Always on Top"), "pin"),
                (self._tr("ย่อเป็นไทล์ลอยขนาดเล็ก (Minimize to Mini-Tile)", "Minimize to Floating Mini-Tile"), "tile"),
                (self._tr("ปิดใช้งาน (None)", "Disabled"), "none"),
            ],
        )
        self._set_btn_text_font(
            self.chk_flicks, "ปัดเมาส์เร็วเพื่อจัดวางหน้าต่าง (Window Flicks)", "Enable Window Flicks (Gesture Throw)", pt=9.2
        )
        self.chk_flicks.setToolTip(
            self._tr(
                "ปิดเป็นค่าเริ่มต้นเพื่อป้องกันการขยาย/ย่อหน้าต่างโดยไม่ตั้งใจเวลาลากเร็ว",
                "Off by default to prevent accidental maximize/minimize during fast drags.",
            )
        )
        self._set_btn_text_font(
            self.chk_edge_snap,
            "ลากชนขอบ/มุมจอเพื่อแบ่งสัดส่วนอัจฉริยะ (Smart Edge Snap)",
            "Enable Monitor Edge & Corner Smart Snap",
            pt=9.2,
        )
        self._set_btn_text_font(
            self.chk_smooth_max,
            "แอนิเมชันลื่นไหลตอนขยายเต็มจอ / ชนขอบจอ (Smooth Maximize & Snap)",
            "Smooth Transition on Maximize / Restore-Down / Edge Snap",
            pt=9.2,
        )
        self.sl_smooth_max_ms.set_label(self._tr("ระยะเวลาขยายเต็มจอ", "Morph Duration"))
        for attr, (lbl_w, th_l, en_l) in self._snap_edge_labels.items():
            self._set_lbl_text_font(lbl_w, th_l, en_l, pt=9.0)
            if attr in self._snap_combos:
                self._retranslate_combo_preserve_data(
                    self._snap_combos[attr],
                    [(self._tr(f"{zth} ({zen})", zen), zk) for zk, (zen, zth) in SNAP_PRESETS.items()],
                )

        # 7) Page 04: Focus & Desktop
        self._set_lbl_text_font(
            self.lbl_sp, "สปอตไลต์โฟกัสหน้าต่างปัจจุบัน", "ACTIVE WINDOW SPOTLIGHT", role="mono", pt=8.5, bold=True
        )
        self._set_btn_text_font(
            self.chk_spotlight,
            "หรี่แสงหน้าต่างเบื้องหลังเพื่อโฟกัสหน้าต่างปัจจุบัน (Spotlight)",
            "Darken Inactive Windows Behind Active Window",
            pt=9.2,
        )
        self._retranslate_combo_preserve_data(
            self.cmb_spot_mode,
            [
                (self._tr("วงแสงรัศมีรอบหน้าต่างปัจจุบัน (Radial Window Halo)", "Radial Window Halo"), "halo"),
                (self._tr("ไฟฉายสปอตไลต์ตามเมาส์ (Cursor Spotlight)", "Cursor Radial Spotlight"), "cursor"),
                (self._tr("หรี่แสงพื้นหลังเรียบทั่วทั้งจอ (Flat Dimmer)", "Flat Fullscreen Dimmer"), "flat"),
            ],
        )
        self.sl_spot_op.set_label(self._tr("ความเข้มการหรี่แสง", "Dimmer Darkness"))
        self._set_btn_text_font(
            self.chk_inactive_fade,
            "ทำให้หน้าต่างที่ไม่ได้ใช้งานโปร่งแสงอัตโนมัติ",
            "Make Inactive Windows Semi-Transparent",
            pt=9.2,
        )
        self.sl_inactive_op.set_label(self._tr("ความทึบของหน้าต่างที่ไม่โฟกัส", "Inactive Opacity"))
        self._set_btn_text_font(
            self.chk_automin,
            "ย่อหน้าต่างที่ไม่ได้ใช้งานนานเกินกำหนดอัตโนมัติ",
            "Auto-Minimize Idle Inactive Windows",
            pt=9.2,
        )
        self.sl_automin_min.set_label(self._tr("ระยะเวลาว่างก่อนย่ออัตโนมัติ", "Idle Timeout"))
        self._set_lbl_text_font(
            self.lbl_d, "ไอคอนเดสก์ท็อปและไทล์หน้าต่างลอย", "DESKTOP ICONS & FLOATING WINDOW TILES", role="mono", pt=8.5, bold=True
        )
        self._set_btn_text_font(
            self.chk_dbl_icons,
            "ดับเบิลคลิกพื้นว่างบนเดสก์ท็อปเพื่อซ่อน/แสดงไอคอน",
            "Double-Click Desktop to Toggle Icons Visibility",
            pt=9.2,
        )
        self._set_btn_text_font(
            self.chk_autohide_icons,
            "ซ่อนไอคอนเดสก์ท็อปอัตโนมัติเมื่อไม่ได้คลิกเดสก์ท็อป",
            "Auto-Hide Desktop Icons Unless Desktop Focused",
            pt=9.2,
        )
        self._set_btn_text_font(
            self.btn_toggle_icons_now, "ซ่อน / แสดงไอคอนเดสก์ท็อปทันที", "Toggle Desktop Icons Now", pt=9.2
        )
        self._set_btn_text_font(
            self.chk_tiles_top, "ให้ไทล์หน้าต่างลอยอยู่บนสุดเสมอ", "Keep Floating Mini-Tiles Always on Top", pt=9.2
        )
        self.sl_tile_size.set_label(self._tr("ขนาดไทล์หน้าต่างลอย", "Mini-Tile Width"))
        self._set_btn_text_font(
            self.btn_restore_tiles, "คืนค่าไทล์หน้าต่างทั้งหมด", "Restore All Floating Tiles", pt=9.2
        )

        # 8) Page 05: Apps & Safety
        self._set_lbl_text_font(
            self.lbl_boot_hdr, "การเปิดพร้อมเครื่องและสิทธิ์ระบบ", "WINDOWS STARTUP & PRIVILEGES", role="mono", pt=8.3, bold=True
        )
        self._set_btn_text_font(
            self.chk_autostart,
            "เปิดโปรแกรมทันทีหลังเปิดเครื่อง (Start with Windows)",
            "Start Automatically with Windows",
            pt=9.0,
        )
        self._set_btn_text_font(
            self.chk_autostart_min,
            "เริ่มแบบย่อลง System Tray ทันที",
            "Start Minimized to Tray on Boot",
            pt=9.0,
        )
        self._set_btn_text_font(
            self.chk_autostart_admin,
            "ใช้สิทธิ์ผู้ดูแลตอนเปิดเครื่อง (ไม่เด้ง UAC — คุมหน้าต่าง Admin ได้)",
            "Elevated Boot Task (Zero-UAC Admin Startup)",
            pt=9.0,
        )
        self._set_btn_text_font(
            self.chk_pause_fs,
            "หยุดอัตโนมัติเมื่อเล่นเกมเต็มจอ (Fullscreen / Borderless)",
            "Auto-Pause on Fullscreen Games",
            pt=9.0,
        )
        self._set_btn_text_font(
            self.chk_pause_bat, "หยุดอัตโนมัติเมื่อใช้แบตเตอรี่", "Auto-Pause on Battery", pt=9.0
        )
        self._set_btn_text_font(
            self.chk_auto_game, "ยกเว้นเกมและ Anti-Cheat อัตโนมัติ", "Auto-Exclude Games & Anti-Cheat", pt=9.0
        )
        self._set_btn_text_font(
            self.chk_suppress_min,
            "ปิดแอนิเมชันย่อ/เปิดเดิมของ Windows (กันกระพริบ)",
            "Suppress Native Win11 Min/Restore Zoom (Zero-Flash)",
            pt=9.0,
        )
        self._set_btn_text_font(
            self.btn_emergency, "กู้คืนทุกหน้าต่างทันที", "Emergency Restore All", pt=9.2, bold=True
        )
        self.btn_emergency.setToolTip(
            self._tr(
                "คืนค่าความโปร่งใส/ปักหมุด/ม้วนเก็บของทุกหน้าต่างทันที (คีย์ลัดฉุกเฉิน: Ctrl+Alt+Shift+R)",
                "Restore all window opacity/pin/rollup states immediately (Global Panic Key: Ctrl+Alt+Shift+R).",
            )
        )
        self._set_lbl_text_font(
            self.lbl_tbl,
            "หน้าต่างที่กำลังทำงานอยู่ (จำแนกหมวดหมู่อัตโนมัติ)",
            "RUNNING WINDOWS & DOMAIN VERDICTS",
            role="mono",
            pt=8.5,
            bold=True,
        )
        self._set_btn_text_font(self.btn_manage_excluded, "จัดการรายการยกเว้น...", "Manage Exclusions...", pt=9.0)
        self._set_btn_text_font(self.btn_ref, "รีเฟรชรายการ", "Refresh Windows", pt=9.0)
        self._apply_apps_table_headers()
        self._sync_startup_ui()
        self._refresh_apps_table()

        # 9) Tray Menu, QuickControlPopup & open FloatingTileWindows
        self._retranslate_tray()
        self.quick_bar.retranslate(self._lang)
        for tw in self._tile_windows.values():
            tw.retranslate(self._lang)

    def _retranslate_tray(self) -> None:
        if self._tray is not None:
            self._tray.setToolTip(
                self._tr(
                    f"{__app_name__} Studio — แอนิเมชันและฟิสิกส์หน้าต่าง GPU 165 Hz",
                    f"{__app_name__} Studio — 165 Hz GPU Window FX",
                )
            )
        if self._act_tray_open is not None:
            self._act_tray_open.setText(self._tr("เปิดหน้าต่าง PrismFX Studio", "Open PrismFX Studio"))
        self._sync_tray_pause_action()
        if self._act_tray_quick is not None:
            self._act_tray_quick.setText(
                self._tr("ควบคุมหน้าต่างด่วน (Ctrl+Alt+W)", "Quick Window Controls (Ctrl+Alt+W)")
            )
        if self._act_tray_icons is not None:
            self._act_tray_icons.setText(self._tr("ซ่อน / แสดงไอคอนเดสก์ท็อป", "Toggle Desktop Icons"))
        if self._act_tray_autostart is not None:
            self._act_tray_autostart.setText(self._tr("เปิดพร้อม Windows", "Start with Windows"))
        if self._act_tray_admin is not None:
            self._act_tray_admin.setText(
                self._tr("ยกระดับสิทธิ์ผู้ดูแลระบบ (Run as Admin)", "Restart as Administrator")
            )
        if self._act_tray_restore is not None:
            self._act_tray_restore.setText(
                self._tr("กู้คืนทุกหน้าต่างทันที (Ctrl+Alt+Shift+R)", "Emergency Restore All (Ctrl+Alt+Shift+R)")
            )
        if self._act_tray_quit is not None:
            self._act_tray_quit.setText(self._tr("ออกจาก PrismFX", "Quit PrismFX"))

    # ------------------------------------------------------------------
    # Lock-free Telemetry & Overlay Bridges
    # ------------------------------------------------------------------
    def _on_stage_fps(self, fps: float, avg_ms: float) -> None:
        self._current_fps = fps
        self._current_frame_ms = avg_ms
        self.lbl_st_fps.setText(f"STAGE: {fps:5.1f} FPS ({avg_ms:4.2f} ms)")
        self.tile_fps.set_value(f"{fps:5.1f} FPS", f"{avg_ms:4.2f} ms/frame ({self.cfg.target_fps} Hz budget)")
        gpu_name = self.home_stage.gpu_renderer_name()
        if gpu_name:
            self.lbl_st_gpu.setText(f"GPU: {gpu_name}")

    def _poll_engine_snapshot(self) -> None:
        frame = self.engine.latest_frame()
        sig = (
            bool(frame.enabled),
            str(frame.paused_reason or ""),
            bool(self._is_admin),
            int(frame.total_animations_played),
            int(frame.journal_pending),
            self._lang,
        )
        if sig != self._last_snapshot_sig:
            self._last_snapshot_sig = sig
            if not frame.enabled:
                self.lbl_st_engine.setText("○ ENGINE: DISABLED")
                self.lbl_st_engine.setStyleSheet(f"color: {TOKENS['ink_muted']}; background: transparent;")
            elif frame.paused_reason:
                self.lbl_st_engine.setText(f"◐ PAUSED ({frame.paused_reason.upper()})")
                self.lbl_st_engine.setStyleSheet(f"color: {TOKENS['warn']}; background: transparent;")
            else:
                mode_tag = "ADMIN // ZERO-INJECTION" if self._is_admin else "ZERO-INJECTION"
                self.lbl_st_engine.setText(f"● ENGINE: ACTIVE ({mode_tag})")
                self.lbl_st_engine.setStyleSheet(f"color: {TOKENS['ok']}; background: transparent;")

            self._sync_battery_banner(frame.paused_reason)
            self.tile_played.set_value(f"{frame.total_animations_played}")
            self.lbl_st_journal.setText(f"JOURNAL: {frame.journal_pending} ACTIVE")
            self.tile_journal.set_value(
                self._tr("ปกติ", "CLEAN") if frame.journal_pending == 0 else f"{frame.journal_pending} TRACKED",
                self._tr("พร้อมกู้คืนสถานะอัตโนมัติ", "Auto-recovery armed"),
            )

        # Close & pop any floating tiles whose target window was destroyed
        while True:
            try:
                dead_hwnd = self.engine.destroyed_tile_hwnds.get_nowait()
            except queue.Empty:
                break
            dead_tw = self._tile_windows.pop(dead_hwnd, None)
            if dead_tw is not None:
                dead_tw.close()

        # Spawn any queued floating window tiles on UI thread
        while True:
            try:
                req = self.engine.tile_requests.get_nowait()
            except queue.Empty:
                break
            if isinstance(req, TileRequest):
                info = req.info
                cap = req.capture if req.capture is not None else self.engine.backend.capture_window_bgra(info.hwnd)
            else:
                info = req
                cap = self.engine.backend.capture_window_bgra(info.hwnd)
            old_tw = self._tile_windows.pop(info.hwnd, None)
            if old_tw is not None:
                old_tw.close()
            tw = FloatingTileWindow(
                info, cap, self.cfg.focus.tile_size_px, self.cfg.focus.tiles_topmost, lang=self._lang
            )
            tw.restored.connect(self._on_tile_restored)
            offset = len(self._tile_windows) * 28
            lx, ly = physical_to_qt_logical(info.x, info.y)
            tx, ty, _, _ = clamp_to_virtual_screens(
                lx + 24 + offset, ly + 24 + offset, tw.width(), tw.height(), margin=12
            )
            tw.move(tx, ty)
            tw.show()
            self.engine.register_own_hwnd(int(tw.winId()))
            self._tile_windows[info.hwnd] = tw

    def _on_tile_restored(self, hwnd: int) -> None:
        self._tile_windows.pop(hwnd, None)
        self.engine.restore_from_tile(hwnd)

    def _dispatch_spotlight_from_thread(self, state: SpotlightState) -> None:
        if self._shutdown_started or self.spotlight_overlay is None:
            return
        try:
            self.spotlightStateChanged.emit(state)
        except RuntimeError:
            pass

    def _dispatch_pause_from_thread(self, reason: str) -> None:
        if self._shutdown_started:
            return
        try:
            self.pauseStateChanged.emit(str(reason or ""))
        except RuntimeError:
            pass

    @QtCore.Slot(str)
    def _on_engine_pause_changed(self, reason: str) -> None:
        if self._shutdown_started:
            return
        is_fs_game = (reason == "fullscreen_game")
        if self.fx_overlay is not None:
            self.fx_overlay.set_fullscreen_game_paused(is_fs_game)
        if is_fs_game and self.spotlight_overlay is not None:
            self.spotlight_overlay.hide()
        if hasattr(self, "battery_banner"):
            self._sync_battery_banner(reason)

    # ------------------------------------------------------------------
    # Hotkeys, Tray & Custom Window Dragging
    # ------------------------------------------------------------------
    def _wire_hotkeys(self) -> None:
        # Local QShortcut fallbacks (automatically disabled when global Win32 RegisterHotKey succeeds)
        sc_quick = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Alt+W"), self)
        sc_quick.setProperty("hotkey_id", w32.HOTKEY_ID_QUICK_BAR)
        sc_quick.activated.connect(self._open_quick_controls)

        sc_panic = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Alt+Shift+R"), self)
        sc_panic.setProperty("hotkey_id", w32.HOTKEY_ID_PANIC_RESTORE)
        sc_panic.activated.connect(self._on_global_panic_restore)

        self._fallback_shortcuts = [sc_quick, sc_panic]
        self._sync_fallback_shortcuts()

    def _setup_tray(self) -> None:
        tray = QtWidgets.QSystemTrayIcon(self._app_icon, self)
        menu = QtWidgets.QMenu()
        self._act_tray_open = menu.addAction("", self._restore_from_tray)
        self._act_tray_toggle_engine = menu.addAction("", self._on_tray_toggle_engine)
        self._act_tray_quick = menu.addAction("", self._open_quick_controls)
        self._act_tray_icons = menu.addAction("", lambda: self.engine.toggle_desktop_icons())
        menu.addSeparator()
        act_auto = menu.addAction("")
        act_auto.setCheckable(True)
        act_auto.setChecked(bool(self.cfg.start_with_windows))
        act_auto.toggled.connect(self._on_autostart_toggled)
        self._act_tray_autostart = act_auto
        if not self._is_admin:
            self._act_tray_admin = menu.addAction("", self._on_relaunch_as_admin)
        menu.addSeparator()
        self._act_tray_restore = menu.addAction("", self._on_emergency_restore)
        self._act_tray_quit = menu.addAction("", self.full_shutdown)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda r: self._restore_from_tray() if r == QtWidgets.QSystemTrayIcon.DoubleClick else None)
        self._tray = tray
        self._retranslate_tray()
        tray.show()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()
        self._select_page(self._active_page)
        self._wake_active_stage()

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _title_mouse_press(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _title_mouse_move(self, event: QtGui.QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & QtCore.Qt.LeftButton:
            if self.isMaximized():
                self.showNormal()
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def _on_close_button(self) -> None:
        self._persist_geometry()
        if self._tray and self._tray.isVisible() and not self.engine.backend.is_demo:
            self.hide()
            self._pause_all_stages()
            self._notify_minimized_to_tray_once()
        else:
            self.full_shutdown()

    def _persist_geometry(self) -> None:
        if not self.isMaximized() and not self.isMinimized():
            g = self.geometry()
            self.cfg.window_geometry = [g.x(), g.y(), g.width(), g.height()]
            self.engine.store.save(self.cfg)

    def full_shutdown(self) -> None:
        self._shutdown_started = True
        self.engine.on_spotlight_changed = None
        self.engine.on_pause_state_changed = None
        self._unregister_global_hotkeys()
        if self._wts_registered:
            self._wts_registered = False
            try:
                w32.unregister_session_notification(int(self.winId()))
            except Exception as exc:
                _log.debug("unregister_session_notification failed on shutdown: %s", exc)
        self._persist_geometry()
        self._ui_timer.stop()
        self.home_stage.cleanup_gl()
        self.lab_stage.cleanup_gl()
        self.wobble_stage.cleanup_gl()
        self._restore_all_tiles()
        if self.fx_overlay:
            self.fx_overlay.shutdown()
        if self.spotlight_overlay:
            self.spotlight_overlay.close()
        self.quick_bar.close()
        if self._tray:
            self._tray.hide()
        OffscreenGpuEngine.shared().shutdown()
        self.engine.stop()
        self.engine.journal.close()
        QtWidgets.QApplication.quit()
