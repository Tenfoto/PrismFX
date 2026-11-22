"""PrismFX Frameless Architectural Studio Window — 5 Complete Control Pages + System Tray."""

from __future__ import annotations

from ctypes import wintypes
import pathlib
import queue
import threading
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .. import __app_name__, __version__
from ..core import startup as win_startup
from ..core import win32_api as w32
from ..core.config import BUILTIN_PRESETS, PrismConfig
from ..core.effects import CATEGORIES, EASING_CURVES, TRIGGER_TYPES, EffectSpec
from ..core.engine import PrismEngine, SpotlightState, TileRequest
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
from .theme import ATLAS, TOKENS, build_stylesheet
from .widgets import (
    ArchToggle,
    EffectCard,
    MetricTile,
    PanelCard,
    ScanlineContainer,
    SectionHeader,
    SliderRow,
)

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
        self._drag_pos: Optional[QtCore.QPoint] = None
        self._active_page = "home"
        self._active_trigger = "open"
        self._active_category = "all"
        self._search_text = ""
        self._effect_cards: dict[str, EffectCard] = {}
        self._nav_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._trigger_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._cat_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._tile_windows: dict[int, FloatingTileWindow] = {}
        self._current_fps = 0.0
        self._current_frame_ms = 0.0
        self._act_tray_autostart: Optional[QtGui.QAction] = None
        self._act_tray_admin: Optional[QtGui.QAction] = None

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
        if engine._cached_paused_reason == "fullscreen_game":
            self._on_engine_pause_changed("fullscreen_game")

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

        # Sync initial selections
        self._select_page("home")
        self._refresh_animations_grid()
        self._sync_trigger_controls()
        self._sync_startup_ui()
        self._refresh_apps_table()
        self._start_async_startup_query()

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
        """Realize native Win32 HWND even when starting hidden in tray (for IPC, UIPI & taskbar pinning)."""
        hwnd = int(self.winId())
        if hwnd:
            self.engine.register_own_hwnd(hwnd)
            if not self._native_bound:
                self._native_bound = True
                if not self._wts_registered:
                    self._wts_registered = bool(w32.register_session_notification(hwnd))
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
        except Exception:
            pass
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
        else:
            self.full_shutdown()
            event.accept()

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

        # 2) Body (Sidebar + Stacked Pages)
        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._sidebar = self._build_sidebar()
        body.addWidget(self._sidebar)

        sep_v = QtWidgets.QFrame()
        sep_v.setFixedWidth(1)
        sep_v.setStyleSheet(f"background: {TOKENS['line']};")
        body.addWidget(sep_v)

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

        body.addWidget(self._stack, 1)
        outer.addLayout(body, 1)

        # 3) Bottom Telemetry Status Bar
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

        lbl_ver = QtWidgets.QLabel(f"v{__version__} // 42 GPU SHADERS")
        lbl_ver.setFont(ATLAS.font_for("mono", 8.5))
        lbl_ver.setStyleSheet(
            f"color: {TOKENS['accent_hi']}; background: {TOKENS['bg_elevated']}; "
            f"border: 1px solid {TOKENS['line']}; padding: 2px 7px;"
        )
        lay.addWidget(lbl_ver)

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
        self.btn_quick_ctrl.clicked.connect(self._open_quick_controls)
        lay.addWidget(self.btn_quick_ctrl)

        # Bilingual Switch
        self.btn_lang = QtWidgets.QPushButton("TH / EN")
        self.btn_lang.setObjectName("btn_lang")
        self.btn_lang.setFixedHeight(28)
        self.btn_lang.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        self.btn_lang.clicked.connect(self._toggle_language)
        lay.addWidget(self.btn_lang)

        # Master Engine Switch
        self.master_toggle = ArchToggle(self.cfg.enabled, "ENGINE ON", "ENGINE OFF")
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

        lbl_nav = QtWidgets.QLabel("  MODULES")
        lbl_nav.setFont(ATLAS.font_for("mono", 8.0, bold=True))
        lbl_nav.setStyleSheet(f"color: {TOKENS['ink_muted']}; padding: 4px 12px;")
        lay.addWidget(lbl_nav)

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
        lbl_st_hdr = QtWidgets.QLabel("STARTUP")
        lbl_st_hdr.setFont(ATLAS.font_for("mono", 7.6, bold=True))
        lbl_st_hdr.setStyleSheet(f"color: {TOKENS['accent_hi']}; border: none; background: transparent;")
        self.lbl_side_priv = QtWidgets.QLabel("● ADMIN" if self._is_admin else "◐ STANDARD")
        self.lbl_side_priv.setFont(ATLAS.font_for("mono", 7.3, bold=True))
        self.lbl_side_priv.setStyleSheet(
            f"color: {TOKENS['ok'] if self._is_admin else TOKENS['warn']}; border: none; background: transparent;"
        )
        st_hdr.addWidget(lbl_st_hdr)
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
        lbl_arch = QtWidgets.QLabel("OUT-OF-PROCESS ENGINE")
        lbl_arch.setFont(ATLAS.font_for("mono", 7.8, bold=True))
        lbl_arch.setStyleSheet(f"color: {TOKENS['ok']}; border: none; background: transparent;")
        lbl_desc = QtWidgets.QLabel(
            self._tr(
                "ปลอดภัย 100% ไม่ฉีด DLL\nไม่ชน Anti-Cheat / เกม",
                "Zero DLL Injection.\nAnti-Cheat & Win11 Safe.",
            )
        )
        lbl_desc.setFont(ATLAS.font_for("body", 8.8, text=lbl_desc.text()))
        lbl_desc.setStyleSheet(f"color: {TOKENS['ink_secondary']}; border: none; background: transparent;")
        lbl_desc.setWordWrap(True)
        cl.addWidget(lbl_arch)
        cl.addWidget(lbl_desc)

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
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(18)

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

        lay.addWidget(self.lbl_st_engine)
        lay.addWidget(self.lbl_st_fps)
        lay.addWidget(self.lbl_st_gpu, 1)
        lay.addWidget(self.lbl_st_journal)
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
        lay.setContentsMargins(18, 14, 18, 18)
        lay.setSpacing(12)

        lay.addWidget(
            SectionHeader(
                "PRISMFX // NEXT-GEN WINDOWS 11 DESKTOP MOTION",
                self._tr("ศูนย์ควบคุมแอนิเมชันและฟิสิกส์หน้าต่าง 165 Hz", "165 Hz GPU Window Motion & Physics Studio"),
                self._tr(
                    "ออกแบบใหม่หมดจดด้วย OpenGL 3.3 Shaders (42+ เอฟเฟกต์) แทนที่ระบบ DX10 เก่า — ปราศจากการฉีด DLL และกู้คืนสถานะอัตโนมัติ",
                    "Clean-room OpenGL 3.3 Core engine with 42+ GPU shaders, closed-form spring physics, and crash-safe restoration.",
                ),
            )
        )

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
        for t in (self.tile_shaders, self.tile_fps, self.tile_played, self.tile_journal):
            kpi.addWidget(t, 1)
        lay.addLayout(kpi)

        # Split Row: Interactive GPU Stage (left) + Curated Presets (right)
        split = QtWidgets.QHBoxLayout()
        split.setSpacing(14)

        # Left: Live GPU Stage Panel
        stage_card = PanelCard()
        sl = QtWidgets.QVBoxLayout(stage_card)
        sl.setContentsMargins(12, 12, 12, 12)
        sl.setSpacing(8)

        sh = QtWidgets.QHBoxLayout()
        lbl_stage = QtWidgets.QLabel(self._tr("เวทีทดสอบเชดเดอร์สด (คลิกเพื่อเล่นซ้ำ)", "LIVE INTERACTIVE GPU STAGE (CLICK TO REPLAY)"))
        lbl_stage.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        lbl_stage.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        sh.addWidget(lbl_stage, 1)

        self.btn_home_replay = QtWidgets.QPushButton(self._tr("เล่นซ้ำ", "Replay"))
        self.btn_home_replay.setObjectName("btn_home_replay")
        self.btn_home_replay.clicked.connect(lambda: self.home_stage.replay())
        self.btn_home_test_real = QtWidgets.QPushButton(self._tr("ทดสอบบนหน้าต่างจริง", "Test on Real Window"))
        self.btn_home_test_real.setObjectName("btn_home_test_real")
        self.btn_home_test_real.setProperty("accent", "true")
        self.btn_home_test_real.clicked.connect(self._test_active_effect_on_real_window)
        sh.addWidget(self.btn_home_replay)
        sh.addWidget(self.btn_home_test_real)
        sl.addLayout(sh)

        self.home_stage = ShaderPreviewWidget()
        self.home_stage.setMinimumHeight(310)
        self.home_stage.fps_updated.connect(self._on_stage_fps)
        self.home_stage.set_effect(self.engine.registry.get(self.cfg.triggers["close"].effect_id))
        sl.addWidget(self.home_stage, 1)

        # Quick Showcase Strip (compact 2-row grid so right Preset panel never overflows!)
        strip = QtWidgets.QGridLayout()
        strip.setSpacing(6)
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
        for idx, (eid, th_s, en_s) in enumerate(quick_effects):
            spec = self.engine.registry.get(eid)
            lbl = self._tr(th_s, en_s)
            b = QtWidgets.QPushButton(lbl)
            b.setFont(ATLAS.font_for("body", 9.0, text=lbl))
            b.setProperty("pill", "true")
            b.clicked.connect(lambda _=False, s=spec: self.home_stage.set_effect(s))
            strip.addWidget(b, idx // 4, idx % 4)
        sl.addLayout(strip)
        split.addWidget(stage_card, 3)

        # Right: Curated Presets Panel
        preset_card = PanelCard()
        preset_card.setMinimumWidth(330)
        preset_card.setMaximumWidth(380)
        pl = QtWidgets.QVBoxLayout(preset_card)
        pl.setContentsMargins(12, 12, 12, 12)
        pl.setSpacing(8)

        ph = QtWidgets.QHBoxLayout()
        lbl_pr = QtWidgets.QLabel(self._tr("ชุดพรีเซตพร้อมใช้", "CURATED PRESETS"))
        lbl_pr.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        lbl_pr.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        btn_save_pr = QtWidgets.QPushButton(self._tr("+ บันทึกพรีเซต", "+ Save Preset"))
        btn_save_pr.setObjectName("btn_save_preset")
        btn_save_pr.clicked.connect(self._save_custom_preset_dialog)
        ph.addWidget(lbl_pr, 1)
        ph.addWidget(btn_save_pr)
        pl.addLayout(ph)

        self._preset_list_layout = QtWidgets.QVBoxLayout()
        self._preset_list_layout.setSpacing(6)
        pl.addLayout(self._preset_list_layout)
        pl.addStretch(1)

        # Global Speed Scale Slider
        self.sl_global_speed = SliderRow(
            self._tr("ความเร็วแอนิเมชันรวม (Global Speed)", "Global Animation Speed"),
            50, 200, int(round(self.cfg.global_speed_scale * 100)),
            formatter=lambda v: f"{v / 100.0:.2f}×",
        )
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
            if item.widget():
                item.widget().deleteLater()
        for name, is_builtin in self.engine.store.list_presets():
            meta = BUILTIN_PRESETS.get(name)
            desc = (meta["desc_th"] if self._lang == "th" else meta["desc_en"]) if meta else "User saved preset"
            active = name == self.cfg.active_preset
            btn = QtWidgets.QPushButton(self._qt_esc(f"{'▸ ' if active else ''}{name}\n{desc[:58]}"))
            btn.setFont(ATLAS.font_for("body", 9.0, text=desc))
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setMinimumHeight(46)
            btn.setStyleSheet(
                f"QPushButton {{ text-align: left; padding: 7px 10px; "
                f"background: {TOKENS['bg_selected'] if active else TOKENS['bg_recessed']}; "
                f"border: 1px solid {TOKENS['accent'] if active else TOKENS['line']}; }}"
                f"QPushButton:hover {{ border-color: {TOKENS['accent_hi']}; }}"
            )
            btn.clicked.connect(lambda _=False, pn=name: self._apply_preset(pn))
            self._preset_list_layout.addWidget(btn)

    # ------------------------------------------------------------------
    # Page 2: Animations Lab (42+ Effects + Custom GLSL Loader)
    # ------------------------------------------------------------------
    def _build_animations_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page.setStyleSheet("background: transparent;")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(10)

        lay.addWidget(
            SectionHeader(
                "ANIMATIONS LAB // 42+ GPU SHADERS",
                self._tr("ปรับแต่งแอนิเมชันแยกตามเหตุการณ์ + เชดเดอร์กำหนดเอง", "Per-Trigger GPU Shaders & Custom GLSL Lab"),
            )
        )

        # Trigger Strip (6 triggers)
        t_row = QtWidgets.QHBoxLayout()
        t_row.setSpacing(6)
        for tid, en, th in TRIGGER_TYPES:
            b = QtWidgets.QPushButton(self._tr_btn(th, en))
            b.setObjectName(f"trigger_{tid}")
            b.setProperty("pill", "true")
            b.setFont(ATLAS.font_for("body", 9.2, text=self._tr(th, en)))
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=tid: self._select_trigger(k))
            self._trigger_buttons[tid] = b
            t_row.addWidget(b)
        t_row.addStretch(1)

        btn_new_glsl = QtWidgets.QPushButton(self._tr_btn("+ สร้างเชดเดอร์ GLSL", "+ Custom GLSL"))
        btn_new_glsl.setObjectName("btn_new_glsl")
        btn_new_glsl.clicked.connect(self._create_sample_custom_shader)
        t_row.addWidget(btn_new_glsl)
        lay.addLayout(t_row)

        # Category Filter + Search Bar
        f_row = QtWidgets.QHBoxLayout()
        f_row.setSpacing(6)
        for cid, (en, th) in CATEGORIES.items():
            b = QtWidgets.QPushButton(self._tr_btn(th, en))
            b.setObjectName(f"cat_{cid}")
            b.setProperty("pill", "true")
            b.setFont(ATLAS.font_for("body", 8.8, text=self._tr(th, en)))
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=cid: self._select_category(k))
            self._cat_buttons[cid] = b
            f_row.addWidget(b)
        self.ed_search = QtWidgets.QLineEdit()
        self.ed_search.setPlaceholderText(self._tr("ค้นหาแอนิเมชัน...", "Search 42+ shaders..."))
        self.ed_search.setFixedWidth(210)
        self.ed_search.textChanged.connect(self._on_search_changed)
        f_row.addWidget(self.ed_search)
        lay.addLayout(f_row)

        # Split: Left Scrollable Grid (2 columns) | Right Inspector + Live GPU Stage
        split = QtWidgets.QHBoxLayout()
        split.setSpacing(14)

        self._anim_scroll = QtWidgets.QScrollArea()
        self._anim_scroll.setWidgetResizable(True)
        self._anim_grid_host = QtWidgets.QWidget()
        self._anim_grid_host.setStyleSheet("background: transparent;")
        self._anim_grid = QtWidgets.QGridLayout(self._anim_grid_host)
        self._anim_grid.setContentsMargins(0, 0, 6, 0)
        self._anim_grid.setSpacing(8)
        self._anim_scroll.setWidget(self._anim_grid_host)
        split.addWidget(self._anim_scroll, 3)

        # Right Inspector Panel
        insp = PanelCard()
        insp.setFixedWidth(370)
        il = QtWidgets.QVBoxLayout(insp)
        il.setContentsMargins(12, 12, 12, 12)
        il.setSpacing(8)

        top_i = QtWidgets.QHBoxLayout()
        self.chk_trig_enabled = QtWidgets.QCheckBox(self._tr("เปิดใช้งานเหตุการณ์นี้", "Enable this trigger"))
        self.chk_trig_enabled.toggled.connect(self._on_trigger_params_changed)
        top_i.addWidget(self.chk_trig_enabled, 1)
        self.btn_lab_dir = QtWidgets.QPushButton("DIR: OPEN ⇄")
        self.btn_lab_dir.setFixedHeight(28)
        self.btn_lab_dir.setStyleSheet(
            f"QPushButton {{ padding: 2px 10px; font-size: 11px; background: {TOKENS['bg_recessed']}; "
            f"border: 1px solid {TOKENS['line_strong']}; }}"
            f"QPushButton:hover {{ border-color: {TOKENS['accent']}; }}"
        )
        self.btn_lab_dir.clicked.connect(self._flip_lab_direction)
        top_i.addWidget(self.btn_lab_dir)
        il.addLayout(top_i)

        self.lab_stage = ShaderPreviewWidget()
        self.lab_stage.setFixedHeight(220)
        il.addWidget(self.lab_stage)

        self.lbl_lab_effect = QtWidgets.QLabel("Prism Glide")
        self.lbl_lab_effect.setFont(ATLAS.font_for("display", 12.0, bold=True))
        self.lbl_lab_effect.setStyleSheet(f"color: {TOKENS['ink_primary']}; background: transparent; border: none;")
        il.addWidget(self.lbl_lab_effect)

        self.sl_trig_dur = SliderRow(self._tr("ระยะเวลาแอนิเมชัน", "Duration"), 80, 1200, 320, " ms")
        self.sl_trig_dur.value_changed.connect(self._on_trigger_params_changed)
        il.addWidget(self.sl_trig_dur)

        self.sl_trig_int = SliderRow(
            self._tr("ความแรงเอฟเฟกต์", "Shader Intensity"),
            30, 180, 100, formatter=lambda v: f"{v}%",
        )
        self.sl_trig_int.value_changed.connect(self._on_trigger_params_changed)
        il.addWidget(self.sl_trig_int)

        lbl_ease = QtWidgets.QLabel(self._tr("เส้นโค้งสปริง (Easing Curve)", "Motion Spring Curve"))
        lbl_ease.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
        il.addWidget(lbl_ease)
        self.cmb_easing = QtWidgets.QComboBox()
        for cid, en, th in EASING_CURVES:
            self.cmb_easing.addItem(self._tr(f"{th} ({en})", en), cid)
        self.cmb_easing.currentIndexChanged.connect(self._on_trigger_params_changed)
        il.addWidget(self.cmb_easing)

        # Accent Color Swatches
        sw_lbl = QtWidgets.QLabel(self._tr("สีแสงเอฟเฟกต์ (Shader Accent)", "Shader Accent Tint"))
        sw_lbl.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
        il.addWidget(sw_lbl)
        sw_row = QtWidgets.QHBoxLayout()
        sw_row.setSpacing(4)
        for hx in ACCENT_SWATCHES:
            sb = QtWidgets.QPushButton()
            sb.setFixedSize(28, 22)
            sb.setCursor(QtCore.Qt.PointingHandCursor)
            sb.setStyleSheet(f"background: {hx}; border: 1px solid #444B5E;")
            sb.clicked.connect(lambda _=False, c=hx: self._set_trigger_accent(c))
            sw_row.addWidget(sb)
        sw_row.addStretch(1)
        il.addLayout(sw_row)

        btn_try = QtWidgets.QPushButton(self._tr("▶ ทดสอบเชดเดอร์นี้บนหน้าต่างจริง", "▶ Fire Live Desktop Preview"))
        btn_try.setProperty("accent", "true")
        btn_try.clicked.connect(self._test_active_effect_on_real_window)
        il.addWidget(btn_try)
        il.addStretch(1)

        split.addWidget(insp, 0)
        lay.addLayout(split, 1)
        return page

    # ------------------------------------------------------------------
    # Page 3: Physics & Gestures (Live Wobbly Sandbox + Momentum + Flicks + Edge Snap)
    # ------------------------------------------------------------------
    def _build_physics_page(self) -> QtWidgets.QWidget:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        page = QtWidgets.QWidget()
        page.setStyleSheet("background: transparent;")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 20)
        lay.setSpacing(14)

        lay.addWidget(
            SectionHeader(
                "PHYSICS & GESTURES // COMPIZ SPRING ENGINE",
                self._tr("หน้าต่างเยลลี่, แรงเฉื่อยตอนลาก, ปัดสแนป และการควบคุมแถบหัวเรื่อง", "Wobbly Windows, Momentum Inertia, Flicks & Smart Edge Snap"),
            )
        )

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(14)

        # Left: Interactive Wobbly Window Sandbox
        wob_card = PanelCard()
        wl = QtWidgets.QVBoxLayout(wob_card)
        wl.setContentsMargins(12, 12, 12, 12)
        wl.setSpacing(8)

        wh = QtWidgets.QHBoxLayout()
        self.chk_wobbly = QtWidgets.QCheckBox(self._tr("เปิดใช้หน้าต่างเยลลี่ (Wobbly Windows)", "Enable Wobbly Windows (Compiz Physics)"))
        self.chk_wobbly.setChecked(self.cfg.physics.wobbly_enabled)
        self.chk_wobbly.toggled.connect(self._save_physics_ui)
        wh.addWidget(self.chk_wobbly, 1)
        hint = QtWidgets.QLabel(self._tr("คลิกลากเขย่าหน้าต่างด้านล่างเพื่อทดสอบ!", "GRAB & SHAKE CARD BELOW"))
        hint.setFont(ATLAS.font_for("mono", 8.0, bold=True))
        hint.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        wh.addWidget(hint)
        wl.addLayout(wh)

        self.wobble_stage = ShaderPreviewWidget()
        self.wobble_stage.setMinimumHeight(250)
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
        ml.setContentsMargins(14, 14, 14, 14)
        ml.setSpacing(10)

        lbl_m = QtWidgets.QLabel("MOMENTUM & DRAG PHYSICS")
        lbl_m.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        lbl_m.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        ml.addWidget(lbl_m)

        self.chk_momentum = QtWidgets.QCheckBox(self._tr("แรงเฉื่อยเมื่อโยนหน้าต่าง (Momentum Drag)", "Enable Momentum / Inertia Window Glide"))
        self.chk_momentum.setChecked(self.cfg.physics.momentum_enabled)
        self.chk_bounce = QtWidgets.QCheckBox(self._tr("เด้งกลับเมื่อชนขอบจอ (Elastic Edge Bounce)", "Elastic Bounce off Monitor Borders"))
        self.chk_bounce.setChecked(self.cfg.physics.momentum_bounce)
        self.chk_clamp = QtWidgets.QCheckBox(self._tr("จำกัดหน้าต่างไม่ให้หลุดขอบจอ (Strict Clamp)", "Strictly Clamp Windows to Screen Limits"))
        self.chk_clamp.setChecked(self.cfg.physics.momentum_strict_clamp)
        self.chk_drag_alpha = QtWidgets.QCheckBox(self._tr("โปร่งใสอัตโนมัติขณะลากย้ายหน้าต่าง", "Semi-Transparent While Moving Window"))
        self.chk_drag_alpha.setChecked(self.cfg.physics.drag_transparency_enabled)

        for c in (self.chk_momentum, self.chk_bounce, self.chk_clamp, self.chk_drag_alpha):
            c.toggled.connect(self._save_physics_ui)
            ml.addWidget(c)

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
        lbl_tb = QtWidgets.QLabel(self._tr("คลิกปุ่มกลางที่แถบหัวเรื่อง (Middle-Click Titlebar)", "Titlebar Middle-Click Action"))
        lbl_tb.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
        ml.addWidget(lbl_tb)
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
        spl.setContentsMargins(14, 14, 14, 14)
        spl.setSpacing(10)

        sr = QtWidgets.QHBoxLayout()
        self.chk_flicks = QtWidgets.QCheckBox(self._tr("ปัดเมาส์เร็วเพื่อจัดวางหน้าต่าง (Window Flicks)", "Enable Window Flicks (Gesture Throw)"))
        self.chk_flicks.setChecked(self.cfg.physics.flicks_enabled)
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
        edges = [
            ("snap_left_zone", self._tr("ขอบซ้าย / ปัดซ้าย", "Left Edge / Flick"), self.cfg.physics.snap_left_zone),
            ("snap_right_zone", self._tr("ขอบขวา / ปัดขวา", "Right Edge / Flick"), self.cfg.physics.snap_right_zone),
            ("snap_top_zone", self._tr("ขอบบน / ปัดขึ้น", "Top Edge / Flick"), self.cfg.physics.snap_top_zone),
            ("snap_bottom_zone", self._tr("ขอบล่าง / ปัดลง", "Bottom Edge / Flick"), self.cfg.physics.snap_bottom_zone),
        ]
        for col, (attr, lbl, cur) in enumerate(edges):
            v = QtWidgets.QVBoxLayout()
            l = QtWidgets.QLabel(lbl)
            l.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
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
        page = QtWidgets.QWidget()
        page.setStyleSheet("background: transparent;")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 20)
        lay.setSpacing(14)

        lay.addWidget(
            SectionHeader(
                "FOCUS, ATMOSPHERE & DESKTOP CONTROLS",
                self._tr("ไฮไลต์หน้าต่างที่ใช้งาน, ย่ออัตโนมัติ, ไทล์ลอย และจัดการไอคอนเดสก์ท็อป", "Spotlight Dimmer, Inactive Fade, Window Tiles & Desktop Icons"),
            )
        )

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(14)

        # Left: Spotlight & Inactive Focus
        sp_card = PanelCard()
        sl = QtWidgets.QVBoxLayout(sp_card)
        sl.setContentsMargins(14, 14, 14, 14)
        sl.setSpacing(10)

        lbl_sp = QtWidgets.QLabel("ACTIVE WINDOW SPOTLIGHT")
        lbl_sp.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        lbl_sp.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        sl.addWidget(lbl_sp)

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
        for hx in SPOTLIGHT_SWATCHES:
            b = QtWidgets.QPushButton()
            b.setFixedSize(32, 22)
            b.setStyleSheet(f"background: {hx}; border: 1px solid #475066;")
            b.clicked.connect(lambda _=False, c=hx: self._set_spotlight_color(c))
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

        lbl_d = QtWidgets.QLabel("DESKTOP ICONS & FLOATING WINDOW TILES")
        lbl_d.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        lbl_d.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")
        dl.addWidget(lbl_d)

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

        btn_restore_tiles = QtWidgets.QPushButton(self._tr("คืนค่าไทล์หน้าต่างทั้งหมด", "Restore All Floating Tiles"))
        btn_restore_tiles.clicked.connect(self._restore_all_tiles)
        dl.addWidget(btn_restore_tiles)
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
        lay.setContentsMargins(20, 14, 20, 16)
        lay.setSpacing(12)

        lay.addWidget(
            SectionHeader(
                "APP CATALOG, EXCLUSIONS & CRASH RECOVERY",
                self._tr("ตรวจสอบหน้าต่างที่เปิดอยู่, สิทธิ์ระบบ/เปิดพร้อมเครื่อง และระบบกู้คืนฉุกเฉิน", "Live App Catalog, Windows Startup & Privileges, Smart Exclusions"),
            )
        )

        # Windows Startup & Administrator Privileges Card (WindowFX-style boot & UIPI elevation)
        boot_card = PanelCard()
        bl = QtWidgets.QVBoxLayout(boot_card)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(8)

        bh = QtWidgets.QHBoxLayout()
        bh.setSpacing(10)
        lbl_boot_hdr = QtWidgets.QLabel("WINDOWS STARTUP & PRIVILEGES")
        lbl_boot_hdr.setFont(ATLAS.font_for("mono", 8.3, bold=True))
        lbl_boot_hdr.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent; border: none;")

        self.lbl_priv_badge = QtWidgets.QLabel()
        self.lbl_priv_badge.setFont(ATLAS.font_for("mono", 7.8, bold=True))

        self.lbl_startup_status = QtWidgets.QLabel()
        self.lbl_startup_status.setFont(ATLAS.font_for("body", 8.8, text="เปิดอัตโนมัติ"))
        self.lbl_startup_status.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")

        self.btn_elevate_admin = QtWidgets.QPushButton()
        self.btn_elevate_admin.setObjectName("btn_elevate_admin")
        self.btn_elevate_admin.setFixedHeight(34)
        self.btn_elevate_admin.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_elevate_admin.clicked.connect(self._on_relaunch_as_admin)

        bh.addWidget(lbl_boot_hdr)
        bh.addWidget(self.lbl_priv_badge)
        bh.addWidget(self.lbl_startup_status, 1)
        bh.addWidget(self.btn_elevate_admin)
        bl.addLayout(bh)

        br = QtWidgets.QHBoxLayout()
        br.setSpacing(16)
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
        sl.setContentsMargins(14, 10, 14, 10)
        sl.setSpacing(16)

        self.chk_pause_fs = QtWidgets.QCheckBox(self._tr("หยุดอัตโนมัติเมื่อเล่นเกมเต็มจอ (Fullscreen D3D)", "Auto-Pause on Fullscreen Games"))
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
        self.btn_emergency.clicked.connect(self._on_emergency_restore)
        sl.addWidget(self.btn_emergency)
        lay.addWidget(safe_card)

        # Running Windows Table Header + Actions
        hdr = QtWidgets.QHBoxLayout()
        lbl_tbl = QtWidgets.QLabel(self._tr("หน้าต่างที่กำลังทำงานอยู่ (จำแนกหมวดหมู่อัตโนมัติ)", "RUNNING WINDOWS & DOMAIN VERDICTS"))
        lbl_tbl.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        lbl_tbl.setStyleSheet(f"color: {TOKENS['accent_hi']}; background: transparent;")
        btn_ref = QtWidgets.QPushButton(self._tr("รีเฟรชรายการ", "Refresh Windows"))
        btn_ref.setObjectName("btn_refresh_apps")
        btn_ref.clicked.connect(self._refresh_apps_table)
        hdr.addWidget(lbl_tbl, 1)
        hdr.addWidget(btn_ref)
        lay.addLayout(hdr)

        self.apps_table = QtWidgets.QTableWidget(0, 6)
        self.apps_table.setHorizontalHeaderLabels(
            ["APPLICATION", "PROCESS", "CATEGORY", "FX VERDICT", "STATUS", "QUICK ACTIONS"]
        )
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
        self.apps_table.setColumnWidth(5, 290)
        lay.addWidget(self.apps_table, 1)
        return page

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
        self.lbl_lab_effect.setText(f"{spec.name_th} // {spec.name_en}" if self._lang == "th" else spec.name_en)
        self.lab_stage.set_effect(
            spec,
            duration_ms=tc.duration_ms,
            easing=tc.easing,
            intensity=tc.intensity,
            accent_hex=tc.accent_hex,
            reverse=(self._active_trigger in ("open", "restore", "menu", "popup")),
        )
        self._update_lab_dir_button()

    def _refresh_animations_grid(self) -> None:
        while self._anim_grid.count():
            item = self._anim_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._effect_cards.clear()

        current_eid = self.cfg.triggers[self._active_trigger].effect_id
        items = self.engine.registry.by_category(self._active_category)
        if self._search_text:
            q = self._search_text
            items = [
                e for e in items
                if q in e.name_en.lower() or q in e.name_th.lower() or q in e.desc_en.lower() or q in e.id.lower()
            ]

        for idx, spec in enumerate(items):
            card = EffectCard(spec, lang=self._lang, is_selected=(spec.id == current_eid))
            card.selected.connect(self._on_effect_card_selected)
            self._effect_cards[spec.id] = card
            self._anim_grid.addWidget(card, idx // 2, idx % 2)

        self._anim_grid.setRowStretch((len(items) + 1) // 2 + 1, 1)

    def _on_effect_card_selected(self, effect_id: str) -> None:
        tc = self.cfg.triggers[self._active_trigger]
        spec = self.engine.registry.get(effect_id)
        tc.effect_id = effect_id
        tc.duration_ms = spec.default_ms
        tc.accent_hex = spec.accent_hex
        self._push_config()
        for eid, card in self._effect_cards.items():
            card.set_selected(eid == effect_id)
        self._sync_trigger_controls()

    def _on_trigger_params_changed(self, *_args) -> None:
        tc = self.cfg.triggers[self._active_trigger]
        tc.enabled = self.chk_trig_enabled.isChecked()
        tc.duration_ms = self.sl_trig_dur.slider.value()
        tc.intensity = self.sl_trig_int.slider.value() / 100.0
        tc.easing = str(self.cmb_easing.currentData() or "spring_smooth")
        self._push_config()
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
        self.cfg.triggers[self._active_trigger].accent_hex = hex_color
        self._push_config()
        self._sync_trigger_controls()

    def _update_lab_dir_button(self) -> None:
        self.btn_lab_dir.setText("DIR: OPEN ⇄" if self.lab_stage._reverse else "DIR: CLOSE ⇄")

    def _flip_lab_direction(self) -> None:
        self.lab_stage.set_direction(not self.lab_stage._reverse)
        self._update_lab_dir_button()

    def _on_master_toggled(self, enabled: bool) -> None:
        self.cfg.enabled = enabled
        self._push_config()

    def _on_global_speed(self, val: int) -> None:
        self.cfg.global_speed_scale = val / 100.0
        self._push_config()

    def _apply_preset(self, name: str) -> None:
        self.cfg = self.engine.store.apply_preset(self.cfg, name)
        self._push_config()
        self._rebuild_preset_buttons()
        self._sync_trigger_controls()
        self._refresh_animations_grid()
        self.home_stage.set_effect(self.engine.registry.get(self.cfg.triggers["close"].effect_id))

    def _save_custom_preset_dialog(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(
            self,
            self._tr("บันทึกพรีเซตใหม่", "Save Custom Preset"),
            self._tr("ตั้งชื่อพรีเซตของคุณ:", "Preset name:"),
        )
        if ok and name.strip():
            self.cfg.active_preset = name.strip()
            self.engine.store.save_user_preset(name.strip(), self.cfg)
            self._push_config()
            self._rebuild_preset_buttons()

    def _create_sample_custom_shader(self) -> None:
        """Scaffold a ready-to-edit custom GLSL shader plugin into user shaders folder and reload."""
        sdir = self.engine.store.shaders_dir
        meta = sdir / "prism_wave.prismfx.json"
        glsl = sdir / "prism_wave.glsl"
        if not glsl.exists():
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
        self.engine.registry.scan_custom_dir(sdir)
        self.tile_shaders.set_value(f"{self.engine.registry.shader_effect_count()}")
        self._select_category("custom")

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
        self._push_config()

    def _set_spotlight_color(self, hx: str) -> None:
        self.cfg.focus.spotlight_color_hex = hx
        self._push_config()

    def _save_safety_ui(self, *_args) -> None:
        sf = self.cfg.safety
        sf.pause_on_fullscreen_game = self.chk_pause_fs.isChecked()
        sf.pause_on_battery = self.chk_pause_bat.isChecked()
        sf.auto_exclude_games = self.chk_auto_game.isChecked()
        sf.suppress_native_min_anim = self.chk_suppress_min.isChecked()
        self._push_config()
        self._refresh_apps_table()

    def _refresh_apps_table(self) -> None:
        wins = self.engine.backend.list_windows(own_pid=self.engine.own_pid)
        self.apps_table.setRowCount(len(wins))
        for row, win in enumerate(wins):
            c = win.classification
            excluded = self.engine.is_excluded(win)
            self.apps_table.setRowHeight(row, 46)

            self.apps_table.setItem(row, 0, QtWidgets.QTableWidgetItem(f"{c.friendly_name} — {win.title[:28]}"))
            self.apps_table.setItem(row, 1, QtWidgets.QTableWidgetItem(win.exe_name))
            self.apps_table.setItem(row, 2, QtWidgets.QTableWidgetItem(self._tr(c.category_th, c.category)))
            self.apps_table.setItem(row, 3, QtWidgets.QTableWidgetItem(self._tr(c.reason_th, c.reason_en)))

            st_item = QtWidgets.QTableWidgetItem("EXCLUDED" if excluded else "ACTIVE FX")
            st_item.setForeground(QtGui.QColor(TOKENS["warn"] if excluded else TOKENS["ok"]))
            self.apps_table.setItem(row, 4, st_item)

            act_w = QtWidgets.QWidget()
            act_w.setStyleSheet("background: transparent;")
            al = QtWidgets.QHBoxLayout(act_w)
            al.setContentsMargins(4, 3, 4, 3)
            al.setSpacing(4)

            compact_btn_css = (
                f"QPushButton {{ padding: 2px 8px; font-size: 12px; background: {TOKENS['bg_elevated']}; "
                f"border: 1px solid {TOKENS['line_strong']}; color: {TOKENS['ink_primary']}; }}"
                f"QPushButton:hover {{ border-color: {TOKENS['accent']}; background: {TOKENS['bg_hover']}; }}"
            )
            btn_ex = QtWidgets.QPushButton(self._tr("อนุญาต", "Allow") if excluded else self._tr("ยกเว้น", "Exclude"))
            btn_ex.setFixedHeight(30)
            btn_ex.setStyleSheet(compact_btn_css)
            btn_ex.clicked.connect(lambda _=False, exe=win.exe_name: self._toggle_exclude_exe(exe))

            btn_pin = QtWidgets.QPushButton("Pin")
            btn_pin.setFixedHeight(30)
            btn_pin.setStyleSheet(compact_btn_css)
            btn_pin.clicked.connect(lambda _=False, h=win.hwnd: self.engine.toggle_pin_window(h))

            btn_roll = QtWidgets.QPushButton("Roll")
            btn_roll.setFixedHeight(30)
            btn_roll.setStyleSheet(compact_btn_css)
            btn_roll.clicked.connect(lambda _=False, h=win.hwnd: self.engine.toggle_rollup_window(h))

            btn_fx = QtWidgets.QPushButton("FX ▶")
            btn_fx.setFixedHeight(30)
            btn_fx.setStyleSheet(compact_btn_css)
            btn_fx.clicked.connect(lambda _=False, h=win.hwnd: self.engine.trigger_effect_for_window(h, "preview"))

            for b in (btn_ex, btn_pin, btn_roll, btn_fx):
                al.addWidget(b)
            self.apps_table.setCellWidget(row, 5, act_w)

    def _toggle_exclude_exe(self, exe: str) -> None:
        low = exe.lower()
        existing = [x for x in self.cfg.safety.excluded_exes if x.lower() == low]
        if existing:
            self.cfg.safety.excluded_exes = [x for x in self.cfg.safety.excluded_exes if x.lower() != low]
        else:
            self.cfg.safety.excluded_exes.append(exe)
        self._push_config()
        self._refresh_apps_table()

    def _test_active_effect_on_real_window(self) -> None:
        eid = self.cfg.triggers[self._active_trigger].effect_id
        if eid in ("none", "random"):
            eid = "ember_burn"
        wins = self.engine.backend.list_windows(own_pid=self.engine.own_pid)
        target = wins[0].hwnd if wins else int(self.winId())
        self.engine.trigger_effect_for_window(target, "preview", override_effect_id=eid, reverse_override=False)

    def _open_quick_controls(self) -> None:
        wins = self.engine.backend.list_windows(own_pid=self.engine.own_pid)
        fg = self.engine.backend.get_foreground_hwnd()
        target = fg if (fg and fg != int(self.winId())) else (wins[0].hwnd if wins else 0)
        if target:
            self.quick_bar.show_for_window(target)

    def _on_emergency_restore(self) -> None:
        self._restore_all_tiles()
        restored = self.engine.restore_all_os_state()
        self.tile_journal.set_value("RESTORED", f"{restored} items reset cleanly")

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
            self.lbl_startup_status.setText(status_note)
        elif self.cfg.start_with_windows:
            if self.cfg.start_with_windows_admin:
                self.lbl_startup_status.setText(
                    self._tr("สถานะ: เปิดอัตโนมัติพร้อมสิทธิ์ Admin (Task Scheduler — ไม่เด้ง UAC)", "Active: Elevated Logon Task (Zero-UAC Admin Boot)")
                )
            else:
                self.lbl_startup_status.setText(
                    self._tr("สถานะ: เปิดอัตโนมัติพร้อม Windows (HKCU Run)", "Active: Standard Windows Startup (HKCU Run)")
                )
        else:
            self.lbl_startup_status.setText(
                self._tr("สถานะ: ปิดการเปิดอัตโนมัติหลังเปิดเครื่อง", "Disabled: Manual launch only")
            )

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

    def _restore_all_tiles(self) -> None:
        for hwnd, tw in list(self._tile_windows.items()):
            self.engine.restore_from_tile(hwnd)
            tw.close()
        self._tile_windows.clear()

    def _toggle_language(self) -> None:
        self._lang = "en" if self._lang == "th" else "th"
        self.cfg.language = self._lang
        self._push_config()
        self._rebuild_preset_buttons()
        self._refresh_animations_grid()
        self._sync_trigger_controls()
        self._refresh_apps_table()
        # Update nav labels
        labels = {
            "home": ("01", "หน้าหลักสตูดิโอ", "Studio Home"),
            "animations": ("02", "คลังแอนิเมชัน 42+", "Animations Lab"),
            "physics": ("03", "ฟิสิกส์และการปัด", "Physics & Gestures"),
            "focus": ("04", "โฟกัสและเดสก์ท็อป", "Focus & Desktop"),
            "apps": ("05", "แอปและความปลอดภัย", "Apps & Safety"),
        }
        for k, (n, th, en) in labels.items():
            txt = f"{n}  {self._tr(th, en)}"
            self._nav_buttons[k].setText(self._qt_esc(txt))
            self._nav_buttons[k].setFont(ATLAS.font_for("body", 10.5, text=txt, bold=True))
        # Update Startup & Privilege labels
        self.chk_side_autostart.setText(self._tr_btn("เปิดพร้อม Windows", "Start with Windows"))
        self.chk_autostart.setText(
            self._tr_btn("เปิดโปรแกรมทันทีหลังเปิดเครื่อง (Start with Windows)", "Start Automatically with Windows")
        )
        self.chk_autostart_min.setText(
            self._tr_btn("เริ่มแบบย่อลง System Tray ทันที", "Start Minimized to Tray on Boot")
        )
        self.chk_autostart_admin.setText(
            self._tr_btn("ใช้สิทธิ์ผู้ดูแลตอนเปิดเครื่อง (ไม่เด้ง UAC — คุมหน้าต่าง Admin ได้)", "Elevated Boot Task (Zero-UAC Admin Startup)")
        )
        self._sync_startup_ui()

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

        self.tile_played.set_value(f"{frame.total_animations_played}")
        self.lbl_st_journal.setText(f"JOURNAL: {frame.journal_pending} ACTIVE")
        self.tile_journal.set_value(
            "CLEAN" if frame.journal_pending == 0 else f"{frame.journal_pending} TRACKED",
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
            tw = FloatingTileWindow(info, cap, self.cfg.focus.tile_size_px, self.cfg.focus.tiles_topmost)
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

    # ------------------------------------------------------------------
    # Hotkeys, Tray & Custom Window Dragging
    # ------------------------------------------------------------------
    def _wire_hotkeys(self) -> None:
        sc = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Alt+W"), self)
        sc.activated.connect(self._open_quick_controls)

    def _setup_tray(self) -> None:
        tray = QtWidgets.QSystemTrayIcon(self._app_icon, self)
        tray.setToolTip(f"{__app_name__} Studio — 165 Hz GPU Window FX")
        menu = QtWidgets.QMenu()
        menu.addAction("Open PrismFX Studio", self._restore_from_tray)
        menu.addAction("Quick Window Controls", self._open_quick_controls)
        menu.addAction("Toggle Desktop Icons", lambda: self.engine.toggle_desktop_icons())
        menu.addSeparator()
        act_auto = menu.addAction("Start with Windows")
        act_auto.setCheckable(True)
        act_auto.setChecked(bool(self.cfg.start_with_windows))
        act_auto.toggled.connect(self._on_autostart_toggled)
        self._act_tray_autostart = act_auto
        if not self._is_admin:
            self._act_tray_admin = menu.addAction("Restart as Administrator", self._on_relaunch_as_admin)
        menu.addSeparator()
        menu.addAction("Emergency Restore All Windows", self._on_emergency_restore)
        menu.addAction("Quit PrismFX", self.full_shutdown)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda r: self._restore_from_tray() if r == QtWidgets.QSystemTrayIcon.DoubleClick else None)
        tray.show()
        self._tray = tray

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
        if self._wts_registered:
            self._wts_registered = False
            try:
                w32.unregister_session_notification(int(self.winId()))
            except Exception:
                pass
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
