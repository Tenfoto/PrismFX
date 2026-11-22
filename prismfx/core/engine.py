"""Headless PrismFX Core Engine — zero Qt/UI imports.

Coordinates out-of-process WinEvent hooks, zero-flash Open/Restore choreography,
physics simulation, spotlight & inactive focus management, crash recovery journaling,
and lock-free snapshot publishing.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import win32_api as w32
from .backend import IWindowSystemBackend, WindowInfo
from .catalog import classify_app, is_protected_window_class
from .config import ConfigStore, PrismConfig
from .effects import EffectRegistry
from .journal import RecoveryJournal, WindowMutation
from .logging_setup import get_logger
from .physics import (
    DragVelocityTracker,
    MomentumBody,
    compute_snap_rect,
    detect_edge_snap_zone,
)

_log = get_logger("prismfx.core.engine")


_WM_APP_SYNC_RAWINPUT = w32.WM_APP + 11
_WM_APP_UNHOOK_DRAG = w32.WM_APP + 12

MAX_CAPTURE_CACHE_BYTES = 64 * 1024 * 1024  # Strict 64 MB capture cache memory budget

_ACTIVE_NATIVE_CALLBACKS: dict[int, object] = {}
_ACTIVE_NATIVE_CALLBACKS_LOCK = threading.Lock()

_INTERNAL_CREATE_CLASS_SUBSTRS = (
    "DummyWindow",
    "ObserverWindow",
    "ClipboardView",
    "CLIPBRDWNDCLASS",
    "CicMarshal",
    "CicLoader",
    "CiceroUI",
    "OleMainThread",
    "QEventDispatcher",
    "MSCTFIME",
    "_q_titlebar",
    "ANIMATION_TIMER",
    "SystemUserAdapter",
    "DcppUserAdapter",
    "Input Switch",
    "UAC_Input",
    "UAC Input",
    "Touch Tooltip",
    "ApplicationFrameInputSink",
    "PrismFX_LayeredOverlay",
    "XCPTimerClass",
    "XAMLMessageWindow",
    "UIAInvokeHelper",
    "URL Moniker",
    "WinUI_urlmon",
    "Shell Preview Extension",
    "MITMessageOnly",
    "LiftedDMIT",
)


def _analyze_bgra_readiness(raw: bytes) -> tuple[bool, bool]:
    """O(1) 200-point probe returning (is_blank, is_uniform) in ~0.06 ms.

    is_blank:   True if almost every sampled pixel is near-black (unrendered GPU/DComp buffer).
    is_uniform: True if < 4% of sampled pixels differ from the window's dominant canvas color
                (catches empty #191919 / #202020 / #FFFFFF shells with a 1px border before
                Win11 File Explorer / Settings / Chromium paints actual UI content!).
    """
    n = len(raw)
    if n < 800:
        return (True, True)
    # Reference canvas pixel near the center of the window (avoids 1px frame border)
    mid = ((n // 2) & ~3)
    ref_b, ref_g, ref_r = raw[mid], raw[mid + 1], raw[mid + 2]
    step = max(4, (n // 200) & ~3)
    non_zero = 0
    distinct = 0
    for off in range(0, n - 4, step):
        b = raw[off]
        g = raw[off + 1]
        r = raw[off + 2]
        if (b | g | r) > 4:
            non_zero += 1
        if abs(b - ref_b) + abs(g - ref_g) + abs(r - ref_r) > 16:
            distinct += 1
    is_blank = non_zero < 6
    is_uniform = is_blank or (distinct < 6)
    return (is_blank, is_uniform)


def _is_mostly_blank_bgra(raw: bytes) -> bool:
    """Quick O(1) probe to detect unrendered black/transparent UWP/Chromium captures."""
    return _analyze_bgra_readiness(raw)[0]


_TASKBAR_CLICK_CLASSES = frozenset({
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "MSTaskListWClass",
    "MSTaskSwWClass",
    "ReBarWindow32",
    "XamlExplorerHostIslandWindow",
    "Windows.UI.Composition.DesktopWindowContentBridge",
    "DesktopWindowXamlSource",
})


@dataclass
class _PendingOpen:
    """Newly opening window held cleanly at alpha=0 off the hook thread until its first painted frame arrives."""
    hwnd: int
    trigger: str           # open | popup
    enqueued_at: float
    orig_alpha: int
    had_layered: bool = False
    was_cloaked: bool = False
    first_uncloaked_at: float = 0.0


@dataclass
class _PendingMorph:
    """Window transitioning between normal/maximized/snapped bounds — held at alpha=0 until to_rect paints."""
    hwnd: int
    from_rect: tuple[int, int, int, int]
    from_cap: tuple[int, int, bytes]
    enqueued_at: float
    orig_alpha: int
    had_layered: bool = False
    to_rect: Optional[tuple[int, int, int, int]] = None
    target_changed_at: float = 0.0
    already_changed: bool = False


@dataclass
class MorphJob:
    """165 Hz dual-texture GPU spring morph (from_rect -> to_rect) for Maximize / Restore-Down / Edge Snap."""
    hwnd: int
    from_rect: tuple[int, int, int, int]
    to_rect: tuple[int, int, int, int]
    from_img_w: int
    from_img_h: int
    from_bgra: bytes
    to_img_w: int
    to_img_h: int
    to_bgra: Optional[bytes]
    duration_ms: int
    restore_hidden_hwnd: int = 0
    restore_alpha: int = 255
    started_at: float = field(default_factory=time.perf_counter)


@dataclass
class WobblyDragStart:
    """Payload emitted on the first pixel of a live window drag to start 165 Hz GPU Wobbly Windows."""
    hwnd: int
    x: int
    y: int
    w: int
    h: int
    img_w: int
    img_h: int
    bgra_bytes: bytes
    grab_u: float
    grab_v: float
    orig_alpha: int
    init_dx: int
    init_dy: int


@dataclass
class WobblyTextureUpdate:
    """Seamless mid-drag texture swap when a dragged window rescales across Mixed-DPI monitors."""
    hwnd: int
    w: int
    h: int
    img_w: int
    img_h: int
    bgra_bytes: bytes


def is_proportional_dpi_scale(
    old_w: int,
    old_h: int,
    new_w: int,
    new_h: int,
    crossed_monitor: bool = False,
) -> bool:
    """Return True if (old_w, old_h) -> (new_w, new_h) looks like a Mixed-DPI monitor rescale."""
    if old_w < 40 or old_h < 40 or new_w < 40 or new_h < 40:
        return False
    if old_w == new_w and old_h == new_h:
        return False
    old_ar = old_w / float(old_h)
    new_ar = new_w / float(new_h)
    if abs(new_ar - old_ar) / max(1e-6, old_ar) > 0.18:
        return False
    sw = new_w / float(old_w)
    sh = new_h / float(old_h)
    if not ((sw >= 0.98 and sh >= 0.98) or (sw <= 1.02 and sh <= 1.02)):
        return False
    if 0.55 <= sw <= 1.85 and 0.55 <= sh <= 1.85:
        return True
    if crossed_monitor and 0.35 <= sw <= 3.0 and 0.35 <= sh <= 3.0:
        return True
    return False


@dataclass
class PreCoverJob:
    """Pixel-identical pre-emptive Frame-0 cover placed BEFORE Windows unmaps a minimizing/closing window."""
    hwnd: int
    trigger: str           # minimize | close
    x: int
    y: int
    w: int
    h: int
    img_w: int
    img_h: int
    bgra_bytes: bytes
    armed_at: float = field(default_factory=time.perf_counter)


@dataclass
class AnimationJob:
    job_id: int
    hwnd: int
    trigger: str           # open | close | minimize | restore | menu | popup | preview
    effect_id: str
    shader_id: int
    reverse: bool          # True = appearing (Open/Restore), False = disappearing (Close/Min)
    duration_ms: int
    easing: str
    intensity: float
    accent_hex: str
    x: int
    y: int
    w: int
    h: int
    img_w: int
    img_h: int
    bgra_bytes: bytes
    anchor_x: float = 0.5  # normalized 0..1 target point (e.g. taskbar/cursor)
    anchor_y: float = 1.0
    restore_hidden_hwnd: int = 0     # If > 0, overlay performs zero-flash handoff on completion
    restore_alpha: int = 255
    started_at: float = field(default_factory=time.perf_counter)


@dataclass
class SpotlightState:
    active: bool = False
    mode: str = "halo"
    opacity: float = 0.42
    color_hex: str = "#06080E"
    active_hwnd: int = 0
    active_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    cursor_pos: tuple[int, int] = (0, 0)


@dataclass
class TileRequest:
    info: WindowInfo
    capture: Optional[tuple[int, int, bytes]] = None


@dataclass(frozen=True)
class EngineFrame:
    """Immutable snapshot published atomically for lock-free UI reads."""
    timestamp: float
    enabled: bool
    paused_reason: str          # "" | "fullscreen_game" | "battery" | "disabled"
    demo_mode: bool
    active_animations: int
    total_animations_played: int
    active_window_title: str
    active_window_exe: str
    active_window_hwnd: int
    rolled_up_count: int
    pinned_count: int
    tiled_count: int
    journal_pending: int
    desktop_icons_visible: bool
    on_battery: bool
    fullscreen_detected: bool
    monitors_count: int


class PrismEngine:
    """Headless desktop motion engine — completely decoupled from Qt/UI."""

    def __init__(
        self,
        backend: IWindowSystemBackend,
        store: ConfigStore,
        registry: EffectRegistry,
        own_pid: Optional[int] = None,
    ) -> None:
        self.backend = backend
        self.store = store
        self.registry = registry
        self.own_pid = own_pid if own_pid is not None else os.getpid()
        self.config: PrismConfig = store.load()
        self.journal = RecoveryJournal(store.data_dir / "recovery_journal.json")

        self._rng = random.Random()
        self._job_counter = 0
        self._total_played = 0
        self._running = False
        self._own_hwnds: set[int] = set()

        # Thread-safe queues for UI/renderer consumers
        self.animation_queue: queue.Queue[AnimationJob] = queue.Queue(maxsize=32)
        self.tile_requests: queue.Queue[TileRequest] = queue.Queue(maxsize=16)
        self.destroyed_tile_hwnds: queue.Queue[int] = queue.Queue(maxsize=64)

        # Locks for cross-thread cache and state safety
        self._cache_lock = threading.Lock()
        self._state_lock = threading.RLock()

        # Tracked runtime state
        self._cached_captures: dict[int, tuple[int, int, bytes, tuple[int, int, int, int], float]] = {}
        self._last_trigger_info: dict[int, tuple[str, float]] = {}  # hwnd -> (trigger, ts)
        self._hidden_for_anim: dict[int, tuple[int, float, bool]] = {}    # hwnd -> (orig_alpha, hidden_at, had_layered)
        self._hidden_for_wobble: dict[int, tuple[int, float, bool]] = {}  # hwnd -> (orig_alpha, heartbeat_ts, had_layered)
        self._prehidden_minimized: dict[int, tuple[int, bool]] = {}       # hwnd -> (orig_alpha, had_layered)
        self._prehidden_uniconic_since: dict[int, float] = {}       # grace period before watchdog reveals
        self._prehidden_creating: dict[int, tuple[float, float, bool]] = {}  # hwnd -> (created_at, first_visible_at, had_layered)
        self._pending_opens: dict[int, _PendingOpen] = {}           # async first-frame staging off hook thread!
        self._pending_morphs: dict[int, _PendingMorph] = {}         # async smooth maximize/snap morph staging!
        self._pending_hook_triggers: list[tuple[int, str]] = []     # async restore/menu triggers off hook thread!
        self._pending_wobble_captures: dict[int, tuple[int, int, int, int, float]] = {}  # async drag captures!
        self._pending_dpi_wobble_captures: dict[int, tuple[int, int, int, int, float]] = {}  # async Mixed-DPI recaptures!
        self._known_visible_hwnds: set[int] = set()                 # already-open windows (never fake-open on Alt+Tab)
        self._cloaked_hwnds: set[int] = set()                       # virtual-desktop cloaked known windows
        self._recent_uncloak_times: list[float] = []                # virtual-desktop burst guard
        self._last_cloak_ts: float = 0.0
        self._last_caption_click: tuple[int, int, int, float] = (0, 0, 0, 0.0)
        self._drag_last_normal_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._drag_last_normal_cap: Optional[tuple[int, int, bytes]] = None
        self._drag_morph_handled: bool = False
        self._drag_was_zoomed_start: bool = False
        self._pending_prehide: dict[int, float] = {}                # hwnd -> ready_at_ts
        self._pending_cache_hwnds: set[int] = set()                 # async cache refreshes off hook thread
        self._bg_cache_cursor: int = 0
        self._last_bg_scan: float = 0.0
        self._cached_paused_reason: str = ""
        self._battery_pause_override: bool = False
        self._hidden_lock = threading.Lock()
        self._last_active_time: dict[int, float] = {}
        self._faded_hwnds: dict[int, tuple[int, bool]] = {}
        self._custom_opacity_orig: dict[int, tuple[int, bool]] = {}
        self._rolled_up: dict[int, int] = {}
        self._pinned: set[int] = set()
        self._tiled: set[int] = set()
        self._drag_hwnd: int = 0
        self._drag_loc_hook: int = 0
        self._drag_start_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._drag_last_pos: tuple[int, int] = (0, 0)
        self._drag_orig_alpha: int = 255
        self._drag_had_layered: bool = False
        self._drag_alpha_mutated: bool = False
        self._drag_unsafe_layered: bool = False
        self._drag_grab_uv: tuple[float, float] = (0.5, 0.08)
        self._drag_wobble_started: bool = False
        self._drag_is_resize: bool = False
        self._drag_size_changed: bool = False
        self._drag_dpi_recapture_pending: bool = False
        self._drag_monitor_key: tuple[str, int, int, int, int] = ("", 0, 0, 0, 0)
        self._drag_size_changed_at: float = 0.0
        self._drag_stable_moves: int = 0
        self._drag_recapture_tries: int = 0
        self._drag_wobble_cap: Optional[tuple[int, int, bytes]] = None
        self._drag_lbutton_up_since: float = 0.0
        self._drag_last_move_ts: float = 0.0
        self._wobble_live_pos: Optional[tuple[int, int, int, int, int]] = None  # lock-free (hwnd, x, y, w, h)
        self._momentum_wobble_hwnd: int = 0
        self._drag_tracker = DragVelocityTracker()
        self._momentum: Optional[MomentumBody] = None
        self._last_desktop_click: float = 0.0
        self._last_real_fg_hwnd: int = 0
        self._armed_precover: Optional[tuple[int, str, str, Optional[tuple[int, int, int, int]], float]] = None
        self._active_precover_hwnd: int = 0
        self._spotlight = SpotlightState()
        self._suppressed_min_anim = False

        # Out-of-process WinEvent pump + separate 100% asynchronous Raw Input thread (ZERO WH_MOUSE_LL!)
        self._hook_thread: Optional[threading.Thread] = None
        self._hook_tid: int = 0
        self._raw_input_thread: Optional[threading.Thread] = None
        self._raw_input_tid: int = 0
        self._raw_input_hwnd: int = 0
        self._raw_input_registered: bool = False
        self._poll_thread: Optional[threading.Thread] = None
        self._open_stager_thread: Optional[threading.Thread] = None
        self._open_wake = threading.Event()
        self._stop_event = threading.Event()
        self._stop_lock = threading.RLock()
        self.last_restore_failed_count: int = 0
        self._win_event_hooks: list[int] = []
        self._win_event_cb_ref = None
        self._raw_wndproc_ref = None

        self._latest_frame: EngineFrame = self._build_frame(0, "")
        self.on_animation_queued: Optional[Callable[[AnimationJob], None]] = None
        self.on_morph_queued: Optional[Callable[[MorphJob], None]] = None
        self.on_precover_show: Optional[Callable[[PreCoverJob], None]] = None
        self.on_precover_cancel: Optional[Callable[[int], None]] = None
        self.on_wobbly_start: Optional[Callable[[WobblyDragStart], None]] = None
        self.on_wobbly_end: Optional[Callable[[int, int, int, bool], None]] = None
        self.on_wobbly_texture_update: Optional[Callable[[WobblyTextureUpdate], None]] = None
        self.on_wobbly_drag: Optional[Callable[[int, int, int, int, int, float, float, bool], None]] = None
        self.on_spotlight_changed: Optional[Callable[[SpotlightState], None]] = None
        self.on_pause_state_changed: Optional[Callable[[str], None]] = None

        # Crash recovery on startup!
        if self.journal.has_pending_recovery() and not backend.is_demo:
            self.restore_all_os_state()

    def register_own_hwnd(self, hwnd: int) -> None:
        if hwnd:
            with self._state_lock:
                self._own_hwnds.add(int(hwnd))

    def is_own_hwnd(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        with self._state_lock:
            return int(hwnd) in self._own_hwnds

    def is_window_pinned(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        ihwnd = int(hwnd)
        with self._state_lock:
            if ihwnd in self._pinned:
                return True
        try:
            info = self.backend.get_window_info(ihwnd)
            return bool(info and info.topmost)
        except Exception as exc:
            _log.debug("is_window_pinned lookup failed for hwnd=%#x: %s", ihwnd, exc)
            return False

    def is_window_rolled_up(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        with self._state_lock:
            return int(hwnd) in self._rolled_up

    def _window_identity(self, hwnd: int, info: Optional[WindowInfo] = None) -> tuple[int, str, int]:
        """Return (pid, class_name, boot_tick_ms) for crash-journal HWND identity verification."""
        if not hwnd:
            return (0, "", 0)
        if self.backend.is_demo:
            w_info = info or self.backend.get_window_info(hwnd)
            if w_info is not None:
                return (int(w_info.pid or 0), str(w_info.class_name or ""), 0)
            return (0, "", 0)
        pid_dw = w32.wintypes.DWORD(0)
        try:
            w32.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_dw))
            pid = int(pid_dw.value)
        except Exception:
            pid = int(info.pid) if info is not None else 0
        if not pid and info is not None and info.pid:
            pid = int(info.pid)
        try:
            cls = w32.get_class_name(hwnd) or (info.class_name if info is not None else "")
        except Exception:
            cls = info.class_name if info is not None else ""
        return (pid, str(cls or ""), w32.get_boot_tick_ms())

    def _verify_journal_mutation(self, m: WindowMutation) -> bool:
        """Verify that a journaled HWND still refers to the same OS window in the same boot session."""
        if not self.backend.is_valid_window(m.hwnd):
            return False
        if m.boot_tick_ms > 0:
            cur_boot = w32.get_boot_tick_ms()
            if (not self.backend.is_demo or cur_boot > 0) and cur_boot < m.boot_tick_ms:
                return False
        if not m.pid and not m.class_name:
            return True
        if self.backend.is_demo:
            info = self.backend.get_window_info(m.hwnd)
            live_pid = int(info.pid) if info is not None else 0
            live_cls = str(info.class_name) if info is not None else ""
        else:
            live_pid, live_cls, _ = self._window_identity(m.hwnd)
            if not live_pid and not live_cls:
                info = self.backend.get_window_info(m.hwnd)
                if info is not None:
                    live_pid = int(info.pid)
                    live_cls = str(info.class_name)
        if m.pid > 0 and live_pid != m.pid:
            return False
        if m.class_name and live_cls != m.class_name:
            return False
        return True

    def latest_frame(self) -> EngineFrame:
        return self._latest_frame

    def update_config(self, cfg: PrismConfig, persist: bool = True) -> None:
        old_ex = tuple(self.config.safety.excluded_exes)
        old_auto_games = bool(self.config.safety.auto_exclude_games)
        self.config = cfg
        if (
            tuple(cfg.safety.excluded_exes) != old_ex
            or bool(cfg.safety.auto_exclude_games) != old_auto_games
        ):
            with self._cache_lock:
                self._cached_captures.clear()
        if persist:
            self.store.save(cfg)
        self._apply_paused_reason_transition(self.paused_reason())
        if not cfg.enabled or not cfg.physics.wobbly_enabled:
            if (self._drag_wobble_started or self._momentum_wobble_hwnd) and self.on_wobbly_end:
                wh = self._drag_hwnd or self._momentum_wobble_hwnd
                self._drag_wobble_started = False
                self._drag_dpi_recapture_pending = False
                self._momentum_wobble_hwnd = 0
                try:
                    self.on_wobbly_end(wh, 0, 0, True)
                except Exception as exc:
                    _log.warning("on_wobbly_end failed in update_config for hwnd=%#x: %s", wh, exc)
        if not cfg.enabled:
            self._clear_inactive_fades()
            self._release_all_prehidden()
            self._update_spotlight_state(force_off=True)
            self._sync_native_min_anim(active=False)
        else:
            if not self._can_prehide_restore():
                self._release_all_prehidden(minimized_only=True)
            if not self._can_prehide_open():
                self._release_all_prehidden(creating_only=True)
            self._sync_native_min_anim(active=True)
        if self._raw_input_hwnd:
            try:
                w32.user32.PostMessageW(self._raw_input_hwnd, _WM_APP_SYNC_RAWINPUT, 0, 0)
            except Exception as exc:
                _log.debug("Failed to post _WM_APP_SYNC_RAWINPUT in update_config: %s", exc)
        self._publish_frame()

    def _want_raw_input_mouse(self) -> bool:
        if not self.config.enabled or self._cached_paused_reason == "fullscreen_game":
            return False
        tc_min = self.config.triggers.get("minimize")
        tc_cls = self.config.triggers.get("close")
        want_precover = bool(
            (tc_min and tc_min.enabled and tc_min.effect_id != "none")
            or (tc_cls and tc_cls.enabled and tc_cls.effect_id != "none")
        )
        return bool(
            want_precover
            or self.config.physics.smooth_maximize_enabled
            or self.config.physics.middle_click_titlebar != "none"
            or self.config.physics.double_click_desktop_icons
        )

    def _can_prehide_restore(self) -> bool:
        if not self._running or self._stop_event.is_set():
            return False
        if self.backend.is_demo or not self.config.enabled or self._cached_paused_reason:
            return False
        tc = self.config.triggers.get("restore")
        return bool(tc and tc.enabled and tc.effect_id != "none")

    def _can_prehide_open(self) -> bool:
        if not self._running or self._stop_event.is_set():
            return False
        if self.backend.is_demo or not self.config.enabled or self._cached_paused_reason:
            return False
        tc = self.config.triggers.get("open")
        return bool(tc and tc.enabled and tc.effect_id != "none")

    def _prehide_minimized_hwnd(self, hwnd: int) -> bool:
        """Pre-hide a minimized (IsIconic) window at alpha=0 so restoring it has 0 ms pre-anim flash!"""
        if not hwnd or not self._can_prehide_restore():
            return False
        if not self.backend.is_valid_window(hwnd) or not w32.user32.IsIconic(hwnd):
            return False
        with self._hidden_lock:
            if hwnd in self._prehidden_minimized or hwnd in self._hidden_for_anim:
                return True
        info = self.backend.get_window_info(hwnd)
        if not info or self.is_excluded(info) or not info.minimized:
            return False
        had_layered, cur_alpha = self.backend.get_window_layered_state(hwnd)
        if cur_alpha is None or cur_alpha == 0:
            return False  # Per-pixel ULW/ColorKey or already hidden by another owner; do not touch!
        orig_alpha = cur_alpha if cur_alpha >= 20 else 255
        if not self._running or self._stop_event.is_set():
            return False
        pid, cls_name, boot_ms = self._window_identity(hwnd, info)
        self.journal.record_alpha(
            hwnd, orig_alpha, had_layered=had_layered, pid=pid, class_name=cls_name, boot_tick_ms=boot_ms, sync=False
        )
        if self.backend.set_window_hidden_alpha(hwnd, True):
            with self._hidden_lock:
                self._prehidden_minimized[hwnd] = (orig_alpha, had_layered)
            return True
        self.journal.record_alpha(hwnd, None, sync=False)
        return False

    def _try_prehide_on_create(self, hwnd: int) -> bool:
        """Strictly filtered pre-hide on CREATE / early FOREGROUND so new windows open with 0 ms flash!"""
        if not hwnd or not self._can_prehide_open():
            return False
        with self._state_lock:
            is_own_or_known = (hwnd in self._own_hwnds) or (hwnd in self._known_visible_hwnds)
        if is_own_or_known or not w32.user32.IsWindow(hwnd):
            return False
        if w32.user32.IsIconic(hwnd):
            return False
        with self._hidden_lock:
            if (
                hwnd in self._prehidden_creating
                or hwnd in self._hidden_for_anim
                or hwnd in self._prehidden_minimized
                or hwnd in self._pending_opens
            ):
                return True
        style = w32.user32.GetWindowLongPtrW(hwnd, w32.GWL_STYLE)
        if style & w32.WS_CHILD:
            return False
        if not (style & w32.WS_CAPTION):
            return False
        if not (style & (w32.WS_SYSMENU | w32.WS_THICKFRAME | w32.WS_MINIMIZEBOX | w32.WS_MAXIMIZEBOX)):
            return False
        ex = w32.user32.GetWindowLongPtrW(hwnd, w32.GWL_EXSTYLE)
        if ex & (w32.WS_EX_TOOLWINDOW | w32.WS_EX_NOACTIVATE | w32.WS_EX_TRANSPARENT | w32.WS_EX_LAYERED):
            return False
        if int(w32.user32.GetAncestor(hwnd, w32.GA_ROOT) or 0) != hwnd:
            return False
        cls = w32.get_class_name(hwnd)
        if not cls or is_protected_window_class(cls):
            return False
        if any(sub in cls for sub in _INTERNAL_CREATE_CLASS_SUBSTRS):
            return False
        pid, exe, _ = w32.get_process_info(hwnd)
        if self.own_pid and pid == self.own_pid:
            return False
        low_exe = exe.lower()
        if low_exe and any(e.lower() == low_exe for e in self.config.safety.excluded_exes):
            return False
        if self.config.safety.auto_exclude_games and classify_app(exe, cls).recommended_exclude:
            return False
        had_layered, cur_alpha = self.backend.get_window_layered_state(hwnd)
        if cur_alpha is None or cur_alpha == 0 or had_layered:
            return False
        orig_alpha = cur_alpha if cur_alpha >= 20 else 255
        self.journal.record_alpha(
            hwnd,
            orig_alpha,
            had_layered=had_layered,
            pid=pid,
            class_name=cls,
            boot_tick_ms=w32.get_boot_tick_ms(),
            sync=False,
        )
        if self.backend.set_window_hidden_alpha(hwnd, True):
            now = time.perf_counter()
            vis_now = now if (w32.user32.IsWindowVisible(hwnd) and not w32.is_window_cloaked(hwnd)) else 0.0
            with self._hidden_lock:
                self._prehidden_creating[hwnd] = (now, vis_now, had_layered)
            return True
        self.journal.record_alpha(hwnd, None, sync=False)
        return False

    def _unhide_if_prehidden(self, hwnd: int) -> None:
        """Guarantee any pre-hidden window is immediately revealed if an animation is skipped/aborted."""
        orig_alpha: Optional[int] = None
        had_layered: Optional[bool] = None
        with self._hidden_lock:
            if hwnd in self._hidden_for_anim:
                return
            if hwnd in self._prehidden_minimized:
                orig_alpha, had_layered = self._prehidden_minimized.pop(hwnd)
            if hwnd in self._prehidden_creating:
                crt_entry = self._prehidden_creating.pop(hwnd, None)
                if orig_alpha is None:
                    orig_alpha = 255
                    had_layered = crt_entry[2] if crt_entry is not None else False
        if orig_alpha is not None:
            if self.backend.is_valid_window(hwnd):
                self.backend.set_window_hidden_alpha(hwnd, False, orig_alpha, had_layered=had_layered)
            self.journal.record_alpha(hwnd, None, sync=False)

    def _release_all_prehidden(self, minimized_only: bool = False, creating_only: bool = False) -> None:
        to_restore: list[tuple[int, int, bool]] = []
        with self._hidden_lock:
            if not creating_only:
                self._pending_prehide.clear()
                self._prehidden_uniconic_since.clear()
                for h, (a, hl) in list(self._prehidden_minimized.items()):
                    self._prehidden_minimized.pop(h, None)
                    if h not in self._hidden_for_anim:
                        to_restore.append((h, a, hl))
            if not minimized_only:
                for h, (_c, _v, hl) in list(self._prehidden_creating.items()):
                    self._prehidden_creating.pop(h, None)
                    if h not in self._hidden_for_anim:
                        to_restore.append((h, 255, hl))
        for h, orig_a, hl in to_restore:
            if self.backend.is_valid_window(h):
                self.backend.set_window_hidden_alpha(h, False, orig_a, had_layered=hl)
            self.journal.record_alpha(h, None, sync=False)

    def _sync_native_min_anim(self, active: bool) -> None:
        """Crash-safe suppression of conflicting Win11 native minimize/restore zoom."""
        if self.backend.is_demo:
            return
        want_suppress = active and self.config.enabled and self.config.safety.suppress_native_min_anim
        if want_suppress and not self._suppressed_min_anim:
            orig = self.backend.get_native_min_animate()
            if orig:
                if self.backend.set_native_min_animate(False):
                    self.journal.record_native_min_animate(True)
                    self._suppressed_min_anim = True
        elif not want_suppress and self._suppressed_min_anim:
            if self.backend.set_native_min_animate(True):
                self.journal.record_native_min_animate(None)
                self._suppressed_min_anim = False

    def is_excluded(self, info: WindowInfo) -> bool:
        with self._state_lock:
            is_own = info.hwnd in self._own_hwnds
        if is_own or (self.own_pid and info.pid == self.own_pid):
            return True
        if is_protected_window_class(info.class_name):
            return True
        if self.backend.is_overlay_window(info.hwnd):
            return True
        low_exe = info.exe_name.lower()
        if any(e.lower() == low_exe for e in self.config.safety.excluded_exes):
            return True
        if self.config.safety.auto_exclude_games and info.classification.recommended_exclude:
            return True
        return False

    def paused_reason(self) -> str:
        if not self.config.enabled:
            return "disabled"
        if self.config.safety.pause_on_fullscreen_game and self.backend.is_fullscreen_game():
            return "fullscreen_game"
        if self.config.safety.pause_on_battery and (not self._battery_pause_override) and self.backend.is_on_battery():
            return "battery"
        return ""

    def set_battery_pause_override(self, enabled: bool) -> None:
        with self._state_lock:
            self._battery_pause_override = bool(enabled)
        self._apply_paused_reason_transition(self.paused_reason())
        self._publish_frame()

    def is_battery_pause_overridden(self) -> bool:
        with self._state_lock:
            return bool(self._battery_pause_override)

    def _cancel_active_interaction_for_pause(self) -> None:
        """Cancel any active drag, wobble, momentum, or precover cleanly when entering fullscreen_game."""
        if self._drag_hwnd:
            drag_h = self._drag_hwnd
            was_wob = self._drag_wobble_started
            was_mutated = self._drag_alpha_mutated
            drag_orig_a = self._drag_orig_alpha
            drag_had_l = self._drag_had_layered
            self._drag_hwnd = 0
            self._drag_wobble_started = False
            self._drag_dpi_recapture_pending = False
            self._drag_alpha_mutated = False
            self._drag_unsafe_layered = False
            self._drag_lbutton_up_since = 0.0
            if self._hook_tid:
                try:
                    w32.user32.PostThreadMessageW(self._hook_tid, _WM_APP_UNHOOK_DRAG, 0, 0)
                except Exception as exc:
                    _log.debug("Failed to post _WM_APP_UNHOOK_DRAG during pause cancel: %s", exc)
            if (not was_wob) and was_mutated:
                if self.backend.is_valid_window(drag_h):
                    self.backend.set_window_opacity(drag_h, drag_orig_a, had_layered=drag_had_l)
                self.journal.record_alpha(drag_h, None)
            if was_wob and self.on_wobbly_end:
                try:
                    self.on_wobbly_end(drag_h, 0, 0, True)
                except Exception as exc:
                    _log.warning("on_wobbly_end failed during pause cancel for hwnd=%#x: %s", drag_h, exc)
        self._momentum = None
        if self._momentum_wobble_hwnd:
            mw_h = self._momentum_wobble_hwnd
            self._momentum_wobble_hwnd = 0
            if self.on_wobbly_end:
                try:
                    self.on_wobbly_end(mw_h, 0, 0, True)
                except Exception as exc:
                    _log.warning("on_wobbly_end failed for momentum hwnd=%#x during pause cancel: %s", mw_h, exc)
        self._drag_dpi_recapture_pending = False
        self._wobble_live_pos = None
        self._armed_precover = None
        if self._active_precover_hwnd:
            self._cancel_active_precover()
        with self._hidden_lock:
            wobble_items = list(self._hidden_for_wobble.items())
            self._hidden_for_wobble.clear()
            self._pending_wobble_captures.clear()
            self._pending_dpi_wobble_captures.clear()
        for h, (orig_a, _ts, had_layered) in wobble_items:
            if self.backend.is_valid_window(h):
                self.backend.set_window_hidden_alpha(h, False, orig_a, had_layered=had_layered)
            self.journal.record_alpha(h, None)
        self._open_wake.set()

    def _apply_paused_reason_transition(self, new_paused: str) -> bool:
        """Atomically update _cached_paused_reason and enforce full hygiene on pause transitions."""
        with self._state_lock:
            prev_paused = self._cached_paused_reason
            if new_paused == prev_paused:
                return False
            self._cached_paused_reason = new_paused

        # Immediately sync Raw Input registration (RIDEV_REMOVE on fullscreen_game, re-register on exit)
        if self._raw_input_hwnd:
            try:
                w32.user32.PostMessageW(self._raw_input_hwnd, _WM_APP_SYNC_RAWINPUT, 0, 0)
            except Exception as exc:
                _log.debug("Failed to post _WM_APP_SYNC_RAWINPUT on pause transition: %s", exc)

        if new_paused == "fullscreen_game":
            self._cancel_active_interaction_for_pause()
            self._release_all_prehidden()
            self._update_spotlight_state(force_off=True)

        cb = self.on_pause_state_changed
        if cb is not None:
            try:
                cb(new_paused)
            except Exception as exc:
                _log.warning("on_pause_state_changed callback failed (paused=%r): %s", new_paused, exc)
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._open_wake.clear()
        self._apply_paused_reason_transition(self.paused_reason())
        self._sync_native_min_anim(active=True)
        # Seed known visible windows so focusing/Alt-Tabbing existing apps never false-triggers Open,
        # and prime one-time event-driven capture queue for already-open desktop windows (zero polling)!
        initial_wins = [
            w for w in self.backend.list_windows(own_pid=self.own_pid) if not w.minimized
        ]
        seed_vis = {w.hwnd for w in initial_wins}
        with self._state_lock:
            self._known_visible_hwnds = seed_vis
        if not self.backend.is_demo:
            prime_hwnds = [w.hwnd for w in initial_wins if not self.is_excluded(w)][:6]
            if prime_hwnds:
                with self._cache_lock:
                    self._pending_cache_hwnds.update(prime_hwnds)
        self._poll_thread = threading.Thread(target=self._poll_loop, name="PrismFX-Core", daemon=True)
        self._poll_thread.start()
        if not self.backend.is_demo:
            self._open_stager_thread = threading.Thread(
                target=self._open_stager_loop, name="PrismFX-OpenStager", daemon=True
            )
            self._open_stager_thread.start()
            self._hook_thread = threading.Thread(target=self._hook_pump_loop, name="PrismFX-Hooks", daemon=True)
            self._hook_thread.start()
            self._raw_input_thread = threading.Thread(
                target=self._raw_input_loop, name="PrismFX-RawInput", daemon=True
            )
            self._raw_input_thread.start()

    def stop(self) -> None:
        with self._stop_lock:
            if not self._running:
                self.journal.flush_sync()
                return
            self._running = False
            self._stop_event.set()
            self._open_wake.set()
            if self._hook_tid:
                try:
                    w32.user32.PostThreadMessageW(self._hook_tid, w32.WM_QUIT, 0, 0)
                except Exception as exc:
                    _log.debug("Failed to post WM_QUIT to hook thread: %s", exc)
            if self._raw_input_tid:
                try:
                    w32.user32.PostThreadMessageW(self._raw_input_tid, w32.WM_QUIT, 0, 0)
                except Exception as exc:
                    _log.debug("Failed to post WM_QUIT to raw input thread: %s", exc)
            self._momentum = None
            # Pass 1: immediate synchronous restore (critical under CTRL_CLOSE / WM_ENDSESSION deadlines)
            self.restore_all_os_state()
            self._update_spotlight_state(force_off=True)
            cur = threading.current_thread()
            # Re-post WM_QUIT while joining in case hook/raw threads were still creating their Win32 message queue
            deadline = time.perf_counter() + 0.8
            for th in (self._poll_thread, self._open_stager_thread, self._hook_thread, self._raw_input_thread):
                while th and th is not cur and th.is_alive() and time.perf_counter() < deadline:
                    if self._hook_tid:
                        try:
                            w32.user32.PostThreadMessageW(self._hook_tid, w32.WM_QUIT, 0, 0)
                        except Exception:
                            pass
                    if self._raw_input_tid:
                        try:
                            w32.user32.PostThreadMessageW(self._raw_input_tid, w32.WM_QUIT, 0, 0)
                        except Exception:
                            pass
                    th.join(timeout=0.08)
            # Pass 2: final sweep in case a worker mutated a window right as stop_event fired
            if self.journal.has_pending_recovery() or self._prehidden_minimized or self._hidden_for_anim:
                self.restore_all_os_state()
            self.journal.flush_sync()

    def on_system_suspend(self) -> None:
        """Immediately release all hidden/pre-hidden/wobble-hidden windows before system sleep or session lock."""
        if self._drag_hwnd:
            drag_h = self._drag_hwnd
            was_wob = self._drag_wobble_started
            was_mutated = self._drag_alpha_mutated
            drag_orig_a = self._drag_orig_alpha
            drag_had_l = self._drag_had_layered
            self._drag_hwnd = 0
            self._drag_wobble_started = False
            self._drag_dpi_recapture_pending = False
            self._drag_alpha_mutated = False
            self._drag_unsafe_layered = False
            self._drag_lbutton_up_since = 0.0
            if self._hook_tid:
                try:
                    w32.user32.PostThreadMessageW(self._hook_tid, _WM_APP_UNHOOK_DRAG, 0, 0)
                except Exception as exc:
                    _log.debug("Failed to post _WM_APP_UNHOOK_DRAG on suspend: %s", exc)
            if (not was_wob) and was_mutated:
                if self.backend.is_valid_window(drag_h):
                    self.backend.set_window_opacity(drag_h, drag_orig_a, had_layered=drag_had_l)
                self.journal.record_alpha(drag_h, None, sync=False)
            if was_wob and self.on_wobbly_end:
                try:
                    self.on_wobbly_end(drag_h, 0, 0, True)
                except Exception as exc:
                    _log.warning("on_wobbly_end failed on suspend for hwnd=%#x: %s", drag_h, exc)
        self._momentum = None
        if self._momentum_wobble_hwnd:
            mw_h = self._momentum_wobble_hwnd
            self._momentum_wobble_hwnd = 0
            if self.on_wobbly_end:
                try:
                    self.on_wobbly_end(mw_h, 0, 0, True)
                except Exception as exc:
                    _log.warning("on_wobbly_end failed on suspend for momentum hwnd=%#x: %s", mw_h, exc)
        self._wobble_live_pos = None
        self._armed_precover = None
        if self._active_precover_hwnd:
            self._cancel_active_precover()

        with self._hidden_lock:
            hidden_items = list(self._hidden_for_anim.items())
            self._hidden_for_anim.clear()
            wobble_items = list(self._hidden_for_wobble.items())
            self._hidden_for_wobble.clear()
            pre_min_items = list(self._prehidden_minimized.items())
            self._prehidden_minimized.clear()
            pre_crt_items = list(self._prehidden_creating.items())
            self._prehidden_creating.clear()
            po_items = list(self._pending_opens.values())
            self._pending_opens.clear()
            pm_items = list(self._pending_morphs.values())
            self._pending_morphs.clear()
            self._pending_hook_triggers.clear()
            self._pending_wobble_captures.clear()
            self._pending_dpi_wobble_captures.clear()
            self._pending_prehide.clear()
            self._prehidden_uniconic_since.clear()

        restored_set: set[int] = set()
        for hwnd, (orig_a, _ts, had_layered) in hidden_items:
            restored_set.add(hwnd)
            if self.backend.is_valid_window(hwnd):
                self.backend.set_window_hidden_alpha(hwnd, False, orig_a, had_layered=had_layered)
            self.journal.record_alpha(hwnd, None, sync=False)
        for hwnd, (orig_a, _ts, had_layered) in wobble_items:
            restored_set.add(hwnd)
            if self.backend.is_valid_window(hwnd):
                self.backend.set_window_hidden_alpha(hwnd, False, orig_a, had_layered=had_layered)
            self.journal.record_alpha(hwnd, None, sync=False)
        for hwnd, (orig_a, had_layered) in pre_min_items:
            restored_set.add(hwnd)
            if self.backend.is_valid_window(hwnd):
                self.backend.set_window_hidden_alpha(hwnd, False, orig_a, had_layered=had_layered)
            self.journal.record_alpha(hwnd, None, sync=False)
        for hwnd, (_created, _vis, had_layered) in pre_crt_items:
            restored_set.add(hwnd)
            if self.backend.is_valid_window(hwnd):
                self.backend.set_window_hidden_alpha(hwnd, False, 255, had_layered=had_layered)
            self.journal.record_alpha(hwnd, None, sync=False)
        for po in po_items:
            if po.hwnd not in restored_set:
                restored_set.add(po.hwnd)
                if self.backend.is_valid_window(po.hwnd):
                    self.backend.set_window_hidden_alpha(po.hwnd, False, po.orig_alpha, had_layered=po.had_layered)
                self.journal.record_alpha(po.hwnd, None, sync=False)
        for pm in pm_items:
            if pm.hwnd not in restored_set:
                restored_set.add(pm.hwnd)
                if self.backend.is_valid_window(pm.hwnd):
                    self.backend.set_window_hidden_alpha(pm.hwnd, False, pm.orig_alpha, had_layered=pm.had_layered)
                self.journal.record_alpha(pm.hwnd, None, sync=False)

        self.journal.flush_sync()
        self._clear_inactive_fades()
        self._update_spotlight_state(force_off=True)
        self._publish_frame()

    def on_system_resume_or_display_change(self) -> None:
        """Reset stale timestamps, flush cached captures, and refresh monitor topology after wake/display change."""
        now = time.perf_counter()
        self._last_bg_scan = 0.0
        self._last_cloak_ts = 0.0
        self._recent_uncloak_times.clear()
        self._armed_precover = None
        with self._state_lock:
            self._last_trigger_info.clear()
            self._cloaked_hwnds.clear()
            for h in list(self._last_active_time):
                self._last_active_time[h] = now
        with self._cache_lock:
            self._cached_captures.clear()
            self._pending_cache_hwnds.clear()
        self._apply_paused_reason_transition(self.paused_reason())
        self.backend.list_monitors()
        self._publish_frame()

    def restore_all_os_state(self) -> int:
        """Emergency & shutdown restoration via Crash Recovery Journal."""
        with self._state_lock:
            self._faded_hwnds.clear()
            self._custom_opacity_orig.clear()
            self._rolled_up.clear()
            self._pinned.clear()
        self._suppressed_min_anim = False
        self._drag_hwnd = 0
        self._drag_wobble_started = False
        self._drag_dpi_recapture_pending = False
        self._drag_alpha_mutated = False
        self._drag_unsafe_layered = False
        self._momentum_wobble_hwnd = 0
        self._wobble_live_pos = None
        with self._hidden_lock:
            hidden_items = list(self._hidden_for_anim.items())
            self._hidden_for_anim.clear()
            wobble_items = list(self._hidden_for_wobble.items())
            self._hidden_for_wobble.clear()
            pre_min_items = list(self._prehidden_minimized.items())
            self._prehidden_minimized.clear()
            pre_crt_items = list(self._prehidden_creating.items())
            self._prehidden_creating.clear()
            self._pending_opens.clear()
            self._pending_morphs.clear()
            self._pending_hook_triggers.clear()
            self._pending_wobble_captures.clear()
            self._pending_dpi_wobble_captures.clear()
            self._pending_prehide.clear()
        for hwnd, (orig_a, _ts, had_layered) in hidden_items:
            try:
                if self.backend.is_valid_window(hwnd):
                    self.backend.set_window_hidden_alpha(hwnd, False, orig_a, had_layered=had_layered)
            except Exception as exc:
                _log.warning("restore_all_os_state failed to unhide anim hwnd=%#x: %s", hwnd, exc)
        for hwnd, (orig_a, _ts, had_layered) in wobble_items:
            try:
                if self.backend.is_valid_window(hwnd):
                    self.backend.set_window_hidden_alpha(hwnd, False, orig_a, had_layered=had_layered)
            except Exception as exc:
                _log.warning("restore_all_os_state failed to unhide wobble hwnd=%#x: %s", hwnd, exc)
        for hwnd, (orig_a, had_layered) in pre_min_items:
            try:
                if self.backend.is_valid_window(hwnd):
                    self.backend.set_window_hidden_alpha(hwnd, False, orig_a, had_layered=had_layered)
            except Exception as exc:
                _log.warning("restore_all_os_state failed to unhide minimized hwnd=%#x: %s", hwnd, exc)
        for hwnd, (_created, _vis, had_layered) in pre_crt_items:
            try:
                if self.backend.is_valid_window(hwnd):
                    self.backend.set_window_hidden_alpha(hwnd, False, 255, had_layered=had_layered)
            except Exception as exc:
                _log.warning("restore_all_os_state failed to unhide creating hwnd=%#x: %s", hwnd, exc)

        count = self.journal.restore_all(
            uncloak_fn=lambda h: True if not self.backend.is_valid_window(h) else bool(self.backend.set_window_cloaked(h, False)),
            restore_alpha_fn=lambda h, a: True if not self.backend.is_valid_window(h) else bool(self.backend.set_window_hidden_alpha(h, False, a)),
            restore_alpha_layered_fn=lambda h, a, hl: True if not self.backend.is_valid_window(h) else bool(self.backend.set_window_hidden_alpha(h, False, a, had_layered=hl)),
            unpin_fn=lambda h: True if not self.backend.is_valid_window(h) else bool(self.backend.set_window_topmost(h, False)),
            unroll_fn=self._restore_window_height,
            show_desktop_icons_fn=lambda: bool(self.backend.set_desktop_icons_visible(True)),
            restore_min_animate_fn=lambda orig: bool(self.backend.set_native_min_animate(orig)),
            verify_window_fn=self._verify_journal_mutation,
        )
        self.last_restore_failed_count = self.journal.last_restore_failed_count
        if self.last_restore_failed_count > 0:
            _log.warning(
                "restore_all_os_state finished with %d failed journal mutation(s) (restored=%d)",
                self.last_restore_failed_count,
                count,
            )
        self._publish_frame()
        return count

    def finish_hidden_handoff(
        self,
        hwnd: int,
        restore_alpha: int = 255,
        had_layered: Optional[bool] = None,
        flush_dwm: bool = True,
    ) -> None:
        """Called by overlay on the final opaque frame BEFORE hiding overlay -> zero flash!"""
        with self._hidden_lock:
            entry = self._hidden_for_anim.pop(hwnd, None)
            po_entry = self._pending_opens.pop(hwnd, None)
            pm_entry = self._pending_morphs.pop(hwnd, None)
            pre_min = self._prehidden_minimized.pop(hwnd, None)
            pre_crt = self._prehidden_creating.pop(hwnd, None)
        alpha = entry[0] if entry is not None else restore_alpha
        hl = had_layered
        if hl is None:
            if entry is not None:
                hl = entry[2]
            elif po_entry is not None:
                hl = po_entry.had_layered
            elif pm_entry is not None:
                hl = pm_entry.had_layered
            elif pre_min is not None:
                hl = pre_min[1]
            elif pre_crt is not None:
                hl = pre_crt[2]
            else:
                mut = self.journal.get_mutation(hwnd)
                hl = mut.had_layered if mut is not None else None
        if self.backend.is_valid_window(hwnd):
            self.backend.set_window_hidden_alpha(hwnd, False, alpha, had_layered=hl)
        self.journal.record_alpha(hwnd, None, sync=False)
        if flush_dwm:
            self.backend.dwm_flush()
        # Queue a background cache refresh now that the restored/opened window is visible & crisp
        if hwnd:
            with self._cache_lock:
                self._pending_cache_hwnds.add(hwnd)

    def hide_window_for_wobble(
        self, hwnd: int, orig_alpha: int = 255, had_layered: Optional[bool] = None
    ) -> bool:
        """Hide real window at alpha=0 AFTER Frame-0 wobbly overlay is live -> 0 ms flash on drag start!"""
        if self.backend.is_demo or not hwnd or not self.backend.is_valid_window(hwnd):
            return False
        with self._hidden_lock:
            existing = self._hidden_for_wobble.get(hwnd)
        if existing is not None:
            with self._hidden_lock:
                self._hidden_for_wobble[hwnd] = (existing[0], time.perf_counter(), existing[2])
            return True
        with self._state_lock:
            faded_entry = self._faded_hwnds.pop(hwnd, None)
        hl_state, cur_alpha = self.backend.get_window_layered_state(hwnd)
        if cur_alpha is None:
            if faded_entry is not None:
                with self._state_lock:
                    self._faded_hwnds[hwnd] = faded_entry
            return False  # Per-pixel ULW / ColorKey window: do NOT mutate!
        if had_layered is None:
            if hwnd == self._drag_hwnd:
                if self._drag_unsafe_layered:
                    if faded_entry is not None:
                        with self._state_lock:
                            self._faded_hwnds[hwnd] = faded_entry
                    return False
                hl = self._drag_had_layered
            elif faded_entry is not None:
                hl = faded_entry[1]
            else:
                hl = hl_state
        else:
            hl = bool(had_layered)
        if hwnd == self._drag_hwnd and self._drag_orig_alpha > 0:
            alpha = max(20, min(255, int(self._drag_orig_alpha)))
        elif faded_entry is not None:
            if orig_alpha != 255 and orig_alpha > 0:
                alpha = max(20, min(255, int(orig_alpha)))
            else:
                alpha = max(20, min(255, int(faded_entry[0])))
        elif orig_alpha == 255 and cur_alpha >= 20:
            alpha = max(20, min(255, int(cur_alpha)))
        else:
            alpha = max(20, min(255, int(orig_alpha))) if orig_alpha > 0 else 255
        pid, cls_name, boot_ms = self._window_identity(hwnd)
        self.journal.record_alpha(
            hwnd, alpha, had_layered=hl, pid=pid, class_name=cls_name, boot_tick_ms=boot_ms, sync=False
        )
        if self.backend.set_window_hidden_alpha(hwnd, True):
            with self._hidden_lock:
                self._hidden_for_wobble[hwnd] = (alpha, time.perf_counter(), hl)
            return True
        if faded_entry is not None:
            with self._state_lock:
                self._faded_hwnds[hwnd] = faded_entry
        else:
            self.journal.record_alpha(hwnd, None, sync=False)
        return False

    def finish_wobble_handoff(
        self,
        hwnd: int,
        restore_alpha: int = 255,
        had_layered: Optional[bool] = None,
        flush_dwm: bool = True,
    ) -> bool:
        """Called by overlay after jelly settles & flat opaque Frame is presented -> 0 ms handoff!"""
        with self._hidden_lock:
            entry = self._hidden_for_wobble.pop(hwnd, None)
            claimed_by_morph = (hwnd in self._hidden_for_anim) or (hwnd in self._pending_morphs)
        if claimed_by_morph:
            return False
        alpha = entry[0] if entry is not None else restore_alpha
        hl = had_layered
        if hl is None:
            if entry is not None:
                hl = entry[2]
            else:
                mut = self.journal.get_mutation(hwnd)
                hl = mut.had_layered if mut is not None else None
        if self.backend.is_valid_window(hwnd):
            self.backend.set_window_hidden_alpha(hwnd, False, alpha, had_layered=hl)
        self.journal.record_alpha(hwnd, None, sync=False)
        if flush_dwm:
            self.backend.dwm_flush()
        if hwnd:
            with self._cache_lock:
                self._pending_cache_hwnds.add(hwnd)
        return True

    def _restore_window_height(self, hwnd: int, orig_h: int) -> bool:
        if not self.backend.is_valid_window(hwnd):
            return True
        info = self.backend.get_window_info(hwnd)
        if not info or orig_h <= 40:
            return True
        return bool(self.backend.move_resize_window(hwnd, info.x, info.y, info.w, orig_h))

    # ------------------------------------------------------------------
    # Window Controls (Pin, Opacity, Roll-Up, Snap, Tile, Test FX)
    # ------------------------------------------------------------------
    def toggle_pin_window(self, hwnd: int) -> bool:
        info = self.backend.get_window_info(hwnd)
        if not info or is_protected_window_class(info.class_name):
            return False
        new_state = not info.topmost
        ok = bool(self.backend.set_window_topmost(hwnd, new_state))
        if not ok:
            self._publish_frame()
            return info.topmost
        pid, cls_name, boot_ms = self._window_identity(hwnd, info)
        self.journal.record_pin(hwnd, new_state, pid=pid, class_name=cls_name, boot_tick_ms=boot_ms)
        with self._state_lock:
            if new_state:
                self._pinned.add(hwnd)
            else:
                self._pinned.discard(hwnd)
        self._publish_frame()
        return new_state

    def set_custom_opacity(self, hwnd: int, opacity_pct: int) -> bool:
        info = self.backend.get_window_info(hwnd)
        if not info or is_protected_window_class(info.class_name):
            return False
        had_layered, cur_alpha = self.backend.get_window_layered_state(hwnd)
        if cur_alpha is None:
            return False
        alpha = max(35, min(255, int(round(opacity_pct * 2.55))))
        with self._state_lock:
            existing_orig = self._custom_opacity_orig.get(hwnd)
        if existing_orig is not None:
            orig_alpha, orig_had_layered = existing_orig
        else:
            orig_alpha, orig_had_layered = cur_alpha, had_layered
        if alpha < 255:
            ok = bool(self.backend.set_window_opacity(hwnd, alpha, had_layered=orig_had_layered))
            if ok:
                pid, cls_name, boot_ms = self._window_identity(hwnd, info)
                self.journal.record_alpha(
                    hwnd, orig_alpha, had_layered=orig_had_layered, pid=pid, class_name=cls_name, boot_tick_ms=boot_ms
                )
                with self._state_lock:
                    self._custom_opacity_orig[hwnd] = (orig_alpha, orig_had_layered)
            return ok
        ok = bool(self.backend.set_window_opacity(hwnd, alpha, had_layered=orig_had_layered))
        if ok:
            with self._state_lock:
                self._custom_opacity_orig.pop(hwnd, None)
            self.journal.record_alpha(hwnd, None)
        return ok

    def toggle_rollup_window(self, hwnd: int) -> bool:
        info = self.backend.get_window_info(hwnd)
        if not info or is_protected_window_class(info.class_name):
            return False
        with self._state_lock:
            orig_h = self._rolled_up.get(hwnd)
        if orig_h is not None:
            ok = bool(self.backend.move_resize_window(hwnd, info.x, info.y, info.w, orig_h))
            if not ok:
                self._publish_frame()
                return True
            with self._state_lock:
                self._rolled_up.pop(hwnd, None)
            self.journal.record_rollup(hwnd, False)
            self._publish_frame()
            return False
        if info.h <= 56:
            return False
        ok = bool(self.backend.move_resize_window(hwnd, info.x, info.y, info.w, 38))
        if not ok:
            self._publish_frame()
            return False
        with self._state_lock:
            self._rolled_up[hwnd] = info.h
        pid, cls_name, boot_ms = self._window_identity(hwnd, info)
        self.journal.record_rollup(hwnd, True, info.h, pid=pid, class_name=cls_name, boot_tick_ms=boot_ms)
        self._publish_frame()
        return True

    def snap_window_to_zone(
        self,
        hwnd: int,
        zone: str,
        from_rect_override: Optional[tuple[int, int, int, int]] = None,
        from_cap_override: Optional[tuple[int, int, bytes]] = None,
    ) -> bool:
        info = self.backend.get_window_info(hwnd)
        if not info or is_protected_window_class(info.class_name):
            return False
        if zone == "minimize":
            return self.backend.minimize_window(hwnd)

        # Stage zero-flash smooth spring morph BEFORE mutating bounds when Smooth Maximize/Snap is enabled!
        staged_morph = False
        if (
            self.config.enabled
            and not self._cached_paused_reason
            and self.config.physics.smooth_maximize_enabled
            and self.on_morph_queued
            and not self.backend.is_demo
            and not self.is_excluded(info)
        ):
            f_rect = from_rect_override if (from_rect_override and from_rect_override[2] >= 40) else (info.x, info.y, info.w, info.h)
            f_cap = from_cap_override
            if f_cap is None:
                with self._cache_lock:
                    cached = self._cached_captures.get(hwnd)
                if cached and abs(cached[0] - f_rect[2]) <= 16 and abs(cached[1] - f_rect[3]) <= 16:
                    f_cap = (cached[0], cached[1], cached[2])
                else:
                    fresh = self.backend.capture_window_bgra(hwnd)
                    if fresh and not _is_mostly_blank_bgra(fresh[2]):
                        f_cap = fresh
            if f_cap is not None and f_rect[2] >= 40 and f_rect[3] >= 40 and f_rect[0] > -10000:
                staged_morph = self._stage_morph_window(hwnd, f_rect, f_cap, stamp_precover=True)

        if zone == "maximize":
            ok = self.backend.maximize_window(hwnd)
        else:
            mon = self._monitor_for_point(info.x + info.w // 2, info.y + info.h // 2)
            rect = compute_snap_rect(mon, zone)
            if not rect:
                if staged_morph:
                    self._cancel_morph_for_hwnd(hwnd)
                return False
            self.backend.restore_window(hwnd)
            ok = self.backend.move_resize_window(hwnd, *rect)
        if not ok and staged_morph:
            self._cancel_morph_for_hwnd(hwnd)
        return ok

    def minimize_to_tile(self, hwnd: int) -> bool:
        info = self.backend.get_window_info(hwnd)
        if not info or is_protected_window_class(info.class_name):
            return False
        cap: Optional[tuple[int, int, bytes]] = None
        with self._cache_lock:
            cached = self._cached_captures.get(hwnd)
        if (
            cached is not None
            and abs(cached[0] - info.w) <= 16
            and abs(cached[1] - info.h) <= 16
        ):
            cap = (cached[0], cached[1], cached[2])
        else:
            fresh = self.backend.capture_window_bgra(hwnd)
            if fresh is not None:
                cap = fresh
            elif cached is not None:
                cap = (cached[0], cached[1], cached[2])
        with self._state_lock:
            self._tiled.add(hwnd)
        try:
            self.tile_requests.put_nowait(TileRequest(info=info, capture=cap))
        except queue.Full:
            pass
        self.backend.minimize_window(hwnd)
        self._publish_frame()
        return True

    def restore_from_tile(self, hwnd: int) -> None:
        with self._state_lock:
            self._tiled.discard(hwnd)
        self.backend.restore_window(hwnd)
        self._publish_frame()

    def toggle_desktop_icons(self) -> bool:
        currently = self.backend.are_desktop_icons_visible()
        new_vis = not currently
        ok = bool(self.backend.set_desktop_icons_visible(new_vis))
        if not ok:
            self._publish_frame()
            return currently
        self.journal.record_desktop_icons_hidden(not new_vis)
        self._publish_frame()
        return new_vis

    def trigger_effect_for_window(
        self,
        hwnd: int,
        trigger: str,
        override_effect_id: Optional[str] = None,
        reverse_override: Optional[bool] = None,
    ) -> Optional[AnimationJob]:
        """Capture 1:1 DWM-cropped window image & emit GPU AnimationJob with zero-flash choreography."""
        paused_now = self._cached_paused_reason if self._running else self.paused_reason()
        if trigger != "preview" and paused_now:
            if trigger in ("open", "restore"):
                self._unhide_if_prehidden(hwnd)
            return None

        # Directional per-HWND debounce: blocks Windows firing MINIMIZEEND+OBJECT_SHOW or
        # MINIMIZESTART+OBJECT_HIDE duplicates, while allowing instant Minimize <-> Restore toggles!
        now = time.perf_counter()
        if trigger != "preview":
            with self._state_lock:
                prev_trig, last_t = self._last_trigger_info.get(hwnd, ("", 0.0))
            same_dir = (
                (trigger in ("open", "restore") and prev_trig in ("open", "restore"))
                or (trigger in ("minimize", "close") and prev_trig in ("minimize", "close"))
                or (trigger == prev_trig)
            )
            min_gap = 0.35 if same_dir else 0.04
            if (now - last_t) < min_gap:
                if trigger in ("open", "restore"):
                    self._unhide_if_prehidden(hwnd)
                return None

        tc = self.config.triggers.get(trigger) or self.config.triggers["open"]
        if trigger != "preview" and not tc.enabled:
            if trigger in ("open", "restore"):
                self._unhide_if_prehidden(hwnd)
            return None

        eid = override_effect_id or tc.effect_id
        if eid == "none":
            if trigger in ("open", "restore"):
                self._unhide_if_prehidden(hwnd)
            return None
        if eid == "random":
            pool = [x for x in self.config.random_pool if x not in ("none", "random")]
            eid = self._rng.choice(pool) if pool else "prism_glide"

        spec = self.registry.get(eid)
        if spec.shader_id <= 0:
            if trigger in ("open", "restore"):
                self._unhide_if_prehidden(hwnd)
            return None

        reverse = (
            reverse_override
            if reverse_override is not None
            else (trigger in ("open", "restore", "menu", "popup"))
        )
        with self._cache_lock:
            cached = self._cached_captures.get(hwnd)
        cap_data: Optional[tuple[int, int, bytes]] = None
        rect: Optional[tuple[int, int, int, int]] = None
        hidden_orig_alpha = 255
        hidden_had_layered: Optional[bool] = None
        did_hide = False

        # ======================================================================
        # ULTRA-FAST PATH (< 0.05 ms!) FOR CACHED MINIMIZE / CLOSE:
        # Skips OpenProcess/GetWindowText/PrintWindow so Frame 0 hits screen in < 1.5 ms (ZERO black gap!)
        # ======================================================================
        if (not reverse) and trigger in ("minimize", "close") and cached is not None:
            img_w, img_h, raw_c, cached_rect, _ts = cached
            cap_data = (img_w, img_h, raw_c)
            rect = cached_rect
        else:
            should_hide_real = (
                reverse
                and trigger in ("open", "restore")
                and not self.backend.is_demo
            )

            # Claim pre-hidden window (0 ms pre-animation flash!) OR immediately hide at alpha=0
            if should_hide_real and self.backend.is_valid_window(hwnd):
                pre_orig: Optional[int] = None
                pre_hl: bool = False
                with self._hidden_lock:
                    self._pending_prehide.pop(hwnd, None)
                    if hwnd in self._prehidden_minimized:
                        pre_orig, pre_hl = self._prehidden_minimized.pop(hwnd)
                        self._prehidden_creating.pop(hwnd, None)
                    elif hwnd in self._prehidden_creating:
                        crt_entry = self._prehidden_creating.pop(hwnd, None)
                        pre_orig = 255
                        pre_hl = crt_entry[2] if crt_entry is not None else False
                    if pre_orig is not None:
                        hidden_orig_alpha = pre_orig
                        hidden_had_layered = pre_hl
                        did_hide = True
                        self._hidden_for_anim[hwnd] = (hidden_orig_alpha, now, pre_hl)

                if not did_hide:
                    pre_info = self.backend.get_window_info(hwnd)
                    if not pre_info or self.is_excluded(pre_info):
                        return None
                    had_layered, cur_alpha = self.backend.get_window_layered_state(hwnd)
                    if cur_alpha is None or cur_alpha == 0:
                        return None
                    hidden_orig_alpha = max(20, cur_alpha) if cur_alpha > 0 else 255
                    hidden_had_layered = had_layered
                    pid, cls_name, boot_ms = self._window_identity(hwnd, pre_info)
                    self.journal.record_alpha(
                        hwnd,
                        hidden_orig_alpha,
                        had_layered=had_layered,
                        pid=pid,
                        class_name=cls_name,
                        boot_tick_ms=boot_ms,
                        sync=False,
                    )
                    if self.backend.set_window_hidden_alpha(hwnd, True):
                        did_hide = True
                        with self._hidden_lock:
                            self._hidden_for_anim[hwnd] = (hidden_orig_alpha, now, had_layered)
                    else:
                        self.journal.record_alpha(hwnd, None, sync=False)
                        return None

                # Only wait if window is STILL reporting minimized (-32000) coordinates (0 ms when ready!)
                info = self.backend.get_window_info(hwnd)
                if not (info and not info.minimized and info.x > -10000 and info.w >= 80 and info.h >= 60):
                    for _ in range(5):
                        time.sleep(0.004)
                        info = self.backend.get_window_info(hwnd)
                        if info and not info.minimized and info.x > -10000 and info.w >= 80 and info.h >= 60:
                            break
            else:
                info = self.backend.get_window_info(hwnd)

            if info and info.w >= 40 and info.h >= 40 and info.x > -10000 and not info.minimized:
                if trigger != "preview" and self.is_excluded(info):
                    if did_hide:
                        self.finish_hidden_handoff(hwnd, hidden_orig_alpha, had_layered=hidden_had_layered)
                    else:
                        self._unhide_if_prehidden(hwnd)
                    return None
                rect = (info.x, info.y, info.w, info.h)

                # On RESTORE: if cached snapshot matches exact restored dimensions -> 0 ms capture latency!
                if (
                    trigger == "restore"
                    and cached is not None
                    and cached[0] == info.w
                    and cached[1] == info.h
                ):
                    cap_data = (cached[0], cached[1], cached[2])
                elif trigger in ("minimize", "close") and cached is not None:
                    cap_data = (cached[0], cached[1], cached[2])
                else:
                    fresh = self.backend.capture_window_bgra(hwnd, allow_screen_fallback=not did_hide)
                    # Newly opened or cold-restored pre-hidden windows (Electron/Chromium/UWP) may need
                    # 10-40ms to repaint their compositor surface before PrintWindow returns non-blank pixels
                    if trigger in ("open", "restore") and did_hide and (not fresh or _is_mostly_blank_bgra(fresh[2])):
                        retries = 4 if trigger == "restore" else 3
                        for _ in range(retries):
                            time.sleep(0.010 if trigger == "restore" else 0.008)
                            fresh = self.backend.capture_window_bgra(hwnd, allow_screen_fallback=False)
                            if fresh and not _is_mostly_blank_bgra(fresh[2]):
                                break
                    if fresh and not _is_mostly_blank_bgra(fresh[2]):
                        cap_data = fresh
                    elif cached is not None:
                        cap_data = (cached[0], cached[1], cached[2])
                    elif fresh and trigger != "open":
                        cap_data = fresh

            if cap_data is None and cached is not None and trigger != "open":
                img_w, img_h, raw_c, cached_rect, _ts = cached
                cap_data = (img_w, img_h, raw_c)
                if rect is None:
                    rect = cached_rect

        if cap_data is None or rect is None or rect[2] < 40 or rect[3] < 40:
            if did_hide:
                self.finish_hidden_handoff(hwnd, hidden_orig_alpha, had_layered=hidden_had_layered)
            else:
                self._unhide_if_prehidden(hwnd)
            return None

        with self._state_lock:
            self._last_trigger_info[hwnd] = (trigger, now)
        img_w, img_h, raw_bytes = cap_data
        # Refresh cache with clean capture for future close/minimize/restore (skip preview on excluded)
        if trigger != "preview" and not _is_mostly_blank_bgra(raw_bytes):
            self._store_cached_capture(hwnd, img_w, img_h, raw_bytes, rect, now)

        # Schedule pre-hide for minimized window once IsIconic settles -> 0 ms flash on next Restore!
        if trigger == "minimize" and self._can_prehide_restore():
            with self._hidden_lock:
                self._pending_prehide[hwnd] = now + 0.06

        dur_ms = max(80, int(round(tc.duration_ms / max(0.35, self.config.global_speed_scale))))
        if override_effect_id:
            dur_ms = max(120, int(round(spec.default_ms / max(0.35, self.config.global_speed_scale))))

        cx, cy = w32.get_cursor_pos() if not self.backend.is_demo else (rect[0] + rect[2] // 2, rect[1] + rect[3])
        ax = max(0.05, min(0.95, (cx - rect[0]) / max(1, rect[2])))
        ay = max(0.05, min(0.95, (cy - rect[1]) / max(1, rect[3])))
        if trigger in ("minimize", "restore"):
            ay = 1.0  # anchor cleanly toward bottom taskbar

        self._job_counter += 1
        self._total_played += 1
        job = AnimationJob(
            job_id=self._job_counter,
            hwnd=hwnd,
            trigger=trigger,
            effect_id=spec.id,
            shader_id=spec.shader_id,
            reverse=reverse,
            duration_ms=dur_ms,
            easing=tc.easing,
            intensity=tc.intensity,
            accent_hex=spec.accent_hex if override_effect_id else tc.accent_hex,
            x=rect[0],
            y=rect[1],
            w=rect[2],
            h=rect[3],
            img_w=img_w,
            img_h=img_h,
            bgra_bytes=raw_bytes,
            anchor_x=ax,
            anchor_y=ay,
            restore_hidden_hwnd=hwnd if did_hide else 0,
            restore_alpha=hidden_orig_alpha,
            started_at=now,
        )

        try:
            self.animation_queue.put_nowait(job)
        except queue.Full:
            try:
                dropped = self.animation_queue.get_nowait()
                if dropped.restore_hidden_hwnd:
                    self.finish_hidden_handoff(dropped.restore_hidden_hwnd, dropped.restore_alpha)
                self.animation_queue.put_nowait(job)
            except Exception:
                if did_hide:
                    self.finish_hidden_handoff(hwnd, hidden_orig_alpha, had_layered=hidden_had_layered)

        if self._active_precover_hwnd == hwnd and trigger in ("minimize", "close"):
            self._active_precover_hwnd = 0

        if self.on_animation_queued:
            try:
                self.on_animation_queued(job)
            except Exception as exc:
                _log.warning("on_animation_queued callback failed (job_id=%d): %s", job.job_id, exc)
        return job

    def _stage_open_window(self, hwnd: int, trigger: str = "open") -> bool:
        """Ultra-fast (< 0.15 ms) hook-thread staging for opening windows — ZERO blocking on hook thread!

        Hides/claims the newly opening window at alpha=0 and hands off first-frame waiting to
        PrismFX-OpenStager so slow WinUI 3 / XAML / Explorer / UWP / Chromium apps NEVER flash
        full-screen before animating and NEVER get dropped after 24 ms!
        """
        with self._state_lock:
            is_own = hwnd in self._own_hwnds
        if self.backend.is_demo or not hwnd or is_own or not self._trigger_enabled(trigger):
            return False
        if not self.config.enabled or self._cached_paused_reason:
            return False
        if not self.backend.is_valid_window(hwnd) or w32.user32.IsIconic(hwnd):
            return False

        now = time.perf_counter()
        with self._state_lock:
            prev_trig, last_t = self._last_trigger_info.get(hwnd, ("", 0.0))
        if prev_trig in ("open", "restore", "popup") and (now - last_t) < 0.45:
            self._unhide_if_prehidden(hwnd)
            return False

        is_cloaked = w32.is_window_cloaked(hwnd)
        with self._hidden_lock:
            # Already staged -> just wake stager (e.g. UNCLOAKED arrived right after SHOW!)
            if hwnd in self._pending_opens:
                po_ex = self._pending_opens[hwnd]
                if not is_cloaked and po_ex.first_uncloaked_at <= 0.0:
                    po_ex.first_uncloaked_at = now
                self._open_wake.set()
                return True
            if hwnd in self._prehidden_minimized or hwnd in self._hidden_for_anim:
                return False

            pre_entry = self._prehidden_creating.pop(hwnd, None)
            if pre_entry is not None:
                orig_alpha = 255
                had_layered = pre_entry[2]
                self._hidden_for_anim[hwnd] = (orig_alpha, now, had_layered)
                self._pending_opens[hwnd] = _PendingOpen(
                    hwnd=hwnd,
                    trigger=trigger,
                    enqueued_at=now,
                    orig_alpha=orig_alpha,
                    had_layered=had_layered,
                    was_cloaked=is_cloaked,
                    first_uncloaked_at=(0.0 if is_cloaked else now),
                )
                self._open_wake.set()
                return True

        # Not pre-hidden yet -> verify exclusion BEFORE touching WS_EX_LAYERED on protected/overlay/game windows!
        pid, exe, _ = w32.get_process_info(hwnd)
        if self.own_pid and pid == self.own_pid:
            return False
        cls_chk = w32.get_class_name(hwnd)
        if is_protected_window_class(cls_chk):
            return False
        if self.backend.is_overlay_window(hwnd):
            return False
        low_exe = exe.lower()
        if low_exe and any(e.lower() == low_exe for e in self.config.safety.excluded_exes):
            return False
        if self.config.safety.auto_exclude_games and classify_app(exe, cls_chk).recommended_exclude:
            return False

        had_layered, cur_alpha = self.backend.get_window_layered_state(hwnd)
        if cur_alpha is None or cur_alpha == 0:
            return False

        # Hide IMMEDIATELY before DWM composites its next frame!
        orig_alpha = max(20, cur_alpha) if cur_alpha > 0 else 255
        self.journal.record_alpha(
            hwnd,
            orig_alpha,
            had_layered=had_layered,
            pid=pid,
            class_name=cls_chk,
            boot_tick_ms=w32.get_boot_tick_ms(),
            sync=False,
        )
        if not self.backend.set_window_hidden_alpha(hwnd, True):
            self.journal.record_alpha(hwnd, None, sync=False)
            return False
        with self._hidden_lock:
            self._hidden_for_anim[hwnd] = (orig_alpha, now, had_layered)
            self._pending_opens[hwnd] = _PendingOpen(
                hwnd=hwnd,
                trigger=trigger,
                enqueued_at=now,
                orig_alpha=orig_alpha,
                had_layered=had_layered,
                was_cloaked=is_cloaked,
                first_uncloaked_at=(0.0 if is_cloaked else now),
            )
        self._open_wake.set()
        return True

    def _emit_staged_open_job(
        self, po: _PendingOpen, info: WindowInfo, cap_data: tuple[int, int, bytes], now: float
    ) -> Optional[AnimationJob]:
        tc = self.config.triggers.get(po.trigger) or self.config.triggers["open"]
        if not tc.enabled or tc.effect_id == "none":
            self.finish_hidden_handoff(po.hwnd, po.orig_alpha, had_layered=po.had_layered)
            return None
        eid = tc.effect_id
        if eid == "random":
            pool = [x for x in self.config.random_pool if x not in ("none", "random")]
            eid = self._rng.choice(pool) if pool else "prism_glide"
        spec = self.registry.get(eid)
        if spec.shader_id <= 0:
            self.finish_hidden_handoff(po.hwnd, po.orig_alpha, had_layered=po.had_layered)
            return None

        rect = (info.x, info.y, info.w, info.h)
        img_w, img_h, raw_bytes = cap_data
        with self._state_lock:
            self._last_trigger_info[po.hwnd] = (po.trigger, now)
            self._known_visible_hwnds.add(po.hwnd)
        if not _is_mostly_blank_bgra(raw_bytes):
            self._store_cached_capture(po.hwnd, img_w, img_h, raw_bytes, rect, now)

        dur_ms = max(80, int(round(tc.duration_ms / max(0.35, self.config.global_speed_scale))))
        cx, cy = w32.get_cursor_pos()
        ax = max(0.05, min(0.95, (cx - rect[0]) / max(1, rect[2])))
        ay = max(0.05, min(0.95, (cy - rect[1]) / max(1, rect[3])))

        self._job_counter += 1
        self._total_played += 1
        job = AnimationJob(
            job_id=self._job_counter,
            hwnd=po.hwnd,
            trigger=po.trigger,
            effect_id=spec.id,
            shader_id=spec.shader_id,
            reverse=True,
            duration_ms=dur_ms,
            easing=tc.easing,
            intensity=tc.intensity,
            accent_hex=tc.accent_hex,
            x=rect[0],
            y=rect[1],
            w=rect[2],
            h=rect[3],
            img_w=img_w,
            img_h=img_h,
            bgra_bytes=raw_bytes,
            anchor_x=ax,
            anchor_y=ay,
            restore_hidden_hwnd=po.hwnd,
            restore_alpha=po.orig_alpha,
            started_at=now,
        )
        try:
            self.animation_queue.put_nowait(job)
        except queue.Full:
            try:
                dropped = self.animation_queue.get_nowait()
                if dropped.restore_hidden_hwnd:
                    self.finish_hidden_handoff(dropped.restore_hidden_hwnd, dropped.restore_alpha)
                self.animation_queue.put_nowait(job)
            except Exception:
                self.finish_hidden_handoff(po.hwnd, po.orig_alpha, had_layered=po.had_layered)
                return None

        if self.on_animation_queued:
            try:
                self.on_animation_queued(job)
            except Exception as exc:
                _log.warning("on_animation_queued callback failed (job_id=%d): %s", job.job_id, exc)
        return job

    def _stage_morph_window(
        self,
        hwnd: int,
        from_rect: tuple[int, int, int, int],
        from_cap: tuple[int, int, bytes],
        stamp_precover: bool = True,
        already_changed_to: Optional[tuple[int, int, int, int]] = None,
    ) -> bool:
        """Freeze pixel-identical Frame-0 cover at from_rect, hide real window at alpha=0, and wait off-hook for to_rect."""
        if (
            self.backend.is_demo
            or not hwnd
            or not self.config.enabled
            or self._cached_paused_reason
            or not self.config.physics.smooth_maximize_enabled
            or not self.on_morph_queued
            or not self.backend.is_valid_window(hwnd)
            or w32.user32.IsIconic(hwnd)
        ):
            return False

        now = time.perf_counter()
        with self._hidden_lock:
            wob_entry = self._hidden_for_wobble.get(hwnd)
            anim_entry = self._hidden_for_anim.get(hwnd)
        if wob_entry is not None:
            orig_alpha = wob_entry[0]
            had_layered = wob_entry[2]
            already_hidden = True
        elif anim_entry is not None:
            orig_alpha = anim_entry[0]
            had_layered = anim_entry[2]
            already_hidden = True
        else:
            had_layered, cur_a = self.backend.get_window_layered_state(hwnd)
            if cur_a is None or cur_a == 0:
                return False
            orig_alpha = max(20, cur_a) if cur_a > 0 else 255
            already_hidden = False

        if stamp_precover and self.on_precover_show:
            pjob = PreCoverJob(
                hwnd=hwnd,
                trigger="maximize",
                x=from_rect[0],
                y=from_rect[1],
                w=from_rect[2],
                h=from_rect[3],
                img_w=from_cap[0],
                img_h=from_cap[1],
                bgra_bytes=from_cap[2],
                armed_at=now,
            )
            self._active_precover_hwnd = hwnd
            try:
                self.on_precover_show(pjob)
            except Exception as exc:
                self._active_precover_hwnd = 0
                _log.warning("on_precover_show failed in _stage_morph_window for hwnd=%#x: %s", hwnd, exc)

        if not already_hidden:
            pid, cls_name, boot_ms = self._window_identity(hwnd)
            self.journal.record_alpha(
                hwnd,
                orig_alpha,
                had_layered=had_layered,
                pid=pid,
                class_name=cls_name,
                boot_tick_ms=boot_ms,
                sync=False,
            )
            if not self.backend.set_window_hidden_alpha(hwnd, True):
                self.journal.record_alpha(hwnd, None, sync=False)
                if self._active_precover_hwnd == hwnd:
                    self._cancel_active_precover()
                return False

        with self._hidden_lock:
            self._hidden_for_anim[hwnd] = (orig_alpha, now, had_layered)
            self._pending_morphs[hwnd] = _PendingMorph(
                hwnd=hwnd,
                from_rect=from_rect,
                from_cap=from_cap,
                enqueued_at=now,
                orig_alpha=orig_alpha,
                had_layered=had_layered,
                to_rect=already_changed_to,
                target_changed_at=(now if already_changed_to is not None else 0.0),
                already_changed=(already_changed_to is not None),
            )
        self._open_wake.set()
        return True

    def _cancel_morph_for_hwnd(self, hwnd: int) -> None:
        with self._hidden_lock:
            pm = self._pending_morphs.pop(hwnd, None)
        if self._active_precover_hwnd == hwnd:
            self._cancel_active_precover()
        if pm is not None:
            self.finish_hidden_handoff(hwnd, pm.orig_alpha, had_layered=pm.had_layered)

    def _emit_morph_job(
        self,
        pm: _PendingMorph,
        to_rect: tuple[int, int, int, int],
        to_cap: Optional[tuple[int, int, bytes]],
        now: float,
    ) -> Optional[MorphJob]:
        dur_ms = max(
            100,
            int(round(self.config.physics.smooth_maximize_ms / max(0.35, self.config.global_speed_scale))),
        )
        if to_cap is not None and not _is_mostly_blank_bgra(to_cap[2]):
            self._store_cached_capture(pm.hwnd, to_cap[0], to_cap[1], to_cap[2], to_rect, now)
        mjob = MorphJob(
            hwnd=pm.hwnd,
            from_rect=pm.from_rect,
            to_rect=to_rect,
            from_img_w=pm.from_cap[0],
            from_img_h=pm.from_cap[1],
            from_bgra=pm.from_cap[2],
            to_img_w=to_cap[0] if to_cap else 0,
            to_img_h=to_cap[1] if to_cap else 0,
            to_bgra=to_cap[2] if to_cap else None,
            duration_ms=dur_ms,
            restore_hidden_hwnd=pm.hwnd,
            restore_alpha=pm.orig_alpha,
            started_at=now,
        )
        if self._active_precover_hwnd == pm.hwnd:
            self._active_precover_hwnd = 0
        if self.on_morph_queued:
            try:
                self.on_morph_queued(mjob)
                return mjob
            except Exception as exc:
                _log.warning("on_morph_queued callback failed for hwnd=%#x: %s", pm.hwnd, exc)
        self.finish_hidden_handoff(pm.hwnd, pm.orig_alpha, had_layered=pm.had_layered)
        return None

    def _perform_dpi_wobble_recapture(
        self, hwnd: int, x: int, y: int, w: int, h: int, now: float
    ) -> None:
        """Capture crisp new-DPI texture for an active Mixed-DPI wobble drag and emit texture swap."""
        if (
            hwnd != self._drag_hwnd
            or not self._drag_wobble_started
            or not self._drag_dpi_recapture_pending
            or not self.backend.is_valid_window(hwnd)
        ):
            return
        if not self.backend.is_demo and w32.user32.IsIconic(hwnd):
            return
        fresh = self.backend.capture_window_bgra(hwnd, allow_screen_fallback=False)
        cap_now = time.perf_counter()
        if fresh and fresh[0] >= 40 and fresh[1] >= 40 and not _is_mostly_blank_bgra(fresh[2]):
            if hwnd != self._drag_hwnd or not self._drag_wobble_started:
                return
            self._drag_dpi_recapture_pending = False
            self._drag_wobble_cap = fresh
            self._drag_last_normal_cap = fresh
            self._store_cached_capture(hwnd, fresh[0], fresh[1], fresh[2], (x, y, w, h), cap_now)
            if self.on_wobbly_texture_update:
                try:
                    self.on_wobbly_texture_update(
                        WobblyTextureUpdate(
                            hwnd=hwnd,
                            w=w,
                            h=h,
                            img_w=fresh[0],
                            img_h=fresh[1],
                            bgra_bytes=fresh[2],
                        )
                    )
                except Exception as exc:
                    _log.warning("on_wobbly_texture_update failed for hwnd=%#x: %s", hwnd, exc)
        else:
            if hwnd == self._drag_hwnd:
                self._drag_size_changed_at = cap_now

    def _open_stager_loop(self) -> None:
        """0%-idle background worker staging opens, morphs, async hook triggers & wobble captures off PrismFX-Hooks."""
        while not self._stop_event.is_set():
            try:
                self._open_wake.clear()
                with self._hidden_lock:
                    pending_list = list(self._pending_opens.values())
                    morph_list = list(self._pending_morphs.values())
                    hook_triggers = list(self._pending_hook_triggers)
                    self._pending_hook_triggers.clear()
                    wobble_caps = list(self._pending_wobble_captures.items())
                    self._pending_wobble_captures.clear()
                    dpi_wobble_caps = list(self._pending_dpi_wobble_captures.items())
                    self._pending_dpi_wobble_captures.clear()
                if not pending_list and not morph_list and not hook_triggers and not wobble_caps and not dpi_wobble_caps:
                    self._open_wake.wait(timeout=0.4)
                    continue

                # 1a) Drain async Mixed-DPI wobble texture recaptures off PrismFX-Hooks!
                for wh, (wx, wy, ww, wh_px, req_at) in dpi_wobble_caps:
                    if self._stop_event.is_set():
                        break
                    self._perform_dpi_wobble_recapture(wh, wx, wy, ww, wh_px, req_at)

                # 1b) Drain async wobble drag captures off PrismFX-Hooks (zero PrintWindow on hook thread!)
                for wh, (wx, wy, ww, wh_px, _req_at) in wobble_caps:
                    if self._stop_event.is_set():
                        break
                    if wh != self._drag_hwnd or self._drag_wobble_started or self._drag_is_resize:
                        continue
                    if not self.backend.is_valid_window(wh) or w32.user32.IsIconic(wh):
                        continue
                    fresh = self.backend.capture_window_bgra(wh)
                    cap_now = time.perf_counter()
                    if fresh and fresh[0] >= 40 and fresh[1] >= 40 and not _is_mostly_blank_bgra(fresh[2]):
                        if wh == self._drag_hwnd and not self._drag_wobble_started:
                            cur_sx, cur_sy, cur_sw, cur_sh = self._drag_start_rect
                            if abs(fresh[0] - cur_sw) <= 16 and abs(fresh[1] - cur_sh) <= 16:
                                self._drag_wobble_cap = fresh
                                self._store_cached_capture(
                                    wh, fresh[0], fresh[1], fresh[2], (cur_sx, cur_sy, cur_sw, cur_sh), cap_now
                                )
                                cx, cy = w32.get_cursor_pos()
                                live = self._wobble_live_pos
                                lx, ly = (live[1], live[2]) if (live and live[0] == wh) else (cur_sx, cur_sy)
                                gu = max(0.05, min(0.95, (cx - lx) / max(1, cur_sw)))
                                gv = max(0.02, min(0.35, (cy - ly) / max(1, cur_sh)))
                                self._drag_grab_uv = (gu, gv)
                                self._drag_tracker.reset(cap_now, lx, ly)
                    else:
                        if wh == self._drag_hwnd:
                            self._drag_size_changed_at = cap_now

                # 2) Drain async restore/menu triggers off PrismFX-Hooks (zero sleep/PrintWindow on hook thread!)
                for thwnd, tname in hook_triggers:
                    if self._stop_event.is_set():
                        break
                    if not self.config.enabled or self._cached_paused_reason:
                        if tname == "restore":
                            self._unhide_if_prehidden(thwnd)
                        continue
                    if tname == "restore":
                        if self.trigger_effect_for_window(thwnd, "restore") is None:
                            self._unhide_if_prehidden(thwnd)
                    elif tname == "menu":
                        if not self.backend.is_valid_window(thwnd):
                            continue
                        info = self.backend.get_window_info(thwnd)
                        if info and info.w >= 60 and info.h >= 40 and not self.is_excluded(info):
                            self.trigger_effect_for_window(thwnd, "menu")

                if not pending_list and not morph_list:
                    continue

                now = time.perf_counter()
                for pm in morph_list:
                    hwnd = pm.hwnd
                    if (
                        not self.config.enabled
                        or self._cached_paused_reason
                        or not self.backend.is_valid_window(hwnd)
                        or w32.user32.IsIconic(hwnd)
                    ):
                        self._cancel_morph_for_hwnd(hwnd)
                        continue

                    cur = w32.get_extended_frame_bounds(hwnd)
                    if cur[2] < 40 or cur[3] < 40 or cur[0] <= -10000:
                        if (now - pm.enqueued_at) >= 0.14:
                            self._cancel_morph_for_hwnd(hwnd)
                        continue

                    if pm.to_rect is None:
                        fx, fy, fw, fh = pm.from_rect
                        changed = (
                            abs(cur[2] - fw) >= 24
                            or abs(cur[3] - fh) >= 24
                            or abs(cur[0] - fx) >= 40
                            or abs(cur[1] - fy) >= 40
                        )
                        if not changed:
                            # If the click didn't actually resize/maximize within 130 ms, cancel cleanly!
                            if (now - pm.enqueued_at) >= 0.13:
                                self._cancel_morph_for_hwnd(hwnd)
                            continue
                        pm.to_rect = cur
                        pm.target_changed_at = now
                        continue

                    # If geometry stepped once more (e.g. restore + move), adopt latest rect
                    if cur != pm.to_rect:
                        pm.to_rect = cur
                        pm.target_changed_at = now
                        continue

                    since_change = now - pm.target_changed_at
                    # Capture crisp destination layout (give UWP/XAML ~20-90 ms to reflow!)
                    fresh = (
                        self.backend.capture_window_bgra(hwnd, allow_screen_fallback=False)
                        if since_change >= 0.015
                        else None
                    )
                    is_blank, is_uniform = _analyze_bgra_readiness(fresh[2]) if fresh else (True, True)
                    dest_ready = (fresh is not None) and (
                        (not is_blank and not is_uniform)
                        or (not is_blank and is_uniform and since_change >= 0.08)
                    )
                    if dest_ready or since_change >= 0.12:
                        with self._hidden_lock:
                            if hwnd not in self._pending_morphs:
                                continue
                            self._pending_morphs.pop(hwnd, None)
                            self._hidden_for_anim[hwnd] = (pm.orig_alpha, now, pm.had_layered)
                        to_cap = fresh if (fresh is not None and not is_blank) else None
                        self._emit_morph_job(pm, pm.to_rect, to_cap, now)

                for po in pending_list:
                    hwnd = po.hwnd
                    if (
                        not self.config.enabled
                        or self._cached_paused_reason
                        or not self.backend.is_valid_window(hwnd)
                        or w32.user32.IsIconic(hwnd)
                    ):
                        self.finish_hidden_handoff(hwnd, po.orig_alpha, had_layered=po.had_layered)
                        continue

                    if w32.is_window_cloaked(hwnd):
                        if (now - po.enqueued_at) >= 0.75:
                            self.finish_hidden_handoff(hwnd, po.orig_alpha, had_layered=po.had_layered)
                        continue

                    if po.first_uncloaked_at <= 0.0:
                        po.first_uncloaked_at = now
                    age = now - po.first_uncloaked_at

                    if not w32.user32.IsWindowVisible(hwnd):
                        if (now - po.enqueued_at) >= 0.75:
                            self.finish_hidden_handoff(hwnd, po.orig_alpha, had_layered=po.had_layered)
                        continue

                    x, y, w, h = w32.get_extended_frame_bounds(hwnd)
                    if w < 200 or h < 120 or x <= -10000:
                        if age >= 0.28:
                            self.finish_hidden_handoff(hwnd, po.orig_alpha, had_layered=po.had_layered)
                        continue

                    info = self.backend.get_window_info(hwnd)
                    if not info or self.is_excluded(info):
                        self.finish_hidden_handoff(hwnd, po.orig_alpha, had_layered=po.had_layered)
                        continue

                    # Give apps a brief window to call SetWindowTextW right after ShowWindow
                    if po.trigger == "open" and not info.title:
                        if age < 0.14:
                            continue
                        style = w32.user32.GetWindowLongPtrW(hwnd, w32.GWL_STYLE)
                        if not (style & (w32.WS_SYSMENU | w32.WS_THICKFRAME)):
                            self.finish_hidden_handoff(hwnd, po.orig_alpha, had_layered=po.had_layered)
                            continue

                    fresh = self.backend.capture_window_bgra(hwnd, allow_screen_fallback=False)
                    is_blank, is_uniform = _analyze_bgra_readiness(fresh[2]) if fresh else (True, True)

                    # Ready immediately once real UI pixels appear (>= 4% distinct from canvas);
                    # for plain solid-canvas apps (e.g. empty CMD/PowerShell), accept after 260 ms!
                    frame_ready = (fresh is not None) and (
                        (not is_blank and not is_uniform)
                        or (not is_blank and is_uniform and age >= 0.26)
                    )
                    if frame_ready and fresh is not None:
                        with self._hidden_lock:
                            if hwnd not in self._pending_opens:
                                continue
                            self._pending_opens.pop(hwnd, None)
                            # Refresh _hidden_for_anim timestamp so animation gets full 1.8s budget
                            self._hidden_for_anim[hwnd] = (po.orig_alpha, now, po.had_layered)
                        self._emit_staged_open_job(po, info, fresh, now)
                    elif age >= 0.55:
                        # Hard fail-safe cap: reveal window cleanly if it never painted a capturable frame
                        with self._state_lock:
                            self._known_visible_hwnds.add(hwnd)
                        self.finish_hidden_handoff(hwnd, po.orig_alpha, had_layered=po.had_layered)
            except Exception:
                _log.exception("Unhandled exception in PrismFX-OpenStager _open_stager_loop")

            self._stop_event.wait(0.010)

    def cached_capture_bytes(self) -> int:
        """Return total raw BGRA bytes currently held in the capture cache."""
        with self._cache_lock:
            return sum(len(entry[2]) for entry in self._cached_captures.values())

    def _store_cached_capture(
        self, hwnd: int, img_w: int, img_h: int, raw: bytes, rect: tuple[int, int, int, int], ts: float
    ) -> None:
        raw_len = len(raw)
        if raw_len <= 0 or raw_len > MAX_CAPTURE_CACHE_BYTES:
            return
        with self._cache_lock:
            self._cached_captures[hwnd] = (img_w, img_h, raw, rect, ts)
            total_bytes = sum(len(v[2]) for v in self._cached_captures.values())
            while total_bytes > MAX_CAPTURE_CACHE_BYTES and len(self._cached_captures) > 1:
                oldest = min(
                    (k for k in self._cached_captures if k != hwnd),
                    key=lambda k: self._cached_captures[k][4],
                )
                evicted = self._cached_captures.pop(oldest, None)
                if evicted is not None:
                    total_bytes -= len(evicted[2])

    def _refresh_cache_for_hwnd(self, hwnd: int, now: float) -> None:
        """Non-blocking background window capture refresh (called on Core thread, never hook thread)."""
        with self._state_lock:
            is_own = hwnd in self._own_hwnds
        if not hwnd or is_own or not self.backend.is_valid_window(hwnd):
            return
        with self._hidden_lock:
            if (
                hwnd in self._hidden_for_anim
                or hwnd in self._hidden_for_wobble
                or hwnd in self._prehidden_minimized
                or hwnd in self._prehidden_creating
                or hwnd in self._pending_opens
                or hwnd in self._pending_morphs
            ):
                return
        info = self.backend.get_window_info(hwnd)
        if not info or info.minimized or info.x <= -10000 or info.w < 40 or info.h < 40 or self.is_excluded(info):
            return
        cap = self.backend.capture_window_bgra(hwnd)
        if cap and not _is_mostly_blank_bgra(cap[2]):
            self._store_cached_capture(hwnd, cap[0], cap[1], cap[2], (info.x, info.y, info.w, info.h), now)

    def _watchdog_tick(self, now: float) -> None:
        """High-frequency safety watchdog (~40 ms): guarantees ZERO windows ever stay stuck invisible!"""
        # 1. Process pending minimized pre-hides once IsIconic(hwnd) has settled
        due_prehide: list[int] = []
        with self._hidden_lock:
            for h, ready_at in list(self._pending_prehide.items()):
                if now >= ready_at:
                    self._pending_prehide.pop(h, None)
                    due_prehide.append(h)
        for h in due_prehide:
            self._prehide_minimized_hwnd(h)

        # 2. Enforce strict timeouts & state invariants on all hidden tracking maps
        to_unhide: list[tuple[int, int, bool]] = []
        stale_journal_clear: list[int] = []
        is_demo = self.backend.is_demo
        with self._hidden_lock:
            for h, (orig_a, ts, hl) in list(self._hidden_for_anim.items()):
                if (now - ts) > 1.8:
                    self._hidden_for_anim.pop(h, None)
                    to_unhide.append((h, orig_a, hl))

            for h, (created_at, vis_at, hl) in list(self._prehidden_creating.items()):
                if not self.backend.is_valid_window(h):
                    self._prehidden_creating.pop(h, None)
                    stale_journal_clear.append(h)
                elif h in self._hidden_for_anim or h in self._pending_opens:
                    self._prehidden_creating.pop(h, None)
                else:
                    vis_uncloaked = (
                        True
                        if is_demo
                        else (bool(w32.user32.IsWindowVisible(h)) and not w32.is_window_cloaked(h))
                    )
                    if vis_uncloaked:
                        if vis_at <= 0.0:
                            vis_at = now
                            self._prehidden_creating[h] = (created_at, vis_at, hl)
                        # Once visible & uncloaked, reveal quickly (0.35s) if SHOW/UNCLOAKED didn't claim it
                        if (now - vis_at) > 0.35:
                            self._prehidden_creating.pop(h, None)
                            to_unhide.append((h, 255, hl))
                    elif (now - created_at) > 3.5:
                        # Still invisible/cloaked (slow XAML/Electron startup): generous 3.5s safety cap
                        self._prehidden_creating.pop(h, None)
                        to_unhide.append((h, 255, hl))

            for h, (orig_a, hl) in list(self._prehidden_minimized.items()):
                if not self.backend.is_valid_window(h):
                    self._prehidden_minimized.pop(h, None)
                    self._prehidden_uniconic_since.pop(h, None)
                    stale_journal_clear.append(h)
                else:
                    if is_demo:
                        w_info = self.backend.get_window_info(h)
                        still_iconic = bool(w_info and w_info.minimized)
                    else:
                        still_iconic = bool(w32.user32.IsIconic(h))
                    if not still_iconic:
                        # Give EVENT_SYSTEM_MINIMIZEEND a 250ms grace window to claim the window cleanly
                        # before assuming it was restored outside our hook!
                        if h in self._hidden_for_anim:
                            self._prehidden_minimized.pop(h, None)
                            self._prehidden_uniconic_since.pop(h, None)
                        else:
                            first_seen = self._prehidden_uniconic_since.setdefault(h, now)
                            if (now - first_seen) >= 0.25:
                                self._prehidden_minimized.pop(h, None)
                                self._prehidden_uniconic_since.pop(h, None)
                                to_unhide.append((h, orig_a, hl))
                    else:
                        self._prehidden_uniconic_since.pop(h, None)

        for h in stale_journal_clear:
            self.journal.record_alpha(h, None, sync=False)

        for h, orig_a, hl in to_unhide:
            if self.backend.is_valid_window(h):
                self.backend.set_window_hidden_alpha(h, False, orig_a, had_layered=hl)
            self.journal.record_alpha(h, None, sync=False)

        # 3. Fail-safe drag & wobble watchdog: catch crashed app OR missed MOVESIZEEND (e.g. UAC/Alt+Tab)
        if self._drag_hwnd:
            drag_h = self._drag_hwnd
            if not self.backend.is_valid_window(drag_h):
                self._drag_hwnd = 0
                self._drag_wobble_started = False
                self._drag_dpi_recapture_pending = False
                self._drag_alpha_mutated = False
                self._drag_unsafe_layered = False
                self._drag_lbutton_up_since = 0.0
                if self.journal.get_mutation(drag_h) is not None:
                    self.journal.prune_hwnd(drag_h, sync=False)
                if self._hook_tid:
                    try:
                        w32.user32.PostThreadMessageW(self._hook_tid, _WM_APP_UNHOOK_DRAG, 0, 0)
                    except Exception as exc:
                        _log.debug("Watchdog failed to post _WM_APP_UNHOOK_DRAG (invalid hwnd): %s", exc)
                if self.on_wobbly_end:
                    try:
                        self.on_wobbly_end(drag_h, 0, 0, True)
                    except Exception as exc:
                        _log.warning("Watchdog on_wobbly_end failed for invalid hwnd=%#x: %s", drag_h, exc)
            elif self._cached_paused_reason == "fullscreen_game":
                self._drag_lbutton_up_since = 0.0
            elif not w32.is_lbutton_down():
                if self._drag_lbutton_up_since <= 0.0:
                    self._drag_lbutton_up_since = now
                elif (now - self._drag_lbutton_up_since) > 0.45 and (now - self._drag_last_move_ts) > 0.45:
                    self._drag_hwnd = 0
                    was_wob = self._drag_wobble_started
                    was_mutated = self._drag_alpha_mutated
                    drag_orig_a = self._drag_orig_alpha
                    drag_had_l = self._drag_had_layered
                    self._drag_wobble_started = False
                    self._drag_dpi_recapture_pending = False
                    self._drag_alpha_mutated = False
                    self._drag_unsafe_layered = False
                    self._drag_lbutton_up_since = 0.0
                    if self._hook_tid:
                        try:
                            w32.user32.PostThreadMessageW(self._hook_tid, _WM_APP_UNHOOK_DRAG, 0, 0)
                        except Exception as exc:
                            _log.debug("Watchdog failed to post _WM_APP_UNHOOK_DRAG (stale drag): %s", exc)
                    if (not was_wob) and was_mutated:
                        if self.backend.is_valid_window(drag_h):
                            self.backend.set_window_opacity(drag_h, drag_orig_a, had_layered=drag_had_l)
                        self.journal.record_alpha(drag_h, None, sync=False)
                    if was_wob and self.on_wobbly_end:
                        if is_demo:
                            d_info = self.backend.get_window_info(drag_h)
                            x, y = (d_info.x, d_info.y) if d_info else (0, 0)
                        else:
                            x, y, _, _ = w32.get_extended_frame_bounds(drag_h)
                        try:
                            self.on_wobbly_end(drag_h, x, y, True)
                        except Exception as exc:
                            _log.warning("Watchdog on_wobbly_end failed for stale drag hwnd=%#x: %s", drag_h, exc)
            else:
                self._drag_lbutton_up_since = 0.0

        stale_wobble: list[tuple[int, int, bool]] = []
        active_wob_hwnd = self._drag_hwnd or self._momentum_wobble_hwnd
        with self._hidden_lock:
            for h, (orig_a, ts, hl) in list(self._hidden_for_wobble.items()):
                if h == active_wob_hwnd:
                    self._hidden_for_wobble[h] = (orig_a, now, hl)  # refresh heartbeat while dragging/gliding
                elif (now - ts) > 1.2:
                    self._hidden_for_wobble.pop(h, None)
                    stale_wobble.append((h, orig_a, hl))
        for h, orig_a, hl in stale_wobble:
            if self.backend.is_valid_window(h):
                self.backend.set_window_hidden_alpha(h, False, orig_a, had_layered=hl)
            self.journal.record_alpha(h, None, sync=False)

    # ------------------------------------------------------------------
    # Background Core Loop (Physics, Watchdog, Caching, Spotlight, Auto-Min, Fade)
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        last_slow = 0.0
        last_fast = 0.0
        last_tick = time.perf_counter()

        while not self._stop_event.is_set():
            try:
                now = time.perf_counter()
                dt = min(0.05, now - last_tick)
                last_tick = now

                mom = self._momentum
                if mom is not None:
                    mh = mom.hwnd
                    mw = mom.w
                    mh_px = mom.h
                    nx, ny, moving = mom.step(dt)
                    if self.backend.is_valid_window(mh):
                        self.backend.move_window_only(mh, nx, ny)
                        if self._momentum_wobble_hwnd == mh:
                            self._wobble_live_pos = (mh, nx, ny, mw, mh_px)
                    if not moving:
                        if self._momentum is mom:
                            self._momentum = None
                        if self._momentum_wobble_hwnd == mh:
                            self._momentum_wobble_hwnd = 0
                            if self.on_wobbly_end:
                                try:
                                    self.on_wobbly_end(mh, nx, ny, False)
                                except Exception as exc:
                                    _log.warning("on_wobbly_end failed after momentum stop for hwnd=%#x: %s", mh, exc)

                if now - last_fast >= 0.04:
                    last_fast = now
                    self._watchdog_tick(now)
                    if not self._cached_paused_reason:
                        for _ in range(2):
                            with self._cache_lock:
                                if not self._pending_cache_hwnds:
                                    break
                                ch = self._pending_cache_hwnds.pop()
                            self._refresh_cache_for_hwnd(ch, now)

                if now - last_slow >= 0.35:
                    last_slow = now
                    self._housekeeping_tick(now)
            except Exception:
                _log.exception("Unhandled exception in PrismFX-Core _poll_loop")

            if self._momentum is not None:
                sleep_sec = max(0.0025, 1.0 / max(30, min(360, int(self.config.target_fps))))
            else:
                sleep_sec = 0.04
            self._stop_event.wait(sleep_sec)

    def _housekeeping_tick(self, now: float) -> None:
        # Auto-clear temporary battery pause override once AC power is restored
        if self._battery_pause_override and not self.backend.is_on_battery():
            with self._state_lock:
                self._battery_pause_override = False

        # Refresh paused_reason ONCE per 350 ms here (zero SHQueryUserNotificationState on hooks!)
        paused = self.paused_reason()
        self._apply_paused_reason_transition(paused)

        self._watchdog_tick(now)

        do_bg_scan = (now - self._last_bg_scan) >= 2.0
        if do_bg_scan:
            self._last_bg_scan = now

        if paused:
            self._release_all_prehidden()
        elif do_bg_scan and self._can_prehide_restore():
            # Pre-hide any eligible already-minimized windows (e.g. minimized before PrismFX launched!)
            for iconic_hwnd in w32.list_iconic_top_windows(own_pid=self.own_pid):
                if self._stop_event.is_set():
                    return
                with self._state_lock:
                    is_own = iconic_hwnd in self._own_hwnds
                if not is_own:
                    self._prehide_minimized_hwnd(iconic_hwnd)

        if self._stop_event.is_set():
            return

        fg = self.backend.get_foreground_hwnd()
        fg_info = self.backend.get_window_info(fg) if fg else None

        # Pre-cache clean 1:1 snapshot of active window + keep rect synced cheaply every 0.35 s
        if (
            fg_info
            and not paused
            and fg != self._drag_hwnd
            and fg != self._momentum_wobble_hwnd
            and fg not in self._hidden_for_wobble
            and not self.is_excluded(fg_info)
            and not fg_info.minimized
            and fg_info.x > -10000
        ):
            self._last_real_fg_hwnd = fg
            with self._state_lock:
                self._last_active_time[fg] = now
            cur_rect = (fg_info.x, fg_info.y, fg_info.w, fg_info.h)
            with self._cache_lock:
                prev = self._cached_captures.get(fg)
                if prev and prev[0] == fg_info.w and prev[1] == fg_info.h and prev[3] != cur_rect:
                    self._cached_captures[fg] = (prev[0], prev[1], prev[2], cur_rect, prev[4])
            # Event-driven active capture (zero periodic PrintWindow polling when idle!)
            need_fg_cap = prev is None or prev[0] != fg_info.w or prev[1] != fg_info.h
            if not need_fg_cap and prev is not None and (now - prev[4]) > 2.5 and not self.backend.is_demo:
                cx, cy = w32.get_cursor_pos()
                if (
                    fg_info.x <= cx <= fg_info.x + fg_info.w
                    and fg_info.y <= cy <= fg_info.y + 48
                ):
                    need_fg_cap = True
            if need_fg_cap:
                cap = self.backend.capture_window_bgra(fg)
                if cap and not _is_mostly_blank_bgra(cap[2]):
                    self._store_cached_capture(fg, cap[0], cap[1], cap[2], cur_rect, now)

        # Reuse at most ONE EnumWindows scan per housekeeping tick (zero redundant GIL pressure!)
        need_win_list = (
            not paused
            and not self.backend.is_demo
            and (
                do_bg_scan
                or self.config.focus.inactive_fade_enabled
                or self.config.focus.auto_minimize_enabled
            )
        )
        vis_wins: Optional[list[WindowInfo]] = (
            self.backend.list_windows(own_pid=self.own_pid) if need_win_list else None
        )

        # Round-robin cache 1 uncached/resized background visible window every 2 s (no periodic 8s re-poll!)
        if do_bg_scan and vis_wins is not None:
            eligible = [w for w in vis_wins if not w.minimized and w.hwnd != fg and not self.is_excluded(w)]
            if eligible:
                w_bg = eligible[self._bg_cache_cursor % len(eligible)]
                self._bg_cache_cursor += 1
                with self._cache_lock:
                    prev_bg = self._cached_captures.get(w_bg.hwnd)
                if prev_bg is None or prev_bg[0] != w_bg.w or prev_bg[1] != w_bg.h:
                    self._refresh_cache_for_hwnd(w_bg.hwnd, now)

        if not paused and self.config.focus.spotlight_enabled and fg_info and not self.is_excluded(fg_info):
            self._update_spotlight_state(fg_info=fg_info)
        else:
            self._update_spotlight_state(force_off=True)

        if not paused and self.config.focus.inactive_fade_enabled and not self.backend.is_demo and vis_wins is not None:
            target_alpha = max(60, min(255, int(round(self.config.focus.inactive_opacity_pct * 2.55))))
            if fg:
                with self._state_lock:
                    fg_faded = self._faded_hwnds.get(fg)
                if fg_faded is not None:
                    with self._hidden_lock:
                        fg_is_hidden = (
                            fg in self._hidden_for_wobble
                            or fg in self._hidden_for_anim
                            or fg in self._prehidden_minimized
                            or fg in self._prehidden_creating
                            or fg in self._pending_opens
                            or fg in self._pending_morphs
                        )
                    if not fg_is_hidden:
                        orig_a, had_layered = fg_faded
                        if (not self.backend.is_valid_window(fg)) or self.backend.set_window_opacity(
                            fg, orig_a, had_layered=had_layered
                        ):
                            with self._state_lock:
                                self._faded_hwnds.pop(fg, None)
                            self.journal.record_alpha(fg, None)
            for win in vis_wins:
                if self.is_excluded(win) or win.minimized:
                    continue
                if win.hwnd == fg:
                    with self._state_lock:
                        win_faded = self._faded_hwnds.get(win.hwnd)
                    if win_faded is not None:
                        with self._hidden_lock:
                            win_is_hidden = (
                                win.hwnd in self._hidden_for_wobble
                                or win.hwnd in self._hidden_for_anim
                                or win.hwnd in self._prehidden_minimized
                                or win.hwnd in self._prehidden_creating
                                or win.hwnd in self._pending_opens
                                or win.hwnd in self._pending_morphs
                            )
                        if not win_is_hidden:
                            orig_a, had_layered = win_faded
                            if self.backend.set_window_opacity(win.hwnd, orig_a, had_layered=had_layered):
                                with self._state_lock:
                                    self._faded_hwnds.pop(win.hwnd, None)
                                self.journal.record_alpha(win.hwnd, None)
                else:
                    with self._state_lock:
                        already_faded = win.hwnd in self._faded_hwnds
                    if not already_faded:
                        with self._hidden_lock:
                            is_hidden_now = (
                                win.hwnd in self._hidden_for_anim
                                or win.hwnd in self._hidden_for_wobble
                                or win.hwnd in self._prehidden_minimized
                                or win.hwnd in self._prehidden_creating
                                or win.hwnd in self._pending_opens
                                or win.hwnd in self._pending_morphs
                            )
                        if is_hidden_now or win.hwnd == self._drag_hwnd:
                            continue
                        had_layered, cur_alpha = self.backend.get_window_layered_state(win.hwnd)
                        if cur_alpha is None or cur_alpha == 0:
                            continue
                        orig_a = max(20, cur_alpha) if cur_alpha > 0 else 255
                        if self.backend.set_window_opacity(win.hwnd, target_alpha):
                            pid, cls_name, boot_ms = self._window_identity(win.hwnd, win)
                            self.journal.record_alpha(
                                win.hwnd, orig_a, had_layered=had_layered, pid=pid, class_name=cls_name, boot_tick_ms=boot_ms
                            )
                            with self._state_lock:
                                self._faded_hwnds[win.hwnd] = (orig_a, had_layered)
            with self._state_lock:
                faded_keys = list(self._faded_hwnds)
            for h in faded_keys:
                if not self.backend.is_valid_window(h):
                    with self._state_lock:
                        self._faded_hwnds.pop(h, None)
                    self.journal.record_alpha(h, None)
        else:
            with self._state_lock:
                has_faded = bool(self._faded_hwnds)
            if has_faded:
                self._clear_inactive_fades()

        if not paused and self.config.focus.auto_minimize_enabled and not self.backend.is_demo and vis_wins is not None:
            idle_limit = max(60.0, self.config.focus.auto_minimize_minutes * 60.0)
            for win in vis_wins:
                if win.hwnd == fg or win.minimized or self.is_excluded(win):
                    continue
                with self._state_lock:
                    last_t = self._last_active_time.setdefault(win.hwnd, now)
                if (now - last_t) >= idle_limit:
                    self.backend.minimize_window(win.hwnd)
                    with self._state_lock:
                        self._last_active_time[win.hwnd] = now

        if not paused and self.config.focus.desktop_icons_autohide and not self.backend.is_demo:
            fg_cls = fg_info.class_name if fg_info else ""
            on_desktop = fg_cls in ("Progman", "WorkerW")
            cur_vis = self.backend.are_desktop_icons_visible()
            if on_desktop and not cur_vis:
                if self.backend.set_desktop_icons_visible(True):
                    self.journal.record_desktop_icons_hidden(False)
            elif not on_desktop and cur_vis:
                if self.backend.set_desktop_icons_visible(False):
                    self.journal.record_desktop_icons_hidden(True)

        self._publish_frame(fg_info=fg_info)

    def _clear_inactive_fades(self) -> None:
        with self._state_lock:
            faded_items = list(self._faded_hwnds.items())
        for hwnd, (orig_a, had_layered) in faded_items:
            with self._hidden_lock:
                is_hidden_now = (
                    hwnd in self._hidden_for_wobble
                    or hwnd in self._hidden_for_anim
                    or hwnd in self._prehidden_minimized
                    or hwnd in self._prehidden_creating
                    or hwnd in self._pending_opens
                    or hwnd in self._pending_morphs
                )
            if is_hidden_now:
                with self._state_lock:
                    self._faded_hwnds.pop(hwnd, None)
                continue
            if (not self.backend.is_valid_window(hwnd)) or self.backend.set_window_opacity(
                hwnd, orig_a, had_layered=had_layered
            ):
                with self._state_lock:
                    self._faded_hwnds.pop(hwnd, None)
                self.journal.record_alpha(hwnd, None)

    def _update_spotlight_state(self, fg_info: Optional[WindowInfo] = None, force_off: bool = False) -> None:
        if force_off or not fg_info or self._cached_paused_reason:
            changed = self._spotlight.active
            self._spotlight = SpotlightState(active=False)
            if changed and self.on_spotlight_changed:
                self.on_spotlight_changed(self._spotlight)
            return

        mode = self.config.focus.spotlight_mode
        if mode == "cursor":
            cursor = (
                w32.get_cursor_pos()
                if not self.backend.is_demo
                else (fg_info.x + fg_info.w // 2, fg_info.y + fg_info.h // 2)
            )
        else:
            cursor = (0, 0)
        new_st = SpotlightState(
            active=True,
            mode=mode,
            opacity=self.config.focus.spotlight_opacity_pct / 100.0,
            color_hex=self.config.focus.spotlight_color_hex,
            active_hwnd=fg_info.hwnd,
            active_rect=(fg_info.x, fg_info.y, fg_info.w, fg_info.h),
            cursor_pos=cursor,
        )
        if new_st != self._spotlight:
            self._spotlight = new_st
            if self.on_spotlight_changed:
                self.on_spotlight_changed(new_st)

    # ------------------------------------------------------------------
    # Out-of-Process Windows Event Hook & Async Raw Input Pump (ZERO DLL injection, ZERO WH_MOUSE_LL!)
    # ------------------------------------------------------------------
    def _attach_drag_location_hook(self, hwnd: int) -> None:
        """Install EVENT_OBJECT_LOCATIONCHANGE strictly for the single dragged thread (0 idle overhead!)."""
        self._detach_drag_location_hook()
        if not hwnd or self._win_event_cb_ref is None:
            return
        pid = w32.wintypes.DWORD(0)
        tid = int(w32.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or 0)
        if not pid.value or not tid:
            return
        flags = w32.WINEVENT_OUTOFCONTEXT | w32.WINEVENT_SKIPOWNPROCESS
        h = w32.user32.SetWinEventHook(
            w32.EVENT_OBJECT_LOCATIONCHANGE,
            w32.EVENT_OBJECT_LOCATIONCHANGE,
            0,
            self._win_event_cb_ref,
            pid.value,
            tid,
            flags,
        )
        self._drag_loc_hook = int(h or 0)

    def _detach_drag_location_hook(self) -> bool:
        if self._drag_loc_hook:
            h = self._drag_loc_hook
            self._drag_loc_hook = 0
            return bool(w32.user32.UnhookWinEvent(h))
        return True

    def _hook_pump_loop(self) -> None:
        self._hook_tid = int(w32.kernel32.GetCurrentThreadId())

        @w32.WINEVENTPROC
        def _on_win_event(h_hook, event, hwnd, id_object, id_child, _tid, _ms):
            try:
                if self._stop_event.is_set() or id_object != w32.OBJID_WINDOW or id_child != w32.CHILDID_SELF or not hwnd:
                    return
                ihwnd = int(hwnd)
                with self._state_lock:
                    is_own = ihwnd in self._own_hwnds
                if is_own:
                    return
                ev = int(event)
                # Read cached pause state (0 syscalls! NEVER call SHQueryUserNotificationState here!)
                if not self.config.enabled or self._cached_paused_reason:
                    if ev == w32.EVENT_OBJECT_DESTROY:
                        self._handle_win_event(ev, ihwnd)
                    elif ev in (w32.EVENT_SYSTEM_MINIMIZEEND, w32.EVENT_OBJECT_SHOW, w32.EVENT_OBJECT_UNCLOAKED):
                        self._unhide_if_prehidden(ihwnd)
                    return
                self._handle_win_event(ev, ihwnd)
            except Exception:
                pass

        self._win_event_cb_ref = _on_win_event
        cb_key = id(_on_win_event)
        with _ACTIVE_NATIVE_CALLBACKS_LOCK:
            _ACTIVE_NATIVE_CALLBACKS[cb_key] = _on_win_event
        try:
            flags = w32.WINEVENT_OUTOFCONTEXT | w32.WINEVENT_SKIPOWNPROCESS
            # Note: EVENT_OBJECT_LOCATIONCHANGE is intentionally NOT hooked globally (zero taskbar/cursor flood!)
            ranges = [
                (w32.EVENT_SYSTEM_FOREGROUND, w32.EVENT_SYSTEM_FOREGROUND),
                (w32.EVENT_SYSTEM_MENUPOPUPSTART, w32.EVENT_SYSTEM_MENUPOPUPEND),
                (w32.EVENT_SYSTEM_MOVESIZESTART, w32.EVENT_SYSTEM_MOVESIZEEND),
                (w32.EVENT_SYSTEM_MINIMIZESTART, w32.EVENT_SYSTEM_MINIMIZEEND),
                (w32.EVENT_OBJECT_CREATE, w32.EVENT_OBJECT_HIDE),
                (w32.EVENT_OBJECT_CLOAKED, w32.EVENT_OBJECT_UNCLOAKED),
            ]
            for lo, hi in ranges:
                h = w32.user32.SetWinEventHook(lo, hi, 0, self._win_event_cb_ref, 0, 0, flags)
                if h:
                    self._win_event_hooks.append(int(h))

            # Blocking GetMessageW: < 0.2 ms wakeup latency & 0% CPU when idle
            msg = w32.wintypes.MSG()
            while not self._stop_event.is_set():
                ret = w32.user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if ret <= 0:
                    break
                if msg.message == _WM_APP_UNHOOK_DRAG:
                    self._detach_drag_location_hook()
                    continue
                w32.user32.TranslateMessage(ctypes.byref(msg))
                w32.user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            logging.getLogger(__name__).exception("Unhandled exception in PrismFX-Hooks _hook_pump_loop")
        finally:
            self._hook_tid = 0
            all_unhooked = self._detach_drag_location_hook()
            for h in self._win_event_hooks:
                if not w32.user32.UnhookWinEvent(h):
                    all_unhooked = False
            self._win_event_hooks.clear()
            if all_unhooked:
                with _ACTIVE_NATIVE_CALLBACKS_LOCK:
                    _ACTIVE_NATIVE_CALLBACKS.pop(cb_key, None)

    def _raw_input_loop(self) -> None:
        """100% asynchronous Win32 Raw Input mouse click sink (RIDEV_INPUTSINK on HWND_MESSAGE).

        Unlike WH_MOUSE_LL (which synchronously stalls the Windows hardware mouse cursor on every
        WM_MOUSEMOVE whenever Python/GIL/WinEvents are busy), WM_INPUT is posted asynchronously
        AFTER the OS moves the cursor — making cursor stutter kernel-level impossible!
        """
        self._raw_input_tid = int(w32.kernel32.GetCurrentThreadId())
        hinst = w32.kernel32.GetModuleHandleW(None)
        cls_name = f"PrismFX_RawInputSink_{id(self)}"

        raw_buf = w32.RAWINPUT_MOUSE()
        full_sz = ctypes.sizeof(w32.RAWINPUT_MOUSE)
        hdr_sz = ctypes.sizeof(w32.RAWINPUTHEADER)
        raw_sz = w32.wintypes.UINT(full_sz)
        want_mask = (
            w32.RI_MOUSE_MIDDLE_BUTTON_DOWN
            | w32.RI_MOUSE_LEFT_BUTTON_DOWN
            | w32.RI_MOUSE_LEFT_BUTTON_UP
        )

        def _sync_reg(sink_hwnd: int) -> None:
            want = self._want_raw_input_mouse()
            if want and not self._raw_input_registered and sink_hwnd:
                rid = w32.RAWINPUTDEVICE(0x01, 0x02, w32.RIDEV_INPUTSINK, sink_hwnd)
                if w32.user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(w32.RAWINPUTDEVICE)):
                    self._raw_input_registered = True
            elif (not want) and self._raw_input_registered:
                rid = w32.RAWINPUTDEVICE(0x01, 0x02, w32.RIDEV_REMOVE, 0)
                w32.user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(w32.RAWINPUTDEVICE))
                self._raw_input_registered = False

        @w32.WNDPROC
        def _raw_wndproc(hwnd, msg, wparam, lparam):
            try:
                if self._stop_event.is_set():
                    return w32.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
                if msg == w32.WM_INPUT:
                    if self.config.enabled and not self._cached_paused_reason:
                        raw_sz.value = full_sz
                        got = w32.user32.GetRawInputData(
                            lparam, w32.RID_INPUT, ctypes.byref(raw_buf), ctypes.byref(raw_sz), hdr_sz
                        )
                        if 0 < got < 0xFFFFFFFF and got >= hdr_sz + 8 and raw_buf.header.dwType == w32.RIM_TYPEMOUSE:
                            bflags = raw_buf.mouse.buttons.usButtonFlags
                            if bflags & want_mask:
                                cx, cy = w32.get_cursor_pos()
                                if (
                                    (bflags & w32.RI_MOUSE_MIDDLE_BUTTON_DOWN)
                                    and self.config.physics.middle_click_titlebar != "none"
                                ):
                                    self._handle_mouse_event(w32.WM_MBUTTONDOWN, cx, cy)
                                if bflags & w32.RI_MOUSE_LEFT_BUTTON_DOWN:
                                    if self.config.physics.double_click_desktop_icons:
                                        self._handle_mouse_event(w32.WM_LBUTTONDOWN, cx, cy)
                                    self._on_raw_lbutton_down(cx, cy)
                                if bflags & w32.RI_MOUSE_LEFT_BUTTON_UP:
                                    self._on_raw_lbutton_up(cx, cy)
                    return w32.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
                if msg == _WM_APP_SYNC_RAWINPUT:
                    _sync_reg(int(hwnd))
                    return 0
            except Exception:
                pass
            return w32.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._raw_wndproc_ref = _raw_wndproc
        raw_cb_key = id(_raw_wndproc)
        with _ACTIVE_NATIVE_CALLBACKS_LOCK:
            _ACTIVE_NATIVE_CALLBACKS[raw_cb_key] = _raw_wndproc
        sink_hwnd = 0
        class_registered = False
        destroyed_ok = True
        try:
            wc = w32.WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(w32.WNDCLASSEXW)
            wc.lpfnWndProc = self._raw_wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            atom = w32.user32.RegisterClassExW(ctypes.byref(wc))
            if not atom and ctypes.get_last_error() != 1410:
                return
            class_registered = True

            sink_hwnd = int(
                w32.user32.CreateWindowExW(
                    0, cls_name, "", 0, 0, 0, 0, 0, w32.wintypes.HWND(w32.HWND_MESSAGE), 0, hinst, None
                )
                or 0
            )
            self._raw_input_hwnd = sink_hwnd
            if sink_hwnd:
                _sync_reg(sink_hwnd)
                msg = w32.wintypes.MSG()
                while not self._stop_event.is_set():
                    ret = w32.user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                    if ret <= 0:
                        break
                    w32.user32.TranslateMessage(ctypes.byref(msg))
                    w32.user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            logging.getLogger(__name__).exception("Unhandled exception in PrismFX-RawInput _raw_input_loop")
        finally:
            if self._raw_input_registered:
                rid = w32.RAWINPUTDEVICE(0x01, 0x02, w32.RIDEV_REMOVE, 0)
                w32.user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(w32.RAWINPUTDEVICE))
                self._raw_input_registered = False
            if sink_hwnd:
                destroyed_ok = bool(w32.user32.DestroyWindow(sink_hwnd))
            self._raw_input_hwnd = 0
            self._raw_input_tid = 0
            unreg_ok = bool(w32.user32.UnregisterClassW(cls_name, hinst)) if class_registered else True
            if destroyed_ok and unreg_ok:
                with _ACTIVE_NATIVE_CALLBACKS_LOCK:
                    _ACTIVE_NATIVE_CALLBACKS.pop(raw_cb_key, None)

    def _cancel_active_precover(self) -> None:
        h = self._active_precover_hwnd
        if not h:
            return
        self._active_precover_hwnd = 0
        if self.on_precover_cancel:
            try:
                self.on_precover_cancel(h)
            except Exception as exc:
                _log.warning("on_precover_cancel callback failed for hwnd=%#x: %s", h, exc)

    def _is_taskbar_point(self, cx: int, cy: int) -> bool:
        raw_h = w32.window_from_point(cx, cy)
        if not raw_h:
            return False
        cls = w32.get_class_name(raw_h)
        if cls == "TrayNotifyWnd":
            return False
        root = int(w32.user32.GetAncestor(raw_h, w32.GA_ROOT) or raw_h)
        rcls = w32.get_class_name(root) if root != raw_h else cls
        if rcls not in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd") and cls not in _TASKBAR_CLICK_CLASSES:
            return False
        mon = self._monitor_for_point(cx, cy)
        if cx > mon.right - 220:
            return False  # Ignore system tray clock/icons area on far right
        return True

    def _classify_caption_min_close(self, root: int, cx: int, cy: int) -> Optional[str]:
        """Return 'minimize', 'maximize', or 'close' if (cx, cy) lies on the window's top-right caption buttons."""
        rx, ry, rw, rh = w32.get_extended_frame_bounds(root)
        if rw < 160 or rh < 60 or rx <= -10000:
            return None
        # Fast O(1) strip reject: clicking anywhere in the window body exits in < 0.002 ms with ZERO IPC!
        if not (ry - 2 <= cy <= ry + 48 and rx + rw - 148 <= cx <= rx + rw + 2):
            return None
        ht = w32.hit_test_window(root, cx, cy, timeout_ms=20)
        if ht == w32.HTMINBUTTON:
            return "minimize"
        if ht == w32.HTMAXBUTTON:
            return "maximize"
        if ht == w32.HTCLOSE:
            return "close"
        # Win11 custom DWM caption bars (File Explorer, Settings, Chrome, Electron) may return HTCLIENT/HTCAPTION
        dx_right = (rx + rw) - cx
        if 0 <= dx_right <= 46:
            return "close"
        if 46 < dx_right <= 92:
            return "maximize"
        if 92 < dx_right <= 142:
            return "minimize"
        return None

    def _trigger_enabled(self, trigger: str) -> bool:
        if trigger == "maximize":
            return bool(self.config.physics.smooth_maximize_enabled and self.on_morph_queued)
        tc = self.config.triggers.get(trigger)
        return bool(tc and tc.enabled and tc.effect_id != "none")

    def _on_raw_lbutton_down(self, cx: int, cy: int) -> None:
        """Arm pre-emptive pixel-identical cover on LBUTTONDOWN (60-120 ms advance notice before minimize/max/close!)."""
        self._armed_precover = None
        if not self.on_precover_show:
            return
        now = time.perf_counter()

        # Case A: Taskbar click -> arm active foreground window in case user clicked its own taskbar button!
        if self._trigger_enabled("minimize") and self._is_taskbar_point(cx, cy):
            cur_fg = int(w32.user32.GetForegroundWindow() or 0)
            cur_cls = w32.get_class_name(cur_fg) if cur_fg else ""
            cand = self._last_real_fg_hwnd or (cur_fg if cur_cls not in _TASKBAR_CLICK_CLASSES else 0)
            with self._state_lock:
                cand_is_own = cand in self._own_hwnds
            with self._cache_lock:
                cached = self._cached_captures.get(cand) if cand else None
            if (
                cand
                and not cand_is_own
                and cached is not None
                and w32.user32.IsWindow(cand)
                and w32.user32.IsWindowVisible(cand)
                and not w32.user32.IsIconic(cand)
            ):
                # Only arm if the window itself (or the taskbar/XAML bar that received the click) is foreground
                if cur_fg == cand or cur_cls in _TASKBAR_CLICK_CLASSES:
                    self._armed_precover = (cand, "minimize", "taskbar", None, now)
                    if (now - cached[4]) > 0.25:
                        with self._cache_lock:
                            self._pending_cache_hwnds.add(cand)
            return

        # Case B: Window titlebar Minimize (-), Maximize (□/❐), Close (X), or Titlebar Double-Click!
        raw_h = w32.window_from_point(cx, cy)
        if not raw_h:
            return
        root = int(w32.user32.GetAncestor(raw_h, w32.GA_ROOT) or raw_h)
        with self._state_lock:
            root_is_own = root in self._own_hwnds
        with self._cache_lock:
            cached = self._cached_captures.get(root) if root else None
        if not root or root_is_own or w32.user32.IsIconic(root):
            return
        if cached is None:
            # Prime capture immediately on LBUTTONDOWN so snapshot is ready before LBUTTONUP/MINIMIZESTART/HIDE!
            with self._cache_lock:
                self._pending_cache_hwnds.add(root)
            return
        action = self._classify_caption_min_close(root, cx, cy)
        if action and self._trigger_enabled(action):
            self._armed_precover = (root, action, "caption", w32.get_extended_frame_bounds(root), now)
            if (now - cached[4]) > 0.25:
                with self._cache_lock:
                    self._pending_cache_hwnds.add(root)
            return

        # Case C: Double-clicking a window's titlebar strip to Maximize / Restore-Down!
        if self._trigger_enabled("maximize"):
            rx, ry, rw, rh = w32.get_extended_frame_bounds(root)
            if rw >= 200 and rh >= 80 and ry <= cy <= ry + 42 and rx + 40 <= cx <= rx + rw - 150:
                prev_h, prev_x, prev_y, prev_t = self._last_caption_click
                self._last_caption_click = (root, cx, cy, now)
                if prev_h == root and (now - prev_t) <= 0.45 and abs(cx - prev_x) <= 6 and abs(cy - prev_y) <= 6:
                    self._last_caption_click = (0, 0, 0, 0.0)
                    self._armed_precover = (root, "maximize", "dblclick", (rx, ry, rw, rh), now)

    def _on_raw_lbutton_up(self, cx: int, cy: int) -> None:
        """Stamp pixel-identical Frame-0 pre-cover at LBUTTONUP — BEFORE Windows unmaps/resizes the real window!"""
        # Edge-Bump Aero Snap / Smart Edge Snap Pre-Catch: if user releases a dragged window right at
        # an active snap edge, freeze PreCover & keep alpha=0 BEFORE Windows native Aero Snap resizes it!
        drag_h = self._drag_hwnd
        if (
            drag_h
            and not self._drag_is_resize
            and not self._drag_morph_handled
            and self.config.physics.smooth_maximize_enabled
            and self.on_morph_queued
            and not self.backend.is_demo
            and not w32.user32.IsZoomed(drag_h)
        ):
            mon = self._monitor_for_point(cx, cy)
            will_snap = False
            if self.config.physics.edge_snap_enabled:
                z = detect_edge_snap_zone(
                    cx, cy, mon,
                    left_zone=self.config.physics.snap_left_zone,
                    right_zone=self.config.physics.snap_right_zone,
                    top_zone=self.config.physics.snap_top_zone,
                    bottom_zone=self.config.physics.snap_bottom_zone,
                )
                will_snap = bool(z and z != "minimize")
            else:
                # Native Windows 11 Aero Snap activates only on the outer 2px monitor edge
                will_snap = (
                    (cy <= mon.top + 2)
                    or (cx <= mon.left + 2)
                    or (cx >= mon.right - 2)
                ) and not (cx == 0 and cy == 0)
            if will_snap:
                f_rect = self._drag_last_normal_rect
                if f_rect[2] < 40:
                    f_rect = w32.get_extended_frame_bounds(drag_h)
                cap_use = self._drag_last_normal_cap or self._drag_wobble_cap
                if cap_use is None:
                    with self._cache_lock:
                        cc = self._cached_captures.get(drag_h)
                    if cc and abs(cc[0] - f_rect[2]) <= 16 and abs(cc[1] - f_rect[3]) <= 16:
                        cap_use = (cc[0], cc[1], cc[2])
                if cap_use is not None and f_rect[2] >= 40 and f_rect[3] >= 40 and f_rect[0] > -10000:
                    if self._stage_morph_window(drag_h, f_rect, cap_use, stamp_precover=True):
                        self._drag_morph_handled = True
                        if self._drag_wobble_started and self.on_wobbly_end:
                            self._drag_wobble_started = False
                            self._drag_dpi_recapture_pending = False
                            try:
                                self.on_wobbly_end(drag_h, f_rect[0], f_rect[1], True)
                            except Exception:
                                pass
                        return

        armed = self._armed_precover
        self._armed_precover = None
        if not armed or not self.on_precover_show:
            return
        hwnd, action, source, _orig_rect, armed_at = armed
        now = time.perf_counter()
        if (now - armed_at) > 1.5:
            return
        if source == "caption":
            if self._classify_caption_min_close(hwnd, cx, cy) != action:
                return
        elif source == "taskbar":
            if not self._is_taskbar_point(cx, cy):
                return
        if not (w32.user32.IsWindow(hwnd) and w32.user32.IsWindowVisible(hwnd) and not w32.user32.IsIconic(hwnd)):
            return
        with self._cache_lock:
            cached = self._cached_captures.get(hwnd)
        if not cached:
            return
        img_w, img_h, raw, cached_rect, _ts = cached
        rx, ry, rw, rh = w32.get_extended_frame_bounds(hwnd)
        rect = (rx, ry, rw, rh) if (rx > -10000 and abs(rw - img_w) <= 16 and abs(rh - img_h) <= 16) else cached_rect
        if rect[2] < 40 or rect[3] < 40:
            return

        if action == "maximize":
            self._stage_morph_window(hwnd, rect, (img_w, img_h, raw), stamp_precover=True)
            return

        pjob = PreCoverJob(
            hwnd=hwnd,
            trigger=action,
            x=rect[0],
            y=rect[1],
            w=rect[2],
            h=rect[3],
            img_w=img_w,
            img_h=img_h,
            bgra_bytes=raw,
            armed_at=now,
        )
        self._active_precover_hwnd = hwnd
        try:
            self.on_precover_show(pjob)
        except Exception:
            self._active_precover_hwnd = 0

    def _handle_win_event(self, event: int, hwnd: int) -> None:
        if event == w32.EVENT_SYSTEM_FOREGROUND:
            cls = w32.get_class_name(hwnd)
            if cls in ("Progman", "WorkerW"):
                self._last_real_fg_hwnd = 0
            elif cls not in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd") and not is_protected_window_class(cls):
                with self._state_lock:
                    is_own = hwnd in self._own_hwnds
                if not is_own:
                    self._last_real_fg_hwnd = hwnd
                    if not w32.user32.IsIconic(hwnd):
                        if (not w32.user32.IsWindowVisible(hwnd)) or w32.is_window_cloaked(hwnd):
                            # Early pre-hide for pre-created/pooled apps (e.g. Win11 File Explorer fires
                            # FOREGROUND ~200ms BEFORE SHOW while still invisible -> 0ms pre-flash!)
                            self._try_prehide_on_create(hwnd)
                        else:
                            with self._state_lock:
                                is_known_vis = hwnd in self._known_visible_hwnds
                            with self._cache_lock:
                                is_cached = hwnd in self._cached_captures
                            if not is_known_vis and not is_cached:
                                # Fallback catch for reused UWP/XAML windows that focus without firing SHOW
                                style = w32.user32.GetWindowLongPtrW(hwnd, w32.GWL_STYLE)
                                rw, rh = w32.get_window_rect_size(hwnd)
                                staged = False
                                if (
                                    not (style & w32.WS_CHILD)
                                    and (style & w32.WS_CAPTION)
                                    and not w32.is_overlay_exstyle(hwnd)
                                    and not any(sub in cls for sub in _INTERNAL_CREATE_CLASS_SUBSTRS)
                                    and rw >= 200
                                    and rh >= 120
                                ):
                                    staged = self._stage_open_window(hwnd, "open")
                                if not staged:
                                    with self._state_lock:
                                        self._known_visible_hwnds.add(hwnd)
                                    with self._cache_lock:
                                        self._pending_cache_hwnds.add(hwnd)
                            else:
                                with self._cache_lock:
                                    self._pending_cache_hwnds.add(hwnd)
                if self._active_precover_hwnd and hwnd != self._active_precover_hwnd:
                    self._cancel_active_precover()
            return

        if event == w32.EVENT_OBJECT_CREATE:
            self._try_prehide_on_create(hwnd)
            return

        if event == w32.EVENT_OBJECT_DESTROY:
            if hwnd == self._drag_hwnd:
                was_wob = self._drag_wobble_started
                self._drag_hwnd = 0
                self._drag_wobble_started = False
                self._drag_dpi_recapture_pending = False
                self._drag_alpha_mutated = False
                self._drag_unsafe_layered = False
                self._drag_wobble_cap = None
                self._detach_drag_location_hook()
                if was_wob and self.on_wobbly_end:
                    try:
                        self.on_wobbly_end(hwnd, 0, 0, True)
                    except Exception:
                        pass
            if hwnd == self._momentum_wobble_hwnd:
                self._momentum_wobble_hwnd = 0
                self._momentum = None
                if self.on_wobbly_end:
                    try:
                        self.on_wobbly_end(hwnd, 0, 0, True)
                    except Exception:
                        pass
            if self._active_precover_hwnd == hwnd:
                self._cancel_active_precover()
            with self._cache_lock:
                self._cached_captures.pop(hwnd, None)
                self._pending_cache_hwnds.discard(hwnd)
            was_tiled = False
            with self._state_lock:
                self._rolled_up.pop(hwnd, None)
                self._pinned.discard(hwnd)
                if hwnd in self._tiled:
                    self._tiled.discard(hwnd)
                    was_tiled = True
                self._faded_hwnds.pop(hwnd, None)
                self._custom_opacity_orig.pop(hwnd, None)
                self._last_active_time.pop(hwnd, None)
                self._last_trigger_info.pop(hwnd, None)
                self._known_visible_hwnds.discard(hwnd)
                self._cloaked_hwnds.discard(hwnd)
            if was_tiled:
                try:
                    self.destroyed_tile_hwnds.put_nowait(hwnd)
                except queue.Full:
                    pass
            with self._hidden_lock:
                self._prehidden_minimized.pop(hwnd, None)
                self._prehidden_uniconic_since.pop(hwnd, None)
                self._prehidden_creating.pop(hwnd, None)
                self._pending_opens.pop(hwnd, None)
                self._pending_morphs.pop(hwnd, None)
                self._pending_wobble_captures.pop(hwnd, None)
                self._pending_dpi_wobble_captures.pop(hwnd, None)
                if self._pending_hook_triggers:
                    self._pending_hook_triggers = [
                        item for item in self._pending_hook_triggers if item[0] != hwnd
                    ]
                self._pending_prehide.pop(hwnd, None)
                self._hidden_for_anim.pop(hwnd, None)
                self._hidden_for_wobble.pop(hwnd, None)
            if self.journal.get_mutation(hwnd) is not None:
                self.journal.prune_hwnd(hwnd, sync=False)
            return

        if event == w32.EVENT_SYSTEM_MOVESIZESTART:
            info = self.backend.get_window_info(hwnd)
            if not info or self.is_excluded(info):
                return
            # End any prior gliding momentum wobble cleanly before starting a new drag
            if self._momentum_wobble_hwnd and self._momentum_wobble_hwnd != hwnd:
                old_mh = self._momentum_wobble_hwnd
                self._momentum_wobble_hwnd = 0
                if self.on_wobbly_end:
                    try:
                        self.on_wobbly_end(old_mh, 0, 0, True)
                    except Exception:
                        pass
            now = time.perf_counter()
            self._drag_hwnd = hwnd
            self._momentum = None
            self._momentum_wobble_hwnd = 0
            self._drag_start_rect = (info.x, info.y, info.w, info.h)
            self._drag_last_pos = (info.x, info.y)
            self._drag_last_normal_rect = (0, 0, 0, 0) if info.maximized else (info.x, info.y, info.w, info.h)
            self._drag_last_normal_cap = None
            self._drag_morph_handled = False
            self._drag_was_zoomed_start = bool(info.maximized)
            with self._hidden_lock:
                wob_existing = self._hidden_for_wobble.get(hwnd)
            with self._state_lock:
                faded_existing = self._faded_hwnds.get(hwnd)
            if wob_existing is not None:
                had_layered = wob_existing[2]
                cur_alpha: Optional[int] = wob_existing[0]
            elif faded_existing is not None:
                cur_alpha, had_layered = faded_existing
            else:
                had_layered, cur_alpha = self.backend.get_window_layered_state(hwnd)
            self._drag_unsafe_layered = (cur_alpha is None) or (cur_alpha == 0)
            self._drag_had_layered = had_layered
            self._drag_orig_alpha = cur_alpha if (cur_alpha is not None and cur_alpha >= 20) else 255
            self._drag_alpha_mutated = False
            self._drag_wobble_started = False
            self._drag_size_changed = False
            self._drag_dpi_recapture_pending = False
            mon0 = self._monitor_for_point(info.x + info.w // 2, info.y + info.h // 2)
            self._drag_monitor_key = (mon0.name, mon0.left, mon0.top, mon0.width, mon0.height)
            self._drag_size_changed_at = 0.0
            self._drag_stable_moves = 0
            self._drag_recapture_tries = 0
            self._drag_wobble_cap = None
            self._drag_lbutton_up_since = 0.0
            self._drag_last_move_ts = now

            cx, cy = w32.get_cursor_pos()
            # Distinguish real border resizes immediately via hit-test (never treat maximized titlebar as border!)
            ht = 0 if (info.maximized or self.backend.is_demo) else w32.hit_test_window(hwnd, cx, cy, timeout_ms=15)
            self._drag_is_resize = ht in w32.HT_RESIZE_BORDERS

            gu = max(0.05, min(0.95, (cx - info.x) / max(1, info.w)))
            gv = max(0.02, min(0.35, (cy - info.y) / max(1, info.h)))
            self._drag_grab_uv = (gu, gv)

            self._drag_tracker.reset(now, info.x, info.y)
            self._attach_drag_location_hook(hwnd)

            # Prepare 1:1 DWM capture for live Wobbly Windows (skip throwaway capture if border-resize,
            # per-pixel layered/ColorKey, or currently maximized — maximized windows capture fresh immediately after Aero un-maximize!)
            if (
                self.config.physics.wobbly_enabled
                and self.on_wobbly_start
                and not self.backend.is_demo
                and not self._drag_is_resize
                and not self._drag_unsafe_layered
                and not info.maximized
            ):
                with self._cache_lock:
                    cached = self._cached_captures.get(hwnd)
                cache_dim_ok = (
                    cached is not None
                    and abs(cached[0] - info.w) <= 16
                    and abs(cached[1] - info.h) <= 16
                )
                if cache_dim_ok and cached is not None:
                    self._drag_wobble_cap = (cached[0], cached[1], cached[2])
                else:
                    # Cache miss: enqueue async capture on PrismFX-OpenStager (ZERO PrintWindow on PrismFX-Hooks!)
                    with self._hidden_lock:
                        self._pending_wobble_captures[hwnd] = (info.x, info.y, info.w, info.h, now)
                    self._open_wake.set()
            elif (
                not self._drag_is_resize
                and not self._drag_unsafe_layered
                and not self.config.physics.wobbly_enabled
                and self.config.physics.drag_transparency_enabled
            ):
                alpha = max(60, min(255, int(round(self.config.physics.drag_opacity_pct * 2.55))))
                if self.backend.set_window_opacity(hwnd, alpha):
                    pid, cls_name, boot_ms = self._window_identity(hwnd, info)
                    self.journal.record_alpha(
                        hwnd,
                        self._drag_orig_alpha,
                        had_layered=self._drag_had_layered,
                        pid=pid,
                        class_name=cls_name,
                        boot_tick_ms=boot_ms,
                        sync=False,
                    )
                    self._drag_alpha_mutated = True
                    with self._state_lock:
                        self._faded_hwnds.pop(hwnd, None)
            return

        if event == w32.EVENT_OBJECT_LOCATIONCHANGE:
            if hwnd != self._drag_hwnd:
                return
            # Fast kernel/DWM bounds only (~0.01 ms; zero OpenProcess/GetWindowText during drag!)
            if self.backend.is_demo:
                d_info = self.backend.get_window_info(hwnd)
                if not d_info:
                    return
                x, y, w, h = d_info.x, d_info.y, d_info.w, d_info.h
            else:
                x, y, w, h = w32.get_extended_frame_bounds(hwnd)
            if w <= 0 or h <= 0 or x <= -10000:
                return
            now = time.perf_counter()
            self._drag_last_move_ts = now
            prev_vx, prev_vy = self._drag_tracker.velocity()
            self._drag_tracker.push(now, x, y)

            ox, oy, ow, oh = self._drag_start_rect
            prev_x, prev_y = self._drag_last_pos
            self._drag_last_pos = (x, y)

            if w != ow or h != oh:
                # Check if Windows 11 native Aero Snap just maximized or edge-snapped this dragged window!
                # (Windows fires LOCATIONCHANGE with the maximized/snapped rect ~1ms BEFORE MOVESIZEEND!)
                is_zoomed_now = (not self.backend.is_demo) and bool(w32.user32.IsZoomed(hwnd))
                is_aero_snap_rect = is_zoomed_now
                if (
                    not is_aero_snap_rect
                    and not self.backend.is_demo
                    and self._cached_paused_reason != "fullscreen_game"
                    and not w32.is_lbutton_down()
                ):
                    mon_c = self._monitor_for_point(x + w // 2, y + h // 2)
                    if h >= mon_c.work_height - 24 and (
                        x <= mon_c.work_left + 16 or (x + w) >= mon_c.work_right - 16
                    ):
                        is_aero_snap_rect = True
                if (
                    not self._drag_is_resize
                    and not self._drag_morph_handled
                    and self.config.physics.smooth_maximize_enabled
                    and self.on_morph_queued
                    and not self.backend.is_demo
                    and is_aero_snap_rect
                    and self._drag_last_normal_rect[2] >= 40
                    and (abs(w - self._drag_last_normal_rect[2]) >= 40 or abs(h - self._drag_last_normal_rect[3]) >= 40)
                ):
                    cap_use = self._drag_last_normal_cap or self._drag_wobble_cap
                    if cap_use is None:
                        with self._cache_lock:
                            cc = self._cached_captures.get(hwnd)
                        if cc and abs(cc[0] - self._drag_last_normal_rect[2]) <= 16 and abs(cc[1] - self._drag_last_normal_rect[3]) <= 16:
                            cap_use = (cc[0], cc[1], cc[2])
                    if cap_use is not None:
                        if self._stage_morph_window(
                            hwnd,
                            self._drag_last_normal_rect,
                            cap_use,
                            stamp_precover=True,
                            already_changed_to=(x, y, w, h),
                        ):
                            self._drag_morph_handled = True
                            if self._drag_wobble_started:
                                self._drag_wobble_started = False
                                self._drag_dpi_recapture_pending = False
                                self._wobble_live_pos = None
                                if self.on_wobbly_end:
                                    try:
                                        self.on_wobbly_end(hwnd, x, y, True)
                                    except Exception:
                                        pass
                            self._drag_start_rect = (x, y, w, h)
                            return

                if self._drag_morph_handled:
                    return

                cur_mon = self._monitor_for_point(x + w // 2, y + h // 2)
                cur_mon_key = (cur_mon.name, cur_mon.left, cur_mon.top, cur_mon.width, cur_mon.height)
                crossed_monitor = bool(self._drag_monitor_key[0] and cur_mon_key != self._drag_monitor_key)

                # Smooth cross-monitor Mixed-DPI rescale (PFX-P05): keep Wobbly alive and smoothly
                # scale the live texture without dropping/flashing, then hot-swap a crisp new-DPI
                # texture once bounds stabilize (>= 2 moves & >= 38 ms)!
                if (
                    self._drag_wobble_started
                    and not self._drag_is_resize
                    and not is_zoomed_now
                    and not is_aero_snap_rect
                    and (self.backend.is_demo or (self._cached_paused_reason != "fullscreen_game" and w32.is_lbutton_down()))
                    and is_proportional_dpi_scale(ow, oh, w, h, crossed_monitor=crossed_monitor)
                ):
                    self._drag_start_rect = (x, y, w, h)
                    self._drag_monitor_key = cur_mon_key
                    self._drag_dpi_recapture_pending = True
                    self._drag_size_changed_at = now
                    self._drag_stable_moves = 0
                    self._drag_recapture_tries = 0
                    self._wobble_live_pos = (hwnd, x, y, w, h)
                    if not is_zoomed_now:
                        self._drag_last_normal_rect = (x, y, w, h)
                    return

                # Size jumped mid-drag (Aero Un-Maximize / Un-Snap, OR border resize).
                # Cancel any stale-size wobble, adopt the new (w, h) baseline, and wait for size to stabilize
                # across pure moves before re-capturing a crisp 1:1 texture at the restored dimensions!
                if self._drag_wobble_started:
                    self._drag_wobble_started = False
                    self._wobble_live_pos = None
                    if self.on_wobbly_end:
                        try:
                            self.on_wobbly_end(hwnd, x, y, True)
                        except Exception:
                            pass
                self._drag_start_rect = (x, y, w, h)
                self._drag_monitor_key = cur_mon_key
                self._drag_dpi_recapture_pending = False
                self._drag_wobble_cap = None
                self._drag_size_changed = True
                self._drag_size_changed_at = now
                self._drag_stable_moves = 0
                self._drag_recapture_tries = 0
                return

            if x != prev_x or y != prev_y:
                self._drag_stable_moves += 1
                if not self.backend.is_demo and not w32.user32.IsZoomed(hwnd):
                    self._drag_last_normal_rect = (x, y, w, h)
                    if self._drag_wobble_cap is not None:
                        self._drag_last_normal_cap = self._drag_wobble_cap

            # If initial hit-test reported a border, stay completely idle unless pure constant-size
            # movement proves Windows actually entered a move (not a border resize).
            if self._drag_is_resize:
                if self._drag_stable_moves >= 3 and (abs(x - ox) + abs(y - oy)) >= 20:
                    self._drag_is_resize = False
                else:
                    return

            if self.config.physics.wobbly_enabled and not self._drag_unsafe_layered:
                # Wait until maximized window actually un-maximizes before starting Wobbly!
                if not self._drag_wobble_started and not self.backend.is_demo and w32.user32.IsZoomed(hwnd):
                    return

                self._wobble_live_pos = (hwnd, x, y, w, h)

                # Seamless Mixed-DPI recapture while Wobbly stays active: swap in crisp new-DPI texture
                # asynchronously off PrismFX-Hooks on live OS (or inline in demo/headless unit tests)!
                if (
                    self._drag_wobble_started
                    and self._drag_dpi_recapture_pending
                    and self._drag_stable_moves >= 2
                    and (now - self._drag_size_changed_at) >= 0.038
                ):
                    if self._drag_recapture_tries < 3:
                        if self._open_stager_thread is not None and self._open_stager_thread.is_alive():
                            enq_dpi = False
                            with self._hidden_lock:
                                if hwnd not in self._pending_dpi_wobble_captures:
                                    self._pending_dpi_wobble_captures[hwnd] = (x, y, w, h, now)
                                    self._drag_recapture_tries += 1
                                    self._drag_size_changed_at = now
                                    enq_dpi = True
                            if enq_dpi:
                                self._open_wake.set()
                        else:
                            self._drag_recapture_tries += 1
                            self._perform_dpi_wobble_recapture(hwnd, x, y, w, h, now)
                    else:
                        self._drag_dpi_recapture_pending = False

                # After Aero Un-Maximize / Un-Snap, capture fresh restored texture asynchronously
                # off PrismFX-Hooks once size stays stable for ~38 ms & >= 2 pure moves!
                if not self._drag_wobble_started and self._drag_wobble_cap is None and not self.backend.is_demo:
                    if not self._drag_size_changed:
                        with self._cache_lock:
                            cached_loc = self._cached_captures.get(hwnd)
                        if (
                            cached_loc is not None
                            and abs(cached_loc[0] - w) <= 16
                            and abs(cached_loc[1] - h) <= 16
                        ):
                            self._drag_wobble_cap = (cached_loc[0], cached_loc[1], cached_loc[2])
                    if self._drag_wobble_cap is None:
                        ready_for_recap = (
                            (not self._drag_size_changed)
                            or (self._drag_stable_moves >= 2 and (now - self._drag_size_changed_at) >= 0.038)
                        )
                        if ready_for_recap and self._drag_recapture_tries < 3:
                            enqueued = False
                            with self._hidden_lock:
                                if hwnd not in self._pending_wobble_captures:
                                    self._pending_wobble_captures[hwnd] = (x, y, w, h, now)
                                    self._drag_recapture_tries += 1
                                    self._drag_size_changed_at = now
                                    enqueued = True
                            if enqueued:
                                self._open_wake.set()

                if (
                    not self._drag_wobble_started
                    and (x != ox or y != oy or self._drag_stable_moves >= 2)
                    and self._drag_wobble_cap is not None
                ):
                    self._drag_wobble_started = True
                    iw, ih, raw_bgra = self._drag_wobble_cap
                    gu, gv = self._drag_grab_uv
                    step_dx = max(-80, min(80, x - prev_x))
                    step_dy = max(-80, min(80, y - prev_y))
                    if self.on_wobbly_start:
                        try:
                            self.on_wobbly_start(
                                WobblyDragStart(
                                    hwnd=hwnd,
                                    x=x,
                                    y=y,
                                    w=w,
                                    h=h,
                                    img_w=iw,
                                    img_h=ih,
                                    bgra_bytes=raw_bgra,
                                    grab_u=gu,
                                    grab_v=gv,
                                    orig_alpha=self._drag_orig_alpha,
                                    init_dx=step_dx,
                                    init_dy=step_dy,
                                )
                            )
                        except Exception:
                            self._drag_wobble_started = False
                if self.on_wobbly_drag:
                    vx, vy = self._drag_tracker.velocity()
                    self.on_wobbly_drag(hwnd, x, y, w, h, vx - prev_vx, vy - prev_vy, True)
            return

        if event == w32.EVENT_SYSTEM_MOVESIZEEND and hwnd == self._drag_hwnd:
            was_wobbling = self._drag_wobble_started
            morph_handled = self._drag_morph_handled
            drag_alpha_mutated = self._drag_alpha_mutated
            drag_orig_alpha = self._drag_orig_alpha
            drag_had_layered = self._drag_had_layered
            last_norm_rect = self._drag_last_normal_rect
            last_norm_cap = self._drag_last_normal_cap or self._drag_wobble_cap
            self._drag_hwnd = 0
            self._drag_wobble_started = False
            self._drag_dpi_recapture_pending = False
            self._drag_morph_handled = False
            self._drag_alpha_mutated = False
            self._drag_unsafe_layered = False
            self._drag_wobble_cap = None
            self._drag_last_normal_rect = (0, 0, 0, 0)
            self._drag_last_normal_cap = None
            self._drag_lbutton_up_since = 0.0
            self._drag_last_move_ts = time.perf_counter()
            self._detach_drag_location_hook()
            with self._hidden_lock:
                self._pending_wobble_captures.pop(hwnd, None)
                self._pending_dpi_wobble_captures.pop(hwnd, None)
            with self._cache_lock:
                self._pending_cache_hwnds.add(hwnd)
            info = self.backend.get_window_info(hwnd)
            if info:
                with self._cache_lock:
                    cached = self._cached_captures.get(hwnd)
                    if cached and cached[0] == info.w and cached[1] == info.h and info.x > -10000:
                        self._cached_captures[hwnd] = (
                            cached[0], cached[1], cached[2], (info.x, info.y, info.w, info.h), cached[4]
                        )
            # Only restore opacity directly if drag transparency mutated it and neither Wobbly nor Morph owns it!
            if (not was_wobbling) and (not morph_handled) and drag_alpha_mutated:
                if self.backend.is_valid_window(hwnd):
                    self.backend.set_window_opacity(hwnd, drag_orig_alpha, had_layered=drag_had_layered)
                self.journal.record_alpha(hwnd, None, sync=False)
            if self.on_wobbly_drag and info:
                self.on_wobbly_drag(hwnd, info.x, info.y, info.w, info.h, 0.0, 0.0, False)

            def _end_wobble(snap_cancel: bool) -> None:
                if was_wobbling and self.on_wobbly_end:
                    fx = info.x if info else 0
                    fy = info.y if info else 0
                    try:
                        self.on_wobbly_end(hwnd, fx, fy, snap_cancel)
                    except Exception:
                        pass

            # If _on_raw_lbutton_up or LOCATIONCHANGE already staged the edge-bump morph:
            # also trigger PrismFX Edge Snap if Windows native Aero Snap hasn't moved it yet!
            if morph_handled:
                _end_wobble(snap_cancel=True)
                if info and self.config.physics.edge_snap_enabled:
                    cx, cy = w32.get_cursor_pos()
                    mon = self._monitor_for_point(cx, cy)
                    zone = detect_edge_snap_zone(
                        cx, cy, mon,
                        left_zone=self.config.physics.snap_left_zone,
                        right_zone=self.config.physics.snap_right_zone,
                        top_zone=self.config.physics.snap_top_zone,
                        bottom_zone=self.config.physics.snap_bottom_zone,
                    )
                    if zone and zone != "minimize":
                        same_as_norm = (
                            last_norm_rect[2] >= 40
                            and abs(info.w - last_norm_rect[2]) < 24
                            and abs(info.h - last_norm_rect[3]) < 24
                        )
                        if same_as_norm:
                            if zone == "maximize":
                                self.backend.maximize_window(hwnd)
                            else:
                                rect = compute_snap_rect(mon, zone)
                                if rect:
                                    self.backend.restore_window(hwnd)
                                    self.backend.move_resize_window(hwnd, *rect)
                return

            def _morph_snap(zone: str) -> None:
                # Stage morph BEFORE ending wobble so finish_wobble_handoff sees claimed_by_morph (0 ms flash!)
                f_rect = last_norm_rect if (last_norm_rect[2] >= 40) else ((info.x, info.y, info.w, info.h) if info else None)
                self.snap_window_to_zone(hwnd, zone, from_rect_override=f_rect, from_cap_override=last_norm_cap)
                _end_wobble(snap_cancel=True)

            was_pure_resize = self._drag_is_resize or (
                self._drag_size_changed
                and not was_wobbling
                and self._drag_stable_moves < 2
            )
            if not info or was_pure_resize:
                _end_wobble(snap_cancel=True)
                return

            cx, cy = w32.get_cursor_pos()
            mon = self._monitor_for_point(cx, cy)

            # Case 1: Windows 11 native Aero Snap ALREADY maximized/snapped right before MOVESIZEEND arrived!
            if (
                self.config.physics.smooth_maximize_enabled
                and not self.backend.is_demo
                and last_norm_rect[2] >= 40
                and (abs(info.w - last_norm_rect[2]) >= 40 or abs(info.h - last_norm_rect[3]) >= 40)
            ):
                cap_use = last_norm_cap
                if cap_use is None:
                    with self._cache_lock:
                        cc = self._cached_captures.get(hwnd)
                    if cc and abs(cc[0] - last_norm_rect[2]) <= 16 and abs(cc[1] - last_norm_rect[3]) <= 16:
                        cap_use = (cc[0], cc[1], cc[2])
                if cap_use is not None:
                    self._stage_morph_window(
                        hwnd,
                        last_norm_rect,
                        cap_use,
                        stamp_precover=True,
                        already_changed_to=(info.x, info.y, info.w, info.h),
                    )
                    _end_wobble(snap_cancel=True)
                    return

            if self.config.physics.edge_snap_enabled:
                zone = detect_edge_snap_zone(
                    cx, cy, mon,
                    left_zone=self.config.physics.snap_left_zone,
                    right_zone=self.config.physics.snap_right_zone,
                    top_zone=self.config.physics.snap_top_zone,
                    bottom_zone=self.config.physics.snap_bottom_zone,
                )
                if zone:
                    if zone == "minimize":
                        _end_wobble(snap_cancel=True)
                        self.snap_window_to_zone(hwnd, zone)
                    else:
                        _morph_snap(zone)
                    return

            rel_now = time.perf_counter()
            if self.config.physics.flicks_enabled:
                flick = self._drag_tracker.detect_flick(self.config.physics.flick_threshold, now=rel_now)
                if flick == "left":
                    _morph_snap(self.config.physics.snap_left_zone)
                    return
                if flick == "right":
                    _morph_snap(self.config.physics.snap_right_zone)
                    return
                if flick == "up":
                    _morph_snap("maximize")
                    return
                if flick == "down":
                    _end_wobble(snap_cancel=True)
                    self.snap_window_to_zone(hwnd, "minimize")
                    return

            if self.config.physics.momentum_enabled:
                vx, vy = self._drag_tracker.velocity(now=rel_now)
                if (vx * vx + vy * vy) > 220.0 * 220.0:
                    self._momentum = MomentumBody(
                        hwnd=hwnd,
                        x=float(info.x),
                        y=float(info.y),
                        w=info.w,
                        h=info.h,
                        vx=max(-2600.0, min(2600.0, vx * 0.72)),
                        vy=max(-2600.0, min(2600.0, vy * 0.72)),
                        bounds_left=mon.work_left,
                        bounds_top=mon.work_top,
                        bounds_right=mon.work_right,
                        bounds_bottom=mon.work_bottom,
                        friction=self.config.physics.momentum_friction,
                        bounce=self.config.physics.momentum_bounce,
                        strict_clamp=self.config.physics.momentum_strict_clamp,
                    )
                    if was_wobbling:
                        # Keep wobbling smoothly while gliding & bouncing off monitor edges!
                        self._momentum_wobble_hwnd = hwnd
                        return

            _end_wobble(snap_cancel=False)
            return

        if event == w32.EVENT_SYSTEM_MINIMIZESTART:
            self.trigger_effect_for_window(hwnd, "minimize")
            return
        if event == w32.EVENT_SYSTEM_MINIMIZEEND:
            if self._active_precover_hwnd:
                self._cancel_active_precover()
            with self._state_lock:
                self._known_visible_hwnds.add(hwnd)
                self._cloaked_hwnds.discard(hwnd)
            if not self._trigger_enabled("restore"):
                self._unhide_if_prehidden(hwnd)
                return
            if self.backend.is_demo:
                if self.trigger_effect_for_window(hwnd, "restore") is None:
                    self._unhide_if_prehidden(hwnd)
                return
            # Fast-path ONLY on cache hit with matching non-iconic bounds (0 sleep, 0 PrintWindow on PrismFX-Hooks!)
            fast_ok = False
            if not w32.user32.IsIconic(hwnd):
                rx, ry, rw, rh = w32.get_extended_frame_bounds(hwnd)
                if rx > -10000 and rw >= 80 and rh >= 60:
                    with self._cache_lock:
                        cached_res = self._cached_captures.get(hwnd)
                    if cached_res is not None and cached_res[0] == rw and cached_res[1] == rh:
                        fast_ok = True
            if fast_ok:
                if self.trigger_effect_for_window(hwnd, "restore") is None:
                    self._unhide_if_prehidden(hwnd)
            else:
                # Offload capture/wait to PrismFX-OpenStager while keeping pre-hidden window at alpha=0 (0ms flash!)
                with self._hidden_lock:
                    if (hwnd, "restore") not in self._pending_hook_triggers:
                        self._pending_hook_triggers.append((hwnd, "restore"))
                self._open_wake.set()
            return
        if event == w32.EVENT_SYSTEM_MENUPOPUPSTART:
            if self._active_precover_hwnd:
                self._cancel_active_precover()
            if not self._trigger_enabled("menu"):
                return
            # Fast kernel pre-filter before OpenProcess/GetWindowTextW (ignores taskbar/XAML/child popups!)
            style = w32.user32.GetWindowLongPtrW(hwnd, w32.GWL_STYLE)
            if (style & w32.WS_CHILD) or w32.is_overlay_exstyle(hwnd):
                return
            cls = w32.get_class_name(hwnd)
            if not cls or is_protected_window_class(cls):
                return
            rw, rh = w32.get_window_rect_size(hwnd)
            if rw < 60 or rh < 40:
                return
            if self.backend.is_demo:
                info = self.backend.get_window_info(hwnd)
                if info and info.w >= 60 and info.h >= 40 and not self.is_excluded(info):
                    self.trigger_effect_for_window(hwnd, "menu")
                return
            # Enqueue async menu trigger on PrismFX-OpenStager (ZERO PrintWindow on PrismFX-Hooks!)
            with self._hidden_lock:
                if (hwnd, "menu") not in self._pending_hook_triggers:
                    self._pending_hook_triggers.append((hwnd, "menu"))
            self._open_wake.set()
            return
        if event == w32.EVENT_OBJECT_SHOW:
            # Fast kernel pre-filter: reject taskbar/XAML/child/overlay/internal HWNDs in < 0.005 ms
            style = w32.user32.GetWindowLongPtrW(hwnd, w32.GWL_STYLE)
            if (style & w32.WS_CHILD) or not (style & (w32.WS_CAPTION | w32.WS_POPUP)):
                if hwnd in self._prehidden_creating:
                    self._unhide_if_prehidden(hwnd)
                return
            if w32.is_overlay_exstyle(hwnd):
                if hwnd in self._prehidden_creating:
                    self._unhide_if_prehidden(hwnd)
                return
            cls = w32.get_class_name(hwnd)
            if self._active_precover_hwnd and hwnd != self._active_precover_hwnd:
                if cls in ("Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow") or not is_protected_window_class(cls):
                    self._cancel_active_precover()
            if not cls or is_protected_window_class(cls) or any(sub in cls for sub in _INTERNAL_CREATE_CLASS_SUBSTRS):
                if hwnd in self._prehidden_creating:
                    self._unhide_if_prehidden(hwnd)
                return
            if hwnd in self._prehidden_minimized or w32.user32.IsIconic(hwnd):
                return
            staged = False
            if style & w32.WS_CAPTION:
                rw, rh = w32.get_window_rect_size(hwnd)
                has_app_chrome = bool(
                    style & (w32.WS_SYSMENU | w32.WS_THICKFRAME | w32.WS_MINIMIZEBOX | w32.WS_MAXIMIZEBOX)
                )
                if has_app_chrome or (rw >= 200 and rh >= 120):
                    staged = self._stage_open_window(hwnd, "open")
            elif style & w32.WS_POPUP:
                rw, rh = w32.get_window_rect_size(hwnd)
                if rw >= 200 and rh >= 120:
                    staged = self._stage_open_window(hwnd, "popup")
            if not staged:
                self._unhide_if_prehidden(hwnd)
                if not w32.is_window_cloaked(hwnd):
                    with self._state_lock:
                        self._known_visible_hwnds.add(hwnd)
            return
        if event == w32.EVENT_OBJECT_CLOAKED:
            with self._state_lock:
                was_known_vis = hwnd in self._known_visible_hwnds
                if was_known_vis:
                    self._cloaked_hwnds.add(hwnd)
                self._known_visible_hwnds.discard(hwnd)
            # Only record cloak timestamp for real previously-visible app windows (never IME/MSCTFIME/CoreWindow!)
            if was_known_vis:
                self._last_cloak_ts = time.perf_counter()
            return
        if event == w32.EVENT_OBJECT_UNCLOAKED:
            now = time.perf_counter()
            with self._hidden_lock:
                already_pending_open = hwnd in self._pending_opens
            if already_pending_open:
                # Already legitimately staged on SHOW -> mark first_uncloaked_at & wake OpenStager immediately!
                self._stage_open_window(hwnd, "open")
                return
            with self._state_lock:
                was_cloaked_known = hwnd in self._cloaked_hwnds
                is_known_vis = hwnd in self._known_visible_hwnds
            # Virtual Desktop switch guard (1): previously visible window returning from cloak
            if was_cloaked_known:
                with self._state_lock:
                    self._cloaked_hwnds.discard(hwnd)
                    self._known_visible_hwnds.add(hwnd)
                self._unhide_if_prehidden(hwnd)
                return
            if is_known_vis or hwnd in self._prehidden_minimized or w32.user32.IsIconic(hwnd):
                return
            # Filter non-app / shell / IME / CoreWindow HWNDs BEFORE updating Virtual Desktop burst counter!
            style = w32.user32.GetWindowLongPtrW(hwnd, w32.GWL_STYLE)
            if (style & w32.WS_CHILD) or not (style & w32.WS_CAPTION) or w32.is_overlay_exstyle(hwnd):
                self._unhide_if_prehidden(hwnd)
                return
            cls = w32.get_class_name(hwnd)
            if not cls or is_protected_window_class(cls) or any(sub in cls for sub in _INTERNAL_CREATE_CLASS_SUBSTRS):
                self._unhide_if_prehidden(hwnd)
                return
            # Virtual Desktop switch guard (2): burst of >=2 real app windows uncloaking or right after real app cloak
            self._recent_uncloak_times = [t for t in self._recent_uncloak_times if (now - t) < 0.065]
            self._recent_uncloak_times.append(now)
            if (now - self._last_cloak_ts) < 0.25 or len(self._recent_uncloak_times) >= 2:
                with self._state_lock:
                    self._known_visible_hwnds.add(hwnd)
                self._unhide_if_prehidden(hwnd)
                return
            if not self._stage_open_window(hwnd, "open"):
                self._unhide_if_prehidden(hwnd)
                with self._state_lock:
                    self._known_visible_hwnds.add(hwnd)
            return
        if event == w32.EVENT_OBJECT_HIDE:
            # Ignore HIDE fired as a side effect of minimizing (preserves cache for 0 ms Restore!)
            if w32.user32.IsIconic(hwnd):
                return
            with self._state_lock:
                self._known_visible_hwnds.discard(hwnd)
                self._cloaked_hwnds.discard(hwnd)
            if hwnd in self._pending_opens:
                self.finish_hidden_handoff(hwnd, 255)
            with self._cache_lock:
                has_cached = hwnd in self._cached_captures
            if has_cached:
                self.trigger_effect_for_window(hwnd, "close")
                with self._cache_lock:
                    self._cached_captures.pop(hwnd, None)

    def _handle_mouse_event(self, w_param: int, x: int, y: int) -> None:
        if w_param == w32.WM_MBUTTONDOWN and self.config.physics.middle_click_titlebar != "none":
            raw_hwnd = w32.window_from_point(x, y)
            root = int(w32.user32.GetAncestor(raw_hwnd, 2) or raw_hwnd)
            with self._state_lock:
                root_is_own = root in self._own_hwnds
            if root and not root_is_own and w32.hit_test_caption(root, x, y):
                action = self.config.physics.middle_click_titlebar
                if action == "rollup":
                    self.toggle_rollup_window(root)
                elif action == "minimize":
                    self.backend.minimize_window(root)
                elif action == "pin":
                    self.toggle_pin_window(root)
                elif action == "tile":
                    self.minimize_to_tile(root)
        elif w_param == w32.WM_LBUTTONDOWN and self.config.physics.double_click_desktop_icons:
            raw_hwnd = w32.window_from_point(x, y)
            cls = w32.get_class_name(raw_hwnd)
            if cls in ("SysListView32", "SHELLDLL_DefView", "Progman", "WorkerW"):
                now = time.perf_counter()
                if now - self._last_desktop_click < 0.35:
                    self._last_desktop_click = 0.0
                    self.toggle_desktop_icons()
                else:
                    self._last_desktop_click = now

    def _monitor_for_point(self, x: int, y: int) -> w32.MonitorRect:
        mons = self.backend.list_monitors()
        for m in mons:
            if m.left <= x < m.right and m.top <= y < m.bottom:
                return m
        return mons[0]

    def _build_frame(self, active_anims: int, paused: str, fg_info: Optional[WindowInfo] = None) -> EngineFrame:
        with self._state_lock:
            rolled_up_count = len(self._rolled_up)
            pinned_count = len(self._pinned)
            tiled_count = len(self._tiled)
        return EngineFrame(
            timestamp=time.perf_counter(),
            enabled=self.config.enabled,
            paused_reason=paused,
            demo_mode=self.backend.is_demo,
            active_animations=active_anims,
            total_animations_played=self._total_played,
            active_window_title=(fg_info.title if fg_info else ""),
            active_window_exe=(fg_info.exe_name if fg_info else ""),
            active_window_hwnd=(fg_info.hwnd if fg_info else 0),
            rolled_up_count=rolled_up_count,
            pinned_count=pinned_count,
            tiled_count=tiled_count,
            journal_pending=self.journal.pending_count(),
            desktop_icons_visible=self.backend.are_desktop_icons_visible(),
            on_battery=self.backend.is_on_battery(),
            fullscreen_detected=self.backend.is_fullscreen_game(),
            monitors_count=len(self.backend.list_monitors()),
        )

    def _publish_frame(self, active_anims: int = 0, fg_info: Optional[WindowInfo] = None) -> None:
        self._latest_frame = self._build_frame(active_anims, self.paused_reason(), fg_info)
