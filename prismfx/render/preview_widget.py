"""165 Hz Zero-Swapchain GPU Interactive Shader Preview Stage & Wobbly Physics Sandbox Widget.

Backed by OffscreenGpuEngine (FBO -> QPainter.drawImage) so MainWindow has ZERO visible
OpenGL HWND swapchains and NEVER triggers NVIDIA Overlay / ShadowPlay hooking or flicker!
"""

from __future__ import annotations

import gc
import math
import time
from dataclasses import dataclass
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.effects import EffectSpec
from ..core.physics import WobblyMesh, eval_easing
from .gl_renderer import OffscreenGpuEngine, generate_mock_window_image


@dataclass
class FramePerfSummary:
    frames: int = 0
    avg_fps: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    ui_work_ms: float = 0.0
    missed_vsync: int = 0
    gc_gen0: int = 0
    gc_gen2: int = 0
    gpu_renderer: str = "Software Fallback"


class ShaderPreviewWidget(QtWidgets.QWidget):
    """Pure 2D QWidget presenting OffscreenGpuEngine RTX 3070 Ti shader frames at 165 Hz."""

    fps_updated = QtCore.Signal(float, float)  # (fps, avg_frame_ms)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)

        self._gpu = OffscreenGpuEngine.shared()
        self._frame_img: Optional[QtGui.QImage] = None
        self._fallback_mock_img: Optional[QtGui.QImage] = None

        # Playback state
        self._effect_id: int = 1
        self._effect_name: str = "Prism Glide"
        self._custom_frag: Optional[str] = None
        self._duration_ms: int = 420
        self._easing: str = "spring_smooth"
        self._intensity: float = 1.0
        self._accent_hex: str = "#2D81FF"
        self._reverse: bool = False
        self._loop_mode: bool = True
        self._progress: float = 0.0
        self._raw_t: float = 0.0
        self._playing: bool = False
        self._play_start: float = 0.0
        self._hold_until: float = 0.0
        self._elapsed: float = 0.0
        self._anchor = (0.5, 0.92)
        self._manual_scrub: Optional[float] = None

        # Interactive Wobbly Sandbox mode
        self._wobble_sandbox: bool = False
        self._wobble = WobblyMesh(22.0, 0.56, 1.15)
        self._dragging: bool = False
        self._win_offset = QtCore.QPointF(0.0, 0.0)
        self._last_mouse = QtCore.QPointF(0.0, 0.0)

        # Frame pacing, idle auto-pause & telemetry
        self._target_fps: int = 165
        self._idle_timeout_sec: float = 15.0
        self._last_interaction_ts: float = time.perf_counter()
        self._last_tick = time.perf_counter()
        self._frame_times: list[float] = []
        self._work_times: list[float] = []
        self._last_fps_emit = time.perf_counter()

        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._timer.setInterval(max(2, int(round(1000.0 / self._target_fps))))
        self._timer.timeout.connect(self._on_tick)

    def set_target_fps(self, fps: int) -> None:
        clamped = max(30, min(360, int(fps)))
        self._target_fps = clamped
        self._timer.setInterval(max(2, int(round(1000.0 / clamped))))

    def notify_interaction(self) -> None:
        self._last_interaction_ts = time.perf_counter()
        if not self.isVisible():
            return
        win = self.window()
        if win is not None and win.isMinimized():
            return
        self.start_clock()

    def start_clock(self) -> None:
        if not self.isVisible():
            return
        win = self.window()
        if win is not None and win.isMinimized():
            return
        self._last_tick = time.perf_counter()
        if not self._timer.isActive():
            self._timer.start()

    def pause_clock(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    def set_wobble_sandbox(self, enabled: bool) -> None:
        self._wobble_sandbox = enabled
        self._manual_scrub = None
        self._win_offset = QtCore.QPointF(0.0, 0.0)
        self.setCursor(QtCore.Qt.OpenHandCursor if enabled else QtCore.Qt.PointingHandCursor)
        self.notify_interaction()
        self._render_current_frame()
        self.update()

    def configure_wobble(self, stiffness: float, damping: float, amplitude: float) -> None:
        self._wobble.configure(stiffness, damping, amplitude)

    def set_effect(
        self,
        spec: EffectSpec,
        duration_ms: Optional[int] = None,
        easing: str = "spring_smooth",
        intensity: float = 1.0,
        accent_hex: Optional[str] = None,
        reverse: bool = False,
        loop: bool = True,
    ) -> None:
        self._wobble_sandbox = False
        self._manual_scrub = None
        self._effect_id = max(1, spec.shader_id) if spec.shader_id > 0 else 1
        self._effect_name = spec.name_en
        self._custom_frag = spec.custom_frag_path
        self._duration_ms = max(120, duration_ms or spec.default_ms)
        self._easing = easing
        self._intensity = intensity
        self._accent_hex = accent_hex or spec.accent_hex
        self._reverse = reverse
        self._loop_mode = loop
        self.replay()

    def set_direction(self, reverse: bool) -> None:
        self._reverse = reverse
        self.replay()

    def set_static_frame(self, effect_id: int, progress: float, accent_hex: str = "#2D81FF") -> None:
        self._wobble_sandbox = False
        self._effect_id = effect_id
        self._accent_hex = accent_hex
        self._manual_scrub = max(0.0, min(1.0, progress))
        self._progress = self._manual_scrub
        self._raw_t = self._manual_scrub
        self._elapsed = 1.25
        self._render_current_frame()
        self.update()

    def replay(self) -> None:
        self._manual_scrub = None
        self._playing = True
        self._play_start = time.perf_counter()
        self._hold_until = 0.0
        self.notify_interaction()
        self._render_current_frame()
        self.update()

    def gpu_renderer_name(self) -> str:
        self._gpu.ensure_ready()
        return self._gpu.gpu_renderer

    def _render_current_frame(self) -> None:
        if not self._gpu.ensure_ready():
            self._frame_img = None
            return
        w = max(64, self.width())
        h = max(64, self.height())
        dpr = max(1.0, float(self.devicePixelRatioF()))
        # Scale offscreen preview buffer by DPR for crisp HiDPI rendering while bounding GPU cost
        max_rw = min(1920, int(round(720.0 * dpr)))
        rw = max(64, min(int(round(w * dpr)), max_rw))
        rh = max(64, min(1920, int(round(rw * (h / max(1, w))))))

        card_w = min(rw * 0.72, rh * 1.18)
        card_h = card_w * (340.0 / 560.0)
        scale_x = rw / max(1.0, float(w))
        scale_y = rh / max(1.0, float(h))
        cx = (rw - card_w) * 0.5 + self._win_offset.x() * scale_x
        cy = (rh - card_h) * 0.5 + self._win_offset.y() * scale_y

        t0 = time.perf_counter()
        if self._wobble_sandbox:
            img = self._gpu.render_to_image(
                rw, rh, (cx, cy, card_w, card_h),
                effect_id=37, progress=0.0, raw_t=0.0, elapsed_time=self._elapsed,
                intensity=1.0, accent_hex=self._accent_hex, anchor=(0.5, 0.5),
                clear_rgba=(0.043, 0.047, 0.055, 1.0),
                wobble_offsets=self._wobble.flat_offsets(),
            )
        else:
            img = self._gpu.render_to_image(
                rw, rh, (cx, cy, card_w, card_h),
                effect_id=self._effect_id, progress=self._progress, raw_t=self._raw_t,
                elapsed_time=self._elapsed, intensity=self._intensity,
                accent_hex=self._accent_hex, anchor=self._anchor,
                clear_rgba=(0.043, 0.047, 0.055, 1.0),
                custom_frag_path=self._custom_frag,
            )
        if img is not None:
            img.setDevicePixelRatio(dpr)
            self._frame_img = img
        self._work_times.append((time.perf_counter() - t0) * 1000.0)
        if len(self._work_times) > 120:
            self._work_times.pop(0)

    def _paint_status_banner(
        self, p: QtGui.QPainter, title: str, detail: str, at_top: bool = True
    ) -> None:
        margin = 10
        banner_h = 40
        w = max(80, self.width() - margin * 2)
        y = margin if at_top else max(margin, self.height() - banner_h - margin)
        rect = QtCore.QRectF(float(margin), float(y), float(w), float(banner_h))

        p.save()
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setPen(QtGui.QPen(QtGui.QColor("#D97706"), 1.2))
        p.setBrush(QtGui.QColor(38, 24, 10, 232))
        p.drawRoundedRect(rect, 6.0, 6.0)

        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor("#F59E0B"))
        p.drawRoundedRect(
            QtCore.QRectF(rect.left() + 4.0, rect.top() + 5.0, 3.0, rect.height() - 10.0), 1.5, 1.5
        )

        text_x = int(rect.left() + 14.0)
        text_w = max(20, int(rect.width() - 22.0))

        title_font = QtGui.QFont("Segoe UI", 8, QtGui.QFont.Bold)
        p.setFont(title_font)
        p.setPen(QtGui.QColor("#FBBF24"))
        p.drawText(
            QtCore.QRect(text_x, int(rect.top() + 4.0), text_w, 15),
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            title,
        )

        detail_font = QtGui.QFont("Consolas", 8)
        p.setFont(detail_font)
        p.setPen(QtGui.QColor("#FDE68A"))
        single_line = " ".join((detail or "").split())
        fm = QtGui.QFontMetrics(detail_font)
        elided = fm.elidedText(single_line, QtCore.Qt.ElideRight, text_w)
        p.drawText(
            QtCore.QRect(text_x, int(rect.top() + 19.0), text_w, 16),
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            elided,
        )
        p.restore()

    def _paint_software_fallback(self, p: QtGui.QPainter) -> None:
        if self._fallback_mock_img is None:
            self._fallback_mock_img = generate_mock_window_image(560, 340)

        w = float(max(64, self.width()))
        h = float(max(64, self.height()))
        prog = 0.0 if self._wobble_sandbox else max(0.0, min(1.0, float(self._progress)))
        scale = max(0.72, 1.0 - 0.16 * prog * max(0.2, float(self._intensity)))
        opacity = max(0.0, min(1.0, 1.0 - prog))

        base_w = min(w * 0.72, h * 1.18)
        base_h = base_w * (340.0 / 560.0)
        card_w = base_w * scale
        card_h = base_h * scale
        cx = (w - card_w) * 0.5 + (self._win_offset.x() if self._wobble_sandbox else 0.0)
        cy = (h - card_h) * 0.5 + (self._win_offset.y() if self._wobble_sandbox else -14.0 * prog)

        p.save()
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        p.setOpacity(opacity)
        p.drawImage(QtCore.QRectF(cx, cy, card_w, card_h), self._fallback_mock_img)
        p.restore()

        err_detail = self._gpu.init_error or "OpenGL 3.3 Core unavailable — using 2D software preview"
        self._paint_status_banner(
            p,
            title="OPENGL 3.3 UNAVAILABLE — 2D FALLBACK",
            detail=err_detail,
            at_top=True,
        )

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        if self._frame_img is None and self._gpu.ensure_ready():
            self._render_current_frame()
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#0B0C0E"))
        if self._frame_img is not None:
            p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
            p.drawImage(self.rect(), self._frame_img)
        elif not self._gpu.ensure_ready():
            self._paint_software_fallback(p)

        if self._custom_frag and not self._wobble_sandbox:
            custom_err = self._gpu.get_custom_shader_error(self._custom_frag)
            if custom_err:
                self._paint_status_banner(
                    p,
                    title="CUSTOM GLSL ERROR — BUILT-IN FALLBACK",
                    detail=custom_err,
                    at_top=False,
                )
        p.end()

    def benchmark_frames(self, duration_sec: float = 1.0) -> FramePerfSummary:
        """Measure real GPU offscreen shader rendering + widget presentation throughput."""
        was_active = self._timer.isActive()
        self.pause_clock()
        self._manual_scrub = None
        self._playing = True
        self._loop_mode = True
        fps_target = max(30, min(360, int(self._target_fps)))
        target_dt = 1.0 / float(fps_target)
        budget_ms = 1000.0 / float(fps_target)

        try:
            for _ in range(5):
                self._advance_animation(time.perf_counter())
                self._render_current_frame()
                self.repaint()
                QtCore.QCoreApplication.processEvents()

            gc_before = gc.get_count()
            times: list[float] = []
            works: list[float] = []
            t0 = time.perf_counter()
            prev = t0
            while (time.perf_counter() - t0) < duration_sec:
                w0 = time.perf_counter()
                self._advance_animation(w0)
                self._render_current_frame()
                self.repaint()
                QtCore.QCoreApplication.processEvents()
                w1 = time.perf_counter()
                work_ms = (w1 - w0) * 1000.0
                works.append(work_ms)
                # Pace cleanly to configured target_fps budget to report honest headroom
                rem = target_dt - (w1 - w0)
                if rem > 0.0005:
                    time.sleep(rem)
                end_t = time.perf_counter()
                times.append((end_t - prev) * 1000.0)
                prev = end_t

            gc_after = gc.get_count()
            times_sorted = sorted(times) if times else [0.0]
            n = len(times_sorted)
            avg_ms = sum(times_sorted) / max(1, n)
            avg_work = sum(works) / max(1, len(works))
            return FramePerfSummary(
                frames=n,
                avg_fps=round(1000.0 / max(0.001, avg_ms), 1),
                p50_ms=round(times_sorted[int(n * 0.50)], 2),
                p95_ms=round(times_sorted[min(n - 1, int(n * 0.95))], 2),
                p99_ms=round(times_sorted[min(n - 1, int(n * 0.99))], 2),
                max_ms=round(times_sorted[-1], 2),
                ui_work_ms=round(avg_work, 2),
                missed_vsync=sum(1 for ms in works if ms > budget_ms),
                gc_gen0=max(0, gc_after[0] - gc_before[0]),
                gc_gen2=max(0, gc_after[2] - gc_before[2]),
                gpu_renderer=self.gpu_renderer_name(),
            )
        finally:
            if was_active and self.isVisible():
                win = self.window()
                if win is None or (not win.isMinimized() and win.isActiveWindow()):
                    self.start_clock()

    def _on_tick(self) -> None:
        now = time.perf_counter()
        if not self.isVisible():
            self.pause_clock()
            return
        if not self._dragging and (now - self._last_interaction_ts) > self._idle_timeout_sec:
            self.pause_clock()
            return
        dt = min(0.05, max(0.0005, now - self._last_tick))
        self._last_tick = now
        self._elapsed += dt

        self._frame_times.append(dt * 1000.0)
        if len(self._frame_times) > 120:
            self._frame_times.pop(0)

        if self._wobble_sandbox:
            self._wobble.step(dt)
            if not self._dragging:
                self._win_offset *= math.exp(-5.0 * dt)
            self._render_current_frame()
            self.update()
        elif self._manual_scrub is None:
            self._advance_animation(now)
            self._render_current_frame()
            self.update()

        if now - self._last_fps_emit >= 0.4 and self._frame_times:
            self._last_fps_emit = now
            avg_ms = sum(self._frame_times) / len(self._frame_times)
            fps = min(360.0, 1000.0 / max(0.5, avg_ms))
            self.fps_updated.emit(fps, avg_ms)

    def _advance_animation(self, now: float) -> None:
        if not self._playing:
            return
        if self._hold_until > 0.0:
            if now < self._hold_until:
                return
            self._hold_until = 0.0
            self._play_start = now
            self._reverse = not self._reverse

        dur = max(0.12, self._duration_ms / 1000.0)
        raw = min(1.0, max(0.0, (now - self._play_start) / dur))
        eased = eval_easing(raw, self._easing)
        self._raw_t = (1.0 - raw) if self._reverse else raw
        self._progress = (1.0 - eased) if self._reverse else eased

        if raw >= 1.0:
            if self._loop_mode:
                self._hold_until = now + 0.55
            else:
                self._playing = False

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        self.notify_interaction()
        super().enterEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        self.notify_interaction()
        super().wheelEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self.notify_interaction()
        if event.button() == QtCore.Qt.LeftButton:
            if self._wobble_sandbox:
                self._dragging = True
                self._last_mouse = event.position()
                u = max(0.0, min(1.0, event.position().x() / max(1, self.width())))
                v = max(0.0, min(1.0, event.position().y() / max(1, self.height())))
                self._wobble.set_grab_point(u, v)
                self.setCursor(QtCore.Qt.ClosedHandCursor)
            else:
                self._anchor = (
                    max(0.05, min(0.95, event.position().x() / max(1, self.width()))),
                    max(0.05, min(0.95, event.position().y() / max(1, self.height()))),
                )
                self.replay()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        self.notify_interaction()
        if self._wobble_sandbox and self._dragging:
            pos = event.position()
            dx = pos.x() - self._last_mouse.x()
            dy = pos.y() - self._last_mouse.y()
            self._last_mouse = pos
            self._win_offset += QtCore.QPointF(dx, dy)
            lim_x = self.width() * 0.28
            lim_y = self.height() * 0.24
            self._win_offset.setX(max(-lim_x, min(lim_x, self._win_offset.x())))
            self._win_offset.setY(max(-lim_y, min(lim_y, self._win_offset.y())))
            self._wobble.apply_drag_delta(
                dx / max(120.0, self.width() * 0.5),
                dy / max(80.0, self.height() * 0.5),
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._wobble_sandbox and self._dragging:
            self._dragging = False
            self.setCursor(QtCore.Qt.OpenHandCursor)
        self.notify_interaction()
        super().mouseReleaseEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self.pause_clock()
        super().hideEvent(event)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        win = self.window()
        if win is None or (not win.isMinimized() and win.isActiveWindow()):
            self.notify_interaction()
        else:
            self.pause_clock()
            if self._frame_img is None:
                self.update()

    def cleanup_gl(self) -> None:
        self.pause_clock()
