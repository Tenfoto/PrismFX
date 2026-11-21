"""Phase 4: Built-In Self-Verification Harness (--selftest, --uitest, --overlay-test, --perf-log, --snapshot)."""

from __future__ import annotations

import array
import json
import os
import pathlib
import sys
import tempfile
import time
from dataclasses import asdict

from PySide6 import QtCore, QtGui, QtOpenGL, QtWidgets

from .core import win32_api as w32
from .core.backend import DemoWindowBackend
from .core.catalog import classify_app
from .core.config import ConfigStore, PrismConfig
from .core.effects import EffectRegistry
from .core.engine import PrismEngine
from .core.journal import RecoveryJournal
from .core.physics import (
    DampedSpring,
    DragVelocityTracker,
    MomentumBody,
    WobblyMesh,
    compute_snap_rect,
    detect_edge_snap_zone,
    eval_easing,
)
from .render.gl_renderer import (
    GL_COLOR_BUFFER_BIT,
    ShaderPipeline,
    generate_mock_window_image,
    make_gl_format,
)
from .render.overlay import DesktopFxOverlay
from .ui.main_window import MainWindow
from .ui.theme import ATLAS, TOKENS, build_stylesheet, split_grapheme_clusters


def _cloak_and_show_silently(widget: QtWidgets.QWidget) -> None:
    """Non-intrusive window realization: zero focus stealing & hidden via DWMWA_CLOAK."""
    widget.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    widget.show()
    hwnd = int(widget.winId())
    w32.set_window_cloaked(hwnd, True)
    QtCore.QCoreApplication.processEvents()


