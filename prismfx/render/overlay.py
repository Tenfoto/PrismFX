"""Zero-Swapchain Desktop Animation Overlay (UpdateLayeredWindow), Focus Spotlight, Mini-Tiles & Quick Bar.

Replaces fullscreen topmost QOpenGLWidget with tightly-bounded Win32 UpdateLayeredWindow(ULW_ALPHA)
surfaces backed by OffscreenGpuEngine:
  1. ZERO OpenGL swapchain HWNDs -> 100% immune to NVIDIA Overlay (Alt+Z) / MPO flicker!
  2. Tightly bounded per-window rect (+ overscan) in physical screen pixels -> 1:1 DWM alignment!
  3. Zero-flash handoff on Open/Restore: draws final opaque progress=0.0 frame -> unhides real
     window underneath -> DwmFlush() 1-VSync sync -> hides overlay invisibly!
"""

from __future__ import annotations

import math
import queue
import time
from dataclasses import dataclass
from typing import Optional

from PySide6 import QtCore, QtGui, QtOpenGL, QtWidgets

from ..core import win32_api as w32
from ..core.backend import WindowInfo
from ..core.engine import AnimationJob, MorphJob, PreCoverJob, PrismEngine, SpotlightState, WobblyDragStart
from ..core.physics import WobblyMesh, eval_easing
from .gl_renderer import OffscreenGpuEngine


@dataclass
class _PreCoverState:
    hwnd: int
    surface: w32.Win32LayeredSurface
    shown_at: float


@dataclass
class _LiveMorph:
    job: MorphJob
    from_tex: QtOpenGL.QOpenGLTexture
    to_tex: Optional[QtOpenGL.QOpenGLTexture]
    surface: w32.Win32LayeredSurface
    start_time: float
    union_x: int
    union_y: int
    union_w: int
    union_h: int


@dataclass
class _LiveWobble:
    hwnd: int
    texture: QtOpenGL.QOpenGLTexture
    surface: w32.Win32LayeredSurface
    mesh: WobblyMesh
    x: int
    y: int
    w: int
    h: int
    pad_x: int
    pad_y: int
    surf_w: int
    surf_h: int
    orig_alpha: int
    drag_fade_target: float
    cur_fade: float = 0.0
    settling: bool = False
    settle_started_at: float = 0.0


@dataclass
class _LiveAnim:
    job: AnimationJob
    texture: QtOpenGL.QOpenGLTexture
    surface: w32.Win32LayeredSurface
    start_time: float
    pad_x: int
    pad_y: int
    surf_x: int
    surf_y: int
    surf_w: int
    surf_h: int


