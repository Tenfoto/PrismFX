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


@dataclass
class WindowMutation:
    hwnd: int
    cloaked: bool = False
    original_alpha: Optional[int] = None
    pinned_by_app: bool = False
    original_height: Optional[int] = None
    rolled_up: bool = False
    timestamp: float = field(default_factory=time.time)


class RecoveryJournal:
    """Atomic `.part` -> fsync -> `os.replace` crash journal."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._windows: dict[int, WindowMutation] = {}
        self._desktop_icons_hidden_by_app: bool = False
        self._original_min_animate: Optional[bool] = None
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._desktop_icons_hidden_by_app = bool(raw.get("desktop_icons_hidden_by_app", False))
            self._original_min_animate = raw.get("original_min_animate", None)
            for item in raw.get("windows", []):
                m = WindowMutation(
                    hwnd=int(item["hwnd"]),
                    cloaked=bool(item.get("cloaked", False)),
                    original_alpha=item.get("original_alpha"),
                    pinned_by_app=bool(item.get("pinned_by_app", False)),
                    original_height=item.get("original_height"),
                    rolled_up=bool(item.get("rolled_up", False)),
                    timestamp=float(item.get("timestamp", 0.0)),
                )
                self._windows[m.hwnd] = m
        except Exception:
            self._windows.clear()
            self._desktop_icons_hidden_by_app = False
            self._original_min_animate = None

    def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "pid": os.getpid(),
            "updated_at": time.time(),
            "desktop_icons_hidden_by_app": self._desktop_icons_hidden_by_app,
            "original_min_animate": self._original_min_animate,
            "windows": [asdict(m) for m in self._windows.values()],
        }
        tmp = self.path.with_suffix(".json.part")
        data = json.dumps(payload, indent=2).encode("utf-8")
        for attempt in range(6):
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 5:
                    try:
                        # Last-resort direct overwrite if AV/indexer locked atomic replace
                        with open(self.path, "wb") as f:
                            f.write(data)
                            f.flush()
                            os.fsync(f.fileno())
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return
                time.sleep(0.008 * (attempt + 1))
            except OSError:
                return

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
            self._flush_locked()

    def record_cloak(self, hwnd: int, cloaked: bool) -> None:
        with self._lock:
            m = self._windows.get(hwnd) or WindowMutation(hwnd=hwnd)
            m.cloaked = cloaked
            m.timestamp = time.time()
            self._cleanup_entry_locked(hwnd, m)
            self._flush_locked()

    def record_alpha(self, hwnd: int, original_alpha: Optional[int]) -> None:
        with self._lock:
            m = self._windows.get(hwnd) or WindowMutation(hwnd=hwnd)
            if original_alpha is None:
                m.original_alpha = None
            elif m.original_alpha is None:
                m.original_alpha = int(original_alpha)
            m.timestamp = time.time()
            self._cleanup_entry_locked(hwnd, m)
            self._flush_locked()

    def record_pin(self, hwnd: int, pinned: bool) -> None:
        with self._lock:
            m = self._windows.get(hwnd) or WindowMutation(hwnd=hwnd)
            m.pinned_by_app = pinned
            m.timestamp = time.time()
            self._cleanup_entry_locked(hwnd, m)
            self._flush_locked()

    def record_rollup(self, hwnd: int, rolled_up: bool, original_height: Optional[int] = None) -> None:
        with self._lock:
            m = self._windows.get(hwnd) or WindowMutation(hwnd=hwnd)
            m.rolled_up = rolled_up
            if rolled_up and original_height is not None:
                m.original_height = int(original_height)
            elif not rolled_up:
                m.original_height = None
            m.timestamp = time.time()
            self._cleanup_entry_locked(hwnd, m)
            self._flush_locked()

    def record_desktop_icons_hidden(self, hidden: bool) -> None:
        with self._lock:
            self._desktop_icons_hidden_by_app = bool(hidden)
            self._flush_locked()

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
        uncloak_fn: Callable[[int], None],
        restore_alpha_fn: Callable[[int, int], None],
        unpin_fn: Callable[[int], None],
        unroll_fn: Callable[[int, int], None],
        show_desktop_icons_fn: Callable[[], None],
        restore_min_animate_fn: Optional[Callable[[bool], None]] = None,
    ) -> int:
        """Revert every recorded mutation safely and clear the journal."""
        with self._lock:
            items = list(self._windows.values())
            restore_icons = self._desktop_icons_hidden_by_app
            orig_min_anim = self._original_min_animate
            self._windows.clear()
            self._desktop_icons_hidden_by_app = False
            self._original_min_animate = None

        # Always revert live OS mutations FIRST so disk/AV locks can NEVER prevent restoring user windows!
        restored = 0
        for m in items:
            try:
                if m.cloaked:
                    uncloak_fn(m.hwnd)
                    restored += 1
                if m.original_alpha is not None:
                    restore_alpha_fn(m.hwnd, m.original_alpha)
                    restored += 1
                if m.pinned_by_app:
                    unpin_fn(m.hwnd)
                    restored += 1
                if m.rolled_up and m.original_height is not None:
                    unroll_fn(m.hwnd, m.original_height)
                    restored += 1
            except Exception:
                pass
        if restore_icons:
            try:
                show_desktop_icons_fn()
                restored += 1
            except Exception:
                pass
        if orig_min_anim is not None and restore_min_animate_fn is not None:
            try:
                restore_min_animate_fn(orig_min_anim)
                restored += 1
            except Exception:
                pass

        with self._lock:
            self._flush_locked()
        return restored