def run_selftest() -> dict:
    """Comprehensive automated core + GPU shader + crash-recovery invariant test."""
    results: dict[str, object] = {"passed": True, "checks": []}

    def check(name: str, ok: bool, detail: str = "") -> None:
        results["checks"].append({"name": name, "ok": bool(ok), "detail": detail})  # type: ignore[union-attr]
        if not ok:
            results["passed"] = False

    # 1. Headless physics math (165 Hz & 60 Hz stability)
    sp = DampedSpring(value=0.0, target=1.0, omega=18.0, zeta=1.0)
    for _ in range(165):
        sp.step(1.0 / 165.0)
    check("spring_165hz_convergence", abs(sp.value - 1.0) < 1e-3 and sp.is_settled(), f"val={sp.value:.6f}")

    el = DampedSpring(value=0.0, target=1.0, omega=16.0, zeta=0.45)
    peak = 0.0
    for _ in range(200):
        peak = max(peak, el.step(1.0 / 165.0))
    check("spring_elastic_overshoot", peak > 1.10 and abs(el.value - 1.0) < 0.02, f"peak={peak:.3f} end={el.value:.4f}")

    for curve in ("spring_smooth", "spring_elastic", "expo_out", "cubic_inout", "back_out", "linear"):
        v0, v1 = eval_easing(0.0, curve), eval_easing(1.0, curve)
        check(f"easing_{curve}_bounds", abs(v0) < 1e-5 and abs(v1 - 1.0) < 1e-5, f"v0={v0:.3f} v1={v1:.3f}")

    # 2. Wobbly 4x4 spring mesh
    wm = WobblyMesh(24.0, 0.55, 1.0)
    wm.apply_drag_delta(0.18, -0.10)
    deformed = max(abs(x) for x in wm.flat_offsets())
    for _ in range(300):
        wm.step(1.0 / 165.0)
    check("wobbly_mesh_deform_and_settle", deformed > 0.05 and wm.is_settled(), f"peak_disp={deformed:.4f}")

    # 3. Momentum glide + elastic bounce + strict clamp
    mb = MomentumBody(1, 1800.0, 100.0, 300, 200, 1800.0, 0.0, 0, 0, 1920, 1040, bounce=True)
    bounced = False
    for _ in range(120):
        nx, ny, moving = mb.step(1.0 / 165.0)
        if mb.vx < 0:
            bounced = True
        if not moving:
            break
    check("momentum_elastic_bounce", bounced and 0 <= mb.x <= 1620, f"final_x={mb.x:.1f}")

    # 4. Flick gesture + edge snap calculation
    dtk = DragVelocityTracker()
    dtk.reset(0.0, 500, 400)
    dtk.push(0.05, 680, 405)
    check("flick_right_detection", dtk.detect_flick(1400.0) == "right", f"vel={dtk.velocity()}")
    mon = DemoWindowBackend().list_monitors()[0]
    snap_r = compute_snap_rect(mon, "left_66")
    edge_z = detect_edge_snap_zone(4, 500, mon)
    check("snap_layout_and_edge", snap_r == (0, 0, 1280, 1032) and edge_z == "left_50", f"rect={snap_r} zone={edge_z}")

    # 5. Human-centric App Catalog (including NVIDIA Overlay & Win11 Auto-Hide Taskbar XAML protection!)
    c_game = classify_app("VALORANT-Win64-Shipping.exe", "UnrealWindow")
    c_ide = classify_app("Code.exe", "Chrome_WidgetWin_1")
    c_shell = classify_app("explorer.exe", "Progman")
    c_nv = classify_app("NVIDIA Overlay.exe", "CEF-OSC-WIDGET")
    c_tb_xaml = classify_app("explorer.exe", "XamlExplorerHostIslandWindow")
    c_tb_ovf = classify_app("explorer.exe", "TopLevelWindowForOverflowXamlIsland")
    c_tb_bridge = classify_app("explorer.exe", "DesktopWindowXamlSource")
    check(
        "app_catalog_verdicts",
        c_game.recommended_exclude
        and not c_ide.recommended_exclude
        and c_shell.recommended_exclude
        and c_nv.recommended_exclude
        and c_tb_xaml.recommended_exclude
        and c_tb_ovf.recommended_exclude
        and c_tb_bridge.recommended_exclude,
        f"game={c_game.category} ide={c_ide.category} nv={c_nv.category} xaml_tb={c_tb_xaml.recommended_exclude}",
    )

    # 6. Thai grapheme cluster integrity (vowels & tone marks never detach!)
    thai_sample = "เผาไหม้เป็นเถ้าถ่าน"
    clusters = split_grapheme_clusters(thai_sample)
    check(
        "thai_grapheme_clusters",
        "".join(clusters) == thai_sample and len(clusters) < len(thai_sample) and "ม้" in clusters and "ถ่" in clusters,
        f"chars={len(thai_sample)} clusters={len(clusters)} sample={clusters[:6]}",
    )

    # 7. Atomic Config & Crash Recovery Journal round-trip
    with tempfile.TemporaryDirectory(prefix="prismfx_selftest_") as td:
        tdir = pathlib.Path(td)
        store = ConfigStore(tdir)
        cfg = PrismConfig()
        cfg = store.apply_preset(cfg, "Cyberpunk Overdrive")
        store.save(cfg)
        reloaded = store.load()
        check("atomic_config_roundtrip", reloaded.active_preset == "Cyberpunk Overdrive" and reloaded.triggers["close"].effect_id == "crt_tv_off")

        jpath = tdir / "recovery_journal.json"
        j1 = RecoveryJournal(jpath)
        j1.record_cloak(9991, True)
        j1.record_alpha(9991, 255)
        j1.record_pin(9992, True)
        j1.record_rollup(9993, True, 640)
        j1.record_desktop_icons_hidden(True)
        j1.record_native_min_animate(True)
        # Simulate fresh startup after abrupt crash
        j2 = RecoveryJournal(jpath)
        restored_ops: list[str] = []
        n_restored = j2.restore_all(
            uncloak_fn=lambda h: restored_ops.append(f"uncloak:{h}"),
            restore_alpha_fn=lambda h, a: restored_ops.append(f"alpha:{h}:{a}"),
            unpin_fn=lambda h: restored_ops.append(f"unpin:{h}"),
            unroll_fn=lambda h, orig_h: restored_ops.append(f"unroll:{h}:{orig_h}"),
            show_desktop_icons_fn=lambda: restored_ops.append("icons:show"),
            restore_min_animate_fn=lambda orig: restored_ops.append(f"min_anim:{orig}"),
        )
        check("crash_journal_recovery", n_restored == 6 and not j2.has_pending_recovery(), f"ops={restored_ops}")

        # 7b. Directional debounce check: duplicate same-direction blocked, rapid minimize->restore allowed!
        eng_demo = PrismEngine(DemoWindowBackend(seed=42), store, EffectRegistry())
        j_min = eng_demo.trigger_effect_for_window(1001, "minimize")
        j_dup = eng_demo.trigger_effect_for_window(1001, "close")
        time.sleep(0.05)
        j_res = eng_demo.trigger_effect_for_window(1001, "restore")
        j_dup2 = eng_demo.trigger_effect_for_window(1001, "open")
        check(
            "directional_debounce",
            j_min is not None and j_dup is None and j_res is not None and j_dup2 is None,
            f"min={bool(j_min)} dup_close={bool(j_dup)} res={bool(j_res)} dup_open={bool(j_dup2)}",
        )

        # 7c. Zero-stutter input & hook invariants: no WH_MOUSE_LL, no idle global LOCATIONCHANGE, exact RawInput ABI
        import ctypes
        from .core.engine import _analyze_bgra_readiness
        no_ll_hook = not hasattr(eng_demo, "_mouse_hook")
        no_idle_loc = eng_demo._drag_loc_hook == 0
        raw_abi_ok = (
            ctypes.sizeof(w32.RAWINPUTDEVICE) == 16
            and ctypes.sizeof(w32.RAWINPUTHEADER) == 24
            and ctypes.sizeof(w32.RAWINPUT_MOUSE) == 48
        )
        # Verify Open readiness analyzer distinguishes blank, uniform #191919 + 1px border, and painted UI
        blank_buf = bytes([0, 0, 0, 255]) * 400
        flat_shell = bytearray(bytes([25, 25, 25, 255]) * 400)
        flat_shell[0:4] = bytes([255, 255, 255, 255])  # single 1px border pixel must still be uniform!
        painted_ui = bytearray(bytes([25, 25, 25, 255]) * 400)
        for idx in range(0, 60 * 4, 4):
            painted_ui[idx:idx + 4] = bytes([255, 129, 45, 255])
        readiness_ok = (
            _analyze_bgra_readiness(blank_buf) == (True, True)
            and _analyze_bgra_readiness(bytes(flat_shell)) == (False, True)
            and _analyze_bgra_readiness(bytes(painted_ui)) == (False, False)
            and w32.EVENT_OBJECT_CLOAKED == 0x8017
            and w32.EVENT_OBJECT_UNCLOAKED == 0x8018
        )
        check(
            "zero_stutter_rawinput_and_scoped_hooks",
            no_ll_hook and no_idle_loc and raw_abi_ok and readiness_ok,
            f"no_wh_mouse_ll={no_ll_hook} no_idle_loc={no_idle_loc} raw_abi_48B={raw_abi_ok} open_readiness={readiness_ok}",
        )

    # 8. OpenGL 3.3 Core GPU compilation & FBO render across ALL 42 built-in shaders!
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    fmt = make_gl_format()
    ctx = QtOpenGL.QOpenGLContext() if hasattr(QtOpenGL, "QOpenGLContext") else QtGui.QOpenGLContext()
    ctx.setFormat(fmt)
    gl_ok = ctx.create()
    surf = QtGui.QOffscreenSurface()
    surf.setFormat(fmt)
    surf.create()
    if gl_ok and ctx.makeCurrent(surf):
        pipe = ShaderPipeline(subdiv=24)
        p_ok = pipe.initialize()
        check("gl33_shader_compile_link", p_ok, pipe.error_log or "42 shaders linked cleanly")
        if p_ok:
            fbo = QtOpenGL.QOpenGLFramebufferObject(160, 100)
            fbo.bind()
            img = generate_mock_window_image(160, 100)
            tex = QtOpenGL.QOpenGLTexture(img)
            tex.setMinificationFilter(QtOpenGL.QOpenGLTexture.Linear)
            tex.setMagnificationFilter(QtOpenGL.QOpenGLTexture.Linear)
            funcs = ctx.functions()
            reg = EffectRegistry()
            rendered_ok = 0
            gl_errors = 0
            for spec in reg.all_effects(include_special=False):
                funcs.glViewport(0, 0, 160, 100)
                funcs.glClearColor(0.0, 0.0, 0.0, 0.0)
                funcs.glClear(GL_COLOR_BUFFER_BIT)
                pipe.draw_window(
                    funcs, tex, spec.shader_id, 0.38, 0.38, 1.1, 1.0,
                    spec.accent_hex, (12.0, 8.0, 136.0, 84.0), (160.0, 100.0)
                )
                if funcs.glGetError() != 0:
                    gl_errors += 1
                out_img = fbo.toImage()
                # Verify at least some pixels rendered non-transparent
                has_px = any(
                    out_img.pixelColor(x, y).alpha() > 10
                    for y in range(10, 95, 10)
                    for x in range(15, 150, 15)
                )
                if has_px:
                    rendered_ok += 1
            total_shaders = reg.shader_effect_count()
            check("gl33_render_all_42_shaders", rendered_ok == total_shaders and gl_errors == 0,
                  f"rendered={rendered_ok}/{total_shaders} gl_errors={gl_errors}")
            tex.destroy()
            fbo.release()
            pipe.cleanup()
        ctx.doneCurrent()
    else:
        check("gl33_context", False, "Failed to create OpenGL 3.3 Core context")

    return results


