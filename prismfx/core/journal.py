"""Crash-Safe Atomic Recovery Journal (Phase 6: Guaranteed OS State Restoration).

Every temporary OS mutation (cloaked window, modified opacity, topmost pin, rolled-up
original height, hidden desktop icons) is written atomically BEFORE modifying OS state.
On graceful shutdown OR next startup after an unexpected crash/kill, all mutations are
reverted automatically so no user window is ever left invisible, stuck, or mis-sized.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .logging_setup import get_logger

_log = get_logger("prismfx.core.journal")


@dataclass
class WindowMutation:
    hwnd: int
    cloaked: bool = False
    original_alpha: Optional[int] = None
    had_layered: Optional[bool] = None
    pinned_by_app: bool = False
    original_height: Optional[int] = None
    rolled_up: bool = False
    timestamp: float = field(default_factory=time.time)
    pid: int = 0
    class_name: str = ""
    boot_tick_ms: int = 0


class RecoveryJournal:
    """Two-tier crash-safe atomic `.part` -> fsync -> `os.replace` recovery journal.

    - Tier 1 (sync=True): Immediate atomic write + fsync for persistent OS mutations
      (pin, rollup, desktop icons, native minimize suppression, cloak, custom opacity, restore_all).
    - Tier 2 (sync=False): Zero-I/O in-memory update for transient animation alpha=0
      mutations, coalesced over ~25 ms by a dedicated background writer thread.
    """

    def __init__(self, path: pathlib.Path, coalesce_sec: float = 0.025) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._windows: dict[int, WindowMutation] = {}
        self._desktop_icons_hidden_by_app: bool = False
        self._original_min_animate: Optional[bool] = None
        self.last_restore_failed_count: int = 0
        self._coalesce_sec: float = max(0.016, min(0.050, float(coalesce_sec)))
        self._dirty: bool = False
        self._write_gen: int = 0
        self._flushed_gen: int = 0
        self._closed: bool = False
        self._write_event = threading.Event()
        self._stop_event = threading.Event()
        self._writer_thread: Optional[threading.Thread] = None
        self._load_existing()

    def _try_load_file(self, candidate: pathlib.Path) -> bool:
        if not candidate.is_file():
            return False
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                _log.warning("Recovery journal %s root JSON is not an object", candidate)
                return False
            loaded_windows: dict[int, WindowMutation] = {}
            win_list = raw.get("windows", [])
            if not isinstance(win_list, list):
                _log.warning("Recovery journal %s 'windows' entry is not a list", candidate)
                return False
            for item in win_list:
                if not isinstance(item, dict) or "hwnd" not in item:
                    continue
                m = WindowMutation(
                    hwnd=int(item["hwnd"]),
                    cloaked=bool(item.get("cloaked", False)),
                    original_alpha=(
                        int(item["original_alpha"])
                        if item.get("original_alpha") is not None
                        else None
                    ),
                    had_layered=(
                        bool(item["had_layered"])
                        if item.get("had_layered") is not None
                        else None
                    ),
                    pinned_by_app=bool(item.get("pinned_by_app", False)),
                    original_height=(
                        int(item["original_height"])
                        if item.get("original_height") is not None
                        else None
                    ),
                    rolled_up=bool(item.get("rolled_up", False)),
                    timestamp=float(item.get("timestamp", 0.0)),
                    pid=int(item.get("pid", 0) or 0),
                    class_name=str(item.get("class_name", "") or ""),
                    boot_tick_ms=int(item.get("boot_tick_ms", 0) or 0),
                )
                loaded_windows[m.hwnd] = m
            orig_min = raw.get("original_min_animate", None)
            self._windows = loaded_windows
            self._desktop_icons_hidden_by_app = bool(raw.get("desktop_icons_hidden_by_app", False))
            self._original_min_animate = None if orig_min is None else bool(orig_min)
            return True
        except Exception as exc:
            _log.warning("Failed to load recovery journal %s: %s", candidate, exc)
            return False

    def _load_existing(self) -> None:
        part_path = self.path.with_suffix(".json.part")
        if self._try_load_file(self.path):
            return
        if self._try_load_file(part_path):
            return
        self._windows.clear()
        self._desktop_icons_hidden_by_app = False
        self._original_min_animate = None

    def _write_payload_atomic(self, payload: dict[str, object]) -> bool:
        """Atomically write payload to `.json.part` -> fsync -> `os.replace` (caller holds `_io_lock`)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.warning("Failed to ensure recovery journal dir %s: %s", self.path.parent, exc)
            return False
        tmp = self.path.with_suffix(".json.part")
        data = json.dumps(payload, indent=2).encode("utf-8")
        for attempt in range(6):
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return True
            except PermissionError as exc:
                if attempt == 5:
                    # Never fall back to non-atomic direct overwrite; preserve valid .part for recovery
                    _log.warning(
                        "Atomic replace of recovery journal %s failed after 6 attempts: %s",
                        self.path,
                        exc,
                    )
                    return False
                time.sleep(0.008 * (attempt + 1))
            except OSError as exc:
                _log.warning("Failed to write recovery journal %s: %s", self.path, exc)
                return False
        return False

    def _flush_to_disk(self, min_gen: Optional[int] = None, force: bool = False) -> bool:
        """Snapshot latest state under `_lock` and flush atomically under `_io_lock` outside `_lock`."""
        with self._io_lock:
            with self._lock:
                if not force and not self._dirty:
                    if min_gen is None or self._flushed_gen >= min_gen:
                        return False
                snap_gen = self._write_gen
                payload: dict[str, object] = {
                    "version": 1,
                    "pid": os.getpid(),
                    "updated_at": time.time(),
                    "desktop_icons_hidden_by_app": self._desktop_icons_hidden_by_app,
                    "original_min_animate": self._original_min_animate,
                    "windows": [asdict(m) for m in self._windows.values()],
                }
                self._dirty = False
            ok = self._write_payload_atomic(payload)
            with self._lock:
                if ok:
                    if snap_gen > self._flushed_gen:
                        self._flushed_gen = snap_gen
                else:
                    self._dirty = True
            return ok

    def _schedule_async_flush_locked(self) -> bool:
        """Mark dirty and wake background writer thread (caller holds `_lock`). Returns True if caller must sync-flush."""
        self._write_gen += 1
        self._dirty = True
        if self._closed:
            return True
        if self._writer_thread is None or not self._writer_thread.is_alive():
            self._stop_event.clear()
            th = threading.Thread(
                target=self._writer_loop, name="PrismFX-JournalWriter", daemon=True
            )
            self._writer_thread = th
            th.start()
        self._write_event.set()
        return False

    def _writer_loop(self) -> None:
        while not self._stop_event.is_set():
            self._write_event.wait(timeout=0.5)
            if self._stop_event.is_set():
                break
            if not self._write_event.is_set():
                with self._lock:
                    if not self._dirty:
                        continue
            # Coalesce rapid transient animation mutations (~25 ms window)
            self._stop_event.wait(self._coalesce_sec)
            if self._stop_event.is_set():
                break
            self._write_event.clear()
            self._flush_to_disk(force=False)

    def flush_sync(self) -> None:
        """Immediately flush any dirty in-memory journal state atomically with `fsync`."""
        self._flush_to_disk(force=False)

    def close(self) -> None:
        """Stop the background writer thread cleanly and perform a final synchronous flush."""
        with self._lock:
            self._closed = True
            th = self._writer_thread
            self._writer_thread = None
        self._stop_event.set()
        self._write_event.set()
        if th is not None and th is not threading.current_thread() and th.is_alive():
            th.join(timeout=1.0)
        self.flush_sync()

    def has_pending_recovery(self) -> bool:
        with self._lock:
            return (
                bool(self._windows)
                or self._desktop_icons_hidden_by_app
                or self._original_min_animate is not None
            )

    def pending_count(self) -> int:
        with self._lock:
            return (
                len(self._windows)
                + (1 if self._desktop_icons_hidden_by_app else 0)
                + (1 if self._original_min_animate is not None else 0)
            )

    def record_native_min_animate(self, original_value: Optional[bool]) -> None:
        with self._lock:
            if original_value is None:
                self._original_min_animate = None
            elif self._original_min_animate is None:
                self._original_min_animate = bool(original_value)
            self._write_gen += 1
            self._dirty = True
            target_gen = self._write_gen
        self._flush_to_disk(min_gen=target_gen)

    @staticmethod
    def _apply_identity(
        m: WindowMutation,
        pid: int = 0,
        class_name: str = "",
        boot_tick_ms: int = 0,
    ) -> None:
        if pid:
            m.pid = int(pid)
        if class_name:
            m.class_name = str(class_name)
        if boot_tick_ms:
            m.boot_tick_ms = int(boot_tick_ms)

    def record_cloak(
        self,
        hwnd: int,
        cloaked: bool,
        pid: int = 0,
        class_name: str = "",
        boot_tick_ms: int = 0,
    ) -> None:
        with self._lock:
            m = self._windows.get(hwnd) or WindowMutation(hwnd=hwnd)
            m.cloaked = cloaked
            self._apply_identity(m, pid=pid, class_name=class_name, boot_tick_ms=boot_tick_ms)
            m.timestamp = time.time()
            self._cleanup_entry_locked(hwnd, m)
            self._write_gen += 1
            self._dirty = True
            target_gen = self._write_gen
        self._flush_to_disk(min_gen=target_gen)

    def record_alpha(
        self,
        hwnd: int,
        original_alpha: Optional[int],
        had_layered: Optional[bool] = None,
        pid: int = 0,
        class_name: str = "",
        boot_tick_ms: int = 0,
        sync: bool = True,
    ) -> None:
        need_sync_flush = False
        target_gen = 0
        with self._lock:
            existing = self._windows.get(hwnd)
            if original_alpha is None and existing is None:
                return
            m = existing or WindowMutation(hwnd=hwnd)
            if original_alpha is None:
                m.original_alpha = None
                m.had_layered = None
            elif m.original_alpha is None:
                m.original_alpha = int(original_alpha)
                m.had_layered = None if had_layered is None else bool(had_layered)
            self._apply_identity(m, pid=pid, class_name=class_name, boot_tick_ms=boot_tick_ms)
            m.timestamp = time.time()
            self._cleanup_entry_locked(hwnd, m)
            if sync:
                self._write_gen += 1
                self._dirty = True
                target_gen = self._write_gen
                need_sync_flush = True
            else:
                need_sync_flush = self._schedule_async_flush_locked()
                target_gen = self._write_gen
        if need_sync_flush:
            self._flush_to_disk(min_gen=target_gen)

    def record_pin(
        self,
        hwnd: int,
        pinned: bool,
        pid: int = 0,
        class_name: str = "",
        boot_tick_ms: int = 0,
    ) -> None:
        with self._lock:
            m = self._windows.get(hwnd) or WindowMutation(hwnd=hwnd)
            m.pinned_by_app = pinned
            self._apply_identity(m, pid=pid, class_name=class_name, boot_tick_ms=boot_tick_ms)
            m.timestamp = time.time()
            self._cleanup_entry_locked(hwnd, m)
            self._write_gen += 1
            self._dirty = True
            target_gen = self._write_gen
        self._flush_to_disk(min_gen=target_gen)

    def record_rollup(
        self,
        hwnd: int,
        rolled_up: bool,
        original_height: Optional[int] = None,
        pid: int = 0,
        class_name: str = "",
        boot_tick_ms: int = 0,
    ) -> None:
        with self._lock:
            m = self._windows.get(hwnd) or WindowMutation(hwnd=hwnd)
            m.rolled_up = rolled_up
            if rolled_up and original_height is not None:
                m.original_height = int(original_height)
            elif not rolled_up:
                m.original_height = None
            self._apply_identity(m, pid=pid, class_name=class_name, boot_tick_ms=boot_tick_ms)
            m.timestamp = time.time()
            self._cleanup_entry_locked(hwnd, m)
            self._write_gen += 1
            self._dirty = True
            target_gen = self._write_gen
        self._flush_to_disk(min_gen=target_gen)

    def prune_hwnd(self, hwnd: int, sync: bool = True) -> bool:
        """Remove a destroyed HWND from the recovery journal without restoring it."""
        need_sync_flush = False
        target_gen = 0
        with self._lock:
            removed = self._windows.pop(int(hwnd), None)
            if removed is None:
                return False
            if sync:
                self._write_gen += 1
                self._dirty = True
                target_gen = self._write_gen
                need_sync_flush = True
            else:
                need_sync_flush = self._schedule_async_flush_locked()
                target_gen = self._write_gen
        if need_sync_flush:
            self._flush_to_disk(min_gen=target_gen)
        return True

    def record_desktop_icons_hidden(self, hidden: bool) -> None:
        with self._lock:
            self._desktop_icons_hidden_by_app = bool(hidden)
            self._write_gen += 1
            self._dirty = True
            target_gen = self._write_gen
        self._flush_to_disk(min_gen=target_gen)

    def get_mutation(self, hwnd: int) -> Optional[WindowMutation]:
        with self._lock:
            return self._windows.get(hwnd)

    def _cleanup_entry_locked(self, hwnd: int, m: WindowMutation) -> None:
        if (
            not m.cloaked
            and m.original_alpha is None
            and not m.pinned_by_app
            and not m.rolled_up
        ):
            self._windows.pop(hwnd, None)
        else:
            self._windows[hwnd] = m

    def restore_all(
        self,
        uncloak_fn: Callable[[int], object],
        restore_alpha_fn: Callable[[int, int], object],
        unpin_fn: Callable[[int], object],
        unroll_fn: Callable[[int, int], object],
        show_desktop_icons_fn: Callable[[], object],
        restore_min_animate_fn: Optional[Callable[[bool], object]] = None,
        restore_alpha_layered_fn: Optional[Callable[[int, int, Optional[bool]], object]] = None,
        verify_window_fn: Optional[Callable[[WindowMutation], bool]] = None,
    ) -> int:
        """Revert every recorded mutation safely, preserving any failed entries on disk."""
        with self._lock:
            items = [
                WindowMutation(
                    hwnd=m.hwnd,
                    cloaked=m.cloaked,
                    original_alpha=m.original_alpha,
                    had_layered=m.had_layered,
                    pinned_by_app=m.pinned_by_app,
                    original_height=m.original_height,
                    rolled_up=m.rolled_up,
                    timestamp=m.timestamp,
                    pid=m.pid,
                    class_name=m.class_name,
                    boot_tick_ms=m.boot_tick_ms,
                )
                for m in self._windows.values()
            ]
            restore_icons = self._desktop_icons_hidden_by_app
            orig_min_anim = self._original_min_animate

        def _succeeded(res: object) -> bool:
            # Explicit False signals Win32/UIPI failure; True or None (legacy void callbacks) counts as ok.
            return res is not False

        # Always revert live OS mutations FIRST so disk/AV locks can NEVER prevent restoring user windows!
        restored = 0
        failed = 0
        win_results: dict[int, tuple[bool, bool, bool, bool]] = {}

        for m in items:
            if verify_window_fn is not None:
                try:
                    valid_identity = bool(verify_window_fn(m))
                except Exception as exc:
                    _log.warning("Journal verify_window_fn failed for hwnd=%s: %s", m.hwnd, exc)
                    valid_identity = False
                if not valid_identity:
                    win_results[m.hwnd] = (True, True, True, True)
                    continue

            ok_cloak = False
            ok_alpha = False
            ok_pin = False
            ok_roll = False

            if m.cloaked:
                try:
                    if _succeeded(uncloak_fn(m.hwnd)):
                        ok_cloak = True
                        restored += 1
                    else:
                        failed += 1
                except Exception as exc:
                    _log.warning("Journal uncloak_fn failed for hwnd=%s: %s", m.hwnd, exc)
                    failed += 1

            if m.original_alpha is not None:
                try:
                    alpha_res = (
                        restore_alpha_layered_fn(m.hwnd, m.original_alpha, m.had_layered)
                        if restore_alpha_layered_fn is not None
                        else restore_alpha_fn(m.hwnd, m.original_alpha)
                    )
                    if _succeeded(alpha_res):
                        ok_alpha = True
                        restored += 1
                    else:
                        failed += 1
                except Exception as exc:
                    _log.warning("Journal restore_alpha failed for hwnd=%s: %s", m.hwnd, exc)
                    failed += 1

            if m.pinned_by_app:
                try:
                    if _succeeded(unpin_fn(m.hwnd)):
                        ok_pin = True
                        restored += 1
                    else:
                        failed += 1
                except Exception as exc:
                    _log.warning("Journal unpin_fn failed for hwnd=%s: %s", m.hwnd, exc)
                    failed += 1

            if m.rolled_up and m.original_height is not None:
                try:
                    if _succeeded(unroll_fn(m.hwnd, m.original_height)):
                        ok_roll = True
                        restored += 1
                    else:
                        failed += 1
                except Exception as exc:
                    _log.warning("Journal unroll_fn failed for hwnd=%s: %s", m.hwnd, exc)
                    failed += 1

            win_results[m.hwnd] = (ok_cloak, ok_alpha, ok_pin, ok_roll)

        ok_icons = False
        if restore_icons:
            try:
                if _succeeded(show_desktop_icons_fn()):
                    ok_icons = True
                    restored += 1
                else:
                    failed += 1
            except Exception as exc:
                _log.warning("Journal show_desktop_icons_fn failed: %s", exc)
                failed += 1

        ok_min_anim = False
        if orig_min_anim is not None and restore_min_animate_fn is not None:
            try:
                if _succeeded(restore_min_animate_fn(orig_min_anim)):
                    ok_min_anim = True
                    restored += 1
                else:
                    failed += 1
            except Exception as exc:
                _log.warning("Journal restore_min_animate_fn failed: %s", exc)
                failed += 1

        with self._lock:
            for hwnd, (ok_cloak, ok_alpha, ok_pin, ok_roll) in win_results.items():
                cur = self._windows.get(hwnd)
                if cur is None:
                    continue
                if ok_cloak:
                    cur.cloaked = False
                if ok_alpha:
                    cur.original_alpha = None
                    cur.had_layered = None
                if ok_pin:
                    cur.pinned_by_app = False
                if ok_roll:
                    cur.rolled_up = False
                    cur.original_height = None
                self._cleanup_entry_locked(hwnd, cur)

            if restore_icons and ok_icons:
                self._desktop_icons_hidden_by_app = False
            if orig_min_anim is not None and ok_min_anim:
                self._original_min_animate = None

            self.last_restore_failed_count = failed
            self._write_gen += 1
            self._dirty = True
            target_gen = self._write_gen
        self._flush_to_disk(min_gen=target_gen, force=True)
        return restored