class DesktopFxOverlay(QtCore.QObject):
    """Manages zero-swapchain Win32LayeredSurface instances for live 165 Hz GPU window animations."""

    _job_ready = QtCore.Signal()
    _morph_ready_sig = QtCore.Signal(object)
    _precover_show_sig = QtCore.Signal(object)
    _precover_cancel_sig = QtCore.Signal(int)
    _wobbly_start_sig = QtCore.Signal(object)
    _wobbly_end_sig = QtCore.Signal(int, int, int, bool)

    def __init__(self, engine: PrismEngine) -> None:
        super().__init__(None)
        self.engine = engine
        self._gpu = OffscreenGpuEngine.shared()
        self._active: list[_LiveAnim] = []
        self._morphs: list[_LiveMorph] = []
        self._surface_pool: list[w32.Win32LayeredSurface] = []
        self._precover: Optional[_PreCoverState] = None
        self._wobble: Optional[_LiveWobble] = None
        self._elapsed = 0.0
        self._last_tick = time.perf_counter()

        # Pre-allocate & park 1 warm layered surface + OpenGL context so Frame 0 has 0 ms DWM init stall!
        self._gpu.ensure_ready()
        first_surf = w32.Win32LayeredSurface()
        if first_surf.hwnd:
            first_surf.park()
            self.engine.register_own_hwnd(first_surf.hwnd)
            self._surface_pool.append(first_surf)

        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._timer.setInterval(6)  # ~165 Hz pacing
        self._timer.timeout.connect(self._tick)

        self._precover_timer = QtCore.QTimer(self)
        self._precover_timer.setSingleShot(True)
        self._precover_timer.setInterval(85)  # Hard 85 ms auto-cancel safety if window didn't minimize/close
        self._precover_timer.timeout.connect(self._cancel_precover_timeout)

        # Zero-latency signal-driven pickup (~0.14 ms from hook/raw-input threads to GUI thread!)
        self._job_ready.connect(self._drain_jobs, QtCore.Qt.QueuedConnection)
        self._morph_ready_sig.connect(self._on_morph_queued, QtCore.Qt.QueuedConnection)
        self._precover_show_sig.connect(self._on_precover_show, QtCore.Qt.QueuedConnection)
        self._precover_cancel_sig.connect(self._on_precover_cancel, QtCore.Qt.QueuedConnection)
        self._wobbly_start_sig.connect(self._on_wobbly_start, QtCore.Qt.QueuedConnection)
        self._wobbly_end_sig.connect(self._on_wobbly_end, QtCore.Qt.QueuedConnection)
        self.engine.on_animation_queued = self._on_engine_job_queued
        self.engine.on_morph_queued = self._on_engine_morph_queued
        self.engine.on_precover_show = self._on_engine_precover_show
        self.engine.on_precover_cancel = self._on_engine_precover_cancel
        self.engine.on_wobbly_start = self._on_engine_wobbly_start
        self.engine.on_wobbly_end = self._on_engine_wobbly_end

        # Low-frequency safety fallback poll in case a job is enqueued directly without callback
        self._poll_queue_timer = QtCore.QTimer(self)
        self._poll_queue_timer.setInterval(50)
        self._poll_queue_timer.timeout.connect(self._drain_jobs)
        self._poll_queue_timer.start()

    def _on_engine_job_queued(self, _job: AnimationJob) -> None:
        self._job_ready.emit()

    def _on_engine_morph_queued(self, mjob: MorphJob) -> None:
        self._morph_ready_sig.emit(mjob)

    def _on_engine_precover_show(self, pjob: PreCoverJob) -> None:
        self._precover_show_sig.emit(pjob)

    def _on_engine_precover_cancel(self, hwnd: int) -> None:
        self._precover_cancel_sig.emit(int(hwnd))

    def _on_engine_wobbly_start(self, req: WobblyDragStart) -> None:
        self._wobbly_start_sig.emit(req)

    def _on_engine_wobbly_end(self, hwnd: int, final_x: int, final_y: int, snap_cancel: bool) -> None:
        self._wobbly_end_sig.emit(int(hwnd), int(final_x), int(final_y), bool(snap_cancel))

    def _on_precover_show(self, pjob: PreCoverJob) -> None:
        """Stamp pixel-identical opaque Frame-0 cover BEFORE Windows unmaps/resizes the window!"""
        if not isinstance(pjob, PreCoverJob):
            return
        if self._precover is not None:
            surf = self._precover.surface
            self._precover = None
        elif self._wobble is not None and self._wobble.hwnd == pjob.hwnd and pjob.trigger == "maximize":
            # Seamless 1-surface adoption straight from live Wobbly drag -> PreCover -> Smooth Maximize Morph!
            wob = self._wobble
            self._wobble = None
            surf = wob.surface
            self._gpu.destroy_texture(wob.texture)
        else:
            surf = self._acquire_surface()
        ok = surf.present_opaque_bgra(pjob.x, pjob.y, pjob.img_w, pjob.img_h, pjob.bgra_bytes)
        if ok:
            self._precover = _PreCoverState(hwnd=pjob.hwnd, surface=surf, shown_at=time.perf_counter())
            self._precover_timer.start(260 if pjob.trigger == "maximize" else 85)
        else:
            self._release_surface(surf)

    def _on_precover_cancel(self, hwnd: int = 0) -> None:
        if self._precover is None:
            return
        if hwnd and self._precover.hwnd != hwnd:
            return
        self._precover_timer.stop()
        surf = self._precover.surface
        self._precover = None
        self._release_surface(surf)

    def _cancel_precover_timeout(self) -> None:
        self._on_precover_cancel(0)
        self.engine._active_precover_hwnd = 0

    def primary_surface_hwnd(self) -> int:
        if self._active:
            return self._active[0].surface.hwnd
        if self._morphs:
            return self._morphs[0].surface.hwnd
        if self._wobble is not None:
            return self._wobble.surface.hwnd
        if self._precover is not None:
            return self._precover.surface.hwnd
        if self._surface_pool:
            return self._surface_pool[0].hwnd
        return 0

    def _render_morph(self, item: _LiveMorph, eased: float) -> None:
        fx, fy, fw, fh = item.job.from_rect
        tx, ty, tw, th = item.job.to_rect
        cur_x = fx + (tx - fx) * eased
        cur_y = fy + (ty - fy) * eased
        cur_w = max(16.0, fw + (tw - fw) * eased)
        cur_h = max(16.0, fh + (th - fh) * eased)
        rect = (cur_x - item.union_x, cur_y - item.union_y, cur_w, cur_h)

        # Smoothstep cross-dissolve between old layout & reflowed target layout
        bx = max(0.0, min(1.0, (eased - 0.10) / 0.80))
        blend_t = bx * bx * (3.0 - 2.0 * bx)

        dib_ptr = item.surface.acquire_bottom_up_dib(item.union_w, item.union_h)
        if dib_ptr and self._gpu.render_morph_to_dib_ptr(
            item.union_w, item.union_h, rect, item.from_tex, item.to_tex, blend_t, dib_ptr
        ):
            item.surface.commit_premul_dib(item.union_x, item.union_y, item.union_w, item.union_h)
            return

        img = self._gpu.render_morph_to_image(
            item.union_w, item.union_h, rect, item.from_tex, item.to_tex, blend_t
        )
        if img is None:
            return
        if img.format() != QtGui.QImage.Format_ARGB32_Premultiplied:
            img = img.convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
        item.surface.present_premul_bgra(
            item.union_x, item.union_y, img.width(), img.height(), img.constBits()
        )

    def _on_morph_queued(self, mjob: MorphJob) -> None:
        if not isinstance(mjob, MorphJob):
            return
        # Retire any prior morph or wobble for the same HWND cleanly
        if self._wobble is not None and self._wobble.hwnd == mjob.hwnd:
            self._finish_wobble(instant_cancel=True)
        kept_m: list[_LiveMorph] = []
        for ex in self._morphs:
            if ex.job.hwnd == mjob.hwnd:
                self._release_surface(ex.surface)
                self._gpu.destroy_texture(ex.from_tex)
                if ex.to_tex is not None:
                    self._gpu.destroy_texture(ex.to_tex)
            else:
                kept_m.append(ex)
        self._morphs = kept_m

        # Zero-gap adoption of PreCover surface if already covering this window!
        if self._precover is not None and self._precover.hwnd == mjob.hwnd:
            self._precover_timer.stop()
            surf = self._precover.surface
            self._precover = None
        else:
            if self._precover is not None:
                self._on_precover_cancel(0)
            surf = self._acquire_surface()

        from_tex = self._gpu.create_texture_from_bgra(mjob.from_img_w, mjob.from_img_h, mjob.from_bgra)
        if from_tex is None:
            self._release_surface(surf)
            if mjob.restore_hidden_hwnd:
                self.engine.finish_hidden_handoff(mjob.restore_hidden_hwnd, mjob.restore_alpha)
            return
        to_tex = (
            self._gpu.create_texture_from_bgra(mjob.to_img_w, mjob.to_img_h, mjob.to_bgra)
            if (mjob.to_bgra is not None and mjob.to_img_w > 0 and mjob.to_img_h > 0)
            else None
        )

        fx, fy, fw, fh = mjob.from_rect
        tx, ty, tw, th = mjob.to_rect
        ux = min(fx, tx)
        uy = min(fy, ty)
        uw = max(16, max(fx + fw, tx + tw) - ux)
        uh = max(16, max(fy + fh, ty + th) - uy)

        now = time.perf_counter()
        item = _LiveMorph(
            job=mjob,
            from_tex=from_tex,
            to_tex=to_tex,
            surface=surf,
            start_time=now,
            union_x=ux,
            union_y=uy,
            union_w=uw,
            union_h=uh,
        )
        self._render_morph(item, 0.0)
        self._morphs.append(item)
        if not self._timer.isActive():
            self._last_tick = now
            self._timer.start()

    def _compute_wobble_padding(self, w: int, h: int) -> tuple[int, int]:
        """Compute generous overscan around the dragged window so jelly corners never clip."""
        pad_x = min(200, max(96, int(w * 0.20)))
        pad_y = min(160, max(84, int(h * 0.20)))
        if w + pad_x * 2 > 2200:
            pad_x = max(0, (2200 - w) // 2)
        if h + pad_y * 2 > 1320:
            pad_y = max(0, (1320 - h) // 2)
        return (pad_x, pad_y)

    def _soft_clamp_wobble(self, wob: _LiveWobble, raw_offsets: list[float]) -> list[float]:
        """Smooth tanh cushion keeping bent jelly corners comfortably inside the overscan DIB."""
        max_u = max(0.0, (wob.pad_x - 8) / max(1.0, float(wob.w)))
        max_v = max(0.0, (wob.pad_y - 8) / max(1.0, float(wob.h)))
        out: list[float] = [0.0] * len(raw_offsets)
        for i in range(0, len(raw_offsets), 2):
            vx = raw_offsets[i]
            vy = raw_offsets[i + 1]
            out[i] = (max_u * math.tanh(vx / max_u)) if max_u > 1e-4 else 0.0
            out[i + 1] = (max_v * math.tanh(vy / max_v)) if max_v > 1e-4 else 0.0
        return out

    def _render_wobble(
        self, wob: _LiveWobble, wobble_offsets: Optional[list[float]], fade_progress: float
    ) -> bool:
        rect = (float(wob.pad_x), float(wob.pad_y), float(wob.w), float(wob.h))
        surf_x = wob.x - wob.pad_x
        surf_y = wob.y - wob.pad_y
        fade = max(0.0, min(0.75, float(fade_progress)))

        # Fast zero-copy path: glReadPixels(GL_BGRA) straight into bottom-up Win32 DIB
        dib_ptr = wob.surface.acquire_bottom_up_dib(wob.surf_w, wob.surf_h)
        if dib_ptr and self._gpu.render_to_dib_ptr(
            viewport_w=wob.surf_w,
            viewport_h=wob.surf_h,
            rect=rect,
            effect_id=37,  # Pure Crossfade: zero shader distortion, smooth GPU drag opacity
            progress=fade,
            raw_t=fade,
            elapsed_time=self._elapsed,
            dib_ptr=dib_ptr,
            intensity=1.0,
            accent_hex="#2D81FF",
            anchor=(0.5, 0.5),
            texture=wob.texture,
            clear_rgba=(0.0, 0.0, 0.0, 0.0),
            wobble_offsets=wobble_offsets,
        ):
            return wob.surface.commit_premul_dib(surf_x, surf_y, wob.surf_w, wob.surf_h)

        img = self._gpu.render_to_image(
            viewport_w=wob.surf_w,
            viewport_h=wob.surf_h,
            rect=rect,
            effect_id=37,
            progress=fade,
            raw_t=fade,
            elapsed_time=self._elapsed,
            intensity=1.0,
            accent_hex="#2D81FF",
            anchor=(0.5, 0.5),
            texture=wob.texture,
            clear_rgba=(0.0, 0.0, 0.0, 0.0),
            wobble_offsets=wobble_offsets,
        )
        if img is None:
            return False
        if img.format() != QtGui.QImage.Format_ARGB32_Premultiplied:
            img = img.convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
        return wob.surface.present_premul_bgra(surf_x, surf_y, img.width(), img.height(), img.constBits())

    def _finish_wobble(self, instant_cancel: bool = False) -> None:
        wob = self._wobble
        if wob is None:
            return
        self._wobble = None
        if not instant_cancel:
            if not self.engine.backend.is_demo and self.engine.backend.is_valid_window(wob.hwnd):
                rx, ry, rw, rh = w32.get_extended_frame_bounds(wob.hwnd)
                if rw == wob.w and rh == wob.h and rx > -10000:
                    wob.x, wob.y = rx, ry
            # 1) Present pixel-identical flat opaque frame at final coordinates before unhiding real window
            self._render_wobble(wob, None, 0.0)
        # 2) Unhide real window directly behind opaque overlay & wait 1 exact DWM VSync
        self.engine.finish_wobble_handoff(wob.hwnd, wob.orig_alpha)
        # 3) Park warm surface & release GPU texture
        self._release_surface(wob.surface)
        self._gpu.destroy_texture(wob.texture)

    def _on_wobbly_start(self, req: WobblyDragStart) -> None:
        if not isinstance(req, WobblyDragStart):
            return
        ph = self.engine.config.physics
        fade_target = (
            max(0.0, min(0.65, 1.0 - max(25, min(100, ph.drag_opacity_pct)) / 100.0))
            if ph.drag_transparency_enabled
            else 0.0
        )

        # Seamless mid-bounce re-grab of the SAME settling window: keep jelly live with 0 ms flash!
        if (
            self._wobble is not None
            and self._wobble.hwnd == req.hwnd
            and self._wobble.w == req.w
            and self._wobble.h == req.h
        ):
            wob = self._wobble
            wob.settling = False
            wob.drag_fade_target = fade_target
            wob.mesh.grab_u = max(0.0, min(1.0, req.grab_u))
            wob.mesh.grab_v = max(0.0, min(1.0, req.grab_v))
            wob.mesh.configure(ph.wobbly_stiffness, ph.wobbly_damping, ph.wobbly_amplitude)
            live = self.engine._wobble_live_pos
            lx = live[1] if (live is not None and live[0] == req.hwnd) else req.x
            ly = live[2] if (live is not None and live[0] == req.hwnd) else req.y
            dx, dy = lx - wob.x, ly - wob.y
            if dx or dy:
                wob.x, wob.y = lx, ly
                wob.mesh.apply_drag_delta((dx / max(1.0, float(wob.w))) * 0.78, (dy / max(1.0, float(wob.h))) * 0.78)
            if not self._timer.isActive():
                self._last_tick = time.perf_counter()
                self._timer.start()
            return

        if self._wobble is not None:
            self._finish_wobble(instant_cancel=True)
        if self._precover is not None:
            self._on_precover_cancel(0)

        tex = self._gpu.create_texture_from_bgra(req.img_w, req.img_h, req.bgra_bytes)
        if tex is None:
            return

        pad_x, pad_y = self._compute_wobble_padding(req.w, req.h)
        surf_w = req.w + pad_x * 2
        surf_h = req.h + pad_y * 2
        surf = self._acquire_surface()

        mesh = WobblyMesh(ph.wobbly_stiffness, ph.wobbly_damping, ph.wobbly_amplitude)
        mesh.set_grab_point(req.grab_u, req.grab_v)

        cur_x, cur_y = req.x, req.y
        init_dx, init_dy = req.init_dx, req.init_dy
        live = self.engine._wobble_live_pos
        if live is not None and live[0] == req.hwnd and live[3] == req.w and live[4] == req.h:
            init_dx += live[1] - cur_x
            init_dy += live[2] - cur_y
            cur_x, cur_y = live[1], live[2]
        if not self.engine.backend.is_demo and self.engine.backend.is_valid_window(req.hwnd):
            rx, ry, rw, rh = w32.get_extended_frame_bounds(req.hwnd)
            if rw == req.w and rh == req.h and rx > -10000:
                init_dx += rx - cur_x
                init_dy += ry - cur_y
                cur_x, cur_y = rx, ry
        if init_dx or init_dy:
            init_dx = max(-96, min(96, init_dx))
            init_dy = max(-96, min(96, init_dy))
            mesh.apply_drag_delta((init_dx / max(1.0, float(req.w))) * 0.78, (init_dy / max(1.0, float(req.h))) * 0.78)

        wob = _LiveWobble(
            hwnd=req.hwnd,
            texture=tex,
            surface=surf,
            mesh=mesh,
            x=cur_x,
            y=cur_y,
            w=req.w,
            h=req.h,
            pad_x=pad_x,
            pad_y=pad_y,
            surf_w=surf_w,
            surf_h=surf_h,
            orig_alpha=req.orig_alpha,
            drag_fade_target=fade_target,
        )

        # Render Frame 0 BEFORE hiding real window underneath -> 0 ms flash on drag grab!
        f0_offsets = self._soft_clamp_wobble(wob, wob.mesh.flat_offsets())
        if not self._render_wobble(wob, f0_offsets, 0.0):
            self._release_surface(surf)
            self._gpu.destroy_texture(tex)
            return

        if not self.engine.backend.is_demo:
            if not self.engine.hide_window_for_wobble(req.hwnd, req.orig_alpha):
                # UIPI/elevated or closed window -> abort cleanly so no duplicate ghost appears!
                self._release_surface(surf)
                self._gpu.destroy_texture(tex)
                return

        self._wobble = wob
        if not self._timer.isActive():
            self._last_tick = time.perf_counter()
            self._timer.start()

    def _on_wobbly_end(self, hwnd: int, final_x: int, final_y: int, snap_cancel: bool) -> None:
        wob = self._wobble
        if wob is None or (hwnd and wob.hwnd != hwnd):
            return
        if snap_cancel:
            self._finish_wobble(instant_cancel=True)
            if not self._active and self._wobble is None:
                self._timer.stop()
            return
        if final_x > -10000 and (final_x != 0 or final_y != 0):
            dx = final_x - wob.x
            dy = final_y - wob.y
            if dx or dy:
                wob.x = final_x
                wob.y = final_y
                wob.mesh.apply_drag_delta((dx / max(1.0, float(wob.w))) * 0.78, (dy / max(1.0, float(wob.h))) * 0.78)
        wob.settling = True
        wob.settle_started_at = time.perf_counter()
        if not self._timer.isActive():
            self._last_tick = time.perf_counter()
            self._timer.start()

    def _acquire_surface(self) -> w32.Win32LayeredSurface:
        if self._surface_pool:
            return self._surface_pool.pop()
        s = w32.Win32LayeredSurface()
        if s.hwnd:
            self.engine.register_own_hwnd(s.hwnd)
        return s

    def _release_surface(self, surf: w32.Win32LayeredSurface) -> None:
        if len(self._surface_pool) < 4:
            surf.park()  # Park as 1x1 0-alpha pixel offscreen -> keeps DWM surface & DIB warm!
            self._surface_pool.append(surf)
        else:
            surf.destroy()

    def _compute_overscan(self, job: AnimationJob) -> tuple[int, int]:
        """Compute minimal physical overscan margin needed for 3D perspective / overshoot effects."""
        # Full-screen maximized windows shouldn't bloat past monitor size
        if job.w >= 1700 and job.h >= 960:
            return (0, 0)
        # 3D / shatter / bounce / slide effects benefit from subtle breathing room
        if job.shader_id in (3, 6, 7, 8, 10, 15, 29, 31, 33, 34, 35, 41, 42):
            return (min(120, job.w // 8), min(90, job.h // 8))
        return (min(48, job.w // 16), min(40, job.h // 16))

    def _render_and_present(self, item: _LiveAnim, progress: float, raw_t: float) -> None:
        job = item.job
        spec = self.engine.registry.get(job.effect_id)
        rect = (float(item.pad_x), float(item.pad_y), float(job.w), float(job.h))

        # Fast zero-copy path: glReadPixels(GL_BGRA) straight into bottom-up Win32 DIB (~2.3 ms @ 1540x860!)
        dib_ptr = item.surface.acquire_bottom_up_dib(item.surf_w, item.surf_h)
        if dib_ptr and self._gpu.render_to_dib_ptr(
            viewport_w=item.surf_w,
            viewport_h=item.surf_h,
            rect=rect,
            effect_id=job.shader_id,
            progress=progress,
            raw_t=raw_t,
            elapsed_time=self._elapsed,
            dib_ptr=dib_ptr,
            intensity=job.intensity,
            accent_hex=job.accent_hex,
            anchor=(job.anchor_x, job.anchor_y),
            texture=item.texture,
            clear_rgba=(0.0, 0.0, 0.0, 0.0),
            custom_frag_path=spec.custom_frag_path,
        ):
            item.surface.commit_premul_dib(item.surf_x, item.surf_y, item.surf_w, item.surf_h)
            return

        # Fallback path via QImage if glReadPixels is unavailable
        img = self._gpu.render_to_image(
            viewport_w=item.surf_w,
            viewport_h=item.surf_h,
            rect=rect,
            effect_id=job.shader_id,
            progress=progress,
            raw_t=raw_t,
            elapsed_time=self._elapsed,
            intensity=job.intensity,
            accent_hex=job.accent_hex,
            anchor=(job.anchor_x, job.anchor_y),
            texture=item.texture,
            clear_rgba=(0.0, 0.0, 0.0, 0.0),
            custom_frag_path=spec.custom_frag_path,
        )
        if img is None:
            return
        if img.format() != QtGui.QImage.Format_ARGB32_Premultiplied:
            img = img.convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
        ptr = img.constBits()
        item.surface.present_premul_bgra(item.surf_x, item.surf_y, img.width(), img.height(), ptr)

    def _retire_existing_for_hwnd(self, hwnd: int, incoming_hides: bool) -> None:
        """Retire any still-running animation for the same HWND cleanly when user rapidly toggles."""
        if not hwnd or not self._active:
            return
        kept: list[_LiveAnim] = []
        for ex in self._active:
            if ex.job.hwnd == hwnd:
                if ex.job.restore_hidden_hwnd and not incoming_hides:
                    self.engine.finish_hidden_handoff(ex.job.restore_hidden_hwnd, ex.job.restore_alpha)
                self._release_surface(ex.surface)
                self._gpu.destroy_texture(ex.texture)
            else:
                kept.append(ex)
        self._active = kept

    def _drain_jobs(self) -> None:
        if self.engine.animation_queue.empty():
            return
        while True:
            try:
                job = self.engine.animation_queue.get_nowait()
            except queue.Empty:
                break

            self._retire_existing_for_hwnd(job.hwnd, incoming_hides=bool(job.restore_hidden_hwnd))
            if self._wobble is not None and self._wobble.hwnd == job.hwnd:
                self._finish_wobble(instant_cancel=True)

            # ZERO-GAP ADOPTION: If pre-cover is ALREADY covering this minimizing/closing window,
            # adopt its live surface directly -> 0.000 ms black gap, zero second blit, zero flicker!
            instant_f0_shown = False
            if self._precover is not None and (not job.reverse) and self._precover.hwnd == job.hwnd:
                self._precover_timer.stop()
                surf = self._precover.surface
                self._precover = None
                instant_f0_shown = True
            else:
                if self._precover is not None:
                    self._on_precover_cancel(0)
                surf = self._acquire_surface()
                # INSTANT FRAME-0 BLIT FOR KEYBOARD/NON-PRECOVERED MINIMIZE / CLOSE (~2.5 ms, ZERO GL WAIT!)
                if (not job.reverse) and job.img_w == job.w and job.img_h == job.h:
                    instant_f0_shown = surf.present_opaque_bgra(
                        job.x, job.y, job.img_w, job.img_h, job.bgra_bytes
                    )

            tex = self._gpu.create_texture_from_bgra(job.img_w, job.img_h, job.bgra_bytes)
            if tex is None:
                self._release_surface(surf)
                if job.restore_hidden_hwnd:
                    self.engine.finish_hidden_handoff(job.restore_hidden_hwnd, job.restore_alpha)
                continue

            pad_x, pad_y = self._compute_overscan(job)
            surf_x = job.x - pad_x
            surf_y = job.y - pad_y
            surf_w = job.w + pad_x * 2
            surf_h = job.h + pad_y * 2

            # Cap oversized multi-monitor captures gracefully while preserving aspect ratio
            if surf_w > 2560 or surf_h > 1440:
                pad_x, pad_y = 0, 0
                surf_x, surf_y, surf_w, surf_h = job.x, job.y, job.w, job.h

            now = time.perf_counter()
            dur = max(0.08, job.duration_ms / 1000.0)
            if job.reverse:
                # Kick-start appearing animations slightly (~4%) so Frame 0 springs in immediately
                start_t = now - min(0.015, dur * 0.05)
            else:
                # Preserve original trigger timestamp so minimize/close starts moving without a flat pause
                start_t = job.started_at if (now - job.started_at) < 0.04 else now

            item = _LiveAnim(
                job=job,
                texture=tex,
                surface=surf,
                start_time=start_t,
                pad_x=pad_x,
                pad_y=pad_y,
                surf_x=surf_x,
                surf_y=surf_y,
                surf_w=surf_w,
                surf_h=surf_h,
            )
            if not instant_f0_shown:
                raw0 = min(0.15, max(0.0, (now - start_t) / dur))
                eased0 = eval_easing(raw0, job.easing)
                prog0 = (1.0 - eased0) if job.reverse else eased0
                raw_t0 = (1.0 - raw0) if job.reverse else raw0
                self._render_and_present(item, prog0, raw_t0)
            self._active.append(item)

        if self._active and not self._timer.isActive():
            self._last_tick = time.perf_counter()
            self._timer.start()

    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(0.05, now - self._last_tick)
        self._last_tick = now
        self._elapsed += dt

        alive: list[_LiveAnim] = []
        finished: list[_LiveAnim] = []
        for item in self._active:
            job = item.job
            dur = max(0.08, job.duration_ms / 1000.0)
            raw = min(1.0, max(0.0, (now - item.start_time) / dur))
            if raw >= 1.0:
                finished.append(item)
            else:
                eased = eval_easing(raw, job.easing)
                prog = (1.0 - eased) if job.reverse else eased
                raw_t = (1.0 - raw) if job.reverse else raw
                self._render_and_present(item, prog, raw_t)
                alive.append(item)

        self._active = alive

        # Zero-Flash Seamless Handoff for completed animations!
        for item in finished:
            job = item.job
            if job.restore_hidden_hwnd:
                # 1) Present pixel-identical final opaque frame (progress=0.0, zero distortion)
                self._render_and_present(item, 0.0, 0.0)
                # 2) Unhide real window directly BEHIND our opaque overlay & wait 1 exact DWM VSync!
                self.engine.finish_hidden_handoff(job.restore_hidden_hwnd, job.restore_alpha)
            # 3) Now hide overlay invisibly & release GPU texture!
            self._release_surface(item.surface)
            self._gpu.destroy_texture(item.texture)

        if self._wobble is not None:
            wob = self._wobble
            if not wob.settling:
                lx, ly, lw, lh = wob.x, wob.y, wob.w, wob.h
                have_pos = False
                if not self.engine.backend.is_demo:
                    if not w32.user32.IsWindow(wob.hwnd) or w32.user32.IsIconic(wob.hwnd):
                        self._finish_wobble(instant_cancel=True)
                        wob = None
                    else:
                        rx, ry, rw, rh = w32.get_extended_frame_bounds(wob.hwnd)
                        if rw > 0 and rh > 0 and rx > -10000:
                            lx, ly, lw, lh = rx, ry, rw, rh
                            have_pos = True
                if wob is not None and not have_pos:
                    live = self.engine._wobble_live_pos
                    if live is not None and live[0] == wob.hwnd:
                        _, lx, ly, lw, lh = live
                        have_pos = True

                if wob is not None and have_pos:
                    if lw != wob.w or lh != wob.h:
                        # If Windows Aero Snap just maximized the dragged window on release, keep
                        # our last normal surface frozen for < 2 ms so PreCover/Morph adopts it with 0 ms flash!
                        if (
                            not self.engine.backend.is_demo
                            and self.engine.config.physics.smooth_maximize_enabled
                            and w32.user32.IsZoomed(wob.hwnd)
                        ):
                            wob = None
                        else:
                            self._finish_wobble(instant_cancel=True)
                            wob = None
                    elif lx != wob.x or ly != wob.y:
                        dx = lx - wob.x
                        dy = ly - wob.y
                        wob.x = lx
                        wob.y = ly
                        wob.mesh.apply_drag_delta((dx / max(1.0, float(wob.w))) * 0.78, (dy / max(1.0, float(wob.h))) * 0.78)

            if wob is not None:
                wob.mesh.step(dt)
                target_fade = 0.0 if wob.settling else wob.drag_fade_target
                alpha_k = 1.0 - math.exp(-16.0 * dt)
                wob.cur_fade += (target_fade - wob.cur_fade) * alpha_k
                settle_age = (now - wob.settle_started_at) if wob.settling else 0.0
                if wob.settling and (
                    (wob.mesh.is_settled() and wob.cur_fade < 0.008)
                    or settle_age >= 0.80
                ):
                    self._finish_wobble(instant_cancel=False)
                else:
                    raw_off = wob.mesh.flat_offsets()
                    if wob.settling and settle_age > 0.45:
                        # Smoothly ease residual low-damping tail to 0 before handoff (0 px snap!)
                        tail = max(0.0, 1.0 - (settle_age - 0.45) / 0.35)
                        raw_off = [v * tail for v in raw_off]
                    offsets = self._soft_clamp_wobble(wob, raw_off)
                    self._render_wobble(wob, offsets, wob.cur_fade)

        # Advance 165 Hz Smooth Maximize / Restore-Down / Edge-Snap Morphs
        alive_m: list[_LiveMorph] = []
        done_m: list[_LiveMorph] = []
        for m in self._morphs:
            dur = max(0.08, m.job.duration_ms / 1000.0)
            raw = min(1.0, max(0.0, (now - m.start_time) / dur))
            if raw >= 1.0:
                done_m.append(m)
            else:
                eased = eval_easing(raw, "spring_smooth")
                self._render_morph(m, eased)
                alive_m.append(m)
        self._morphs = alive_m

        for m in done_m:
            # 1) Present pixel-identical final target frame (eased=1.0, blend_t=1.0)
            self._render_morph(m, 1.0)
            # 2) Unhide real window directly behind opaque overlay & wait 1 DWM VSync
            if m.job.restore_hidden_hwnd:
                self.engine.finish_hidden_handoff(m.job.restore_hidden_hwnd, m.job.restore_alpha)
            # 3) Park warm surface & free GPU textures
            self._release_surface(m.surface)
            self._gpu.destroy_texture(m.from_tex)
            if m.to_tex is not None:
                self._gpu.destroy_texture(m.to_tex)

        if not self._active and not self._morphs and self._wobble is None:
            self._timer.stop()

    def shutdown(self) -> None:
        if self.engine.on_animation_queued == self._on_engine_job_queued:
            self.engine.on_animation_queued = None
        if self.engine.on_morph_queued == self._on_engine_morph_queued:
            self.engine.on_morph_queued = None
        if self.engine.on_precover_show == self._on_engine_precover_show:
            self.engine.on_precover_show = None
        if self.engine.on_precover_cancel == self._on_engine_precover_cancel:
            self.engine.on_precover_cancel = None
        if self.engine.on_wobbly_start == self._on_engine_wobbly_start:
            self.engine.on_wobbly_start = None
        if self.engine.on_wobbly_end == self._on_engine_wobbly_end:
            self.engine.on_wobbly_end = None
        self._precover_timer.stop()
        self._poll_queue_timer.stop()
        self._timer.stop()
        if self._wobble is not None:
            self._finish_wobble(instant_cancel=True)
        for m in self._morphs:
            if m.job.restore_hidden_hwnd:
                self.engine.finish_hidden_handoff(m.job.restore_hidden_hwnd, m.job.restore_alpha)
            m.surface.destroy()
            self._gpu.destroy_texture(m.from_tex)
            if m.to_tex is not None:
                self._gpu.destroy_texture(m.to_tex)
        self._morphs.clear()
        if self._precover is not None:
            self._precover.surface.destroy()
            self._precover = None
        for item in self._active:
            if item.job.restore_hidden_hwnd:
                self.engine.finish_hidden_handoff(item.job.restore_hidden_hwnd, item.job.restore_alpha)
            item.surface.destroy()
            self._gpu.destroy_texture(item.texture)
        self._active.clear()
        for s in self._surface_pool:
            s.destroy()
        self._surface_pool.clear()


class SpotlightOverlay(QtWidgets.QWidget):
    """Focus Spotlight / Inactive Dimmer positioned in Z-order directly behind the active window."""

    def __init__(self, engine: PrismEngine) -> None:
        super().__init__(None)
        self.engine = engine
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.Tool
            | QtCore.Qt.WindowTransparentForInput
            | QtCore.Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self._state = SpotlightState()

    def apply_state(self, state: SpotlightState) -> None:
        self._state = state
        if not state.active:
            if self.isVisible():
                self.hide()
            return
        mons = self.engine.backend.list_monitors()
        min_x = min(m.left for m in mons)
        min_y = min(m.top for m in mons)
        max_x = max(m.right for m in mons)
        max_y = max(m.bottom for m in mons)
        w, h = max(320, max_x - min_x), max(240, max_y - min_y)
        self.setGeometry(min_x, min_y, w, h)
        if not self.isVisible():
            self.show()
        hwnd = int(self.winId())
        self.engine.register_own_hwnd(hwnd)
        w32.set_click_through_overlay(hwnd, True)
        if state.active_hwnd and not self.engine.backend.is_demo:
            w32.place_behind_window(hwnd, state.active_hwnd, min_x, min_y, w, h)
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        if not self._state.active:
            return
        p = QtGui.QPainter(self)
        col = QtGui.QColor(self._state.color_hex)
        alpha = max(10, min(230, int(round(self._state.opacity * 255))))
        col.setAlpha(alpha)

        if self._state.mode == "flat":
            p.fillRect(self.rect(), col)
        else:
            if self._state.mode == "cursor":
                cx = self._state.cursor_pos[0] - self.x()
                cy = self._state.cursor_pos[1] - self.y()
                radius = 440.0
            else:
                ax, ay, aw, ah = self._state.active_rect
                cx = (ax - self.x()) + aw * 0.5
                cy = (ay - self.y()) + ah * 0.5
                radius = max(aw, ah) * 0.85 + 140.0
            grad = QtGui.QRadialGradient(cx, cy, max(120.0, radius))
            inner = QtGui.QColor(col)
            inner.setAlpha(int(alpha * 0.15))
            grad.setColorAt(0.0, inner)
            grad.setColorAt(0.65, QtGui.QColor(col.red(), col.green(), col.blue(), int(alpha * 0.85)))
            grad.setColorAt(1.0, col)
            p.fillRect(self.rect(), grad)
        p.end()


class FloatingTileWindow(QtWidgets.QWidget):
    """Floating Mini-Preview Window Tile (modernized WindowFX Window Tiles feature)."""

    restored = QtCore.Signal(int)

    def __init__(self, info: WindowInfo, bgra: Optional[tuple[int, int, bytes]], size_px: int, topmost: bool) -> None:
        super().__init__(None)
        self.target_hwnd = info.hwnd
        flags = QtCore.Qt.FramelessWindowHint | QtCore.Qt.Tool
        if topmost:
            flags |= QtCore.Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setFixedSize(size_px, int(size_px * 0.72) + 26)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self._title = info.title[:28]
        self._exe = info.exe_name
        self._pix: Optional[QtGui.QPixmap] = None
        if bgra:
            img = QtGui.QImage(bgra[2], bgra[0], bgra[1], bgra[0] * 4, QtGui.QImage.Format_RGB32).copy()
            self._pix = QtGui.QPixmap.fromImage(img).scaled(
                size_px - 4, int(size_px * 0.72) - 4,
                QtCore.Qt.KeepAspectRatioByExpanding, QtCore.Qt.SmoothTransformation,
            )
        self._drag_origin: Optional[QtCore.QPoint] = None

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#17181C"))
        p.setPen(QtGui.QPen(QtGui.QColor("#2D81FF"), 1))
        p.drawRect(0, 0, self.width() - 1, self.height() - 1)
        if self._pix:
            p.drawPixmap(2, 2, self.width() - 4, self.height() - 28, self._pix)
        p.fillRect(1, self.height() - 25, self.width() - 2, 24, QtGui.QColor("#0B0C0E"))
        p.setPen(QtGui.QColor("#F2F1EE"))
        p.setFont(QtGui.QFont("Segoe UI", 8))
        p.drawText(6, self.height() - 8, self._title or self._exe)
        p.end()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif event.button() == QtCore.Qt.RightButton:
            self.restored.emit(self.target_hwnd)
            self.close()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._drag_origin is not None:
            self.move(event.globalPosition().toPoint() - self._drag_origin)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        self.restored.emit(self.target_hwnd)
        self.close()


class QuickControlPopup(QtWidgets.QWidget):
    """Instant Quick Window Control Bar (Pin, Opacity, Roll-Up, Tile, Snap, Live FX Test)."""

    def __init__(self, engine: PrismEngine, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._target_hwnd: int = 0
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool)
        self.setFixedWidth(360)
        self.setStyleSheet(
            "QWidget { background: #121316; color: #F2F1EE; font-family: 'Segoe UI'; }"
            "QPushButton { background: #17181C; border: 1px solid #2A2D36; padding: 6px 10px; color: #F2F1EE; }"
            "QPushButton:hover { border-color: #2D81FF; background: #1E2430; }"
            "QLabel { background: transparent; }"
        )
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(8)

        hdr = QtWidgets.QHBoxLayout()
        self._lbl_title = QtWidgets.QLabel("QUICK WINDOW CONTROLS")
        self._lbl_title.setStyleSheet("color: #74AAFF; font-weight: bold; font-size: 11px;")
        btn_x = QtWidgets.QPushButton("✕")
        btn_x.setFixedSize(26, 24)
        btn_x.clicked.connect(self.hide)
        hdr.addWidget(self._lbl_title, 1)
        hdr.addWidget(btn_x)
        root.addLayout(hdr)

        self._lbl_meta = QtWidgets.QLabel("No active target window")
        self._lbl_meta.setStyleSheet("color: #9CA3AF; font-size: 11px;")
        root.addWidget(self._lbl_meta)

        row1 = QtWidgets.QHBoxLayout()
        for label, cb in [
            ("Pin Top", self._on_pin),
            ("Roll-Up", self._on_rollup),
            ("Mini Tile", self._on_tile),
            ("Test FX", self._on_test_fx),
        ]:
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(cb)
            row1.addWidget(b)
        root.addLayout(row1)

        op_row = QtWidgets.QHBoxLayout()
        op_row.addWidget(QtWidgets.QLabel("Opacity"))
        self._sl_op = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._sl_op.setRange(25, 100)
        self._sl_op.setValue(100)
        self._sl_op.valueChanged.connect(self._on_opacity)
        self._lbl_op = QtWidgets.QLabel("100%")
        op_row.addWidget(self._sl_op, 1)
        op_row.addWidget(self._lbl_op)
        root.addLayout(op_row)

        snap_lbl = QtWidgets.QLabel("SMART SNAP ZONES")
        snap_lbl.setStyleSheet("color: #74AAFF; font-size: 10px; font-weight: bold;")
        root.addWidget(snap_lbl)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(4)
        zones = [
            ("L 50%", "left_50"), ("R 50%", "right_50"), ("L 66%", "left_66"), ("R 33%", "right_33"),
            ("Center", "center_50"), ("Top", "top_50"), ("Bottom", "bottom_50"), ("Max", "maximize"),
        ]
        for idx, (lbl, z) in enumerate(zones):
            b = QtWidgets.QPushButton(lbl)
            b.clicked.connect(lambda _=False, zone=z: self._on_snap(zone))
            grid.addWidget(b, idx // 4, idx % 4)
        root.addLayout(grid)

    def show_for_window(self, hwnd: int) -> None:
        info = self.engine.backend.get_window_info(hwnd)
        if not info:
            return
        self._target_hwnd = hwnd
        self._lbl_title.setText(f"QUICK CONTROL // {info.classification.friendly_name.upper()}")
        self._lbl_meta.setText(f"{info.exe_name} (PID {info.pid}) • {info.classification.fx_safety}")
        pct = max(25, min(100, int(round(info.opacity / 2.55))))
        self._sl_op.blockSignals(True)
        self._sl_op.setValue(pct)
        self._sl_op.blockSignals(False)
        self._lbl_op.setText(f"{pct}%")
        cx, cy = w32.get_cursor_pos()
        self.move(max(20, cx - 180), max(20, cy - 40))
        self.show()
        self.raise_()
        self.engine.register_own_hwnd(int(self.winId()))

    def _on_pin(self) -> None:
        if self._target_hwnd:
            self.engine.toggle_pin_window(self._target_hwnd)

    def _on_rollup(self) -> None:
        if self._target_hwnd:
            self.engine.toggle_rollup_window(self._target_hwnd)

    def _on_tile(self) -> None:
        if self._target_hwnd:
            self.engine.minimize_to_tile(self._target_hwnd)
            self.hide()

    def _on_test_fx(self) -> None:
        if self._target_hwnd:
            self.engine.trigger_effect_for_window(self._target_hwnd, "preview")

    def _on_opacity(self, val: int) -> None:
        self._lbl_op.setText(f"{val}%")
        if self._target_hwnd:
            self.engine.set_custom_opacity(self._target_hwnd, val)

    def _on_snap(self, zone: str) -> None:
        if self._target_hwnd:
            self.engine.snap_window_to_zone(self._target_hwnd, zone)
            self.hide()