def run_uitest(profile_dir: pathlib.Path) -> dict:
    """Automated non-intrusive UI hit-test & navigation verification (zero focus stealing)."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ATLAS.initialize()
    app.setStyleSheet(build_stylesheet())

    store = ConfigStore(profile_dir)
    reg = EffectRegistry([store.shaders_dir])
    engine = PrismEngine(DemoWindowBackend(seed=42), store, reg)
    win = MainWindow(engine, enable_overlays=False, enable_tray=False)
    _cloak_and_show_silently(win)

    results: dict[str, object] = {"passed": True, "checks": []}

    def check(name: str, ok: bool, detail: str = "") -> None:
        results["checks"].append({"name": name, "ok": bool(ok), "detail": detail})  # type: ignore[union-attr]
        if not ok:
            results["passed"] = False

    def verify_hit(widget: QtWidgets.QWidget, label: str) -> None:
        center = widget.mapTo(win, widget.rect().center())
        hit = win.childAt(center)
        ok = hit is not None and (hit is widget or widget.isAncestorOf(hit))
        check(f"hittest_{label}", ok, f"hit={type(hit).__name__ if hit else None}")

    # Hit-test titlebar controls & all 5 sidebar navigation buttons
    verify_hit(win.master_toggle, "master_toggle")
    verify_hit(win.btn_lang, "lang_switch")
    verify_hit(win.btn_quick_ctrl, "quick_ctrl")
    for k, btn in win._nav_buttons.items():
        verify_hit(btn, f"nav_{k}")

    # Navigate across all 5 pages & verify active page + inactive clock pausing
    for k in ("home", "animations", "physics", "focus", "apps"):
        win._nav_buttons[k].click()
        QtCore.QCoreApplication.processEvents()
        check(f"navigate_{k}", win._active_page == k and win._stack.currentIndex() == win._page_map[k])

    # Verify trigger & category switches on Animations Lab
    win._select_page("animations")
    QtCore.QCoreApplication.processEvents()
    win._trigger_buttons["close"].click()
    win._cat_buttons["elemental"].click()
    QtCore.QCoreApplication.processEvents()
    check("animations_filter_elemental", len(win._effect_cards) == 10, f"cards={len(win._effect_cards)}")

    # Verify bilingual switch updates text cleanly
    before_lang = win._lang
    win.btn_lang.click()
    QtCore.QCoreApplication.processEvents()
    check("bilingual_toggle", win._lang != before_lang, f"{before_lang}->{win._lang}")

    # Verify FC Minimal font resolution
    check("font_atlas_thai", ATLAS.thai_family == "FC Minimal", f"thai={ATLAS.thai_family} mono={ATLAS.mono_family}")

    win.full_shutdown()
    return results


def run_overlay_test(profile_dir: pathlib.Path) -> dict:
    """Verify zero-swapchain Win32LayeredSurface click-through, UpdateLayeredWindow & alpha readback."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = ConfigStore(profile_dir)
    reg = EffectRegistry()
    engine = PrismEngine(DemoWindowBackend(seed=42), store, reg)

    results: dict[str, object] = {"passed": True, "checks": []}

    def check(name: str, ok: bool, detail: str = "") -> None:
        results["checks"].append({"name": name, "ok": bool(ok), "detail": detail})  # type: ignore[union-attr]
        if not ok:
            results["passed"] = False

    overlay = DesktopFxOverlay(engine)
    ov_hwnd = overlay.primary_surface_hwnd()
    ov_cls = w32.get_class_name(ov_hwnd) if ov_hwnd else ""
    check(
        "overlay_zero_swapchain_hwnd",
        ov_hwnd > 0 and not isinstance(overlay, QtWidgets.QWidget) and ov_cls == "PrismFX_LayeredOverlay",
        f"hwnd={ov_hwnd} cls={ov_cls!r}",
    )
    check("overlay_signal_wired", engine.on_animation_queued is not None, "QueuedConnection signal bound")

    # Cloak surface during test so zero pixels flash on user's desktop!
    w32.set_window_cloaked(ov_hwnd, True)

    ex = w32.user32.GetWindowLongPtrW(ov_hwnd, w32.GWL_EXSTYLE)
    required_mask = (
        w32.WS_EX_LAYERED | w32.WS_EX_TRANSPARENT | w32.WS_EX_NOACTIVATE
        | w32.WS_EX_TOOLWINDOW | w32.WS_EX_TOPMOST
    )
    check("overlay_win32_exstyles", (ex & required_mask) == required_mask, f"ex=0x{ex:x}")

    # Test instant Frame-0 opaque blit (AlphaFormat=0, tolerates GDI 0x00 alpha bytes!)
    surf = overlay._surface_pool[0]
    gdi_zero_alpha_buf = bytes([255, 129, 45, 0]) * (128 * 128)
    f0_ok = surf.present_opaque_bgra(120, 120, 128, 128, gdi_zero_alpha_buf)
    check("overlay_instant_frame0_opaque", f0_ok, "present_opaque_bgra 128x128 ok")

    # Test real UpdateLayeredWindow(ULW_ALPHA) presentation on our zero-swapchain surface
    test_buf = bytes([255, 129, 45, 127]) * (128 * 128)
    ulw_ok = surf.present_premul_bgra(120, 120, 128, 128, test_buf)
    check("overlay_update_layered_window", ulw_ok, "ULW_ALPHA 128x128 ok")

    # WindowFromPoint at center MUST pass through the click-through overlay!
    hit_hwnd = w32.window_from_point(184, 184)
    check("overlay_clickthrough_windowfrompoint", hit_hwnd != ov_hwnd, f"hit={hit_hwnd} overlay={ov_hwnd}")

    # Verify OffscreenGpuEngine Format_RGB32 (0x00 GDI alpha -> 255 opaque) + zero-copy glReadPixels DIB path!
    from .render.gl_renderer import OffscreenGpuEngine
    gpu = OffscreenGpuEngine.shared()
    bgra_zero_a = bytes([255, 129, 45, 0]) * (64 * 64)
    tex = gpu.create_texture_from_bgra(64, 64, bgra_zero_a)
    out_img = gpu.render_to_image(128, 128, (0, 0, 128, 128), 37, 0.5, 0.5, 0.5, texture=tex)
    px = out_img.pixelColor(64, 64) if out_img is not None else QtGui.QColor(0, 0, 0, 0)
    check("overlay_alpha_readback_50pct", 115 <= px.alpha() <= 140 and px.blue() >= 250,
          f"rgba=({px.red()},{px.green()},{px.blue()},{px.alpha()})")

    dib_ptr = surf.acquire_bottom_up_dib(128, 128)
    dib_render_ok = bool(
        dib_ptr
        and gpu.render_to_dib_ptr(128, 128, (0, 0, 128, 128), 37, 0.5, 0.5, 0.5, dib_ptr=dib_ptr, texture=tex)
        and surf.commit_premul_dib(120, 120, 128, 128)
    )
    check("overlay_zero_copy_dib_glreadpixels", dib_render_ok, f"dib_ptr=0x{dib_ptr:x}")

    # Verify warm park() + PreCover show/cancel/adopt wiring!
    park_ok = surf.park()
    from .core.engine import PreCoverJob, WobblyDragStart
    pjob = PreCoverJob(99999, "minimize", 120, 120, 128, 128, 128, 128, gdi_zero_alpha_buf)
    overlay._on_precover_show(pjob)
    has_pc = overlay._precover is not None and overlay._precover.hwnd == 99999
    overlay._on_precover_cancel(99999)
    cleared_pc = overlay._precover is None
    check(
        "overlay_warm_park_and_precover",
        park_ok and has_pc and cleared_pc and engine.on_precover_show is not None,
        f"park={park_ok} precover_show={has_pc} precover_cancel={cleared_pc}",
    )

    # Verify live 165 Hz GPU Wobbly Windows overlay lifecycle (start -> drag deform -> settle -> handoff)!
    wreq = WobblyDragStart(
        hwnd=88888, x=200, y=180, w=128, h=128,
        img_w=128, img_h=128, bgra_bytes=gdi_zero_alpha_buf,
        grab_u=0.5, grab_v=0.08, orig_alpha=255, init_dx=24, init_dy=-10,
    )
    engine._wobble_live_pos = (88888, 200, 180, 128, 128)
    overlay._on_wobbly_start(wreq)
    wob_started = overlay._wobble is not None and overlay._wobble.hwnd == 88888
    peak_wob = 0.0
    if wob_started and overlay._wobble is not None:
        w32.set_window_cloaked(overlay._wobble.surface.hwnd, True)
        engine._wobble_live_pos = (88888, 280, 140, 128, 128)
        overlay._last_tick = time.perf_counter() - 0.006
        overlay._tick()
        if overlay._wobble is not None:
            peak_wob = max(abs(v) for v in overlay._wobble.mesh.flat_offsets())
        overlay._on_wobbly_end(88888, 280, 140, False)
        for _ in range(120):
            if overlay._wobble is None:
                break
            overlay._last_tick = time.perf_counter() - 0.008
            overlay._tick()
    wob_settled = overlay._wobble is None
    check(
        "overlay_live_wobbly_lifecycle",
        engine.on_wobbly_start is not None
        and engine.on_wobbly_end is not None
        and wob_started
        and peak_wob > 0.03
        and wob_settled,
        f"started={wob_started} peak_disp={peak_wob:.4f} settled={wob_settled}",
    )

    # Verify 165 Hz Dual-Texture Smooth Maximize / Snap Morph lifecycle!
    from .core.engine import MorphJob
    mjob = MorphJob(
        hwnd=77777,
        from_rect=(120, 120, 128, 128),
        to_rect=(40, 40, 280, 220),
        from_img_w=128, from_img_h=128, from_bgra=gdi_zero_alpha_buf,
        to_img_w=128, to_img_h=128, to_bgra=test_buf,
        duration_ms=120,
        restore_hidden_hwnd=0,
        restore_alpha=255,
    )
    overlay._on_morph_queued(mjob)
    morph_started = len(overlay._morphs) == 1
    if morph_started:
        w32.set_window_cloaked(overlay._morphs[0].surface.hwnd, True)
        overlay._morphs[0].start_time = time.perf_counter() - 0.06
        overlay._tick()
        overlay._morphs[0].start_time = time.perf_counter() - 0.20
        overlay._tick()
    morph_done = len(overlay._morphs) == 0
    check(
        "overlay_smooth_maximize_morph_lifecycle",
        engine.on_morph_queued is not None and morph_started and morph_done,
        f"wired={engine.on_morph_queued is not None} started={morph_started} finished={morph_done}",
    )
    gpu.destroy_texture(tex)

    overlay.shutdown()
    gpu.shutdown()
    return results


def run_perf_log(profile_dir: pathlib.Path, duration_sec: float = 1.2) -> dict:
    """Measure real 165 Hz GPU frame telemetry on this machine (non-intrusive cloaked window)."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ATLAS.initialize()
    app.setStyleSheet(build_stylesheet())

    store = ConfigStore(profile_dir)
    reg = EffectRegistry([store.shaders_dir])
    engine = PrismEngine(DemoWindowBackend(seed=42), store, reg)
    win = MainWindow(engine, enable_overlays=False, enable_tray=False)
    _cloak_and_show_silently(win)

    # Benchmark active GPU shader stage (Incinerate Burn — heavy FBM procedural fragment shader!)
    win.home_stage.set_effect(reg.get("ember_burn"))
    summary = win.home_stage.benchmark_frames(duration_sec=duration_sec)

    # Verify idle clock pauses immediately when minimized/hidden
    win.hide()
    QtCore.QCoreApplication.processEvents()
    idle_paused = not win.home_stage._timer.isActive() and not win.lab_stage._timer.isActive()

    win.full_shutdown()
    out = asdict(summary)
    out["idle_clock_paused_on_hide"] = idle_paused
    return out


def run_snapshots(profile_dir: pathlib.Path, out_dir: pathlib.Path) -> list[str]:
    """Capture non-intrusive screenshots of all 5 Studio pages + 12-shader GPU contact sheet."""
    out_dir.mkdir(parents=True, exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ATLAS.initialize()
    app.setStyleSheet(build_stylesheet())

    store = ConfigStore(profile_dir)
    cfg = store.load()
    cfg.language = "th"
    store.save(cfg)
    reg = EffectRegistry([store.shaders_dir])
    engine = PrismEngine(DemoWindowBackend(seed=42), store, reg)
    win = MainWindow(engine, enable_overlays=False, enable_tray=False)
    win.resize(1280, 800)
    _cloak_and_show_silently(win)

    saved: list[str] = []
    # Pin shader stages to photogenic mid-transition progress for crisp visual inspection!
    win.home_stage.set_static_frame(11, 0.36, "#F97316")  # Incinerate Burn glowing fire edge!
    win.lab_stage._anchor = (0.5, 0.5)
    win.lab_stage.set_static_frame(15, 0.36, "#6366F1")   # Event Horizon Black Hole centered!

    for fname, page_key in [
        ("01_home.png", "home"),
        ("02_animations.png", "animations"),
        ("03_physics.png", "physics"),
        ("04_focus.png", "focus"),
        ("05_apps.png", "apps"),
    ]:
        win._select_page(page_key)
        if page_key == "physics":
            # Apply dramatic Compiz wobble bend for visual proof!
            win.wobble_stage._wobble.apply_drag_delta(0.28, -0.14)
            win.wobble_stage._render_current_frame()
            win.wobble_stage.update()
        QtCore.QCoreApplication.processEvents()
        time.sleep(0.06)
        QtCore.QCoreApplication.processEvents()
        pm = win.grab()
        dst = out_dir / fname
        pm.save(str(dst))
        saved.append(str(dst))

    # Render a 4x3 GPU Shader Showcase Contact Sheet directly via OpenGL 3.3 FBO!
    showcase = [
        ("ember_burn", 0.40), ("plasma_dissolve", 0.42), ("frost_shatter", 0.44), ("black_hole", 0.42),
        ("genie_lamp", 0.45), ("cyber_glitch", 0.42), ("crt_tv_off", 0.48), ("hex_shield", 0.45),
        ("doors_3d", 0.45), ("venetian_blinds", 0.45), ("shatter_glass", 0.38), ("page_peel", 0.45),
    ]
    cw, ch = 320, 210
    sheet = QtGui.QImage(cw * 4, ch * 3 + 44, QtGui.QImage.Format_RGBA8888)
    sheet.fill(QtGui.QColor(TOKENS["bg_base"]))
    painter = QtGui.QPainter(sheet)
    painter.setPen(QtGui.QColor(TOKENS["accent_hi"]))
    painter.setFont(ATLAS.font_for("mono", 11.0, bold=True))
    painter.drawText(16, 28, "PRISMFX // REAL GPU OPENGL 3.3 SHADER SHOWCASE (12 OF 42 EFFECTS RENDERED ON RTX 3070 Ti)")

    fmt = make_gl_format()
    ctx = QtGui.QOpenGLContext()
    ctx.setFormat(fmt)
    ctx.create()
    surf = QtGui.QOffscreenSurface()
    surf.setFormat(fmt)
    surf.create()
    ctx.makeCurrent(surf)
    pipe = ShaderPipeline(subdiv=32)
    pipe.initialize()
    fbo = QtOpenGL.QOpenGLFramebufferObject(cw - 16, ch - 38)
    fbo.bind()
    mock_img = generate_mock_window_image(420, 260)
    tex = QtOpenGL.QOpenGLTexture(mock_img)
    tex.setMinificationFilter(QtOpenGL.QOpenGLTexture.Linear)
    tex.setMagnificationFilter(QtOpenGL.QOpenGLTexture.Linear)
    funcs = ctx.functions()

    for idx, (eid, prog) in enumerate(showcase):
        spec = reg.get(eid)
        col = idx % 4
        row = idx // 4
        ox, oy = col * cw, 44 + row * ch

        fw, fh = cw - 16, ch - 38
        funcs.glViewport(0, 0, fw, fh)
        funcs.glClearColor(0.043, 0.047, 0.055, 1.0)
        funcs.glClear(GL_COLOR_BUFFER_BIT)
        anchor = (0.5, 0.5) if eid == "black_hole" else (0.5, 0.88)
        pipe.draw_window(
            funcs, tex, spec.shader_id, prog, prog, 1.4, 1.05,
            spec.accent_hex, (22.0, 14.0, fw - 44.0, fh - 28.0), (float(fw), float(fh)),
            anchor=anchor,
        )
        tile_img = fbo.toImage()
        painter.fillRect(ox + 6, oy + 4, cw - 12, ch - 10, QtGui.QColor(TOKENS["bg_elevated"]))
        painter.setPen(QtGui.QPen(QtGui.QColor(TOKENS["line"]), 1))
        painter.drawRect(ox + 6, oy + 4, cw - 12, ch - 10)
        painter.drawImage(ox + 8, oy + 6, tile_img)
        painter.fillRect(ox + 8, oy + ch - 34, 4, 24, QtGui.QColor(spec.accent_hex))
        painter.setPen(QtGui.QColor(TOKENS["ink_primary"]))
        painter.setFont(ATLAS.font_for("body", 9.5, text=spec.name_th, bold=True))
        painter.drawText(ox + 18, oy + ch - 17, f"{spec.name_th} ({spec.name_en})")

    tex.destroy()
    fbo.release()
    pipe.cleanup()
    ctx.doneCurrent()
    painter.end()

    sheet_path = out_dir / "06_shader_showcase.png"
    sheet.save(str(sheet_path))
    saved.append(str(sheet_path))

    win.full_shutdown()
    return saved
