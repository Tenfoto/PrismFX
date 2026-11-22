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

        # 7d. Windows Startup XML, Elevation ABI & IPC message registration
        import xml.etree.ElementTree as ET
        from .core import startup as win_startup
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        cmd, args_s, cwd_s, full_cmd = win_startup.build_autostart_command(repo_root)
        xml_txt = win_startup.build_task_scheduler_xml(cmd, args_s, cwd_s, "TESTPC\\DemoUser", elevated=True)
        root_xml = ET.fromstring(xml_txt)
        xml_ok = (
            "HighestAvailable" in xml_txt
            and "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml_txt
            and "<Priority>5</Priority>" in xml_txt
            and "--autostart" in full_cmd
            and root_xml.tag.endswith("Task")
        )
        abi_ok = (
            ctypes.sizeof(w32.SHELLEXECUTEINFOW) == 112
            and ctypes.sizeof(w32.PROPERTYKEY) == 20
            and ctypes.sizeof(w32.PROPVARIANT_LPWSTR) == 24
            and w32.register_ipc_message(str(td)) > 0
        )
        check(
            "startup_xml_and_privilege_abi",
            xml_ok and abi_ok,
            f"xml_valid={xml_ok} sei_112B={ctypes.sizeof(w32.SHELLEXECUTEINFOW)} propkey_20B={ctypes.sizeof(w32.PROPERTYKEY)}",
        )

        # 7e. Sprint 0 Negative Security & Crash-Safety Tests (100% isolated in TemporaryDirectory)
        import math
        import re
        import subprocess
        from .core.effects import MAX_CUSTOM_SHADER_BYTES, sanitize_hex_color
        from .render.shaders import load_custom_fragment_shader

        # (1) sprint0_frozen_autostart: frozen EXE resolution, System32 schtasks, quoting, %TEMP% LPE block
        frozen_dir = tdir / "frozen_app"
        frozen_dir.mkdir(parents=True, exist_ok=True)
        fake_exe = frozen_dir / "PrismFX.exe"
        fake_exe.write_bytes(b"MZ")
        orig_has_frozen = hasattr(sys, "frozen")
        orig_frozen = getattr(sys, "frozen", False)
        orig_executable = sys.executable
        orig_subproc_run = subprocess.run
        orig_shell_exec = w32.shell_execute_elevated
        os_exec_calls: list[str] = []
        try:
            setattr(sys, "frozen", True)
            sys.executable = str(fake_exe)
            r_exe, r_args, r_native = win_startup.resolve_launcher(frozen_dir)
            frozen_launcher_ok = (
                r_exe == fake_exe.resolve()
                and r_args == []
                and r_native is True
                and all("main.py" not in a.lower() for a in r_args)
            )
            schtasks_abs_ok = win_startup.SCHTASKS_EXE.lower().endswith("\\system32\\schtasks.exe")
            quoted_bs = win_startup._quote_arg("C:\\a b\\")
            quote_ok = quoted_bs == '"C:\\a b\\\\"'
            can_elev, block_reason = win_startup.can_enable_elevated_task(frozen_dir)

            def _forbid_subproc(*a, **kw):
                os_exec_calls.append("subprocess.run")
                raise AssertionError("subprocess.run must not be called for blocked_insecure_path")

            def _forbid_shell_exec(*a, **kw):
                os_exec_calls.append("shell_execute_elevated")
                raise AssertionError("shell_execute_elevated must not be called for blocked_insecure_path")

            subprocess.run = _forbid_subproc  # type: ignore[assignment]
            w32.shell_execute_elevated = _forbid_shell_exec  # type: ignore[assignment]
            app_ok, app_code, _app_msg = win_startup.apply_startup_settings(
                frozen_dir, enable=True, elevated_task=True, dry_run=False
            )
            blocked_ok = (
                not can_elev
                and bool(block_reason)
                and app_ok is False
                and app_code == "blocked_insecure_path"
                and not os_exec_calls
            )
        finally:
            subprocess.run = orig_subproc_run  # type: ignore[assignment]
            w32.shell_execute_elevated = orig_shell_exec  # type: ignore[assignment]
            sys.executable = orig_executable
            if orig_has_frozen:
                setattr(sys, "frozen", orig_frozen)
            elif hasattr(sys, "frozen"):
                delattr(sys, "frozen")
        check(
            "sprint0_frozen_autostart",
            frozen_launcher_ok and schtasks_abs_ok and quote_ok and blocked_ok,
            f"launcher={frozen_launcher_ok} schtasks={schtasks_abs_ok} quote={quoted_bs!r} blocked={blocked_ok}",
        )

        # (2) sprint0_shader_traversal_unc: block traversal, UNC SMB leak, >128KB file, non-.glsl
        nested_parent = tdir / "lvl1" / "lvl2"
        shader_bad_dir = nested_parent / "shaders"
        shader_bad_dir.mkdir(parents=True, exist_ok=True)
        (tdir / "README.md").write_text("secret outside dir", encoding="utf-8")
        (shader_bad_dir / "trav.prismfx.json").write_text(
            json.dumps({"id": "trav", "fragment": "..\\..\\README.md"}), encoding="utf-8"
        )
        (shader_bad_dir / "unc.prismfx.json").write_text(
            json.dumps({"id": "unc_leak", "fragment": "\\\\127.0.0.1\\nonexistent_share\\evil.glsl"}),
            encoding="utf-8",
        )
        oversized_glsl = shader_bad_dir / "oversized.glsl"
        oversized_glsl.write_bytes(b"// " + b"A" * (MAX_CUSTOM_SHADER_BYTES + 1024))
        (shader_bad_dir / "oversized.prismfx.json").write_text(
            json.dumps({"id": "oversized", "fragment": "oversized.glsl"}), encoding="utf-8"
        )
        bad_ext = shader_bad_dir / "bad_ext.txt"
        bad_ext.write_text("void main() {}", encoding="utf-8")
        (shader_bad_dir / "bad_ext.prismfx.json").write_text(
            json.dumps({"id": "bad_ext", "fragment": "bad_ext.txt"}), encoding="utf-8"
        )
        reg_bad = EffectRegistry()
        bad_loaded = reg_bad.scan_custom_dir(shader_bad_dir)
        unc_direct_ok = (
            load_custom_fragment_shader("\\\\127.0.0.1\\nonexistent_share\\evil.glsl") is None
            and load_custom_fragment_shader("//127.0.0.1/nonexistent_share/evil.glsl") is None
            and load_custom_fragment_shader("\\/127.0.0.1\\nonexistent_share\\evil.glsl") is None
            and load_custom_fragment_shader("/\\127.0.0.1\\nonexistent_share\\evil.glsl") is None
        )
        oversized_direct_ok = load_custom_fragment_shader(str(oversized_glsl)) is None
        ext_direct_ok = load_custom_fragment_shader(str(bad_ext)) is None
        check(
            "sprint0_shader_traversal_unc",
            bad_loaded == 0 and unc_direct_ok and oversized_direct_ok and ext_direct_ok,
            f"bad_loaded={bad_loaded} unc_blocked={unc_direct_ok} oversized_blocked={oversized_direct_ok} ext_blocked={ext_direct_ok}",
        )

        # (3) sprint0_qss_injection: accent_hex strictly filtered to ^#[0-9A-Fa-f]{6}$
        qss_dir = tdir / "qss_shaders"
        qss_dir.mkdir(parents=True, exist_ok=True)
        evil_hex = "#2D81FF; border: 8px solid #FF00FF; background: url(//attacker/x.png)"
        (qss_dir / "safe.glsl").write_text("void main() { fragColor = vec4(1.0); }", encoding="utf-8")
        (qss_dir / "qss.prismfx.json").write_text(
            json.dumps({"id": "qss_test", "fragment": "safe.glsl", "accent_hex": evil_hex}),
            encoding="utf-8",
        )
        san_hex = sanitize_hex_color(evil_hex)
        reg_qss = EffectRegistry()
        qss_loaded = reg_qss.scan_custom_dir(qss_dir)
        qss_spec = reg_qss.get("custom_qss_test")
        hex_re = re.compile(r"^#[0-9A-Fa-f]{6}$")
        qss_ok = (
            qss_loaded == 1
            and bool(hex_re.fullmatch(san_hex))
            and bool(hex_re.fullmatch(qss_spec.accent_hex))
            and ";" not in qss_spec.accent_hex
            and "url(" not in qss_spec.accent_hex.lower()
        )
        check(
            "sprint0_qss_injection",
            qss_ok,
            f"san={san_hex!r} spec_accent={qss_spec.accent_hex!r}",
        )

        # (4) sprint0_config_resilience: per-field safe fallback, corrupt .bak, preset traversal & reserved names
        bad_raw = {
            "language": "en",
            "excluded_exes": None,
            "global_speed_scale": "not_a_float",
            "target_fps": float("nan"),
            "safety": {"excluded_exes": None},
        }
        parsed_cfg = PrismConfig.from_dict(bad_raw)
        default_cfg = PrismConfig()
        iter_ex = [x.lower() for x in parsed_cfg.safety.excluded_exes]
        from_dict_ok = (
            parsed_cfg.language == "en"
            and isinstance(parsed_cfg.safety.excluded_exes, list)
            and len(iter_ex) == len(parsed_cfg.safety.excluded_exes)
            and math.isfinite(parsed_cfg.global_speed_scale)
            and parsed_cfg.global_speed_scale == default_cfg.global_speed_scale
            and parsed_cfg.target_fps == default_cfg.target_fps
        )
        cfg_iso_dir = tdir / "cfg_resilience"
        cfg_store = ConfigStore(cfg_iso_dir)
        cfg_store.config_path.write_text("{corrupt json!!!", encoding="utf-8")
        recovered_cfg = cfg_store.load()
        corrupt_bak_ok = (
            cfg_store.last_load_recovered_corrupt
            and (cfg_iso_dir / "config.json.corrupt.bak").is_file()
            and isinstance(recovered_cfg, PrismConfig)
        )
        # Escape preset outside presets_dir must NOT be loaded via traversal
        (tdir / "escape.prismpreset.json").write_text(
            json.dumps({"enabled": False, "active_preset": "ESCAPED"}), encoding="utf-8"
        )
        trav_res = cfg_store.apply_preset(default_cfg, "../../escape")
        preset_trav_blocked = (
            trav_res.active_preset == default_cfg.active_preset
            and trav_res.enabled == default_cfg.enabled
            and trav_res.to_dict() == default_cfg.to_dict()
        )
        saved_con = cfg_store.save_user_preset("CON", default_cfg)
        con_safe_ok = (
            not (cfg_store.presets_dir / "CON.json").exists()
            and not (cfg_store.presets_dir / "CON.prismpreset.json").exists()
            and saved_con is not None
            and saved_con.is_file()
            and saved_con.name.lower() not in ("con.json", "con.prismpreset.json")
        )
        check(
            "sprint0_config_resilience",
            from_dict_ok and corrupt_bak_ok and preset_trav_blocked and con_safe_ok,
            f"from_dict={from_dict_ok} bak={corrupt_bak_ok} trav_blocked={preset_trav_blocked} con_safe={con_safe_ok}",
        )

        # (5) sprint0_journal_failed_restore: failed callbacks must preserve pending entries in memory & on disk
        jfail_path = tdir / "journal_fail" / "recovery_journal.json"
        jf = RecoveryJournal(jfail_path)
        jf.record_alpha(2001, 255)
        jf.record_pin(2002, True)
        jf.record_rollup(2003, True, 540)
        jf.record_desktop_icons_hidden(True)
        pending_before = jf.pending_count()
        restored_fail = jf.restore_all(
            uncloak_fn=lambda _h: False,
            restore_alpha_fn=lambda _h, _a: False,
            unpin_fn=lambda _h: False,
            unroll_fn=lambda _h, _oh: False,
            show_desktop_icons_fn=lambda: False,
            restore_min_animate_fn=lambda _orig: False,
        )
        jf_reloaded = RecoveryJournal(jfail_path)
        preserved_ok = (
            pending_before == 4
            and restored_fail == 0
            and jf.pending_count() == pending_before
            and jf.last_restore_failed_count == pending_before
            and jfail_path.is_file()
            and jf_reloaded.pending_count() == pending_before
        )
        restored_ok = jf_reloaded.restore_all(
            uncloak_fn=lambda _h: True,
            restore_alpha_fn=lambda _h, _a: True,
            unpin_fn=lambda _h: True,
            unroll_fn=lambda _h, _oh: True,
            show_desktop_icons_fn=lambda: True,
            restore_min_animate_fn=lambda _orig: True,
        )
        cleared_ok = (
            restored_ok == pending_before
            and jf_reloaded.pending_count() == 0
            and jf_reloaded.last_restore_failed_count == 0
            and RecoveryJournal(jfail_path).pending_count() == 0
        )
        check(
            "sprint0_journal_failed_restore",
            preserved_ok and cleared_ok,
            f"before={pending_before} fail_kept={preserved_ok} retry_cleared={cleared_ok}",
        )

        # (6) sprint0_uipi_ipc_split: separate QUIT message ID (>0 and distinct from Activate IPC message)
        ipc_activate = w32.register_ipc_message(str(tdir))
        ipc_quit = w32.register_ipc_quit_message(str(tdir))
        check(
            "sprint0_uipi_ipc_split",
            ipc_activate > 0 and ipc_quit > 0 and ipc_quit != ipc_activate,
            f"activate={ipc_activate} quit={ipc_quit}",
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
            tex = QtOpenGL.QOpenGLTexture(img, QtOpenGL.QOpenGLTexture.DontGenerateMipMaps)
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

            # 8b. Sprint 1 Regression: GLSL 330 Strictness (PFX-C04) across 42/42 shaders at p=0.0, 0.45, 1.0
            import re as _re
            from .render.shaders import FRAGMENT_SHADER_330, VERTEX_SHADER_330

            strict_gl_errors = 0
            strict_p0_ok = 0
            strict_p45_ok = 0
            strict_p1_ok = 0
            p0_images: dict[str, QtGui.QImage] = {}
            p45_images: dict[str, QtGui.QImage] = {}
            for spec in reg.all_effects(include_special=False):
                anchor = (0.5, 0.5) if spec.id == "black_hole" else (0.5, 0.9)
                for p_val in (0.0, 0.45, 1.0):
                    funcs.glViewport(0, 0, 160, 100)
                    funcs.glClearColor(0.0, 0.0, 0.0, 0.0)
                    funcs.glClear(GL_COLOR_BUFFER_BIT)
                    pipe.draw_window(
                        funcs, tex, spec.shader_id, p_val, p_val, 1.2, 1.0,
                        spec.accent_hex, (12.0, 8.0, 136.0, 84.0), (160.0, 100.0),
                        anchor=anchor,
                    )
                    if funcs.glGetError() != 0:
                        strict_gl_errors += 1
                    frame = fbo.toImage()
                    opaque_cnt = sum(
                        1
                        for y in range(16, 86, 10)
                        for x in range(20, 140, 15)
                        if frame.pixelColor(x, y).alpha() > 10
                    )
                    if p_val == 0.0:
                        if opaque_cnt > 0:
                            strict_p0_ok += 1
                        if spec.id in ("clean_fade", "pixelate_mosaic"):
                            p0_images[spec.id] = frame
                    elif p_val == 0.45:
                        if opaque_cnt > 0:
                            strict_p45_ok += 1
                        if spec.id in ("ember_burn", "black_hole"):
                            p45_images[spec.id] = frame
                    else:
                        strict_p1_ok += 1

            # Verify pixelate_mosaic at p=0.0 matches clean_fade cleanly (no coarse block garbage)
            mosaic_p0_clean = False
            if "clean_fade" in p0_images and "pixelate_mosaic" in p0_images:
                img_cf = p0_images["clean_fade"]
                img_pm = p0_images["pixelate_mosaic"]
                max_diff = 0
                for y in range(14, 88, 6):
                    for x in range(18, 142, 8):
                        c1 = img_cf.pixelColor(x, y)
                        c2 = img_pm.pixelColor(x, y)
                        max_diff = max(
                            max_diff,
                            abs(c1.red() - c2.red()),
                            abs(c1.green() - c2.green()),
                            abs(c1.blue() - c2.blue()),
                            abs(c1.alpha() - c2.alpha()),
                        )
                mosaic_p0_clean = max_diff <= 2

            # Verify ember_burn & black_hole at p=0.45 have non-black, non-zero-alpha visible interior pixels
            def _has_rich_pixels(qim: QtGui.QImage) -> bool:
                lit = 0
                for y in range(14, 88, 6):
                    for x in range(18, 142, 8):
                        c = qim.pixelColor(x, y)
                        if c.alpha() > 20 and (c.red() + c.green() + c.blue()) > 30:
                            lit += 1
                return lit >= 8

            ember_ok = "ember_burn" in p45_images and _has_rich_pixels(p45_images["ember_burn"])
            bh_ok = "black_hole" in p45_images and _has_rich_pixels(p45_images["black_hole"])

            # Static GLSL 330 strictness guard: zero inverted numeric smoothstep(hi, lo, x)
            inverted_smoothsteps = 0
            for src_blob in (VERTEX_SHADER_330, FRAGMENT_SHADER_330):
                for m_ss in _re.finditer(r"smoothstep\s*\(\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*,", src_blob):
                    if float(m_ss.group(1)) >= float(m_ss.group(2)):
                        inverted_smoothsteps += 1

            check(
                "sprint1_glsl330_strictness",
                strict_gl_errors == 0
                and strict_p0_ok == total_shaders
                and strict_p45_ok == total_shaders
                and strict_p1_ok == total_shaders
                and mosaic_p0_clean
                and ember_ok
                and bh_ok
                and inverted_smoothsteps == 0,
                f"p0={strict_p0_ok}/{total_shaders} p45={strict_p45_ok}/{total_shaders} gl_err={strict_gl_errors} "
                f"mosaic_p0_clean={mosaic_p0_clean} ember={ember_ok} black_hole={bh_ok} inv_ss={inverted_smoothsteps}",
            )

            tex.destroy()
            fbo.release()
            pipe.cleanup()
        ctx.doneCurrent()
        surf.destroy()
    else:
        if surf.isValid():
            surf.destroy()
        check("gl33_context", False, "Failed to create OpenGL 3.3 Core context")

    # 9. Sprint 1 Regression Tests (PFX-C03, PFX-H03, PFX-H05, PFX-H01, PFX-H02)
    import ctypes as _ct
    import shiboken6 as _shiboken
    from .render.gl_renderer import OffscreenGpuEngine
    from .render.shaders import load_custom_fragment_shader, validate_custom_glsl_source

    # (9a) ABI Check (PFX-C03): all 14 gdi32/user32 functions + c_ubyte structs
    abi_targets = [
        (w32.gdi32, "CreateCompatibleDC"),
        (w32.gdi32, "CreateDIBSection"),
        (w32.gdi32, "SelectObject"),
        (w32.gdi32, "BitBlt"),
        (w32.gdi32, "DeleteObject"),
        (w32.gdi32, "DeleteDC"),
        (w32.user32, "GetDC"),
        (w32.user32, "ReleaseDC"),
        (w32.user32, "EnumWindows"),
        (w32.user32, "EnumDisplayMonitors"),
        (w32.user32, "GetMonitorInfoW"),
        (w32.user32, "TranslateMessage"),
        (w32.user32, "DispatchMessageW"),
        (w32.user32, "SetForegroundWindow"),
    ]
    missing_abi = [
        name for dll, name in abi_targets
        if getattr(dll, name).argtypes is None or getattr(dll, name).restype is None
    ]
    blend_fields_ok = all(t is _ct.c_ubyte for _, t in w32.BLENDFUNCTION._fields_)
    blend_val_ok = w32.BLENDFUNCTION(0, 0, 255, 1).SourceConstantAlpha == 255
    sps_fields_ok = all(t is _ct.c_ubyte for _, t in w32.SYSTEM_POWER_STATUS._fields_[:4])
    check(
        "sprint1_abi_check",
        not missing_abi and blend_fields_ok and blend_val_ok and sps_fields_ok,
        f"missing={missing_abi} blend_ubyte={blend_fields_ok} alpha255={blend_val_ok} sps_ubyte={sps_fields_ok}",
    )

    # (9b) FBO Leak Probe (PFX-H03): >12 distinct sizes -> evicted FBOs destroyed, 0 leaked handles
    fbo_gpu = OffscreenGpuEngine()
    fbo_probe_ok = False
    fbo_detail = ""
    try:
        if fbo_gpu.ensure_ready():
            seen_fbos: list[tuple[tuple[int, int], QtOpenGL.QOpenGLFramebufferObject]] = []
            for idx in range(14):
                sz_w = 64 + idx * 8
                sz_h = 64 + idx * 6
                out = fbo_gpu.render_to_image(
                    sz_w, sz_h, (4.0, 4.0, float(sz_w - 8), float(sz_h - 8)),
                    37, 0.2, 0.2, 0.5,
                )
                if out is not None and (sz_w, sz_h) in fbo_gpu._fbo_cache:
                    seen_fbos.append(((sz_w, sz_h), fbo_gpu._fbo_cache[(sz_w, sz_h)]))
            active_keys = set(fbo_gpu._fbo_cache.keys())
            evicted = [obj for k, obj in seen_fbos if k not in active_keys]
            leaked_evicted = sum(1 for obj in evicted if _shiboken.isValid(obj))
            live_handles = sorted(int(obj.handle()) for obj in fbo_gpu._fbo_cache.values() if _shiboken.isValid(obj))
            fbo_gpu.shutdown()
            leaked_after_shutdown = sum(1 for _, obj in seen_fbos if _shiboken.isValid(obj))
            fbo_probe_ok = (
                len(seen_fbos) == 14
                and len(active_keys) <= 4
                and len(evicted) >= 10
                and leaked_evicted == 0
                and leaked_after_shutdown == 0
                and len(live_handles) <= 4
                and (max(live_handles) <= 5 if live_handles else False)
            )
            fbo_detail = (
                f"sizes={len(seen_fbos)} evicted={len(evicted)} leaked_evicted={leaked_evicted} "
                f"leaked_shutdown={leaked_after_shutdown} live_handles={live_handles}"
            )
        else:
            fbo_detail = f"gpu_init_failed={fbo_gpu.init_error}"
    finally:
        fbo_gpu.shutdown()
    check("sprint1_fbo_leak_probe", fbo_probe_ok, fbo_detail)

    # (9c) Custom GLSL Negative Cache & Sandbox (PFX-H05): 5x broken -> 1 compile; reject while/do
    cache_gpu = OffscreenGpuEngine()
    cache_sandbox_ok = False
    cache_detail = ""
    try:
        with tempfile.TemporaryDirectory(prefix="prismfx_glsl_probe_") as td_gl:
            gdir = pathlib.Path(td_gl)
            broken_path = gdir / "broken_syntax.glsl"
            broken_path.write_text("void main() { THIS_IS_INVALID_GLSL( }\n", encoding="utf-8")
            while_path = gdir / "bad_while.glsl"
            while_path.write_text("void main() { int i = 0; while (i < 5) { i++; } fragColor = vec4(1.0); }\n", encoding="utf-8")
            do_path = gdir / "bad_do.glsl"
            do_path.write_text("void main() { int i = 0; do { i++; } while (i < 5); fragColor = vec4(1.0); }\n", encoding="utf-8")

            if cache_gpu.ensure_ready():
                for _ in range(5):
                    cache_gpu.render_to_image(
                        64, 64, (4.0, 4.0, 56.0, 56.0),
                        1000, 0.3, 0.3, 0.5,
                        custom_frag_path=str(broken_path),
                    )
                comp_cnt = cache_gpu.custom_compile_count(str(broken_path))
                err_msg = cache_gpu.get_custom_shader_error(str(broken_path))
            else:
                comp_cnt = -1
                err_msg = ""

            while_ok, while_err = validate_custom_glsl_source(while_path.read_text(encoding="utf-8"))
            do_ok, do_err = validate_custom_glsl_source(do_path.read_text(encoding="utf-8"))
            bypass_payloads = [
                "void main() { wh\\\nile (true) {} fragColor = vec4(1.0); }",
                "#define LP (\nvoid main() { for LP ;; ) {} fragColor = vec4(1.0); }",
                "void main() { for (int i = 0; i < 4, true; ++i) {} fragColor = vec4(1.0); }",
                "void main() { for (int i = 0; i < 4; ++i) { i = 0; } fragColor = vec4(1.0); }",
            ]
            bypasses_rejected = all(not validate_custom_glsl_source(src)[0] for src in bypass_payloads)
            while_load_none = load_custom_fragment_shader(str(while_path)) is None
            do_load_none = load_custom_fragment_shader(str(do_path)) is None
            repo_ripple = pathlib.Path(__file__).resolve().parents[1] / "assets" / "shaders" / "chromatic_ripple_custom.glsl"
            ripple_ok = load_custom_fragment_shader(str(repo_ripple)) is not None if repo_ripple.is_file() else True

            cache_sandbox_ok = (
                comp_cnt == 1
                and bool(err_msg)
                and (not while_ok)
                and (not do_ok)
                and bypasses_rejected
                and while_load_none
                and do_load_none
                and ripple_ok
            )
            cache_detail = (
                f"compile_cnt={comp_cnt} err_cached={bool(err_msg)} "
                f"while_rejected={not while_ok and while_load_none} "
                f"do_rejected={not do_ok and do_load_none} bypasses_rejected={bypasses_rejected} ripple_ok={ripple_ok}"
            )
    finally:
        cache_gpu.shutdown()
    check("sprint1_custom_glsl_cache_and_sandbox", cache_sandbox_ok, cache_detail)

    # (9d) Safe GL Fallback Latch (PFX-H01) & DIB Viewport Bounds Guard (PFX-H02)
    fail_gpu = OffscreenGpuEngine()
    orig_ctx_create = QtGui.QOpenGLContext.create
    create_calls = 0
    try:
        def _fail_create(_self) -> bool:
            nonlocal create_calls
            create_calls += 1
            return False

        QtGui.QOpenGLContext.create = _fail_create  # type: ignore[method-assign]
        fail_results = [fail_gpu.ensure_ready() for _ in range(10)]
    finally:
        QtGui.QOpenGLContext.create = orig_ctx_create  # type: ignore[method-assign]
        fail_gpu.shutdown()

    latch_ok = (
        not any(fail_results)
        and create_calls == 1
        and fail_gpu._init_attempted is True
        and bool(fail_gpu.init_error)
    )

    dib_gpu = OffscreenGpuEngine()
    dib_guard_ok = False
    try:
        if dib_gpu.ensure_ready():
            scratch = (_ct.c_ubyte * (64 * 64 * 4))()
            ptr = _ct.addressof(scratch)
            rej_8x8 = not dib_gpu.render_to_dib_ptr(
                8, 8, (0.0, 0.0, 8.0, 8.0), 37, 0.2, 0.2, 0.5, dib_ptr=ptr, dib_byte_size=8 * 8 * 4
            )
            rej_15x32 = not dib_gpu.render_to_dib_ptr(
                15, 32, (0.0, 0.0, 15.0, 32.0), 37, 0.2, 0.2, 0.5, dib_ptr=ptr, dib_byte_size=15 * 32 * 4
            )
            rej_small_buf = not dib_gpu.render_to_dib_ptr(
                64, 64, (0.0, 0.0, 64.0, 64.0), 37, 0.2, 0.2, 0.5, dib_ptr=ptr, dib_byte_size=64
            )
            ok_valid = dib_gpu.render_to_dib_ptr(
                64, 64, (0.0, 0.0, 64.0, 64.0), 37, 0.2, 0.2, 0.5, dib_ptr=ptr, dib_byte_size=64 * 64 * 4
            )
            dib_guard_ok = rej_8x8 and rej_15x32 and rej_small_buf and ok_valid
    finally:
        dib_gpu.shutdown()

    check(
        "sprint1_safe_gl_fallback_and_dib_bounds",
        latch_ok and dib_guard_ok,
        f"create_calls={create_calls} latched={fail_gpu._init_attempted} dib_guard={dib_guard_ok}",
    )

    # 10. Sprint 2 Track 1 Regression Tests (PFX-C07 Borderless Fullscreen Game Auto-Pause + PFX-M05 Anti-Cheat Hygiene)
    # (10a) sprint2_borderless_game_pause
    mon_primary = w32.MonitorRect("\\\\.\\DISPLAY1", 0, 0, 1920, 1080, 0, 0, 1920, 1032, True)
    mon_right = w32.MonitorRect("\\\\.\\DISPLAY2", 1920, 0, 2560, 1440, 1920, 0, 2560, 1392, False)
    mon_left_neg = w32.MonitorRect("\\\\.\\DISPLAY3", -1920, 0, 1920, 1080, -1920, 0, 1920, 1032, False)
    mon_tiny = w32.MonitorRect("\\\\.\\DISPLAY4", 0, 0, 320, 240, 0, 0, 320, 240, False)
    borderless_style = w32.WS_VISIBLE | w32.WS_POPUP
    captioned_style = w32.WS_VISIBLE | w32.WS_CAPTION | w32.WS_THICKFRAME | w32.WS_SYSMENU

    cand_pos_ok = (
        w32.is_borderless_fullscreen_candidate(
            "UnrealWindow", "SomeDX12Game.exe", borderless_style, 0, (0, 0, 1920, 1080), mon_primary
        )
        and w32.is_borderless_fullscreen_candidate(
            "UnrealWindow", "SomeDX12Game.exe", borderless_style, 0, (2, 2, 1916, 1076), mon_primary
        )
        and w32.is_borderless_fullscreen_candidate(
            "VulkanWnd", "VulkanGame.exe", borderless_style, 0, (1920, 0, 2560, 1440), mon_right
        )
        and w32.is_borderless_fullscreen_candidate(
            "DX12Wnd", "LeftGame.exe", borderless_style, 0, (-1920, 0, 1920, 1080), mon_left_neg
        )
        and w32.is_borderless_fullscreen_candidate(
            "SDL_app", "cs2.exe", captioned_style, 0, (0, 0, 1920, 1080), mon_primary
        )
    )

    shell_rejected = all(
        not w32.is_borderless_fullscreen_candidate(
            cls, "explorer.exe", borderless_style, 0, (0, 0, 1920, 1080), mon_primary
        )
        for cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")
    )
    browsers_rejected = all(
        not w32.is_borderless_fullscreen_candidate(
            "Chrome_WidgetWin_1", br, borderless_style, 0, (0, 0, 1920, 1080), mon_primary
        )
        for br in (
            "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
            "opera.exe", "vivaldi.exe", "arc.exe", "iexplore.exe", "waterfox.exe",
        )
    )
    normal_rejected = (
        not w32.is_borderless_fullscreen_candidate(
            "Notepad", "Notepad.exe", captioned_style, 0, (0, 0, 1920, 1080), mon_primary
        )
        and not w32.is_borderless_fullscreen_candidate(
            "Chrome_WidgetWin_1", "Code.exe", captioned_style, 0, (0, 0, 1920, 1032), mon_primary
        )
        and not w32.is_borderless_fullscreen_candidate(
            "UnrealWindow", "SomeDX12Game.exe", borderless_style, 0, (3, 0, 1917, 1080), mon_primary
        )
        and not w32.is_borderless_fullscreen_candidate(
            "UnrealWindow", "SomeDX12Game.exe", borderless_style, 0, (0, 0, 1920, 1080), mon_primary, is_cloaked=True
        )
        and not w32.is_borderless_fullscreen_candidate(
            "UnrealWindow", "SomeDX12Game.exe", borderless_style, 0, (0, 0, 1920, 1080), mon_primary, is_iconic=True
        )
        and not w32.is_borderless_fullscreen_candidate(
            "UnrealWindow", "SomeDX12Game.exe", borderless_style, w32.WS_EX_TRANSPARENT, (0, 0, 1920, 1080), mon_primary
        )
        and not w32.is_borderless_fullscreen_candidate(
            "UnrealWindow", "SomeDX12Game.exe", borderless_style, 0, (0, 0, 320, 240), mon_tiny
        )
        and not w32.is_borderless_fullscreen_hwnd(0, own_pid=os.getpid())
    )

    # Full Engine + DesktopFxOverlay + SpotlightOverlay + MainWindow pause hygiene verification
    eng_pause_ok = False
    lbutton_guarded_ok = False
    overlay_hidden_ok = False
    spotlight_hidden_ok = False
    exit_resume_ok = False
    pause_detail = ""
    with tempfile.TemporaryDirectory(prefix="prismfx_s2_t1_") as td_s2:
        store_s2 = ConfigStore(pathlib.Path(td_s2))
        demo_b = DemoWindowBackend(seed=42)
        eng_s2 = PrismEngine(demo_b, store_s2, EffectRegistry())
        win_s2 = MainWindow(eng_s2, enable_overlays=True, enable_tray=False)
        orig_lbutton_fn = w32.is_lbutton_down
        lbutton_calls = 0
        try:
            w32.set_window_cloaked(int(win_s2.winId()), True)
            assert win_s2.fx_overlay is not None and win_s2.spotlight_overlay is not None
            for s in win_s2.fx_overlay._surface_pool:
                w32.set_window_cloaked(s.hwnd, True)
            from .core.engine import SpotlightState as _S2SpotState
            win_s2.spotlight_overlay.apply_state(
                _S2SpotState(
                    active=True,
                    mode="halo",
                    opacity=0.45,
                    color_hex="#06080E",
                    active_hwnd=1001,
                    active_rect=(120, 80, 1180, 760),
                    cursor_pos=(500, 400),
                )
            )
            QtCore.QCoreApplication.processEvents()

            initial_ok = (
                eng_s2.paused_reason() == ""
                and eng_s2._want_raw_input_mouse() is True
                and win_s2.spotlight_overlay.isVisible()
                and any(s.is_visible and s.is_parked for s in win_s2.fx_overlay._surface_pool)
            )

            # Switch foreground to borderless fullscreen game (1007 = VALORANT 1920x1080)
            demo_b.set_foreground_hwnd(1007)
            eng_s2._drag_hwnd = 1001
            eng_s2._housekeeping_tick(time.perf_counter())
            QtCore.QCoreApplication.processEvents()

            eng_pause_ok = (
                initial_ok
                and eng_s2._cached_paused_reason == "fullscreen_game"
                and eng_s2._want_raw_input_mouse() is False
                and eng_s2._drag_hwnd == 0
            )

            # Verify GetAsyncKeyState (is_lbutton_down) is NEVER called while paused for fullscreen_game
            def _spy_lbutton() -> bool:
                nonlocal lbutton_calls
                lbutton_calls += 1
                return orig_lbutton_fn()

            w32.is_lbutton_down = _spy_lbutton  # type: ignore[assignment]
            eng_s2._drag_hwnd = 1001
            eng_s2._watchdog_tick(time.perf_counter())
            eng_s2._drag_hwnd = 0
            lbutton_guarded_ok = (lbutton_calls == 0)

            # Verify DesktopFxOverlay surfaces are SW_HIDE (not visible, not parked!) + Spotlight hidden
            pool_hidden = bool(win_s2.fx_overlay._surface_pool) and all(
                (not s.is_visible) and (not s.is_parked) and (not w32.user32.IsWindowVisible(s.hwnd))
                for s in win_s2.fx_overlay._surface_pool
            )
            # Also verify _release_surface keeps surface hidden while paused
            tmp_s = win_s2.fx_overlay._acquire_surface()
            w32.set_window_cloaked(tmp_s.hwnd, True)
            win_s2.fx_overlay._release_surface(tmp_s)
            release_hidden = (not tmp_s.is_visible) and (not tmp_s.is_parked) and (not w32.user32.IsWindowVisible(tmp_s.hwnd))
            overlay_hidden_ok = win_s2.fx_overlay._fullscreen_game_paused and pool_hidden and release_hidden
            spotlight_hidden_ok = (
                (not win_s2.spotlight_overlay.isVisible())
                and all(
                    (not s.is_visible) and (not s.is_parked) and (not w32.user32.IsWindowVisible(s.hwnd))
                    for s in win_s2.spotlight_overlay._surfaces.values()
                )
            )

            # Exit fullscreen game -> engine resumes and re-enables raw input & overlay flag
            demo_b.set_foreground_hwnd(1001)
            eng_s2._housekeeping_tick(time.perf_counter())
            QtCore.QCoreApplication.processEvents()
            exit_resume_ok = (
                eng_s2._cached_paused_reason == ""
                and eng_s2._want_raw_input_mouse() is True
                and win_s2.fx_overlay._fullscreen_game_paused is False
            )
            pause_detail = (
                f"cand={cand_pos_ok} shell_rej={shell_rejected} browser_rej={browsers_rejected} "
                f"norm_rej={normal_rejected} eng_pause={eng_pause_ok} lbtn_calls={lbutton_calls} "
                f"ov_hidden={overlay_hidden_ok} spot_hidden={spotlight_hidden_ok} exit_resume={exit_resume_ok}"
            )
        finally:
            w32.is_lbutton_down = orig_lbutton_fn  # type: ignore[assignment]
            win_s2.full_shutdown()
        shutdown_unbound_ok = eng_s2.on_pause_state_changed is None

    check(
        "sprint2_borderless_game_pause",
        cand_pos_ok
        and shell_rejected
        and browsers_rejected
        and normal_rejected
        and eng_pause_ok
        and lbutton_guarded_ok
        and overlay_hidden_ok
        and spotlight_hidden_ok
        and exit_resume_ok
        and shutdown_unbound_ok,
        pause_detail,
    )

    # (10b) sprint2_anticheat_hygiene (PFX-M05)
    win32_src = pathlib.Path(w32.__file__).read_text(encoding="utf-8")
    dead_symbols = (
        "SetWindowsHookExW",
        "UnhookWindowsHookEx",
        "CallNextHookEx",
        "WH_MOUSE_LL",
        "MSLLHOOKSTRUCT",
        "HOOKPROC",
        '_load_system_dll("psapi")',
        "psapi =",
    )
    dead_in_src = [tok for tok in dead_symbols if tok in win32_src]
    dead_on_mod = [sym for sym in ("psapi", "WH_MOUSE_LL", "MSLLHOOKSTRUCT", "HOOKPROC") if hasattr(w32, sym)]
    dead_removed_ok = (not dead_in_src) and (not dead_on_mod)

    # Verify TTL cache in get_process_info avoids repeat OpenProcess calls for the same PID
    open_proc_calls = 0
    orig_k32 = w32.kernel32
    surf_pid_probe = w32.Win32LayeredSurface()

    class _Kernel32OpenProcessSpy:
        def __getattr__(self, name: str):
            return getattr(orig_k32, name)

        def OpenProcess(self, *args, **kwargs):
            nonlocal open_proc_calls
            open_proc_calls += 1
            return orig_k32.OpenProcess(*args, **kwargs)

    cache_ttl_ok = False
    cache_detail = ""
    try:
        w32.clear_process_info_cache()
        w32.kernel32 = _Kernel32OpenProcessSpy()  # type: ignore[assignment]
        p_first = w32.get_process_info(surf_pid_probe.hwnd)
        for _ in range(4):
            p_next = w32.get_process_info(surf_pid_probe.hwnd)
        calls_after_5 = open_proc_calls

        w32.clear_process_info_cache()
        _ = w32.get_process_info(surf_pid_probe.hwnd)
        calls_after_clear = open_proc_calls

        # Age cached timestamp beyond 5.0s TTL and verify refresh calls OpenProcess once more
        with w32._PROCESS_INFO_CACHE_LOCK:
            if p_first[0] in w32._PROCESS_INFO_CACHE:
                ex_n, fp_n, ts_n = w32._PROCESS_INFO_CACHE[p_first[0]]
                w32._PROCESS_INFO_CACHE[p_first[0]] = (ex_n, fp_n, ts_n - 6.0)
        _ = w32.get_process_info(surf_pid_probe.hwnd)
        calls_after_ttl = open_proc_calls

        cache_ttl_ok = (
            p_first[0] == os.getpid()
            and bool(p_first[1])
            and p_next == p_first
            and calls_after_5 == 1
            and calls_after_clear == 2
            and calls_after_ttl == 3
        )
        cache_detail = f"pid={p_first[0]} exe={p_first[1]!r} calls={calls_after_5}/{calls_after_clear}/{calls_after_ttl}"
    finally:
        w32.kernel32 = orig_k32
        w32.clear_process_info_cache()
        surf_pid_probe.destroy()

    # Verify all 9 new games & GPU/game overlays are auto-excluded in catalog.py and SafetyConfig
    required_new_exes = [
        "GameOverlayUI.exe",
        "LosslessScaling.exe",
        "RadeonSoftware.exe",
        "AMDRSServ.exe",
        "r5apex.exe",
        "Overwatch.exe",
        "League of Legends.exe",
        "TslGame.exe",
        "EscapeFromTarkov.exe",
    ]
    safety_ex_low = {e.lower() for e in PrismConfig().safety.excluded_exes}
    missing_catalog = [exe for exe in required_new_exes if not classify_app(exe).recommended_exclude]
    missing_config = [exe for exe in required_new_exes if exe.lower() not in safety_ex_low]
    new_exes_ok = (not missing_catalog) and (not missing_config)

    check(
        "sprint2_anticheat_hygiene",
        dead_removed_ok and cache_ttl_ok and new_exes_ok,
        f"dead_src={dead_in_src} dead_mod={dead_on_mod} {cache_detail} "
        f"missing_cat={missing_catalog} missing_cfg={missing_config}",
    )

    # 11. Sprint 2 Track 2 Regression Tests (PFX-C08, PFX-H11, PFX-P05)
    from .core.engine import SpotlightState, WobblyTextureUpdate, is_proportional_dpi_scale
    from .render.overlay import (
        ScreenTopologyEntry,
        SpotlightOverlay,
        clamp_to_virtual_screens,
        compute_overscan_padding,
        compute_wobble_padding,
        physical_to_qt_logical,
    )

    # (10a) Mixed-DPI & Negative Virtual Desktop Coords + Cross-Monitor Wobbly DPI Rescale
    dual_topo = [
        ScreenTopologyEntry(
            name="\\\\.\\DISPLAY2",
            phys_x=-1920, phys_y=0, phys_w=1920, phys_h=1080,
            log_x=-1280, log_y=0, log_w=1280, log_h=720,
            avail_x=-1280, avail_y=0, avail_w=1280, avail_h=680,
            dpr=1.5, is_primary=False,
        ),
        ScreenTopologyEntry(
            name="\\\\.\\DISPLAY1",
            phys_x=0, phys_y=0, phys_w=1920, phys_h=1080,
            log_x=0, log_y=0, log_w=1920, log_h=1080,
            avail_x=0, avail_y=0, avail_w=1920, avail_h=1040,
            dpr=1.0, is_primary=True,
        ),
    ]
    neg_origin_log = physical_to_qt_logical(-1920, 0, screens=dual_topo)
    neg_center_log = physical_to_qt_logical(-960, 540, screens=dual_topo)
    prim_center_log = physical_to_qt_logical(960, 540, screens=dual_topo)
    neg_map_ok = (
        neg_origin_log == (-1280, 0)
        and neg_center_log == (-640, 360)
        and prim_center_log == (960, 540)
    )

    kept_neg = clamp_to_virtual_screens(-1200, 40, 800, 600, screens=dual_topo)
    clamped_neg_edge = clamp_to_virtual_screens(-1400, -50, 360, 280, screens=dual_topo)
    clamped_oversize = clamp_to_virtual_screens(-1000, 100, 1400, 800, screens=dual_topo)
    clamped_disconn = clamp_to_virtual_screens(9999, 9999, 1120, 720, screens=dual_topo)
    clamp_ok = (
        kept_neg == (-1200, 40, 800, 600)
        and kept_neg[0] < 0
        and clamped_neg_edge == (-1280, 0, 360, 280)
        and clamped_oversize == (-1280, 0, 1400, 800)
        and 0 <= clamped_disconn[0] <= (1920 - 1120)
        and 0 <= clamped_disconn[1] <= (1040 - 720)
    )

    # Verify PFX-P05 engine cross-monitor Mixed-DPI drag keeps Wobbly alive (no cancel) & emits texture update
    dpi_scale_fn_ok = (
        is_proportional_dpi_scale(800, 600, 1200, 900)
        and is_proportional_dpi_scale(1200, 900, 800, 600)
        and is_proportional_dpi_scale(800, 600, 1600, 1200, crossed_monitor=True)
        and not is_proportional_dpi_scale(960, 1040, 1180, 760)
    )
    dpi_drag_ok = False
    with tempfile.TemporaryDirectory(prefix="prismfx_s2_dpi_") as td_dpi:
        demo_be = DemoWindowBackend(seed=42)
        eng_dpi = PrismEngine(demo_be, ConfigStore(pathlib.Path(td_dpi)), EffectRegistry())
        wob_ends: list[tuple[int, int, int, bool]] = []
        tex_updates: list[WobblyTextureUpdate] = []
        eng_dpi.on_wobbly_end = lambda h, fx, fy, sc: wob_ends.append((h, fx, fy, sc))
        eng_dpi.on_wobbly_texture_update = lambda u: tex_updates.append(u)
        demo_be.move_resize_window(1001, 200, 100, 800, 600)
        eng_dpi._drag_hwnd = 1001
        eng_dpi._drag_wobble_started = True
        eng_dpi._drag_start_rect = (200, 100, 800, 600)
        eng_dpi._drag_last_pos = (200, 100)
        eng_dpi._drag_monitor_key = ("\\\\.\\DISPLAY1", 0, 0, 1920, 1080)
        # Cross onto DISPLAY2 at 1.5x DPI scale (800x600 -> 1200x900)
        demo_be.move_resize_window(1001, 2000, 120, 1200, 900)
        eng_dpi._handle_win_event(w32.EVENT_OBJECT_LOCATIONCHANGE, 1001)
        kept_alive = eng_dpi._drag_wobble_started and eng_dpi._drag_dpi_recapture_pending and not wob_ends
        # Simulate >=2 stable moves after >=38 ms
        eng_dpi._drag_size_changed_at = time.perf_counter() - 0.05
        demo_be.move_resize_window(1001, 2020, 125, 1200, 900)
        eng_dpi._handle_win_event(w32.EVENT_OBJECT_LOCATIONCHANGE, 1001)
        demo_be.move_resize_window(1001, 2040, 130, 1200, 900)
        eng_dpi._handle_win_event(w32.EVENT_OBJECT_LOCATIONCHANGE, 1001)
        dpi_drag_ok = (
            kept_alive
            and not wob_ends
            and eng_dpi._drag_wobble_started
            and not eng_dpi._drag_dpi_recapture_pending
            and len(tex_updates) == 1
            and tex_updates[0].hwnd == 1001
            and tex_updates[0].w == 1200
            and tex_updates[0].h == 900
        )

    check(
        "sprint2_mixed_dpi_and_negative_coords",
        neg_map_ok and clamp_ok and dpi_scale_fn_ok and dpi_drag_ok,
        f"neg_map=({neg_origin_log},{neg_center_log}) kept_neg={kept_neg[:2]} "
        f"disconn={clamped_disconn[:2]} dpi_drag={dpi_drag_ok}",
    )

    # (10b) 1080p Maximized / Ultrawide / 4K Overscan + GL_MAX_TEXTURE_SIZE + Per-Monitor SpotlightOverlay
    res_cases = [
        ("1080p_max", 1920, 1080, w32.MonitorRect("M1", 0, 0, 1920, 1080, 0, 0, 1920, 1040, True)),
        ("ultrawide", 3440, 1440, w32.MonitorRect("M2", 0, 0, 3440, 1440, 0, 0, 3440, 1400, True)),
        ("4k", 3840, 2160, w32.MonitorRect("M3", 0, 0, 3840, 2160, 0, 0, 3840, 2120, True)),
    ]
    overscan_ok = True
    overscan_notes: list[str] = []
    for label, rw, rh, mrect in res_cases:
        px_lg, py_lg = compute_overscan_padding(rw, rh, shader_id=3, monitor_rect=mrect, gl_max_texture_size=16384)
        px_sm, py_sm = compute_overscan_padding(rw, rh, shader_id=1, monitor_rect=mrect, gl_max_texture_size=16384)
        wx, wy = compute_wobble_padding(rw, rh, monitor_rect=mrect, gl_max_texture_size=16384)
        case_ok = (
            px_lg > 0 and py_lg > 0
            and px_sm > 0 and py_sm > 0
            and wx > 0 and wy > 0
            and (rw + px_lg * 2) <= 4096 and (rh + py_lg * 2) <= 4096
            and (rw + px_sm * 2) <= 4096 and (rh + py_sm * 2) <= 4096
            and (rw + wx * 2) <= 4096 and (rh + wy * 2) <= 4096
        )
        if not case_ok:
            overscan_ok = False
        overscan_notes.append(f"{label}:os=({px_lg},{py_lg}) wob=({wx},{wy})")

    max_tex_gpu = OffscreenGpuEngine()
    gl_max_tex = 0
    try:
        if max_tex_gpu.ensure_ready():
            gl_max_tex = max_tex_gpu.max_texture_size()
    finally:
        max_tex_gpu.shutdown()
    gl_max_tex_ok = gl_max_tex >= 2048

    spot_ok = False
    spot_detail = ""
    with tempfile.TemporaryDirectory(prefix="prismfx_s2_spot_") as td_sp:
        eng_sp = PrismEngine(DemoWindowBackend(seed=42), ConfigStore(pathlib.Path(td_sp)), EffectRegistry())
        spot = SpotlightOverlay(eng_sp)
        try:
            spot.apply_state(
                SpotlightState(
                    active=True,
                    mode="halo",
                    opacity=0.45,
                    color_hex="#06080E",
                    active_hwnd=1001,
                    active_rect=(120, 80, 1180, 760),
                    cursor_pos=(500, 400),
                )
            )
            surfs = list(spot._surfaces.values())
            n_surfs = len(surfs)
            all_non_topmost = all(
                s.hwnd > 0
                and (w32.user32.GetWindowLongPtrW(s.hwnd, w32.GWL_EXSTYLE) & w32.WS_EX_TOPMOST) == 0
                for s in surfs
            )
            all_cloaked = all(w32.is_window_cloaked(s.hwnd) for s in surfs)
            vis_before_hide = spot.isVisible() and all(s.is_visible for s in surfs)
            spot.hide()
            vis_after_hide = spot.isVisible() or any(s.is_visible for s in surfs)
            spot_ok = (
                n_surfs == 2
                and all_non_topmost
                and all_cloaked
                and vis_before_hide
                and not vis_after_hide
            )
            spot_detail = (
                f"surfs={n_surfs} non_topmost={all_non_topmost} cloaked={all_cloaked} "
                f"vis_on={vis_before_hide} hidden={not vis_after_hide}"
            )
        finally:
            spot.close()

    check(
        "sprint2_4k_overscan_and_spotlight",
        overscan_ok and gl_max_tex_ok and spot_ok,
        f"{' '.join(overscan_notes)} gl_max_tex={gl_max_tex} {spot_detail}",
    )

    # 12. Sprint 2 Track 3 Regression Tests (PFX-H09, PFX-H10, PFX-M02, PFX-M03, PFX-M04, Board Resolutions #1 & #2)
    from .core.engine import AnimationJob, MAX_CAPTURE_CACHE_BYTES, MorphJob, WobblyDragStart

    with tempfile.TemporaryDirectory(prefix="prismfx_s2_t3_") as td_s2:
        s2_dir = pathlib.Path(td_s2)

        # (10a) sprint2_idle_cpu_and_target_fps
        s2_store = ConfigStore(s2_dir / "idle_profile")
        s2_reg = EffectRegistry()
        s2_eng = PrismEngine(DemoWindowBackend(seed=42), s2_store, s2_reg)
        ATLAS.initialize()
        s2_win = MainWindow(s2_eng, enable_overlays=False, enable_tray=False)
        s2_ov = None
        try:
            _cloak_and_show_silently(s2_win)
            QtWidgets.QApplication.sendEvent(s2_win, QtCore.QEvent(QtCore.QEvent.WindowDeactivate))
            # Let initial static paint & async startup query finish before measuring steady-state idle CPU
            settle_loop = QtCore.QEventLoop()
            QtCore.QTimer.singleShot(80, settle_loop.quit)
            settle_loop.exec()

            unfocused_paused_before = (
                not s2_win.home_stage._timer.isActive()
                and not s2_win.lab_stage._timer.isActive()
                and not s2_win.wobble_stage._timer.isActive()
            )
            cpu_t0 = time.process_time()
            wall_t0 = time.perf_counter()
            idle_loop = QtCore.QEventLoop()
            QtCore.QTimer.singleShot(820, idle_loop.quit)
            idle_loop.exec()
            wall_dt = max(0.001, time.perf_counter() - wall_t0)
            idle_cpu_pct = 100.0 * (time.process_time() - cpu_t0) / wall_dt
            unfocused_paused_after = not s2_win.home_stage._timer.isActive()

            # Wake immediately on hover/interaction
            enter_ev = QtGui.QEnterEvent(
                QtCore.QPointF(24.0, 24.0), QtCore.QPointF(24.0, 24.0), QtCore.QPointF(24.0, 24.0)
            )
            QtWidgets.QApplication.sendEvent(s2_win.home_stage, enter_ev)
            wake_on_hover_ok = s2_win.home_stage._timer.isActive()

            # 15s idle timeout pauses clock in _on_tick()
            s2_win.home_stage._last_interaction_ts = time.perf_counter() - (s2_win.home_stage._idle_timeout_sec + 1.0)
            s2_win.home_stage._on_tick()
            idle_timeout_ok = not s2_win.home_stage._timer.isActive()

            # target_fps binding on ShaderPreviewWidget & DesktopFxOverlay
            s2_ov = DesktopFxOverlay(s2_eng)
            ov_h = s2_ov.primary_surface_hwnd()
            if ov_h:
                w32.set_window_cloaked(ov_h, True)
            s2_win.home_stage.set_target_fps(60)
            pw_iv_60 = s2_win.home_stage._timer.interval()
            s2_win.home_stage.set_target_fps(240)
            pw_iv_240 = s2_win.home_stage._timer.interval()
            s2_ov.set_target_fps(60)
            ov_iv_60 = s2_ov._timer.interval()
            s2_ov.set_target_fps(240)
            ov_iv_240 = s2_ov._timer.interval()
            fps_bind_ok = (
                pw_iv_60 == 17
                and pw_iv_240 == 4
                and ov_iv_60 == 17
                and ov_iv_240 == 4
                and pw_iv_60 > pw_iv_240
                and ov_iv_60 > ov_iv_240
            )
            check(
                "sprint2_idle_cpu_and_target_fps",
                unfocused_paused_before
                and unfocused_paused_after
                and idle_cpu_pct < 2.0
                and wake_on_hover_ok
                and idle_timeout_ok
                and fps_bind_ok,
                f"paused={unfocused_paused_before and unfocused_paused_after} cpu={idle_cpu_pct:.2f}% "
                f"wake={wake_on_hover_ok} idle_15s={idle_timeout_ok} iv60={pw_iv_60}/{ov_iv_60} iv240={pw_iv_240}/{ov_iv_240}",
            )
        finally:
            if s2_ov is not None:
                s2_ov.shutdown()
            s2_win.full_shutdown()
            OffscreenGpuEngine.shared().reset_init_attempt()

        # (10b) sprint2_two_tier_journal_and_dwm_coalesce
        j2_path = s2_dir / "two_tier" / "recovery_journal.json"
        j2_tier = RecoveryJournal(j2_path)
        try:
            j2_tier.record_alpha(5001, 255, had_layered=False, sync=False)
            in_mem_immediate = (j2_tier.get_mutation(5001) is not None) and (j2_tier.pending_count() == 1)
            j2_tier.record_alpha(5002, 220, had_layered=False, sync=False)
            coalesced_ok = False
            deadline_j = time.perf_counter() + 0.5
            while time.perf_counter() < deadline_j:
                if j2_path.is_file():
                    reloaded_j = RecoveryJournal(j2_path)
                    if reloaded_j.get_mutation(5001) is not None and reloaded_j.get_mutation(5002) is not None:
                        coalesced_ok = True
                        break
                time.sleep(0.015)

            j2_tier.record_alpha(5003, 180, had_layered=False, sync=False)
            j2_tier.flush_sync()
            flush_sync_ok = RecoveryJournal(j2_path).get_mutation(5003) is not None
        finally:
            j2_tier.close()

        dwm_backend = DemoWindowBackend(seed=42)
        dwm_flush_calls = 0

        def _counted_dwm_flush() -> None:
            nonlocal dwm_flush_calls
            dwm_flush_calls += 1

        dwm_backend.dwm_flush = _counted_dwm_flush  # type: ignore[method-assign]
        dwm_store = ConfigStore(s2_dir / "dwm_profile")
        dwm_eng = PrismEngine(dwm_backend, dwm_store, EffectRegistry())
        dwm_ov = DesktopFxOverlay(dwm_eng)
        try:
            for s in dwm_ov._surface_pool:
                if s.hwnd:
                    w32.set_window_cloaked(s.hwnd, True)
            raw_64 = bytes([255, 129, 45, 255]) * (64 * 64)
            now_t = time.perf_counter()
            for idx, h_id in enumerate((1001, 1002), start=1):
                dwm_eng.animation_queue.put_nowait(
                    AnimationJob(
                        job_id=idx,
                        hwnd=h_id,
                        trigger="open",
                        effect_id="clean_fade",
                        shader_id=37,
                        reverse=True,
                        duration_ms=100,
                        easing="linear",
                        intensity=1.0,
                        accent_hex="#2D81FF",
                        x=100 + idx * 20,
                        y=100,
                        w=64,
                        h=64,
                        img_w=64,
                        img_h=64,
                        bgra_bytes=raw_64,
                        restore_hidden_hwnd=h_id,
                        restore_alpha=255,
                        started_at=now_t,
                    )
                )
            dwm_ov._drain_jobs()
            dwm_ov._on_morph_queued(
                MorphJob(
                    hwnd=1003,
                    from_rect=(120, 120, 64, 64),
                    to_rect=(140, 140, 96, 96),
                    from_img_w=64,
                    from_img_h=64,
                    from_bgra=raw_64,
                    to_img_w=64,
                    to_img_h=64,
                    to_bgra=raw_64,
                    duration_ms=100,
                    restore_hidden_hwnd=1003,
                    restore_alpha=255,
                    started_at=now_t,
                )
            )
            for item in dwm_ov._active:
                if item.surface.hwnd:
                    w32.set_window_cloaked(item.surface.hwnd, True)
                item.start_time = now_t - 1.0
            for m_item in dwm_ov._morphs:
                if m_item.surface.hwnd:
                    w32.set_window_cloaked(m_item.surface.hwnd, True)
                m_item.start_time = now_t - 1.0
            staged_counts_ok = len(dwm_ov._active) == 2 and len(dwm_ov._morphs) == 1
            dwm_flush_calls = 0
            dwm_ov._tick()
            coalesced_dwm_ok = (
                staged_counts_ok
                and len(dwm_ov._active) == 0
                and len(dwm_ov._morphs) == 0
                and dwm_flush_calls == 1
            )
        finally:
            dwm_ov.shutdown()
            dwm_eng.journal.close()

        check(
            "sprint2_two_tier_journal_and_dwm_coalesce",
            in_mem_immediate and coalesced_ok and flush_sync_ok and coalesced_dwm_ok,
            f"in_mem={in_mem_immediate} bg_coalesced={coalesced_ok} flush_sync={flush_sync_ok} dwm_flushes={dwm_flush_calls}",
        )

        # (10c) sprint2_capture_budget_and_quick_fixes
        qf_store = ConfigStore(s2_dir / "quickfix_profile")
        qf_eng = PrismEngine(DemoWindowBackend(seed=42), qf_store, EffectRegistry())
        try:
            # Capture cache <= 64 MB budget
            chunk_10mb = b"\x20" * (10 * 1024 * 1024)
            for idx in range(10):
                qf_eng._store_cached_capture(
                    9000 + idx, 1024, 2560, chunk_10mb, (0, 0, 1024, 2560), float(idx + 1)
                )
            # Single > 64 MB capture must be rejected
            oversize_65mb = b"\x20" * (65 * 1024 * 1024)
            qf_eng._store_cached_capture(9999, 4096, 4160, oversize_65mb, (0, 0, 4096, 4160), 999.0)
            cache_bytes = qf_eng.cached_capture_bytes()
            budget_ok = (
                0 < cache_bytes <= MAX_CAPTURE_CACHE_BYTES
                and 9999 not in qf_eng._cached_captures
                and 9009 in qf_eng._cached_captures
                and 9000 not in qf_eng._cached_captures
            )

            # PFX-M02: menu & popup triggers produce reverse == True
            j_menu = qf_eng.trigger_effect_for_window(1001, "menu")
            j_popup = qf_eng.trigger_effect_for_window(1002, "popup")
            m02_ok = (
                j_menu is not None
                and j_menu.reverse is True
                and j_popup is not None
                and j_popup.reverse is True
            )

            # PFX-M03: _on_wobbly_end(..., snap_cancel=True) must NOT stop overlay._timer while _morphs is non-empty
            m03_ov = DesktopFxOverlay(qf_eng)
            m03_ok = False
            try:
                for s in m03_ov._surface_pool:
                    if s.hwnd:
                        w32.set_window_cloaked(s.hwnd, True)
                raw_64 = bytes([255, 129, 45, 255]) * (64 * 64)
                m03_ov._on_wobbly_start(
                    WobblyDragStart(
                        hwnd=1001, x=100, y=100, w=64, h=64,
                        img_w=64, img_h=64, bgra_bytes=raw_64,
                        grab_u=0.5, grab_v=0.1, orig_alpha=255, init_dx=8, init_dy=0,
                    )
                )
                if m03_ov._wobble is not None and m03_ov._wobble.surface.hwnd:
                    w32.set_window_cloaked(m03_ov._wobble.surface.hwnd, True)
                m03_ov._on_morph_queued(
                    MorphJob(
                        hwnd=1002,
                        from_rect=(120, 120, 64, 64),
                        to_rect=(160, 160, 96, 96),
                        from_img_w=64,
                        from_img_h=64,
                        from_bgra=raw_64,
                        to_img_w=64,
                        to_img_h=64,
                        to_bgra=raw_64,
                        duration_ms=200,
                        restore_hidden_hwnd=0,
                        restore_alpha=255,
                    )
                )
                for m_item in m03_ov._morphs:
                    if m_item.surface.hwnd:
                        w32.set_window_cloaked(m_item.surface.hwnd, True)
                m03_ov._on_wobbly_end(1001, 100, 100, True)
                m03_ok = (
                    m03_ov._wobble is None
                    and len(m03_ov._morphs) == 1
                    and m03_ov._timer.isActive()
                )
            finally:
                m03_ov.shutdown()
        finally:
            qf_eng.journal.close()

        # PFX-M04: Win32 failure (False) must NEVER leave fake pending journal entries + Virtual Desktop cloak guard
        class _FailWin32Backend(DemoWindowBackend):
            @property
            def is_demo(self) -> bool:
                return False

            def set_window_topmost(self, hwnd: int, topmost: bool) -> bool:
                return False

            def move_resize_window(self, hwnd: int, x: int, y: int, w: int, h: int) -> bool:
                return False

            def set_desktop_icons_visible(self, visible: bool) -> bool:
                return False

            def set_window_opacity(self, hwnd: int, alpha: int, had_layered=None) -> bool:
                return False

        m04_store = ConfigStore(s2_dir / "m04_profile")
        m04_cfg = m04_store.load()
        m04_cfg.focus.inactive_fade_enabled = True
        m04_cfg.focus.desktop_icons_autohide = True
        m04_store.save(m04_cfg)
        m04_eng = PrismEngine(_FailWin32Backend(seed=42), m04_store, EffectRegistry())
        try:
            m04_eng.toggle_pin_window(1001)
            m04_eng.toggle_rollup_window(1001)
            m04_eng.toggle_desktop_icons()
            m04_eng.set_custom_opacity(1001, 70)
            m04_eng._housekeeping_tick(time.perf_counter())
            m04_fail_journal_ok = (
                m04_eng.journal.pending_count() == 0
                and len(m04_eng._pinned) == 0
                and len(m04_eng._rolled_up) == 0
                and len(m04_eng._faded_hwnds) == 0
            )

            # Virtual Desktop single-window cloak -> uncloak (>100ms apart) must NOT replay Open
            vd_hwnd = 777701
            stage_calls: list[tuple[int, str]] = []
            m04_eng._stage_open_window = lambda h, trig="open": (stage_calls.append((h, trig)) or True)  # type: ignore[method-assign]
            with m04_eng._state_lock:
                m04_eng._known_visible_hwnds.add(vd_hwnd)
            m04_eng._handle_win_event(w32.EVENT_OBJECT_CLOAKED, vd_hwnd)
            cloaked_recorded = vd_hwnd in m04_eng._cloaked_hwnds
            m04_eng._last_cloak_ts -= 0.50  # simulate >500 ms between cloak and uncloak
            m04_eng._handle_win_event(w32.EVENT_OBJECT_UNCLOAKED, vd_hwnd)
            m04_vd_ok = (
                cloaked_recorded
                and len(stage_calls) == 0
                and vd_hwnd not in m04_eng._cloaked_hwnds
                and vd_hwnd in m04_eng._known_visible_hwnds
            )
        finally:
            m04_eng.journal.close()

        check(
            "sprint2_capture_budget_and_quick_fixes",
            budget_ok and m02_ok and m03_ok and m04_fail_journal_ok and m04_vd_ok,
            f"cache_mb={cache_bytes / (1024 * 1024):.1f} m02={m02_ok} m03={m03_ok} "
            f"m04_journal={m04_fail_journal_ok} m04_vd={m04_vd_ok}",
        )

        # (10d) Live Win32 Hook & RawInput Thread Startup/Shutdown Regression (unhashable ctypes callback fix)
        from .core.backend import Win32WindowBackend
        from .core.engine import _ACTIVE_NATIVE_CALLBACKS, _ACTIVE_NATIVE_CALLBACKS_LOCK

        live_store = ConfigStore(s2_dir / "live_threads_profile")
        live_cfg = live_store.load()
        live_cfg.safety.suppress_native_min_anim = False
        live_cfg.safety.pause_on_fullscreen_game = False
        live_cfg.safety.pause_on_battery = False
        live_store.save(live_cfg)
        live_eng = PrismEngine(Win32WindowBackend(), live_store, EffectRegistry())
        live_threads_ok = False
        live_detail = ""
        try:
            with _ACTIVE_NATIVE_CALLBACKS_LOCK:
                cb_before = len(_ACTIVE_NATIVE_CALLBACKS)
            live_eng.start()
            wait_deadline = time.perf_counter() + 1.0
            while time.perf_counter() < wait_deadline:
                if live_eng._hook_tid and live_eng._raw_input_tid and len(live_eng._win_event_hooks) == 6 and live_eng._raw_input_registered:
                    break
                time.sleep(0.02)
            threads_alive = all(
                t is not None and t.is_alive()
                for t in (
                    live_eng._poll_thread,
                    live_eng._open_stager_thread,
                    live_eng._hook_thread,
                    live_eng._raw_input_thread,
                )
            )
            hooks_installed = len(live_eng._win_event_hooks) == 6
            raw_ready = (live_eng._raw_input_hwnd > 0) and live_eng._raw_input_registered
            with _ACTIVE_NATIVE_CALLBACKS_LOCK:
                cb_during = len(_ACTIVE_NATIVE_CALLBACKS)
            live_eng.stop()
            threads_stopped = all(
                t is None or (not t.is_alive())
                for t in (
                    live_eng._poll_thread,
                    live_eng._open_stager_thread,
                    live_eng._hook_thread,
                    live_eng._raw_input_thread,
                )
            )
            with _ACTIVE_NATIVE_CALLBACKS_LOCK:
                cb_after = len(_ACTIVE_NATIVE_CALLBACKS)
            live_threads_ok = (
                threads_alive
                and hooks_installed
                and raw_ready
                and (cb_during >= cb_before + 2)
                and threads_stopped
                and (len(live_eng._win_event_hooks) == 0)
                and (not live_eng._raw_input_registered)
                and (cb_after == cb_before)
            )
            live_detail = (
                f"alive={threads_alive} hooks={hooks_installed} raw={raw_ready} "
                f"cbs={cb_before}->{cb_during}->{cb_after} stopped={threads_stopped}"
            )
        finally:
            live_eng.stop()
            live_eng.journal.close()
        check("sprint2_live_hook_and_rawinput_threads", live_threads_ok, live_detail)

        # (10e) Borderless Fullscreen False-Positive Guards (NVIDIA Overlay / Maximized Frameless / Shell)
        mon_autohide = w32.MonitorRect("\\\\.\\DISPLAY1", 0, 0, 1920, 1080, 0, 0, 1920, 1080, True)
        frameless_max_style = w32.WS_VISIBLE | w32.WS_POPUP | w32.WS_MAXIMIZE
        overlay_exes = (
            "NVIDIA Overlay.exe",
            "NVIDIA Share.exe",
            "Share.exe",
            "GameBar.exe",
            "RadeonSoftware.exe",
            "LosslessScaling.exe",
            "Overwolf.exe",
            "obs64.exe",
        )
        overlays_rejected = all(
            not w32.is_borderless_fullscreen_candidate(
                "Chrome_WidgetWin_1", ov_exe, borderless_style, 0, (0, 0, 1920, 1080), mon_autohide
            )
            for ov_exe in overlay_exes
        )
        overlay_cat_ok = (
            classify_app("Share.exe").category == "GPU Overlay"
            and classify_app("Share.exe").recommended_exclude is True
            and classify_app("Overwolf.exe").category == "GPU Overlay"
            and classify_app("Overwolf.exe").recommended_exclude is True
        )
        maximized_frameless_rejected = (
            not w32.is_borderless_fullscreen_candidate(
                "Chrome_WidgetWin_1", "Antigravity.exe", frameless_max_style, 0, (0, 0, 1920, 1080), mon_autohide, is_zoomed=True
            )
            and not w32.is_borderless_fullscreen_candidate(
                "Qt6120QWindowIcon", "CustomFramelessApp.exe", borderless_style, 0, (0, 0, 1920, 1080), mon_autohide, is_zoomed=True
            )
            and not w32.is_borderless_fullscreen_candidate(
                "OverlayHUD", "CustomOverlay.exe", borderless_style, w32.WS_EX_LAYERED, (0, 0, 1920, 1080), mon_autohide
            )
            and not w32.is_borderless_fullscreen_candidate(
                "ApplicationFrameWindow", "ApplicationFrameHost.exe", borderless_style, 0, (0, 0, 1920, 1080), mon_autohide
            )
            and not w32.is_borderless_fullscreen_candidate(
                "CabinetWClass", "explorer.exe", borderless_style, 0, (0, 0, 1920, 1080), mon_autohide
            )
        )
        real_games_still_detected = (
            w32.is_borderless_fullscreen_candidate(
                "UnrealWindow", "SomeDX12Game.exe", borderless_style, 0, (0, 0, 1920, 1080), mon_autohide, is_zoomed=False
            )
            and w32.is_borderless_fullscreen_candidate(
                "UnrealWindow", "VALORANT-Win64-Shipping.exe", frameless_max_style, 0, (0, 0, 1920, 1080), mon_autohide, is_zoomed=True
            )
        )
        check(
            "sprint2_borderless_false_positive_guards",
            overlays_rejected and overlay_cat_ok and maximized_frameless_rejected and real_games_still_detected,
            f"ov_rej={overlays_rejected} ov_cat={overlay_cat_ok} max_fl_rej={maximized_frameless_rejected} "
            f"games_ok={real_games_still_detected}",
        )

        # (10f) UWP Cloak/Uncloak Guard (IME/CoreWindow non-poisoning) + Overlay SW_HIDE Resume Playback
        uwp_store = ConfigStore(s2_dir / "uwp_resume_profile")
        uwp_eng = PrismEngine(DemoWindowBackend(seed=42), uwp_store, EffectRegistry())
        dummy_cap_w = QtWidgets.QWidget()
        dummy_cap_w.setWindowTitle("UWPSettingsProbe")
        dummy_cap_w.resize(640, 420)
        dummy_hwnd = int(dummy_cap_w.winId())
        w32.set_window_cloaked(dummy_hwnd, True)
        uwp_ov = DesktopFxOverlay(uwp_eng)
        uwp_uncloak_ok = False
        ov_resume_ok = False
        uwp_detail = ""
        orig_get_cls = w32.get_class_name
        try:
            for s in uwp_ov._surface_pool:
                if s.hwnd:
                    w32.set_window_cloaked(s.hwnd, True)

            staged_opens: list[tuple[int, str]] = []
            uwp_eng._stage_open_window = lambda h, trig="open": (staged_opens.append((h, trig)) or True)  # type: ignore[method-assign]
            uwp_eng._last_cloak_ts = time.perf_counter() - 5.0
            base_cloak_ts = uwp_eng._last_cloak_ts

            # Protected/unknown IME & CoreWindow cloaks/uncloaks must NOT poison _last_cloak_ts or _recent_uncloak_times
            ime_hwnd, msctf_hwnd, core_hwnd = 888001, 888002, 888003
            cls_overrides = {
                ime_hwnd: "IME",
                msctf_hwnd: "MSCTFIME UI",
                core_hwnd: "Windows.UI.Core.CoreWindow",
                dummy_hwnd: "ApplicationFrameWindow",
            }
            w32.get_class_name = lambda h: cls_overrides.get(int(h), orig_get_cls(h))  # type: ignore[assignment]

            uwp_eng._handle_win_event(w32.EVENT_OBJECT_CLOAKED, ime_hwnd)
            uwp_eng._handle_win_event(w32.EVENT_OBJECT_CLOAKED, msctf_hwnd)
            uwp_eng._handle_win_event(w32.EVENT_OBJECT_CLOAKED, core_hwnd)
            cloak_ts_clean = (uwp_eng._last_cloak_ts == base_cloak_ts)

            uwp_eng._handle_win_event(w32.EVENT_OBJECT_UNCLOAKED, ime_hwnd)
            uwp_eng._handle_win_event(w32.EVENT_OBJECT_UNCLOAKED, msctf_hwnd)
            uwp_eng._handle_win_event(w32.EVENT_OBJECT_UNCLOAKED, core_hwnd)
            burst_clean = (len(uwp_eng._recent_uncloak_times) == 0)

            # Real UWP ApplicationFrameWindow uncloak immediately after IME cloaks MUST stage Open!
            uwp_eng._handle_win_event(w32.EVENT_OBJECT_UNCLOAKED, dummy_hwnd)
            uwp_opened_once = (staged_opens == [(dummy_hwnd, "open")])

            # Close via CLOAK -> HIDE (without DESTROY) must clear _cloaked_hwnds so reused UWP HWND can reopen!
            with uwp_eng._state_lock:
                uwp_eng._known_visible_hwnds.add(dummy_hwnd)
            uwp_eng._handle_win_event(w32.EVENT_OBJECT_CLOAKED, dummy_hwnd)
            uwp_eng._handle_win_event(w32.EVENT_OBJECT_HIDE, dummy_hwnd)
            cleared_on_hide = (dummy_hwnd not in uwp_eng._cloaked_hwnds)
            uwp_eng._last_cloak_ts = time.perf_counter() - 1.0
            uwp_eng._recent_uncloak_times.clear()
            uwp_eng._handle_win_event(w32.EVENT_OBJECT_UNCLOAKED, dummy_hwnd)
            reused_uwp_opened = (len(staged_opens) == 2 and staged_opens[-1] == (dummy_hwnd, "open"))
            uwp_uncloak_ok = cloak_ts_clean and burst_clean and uwp_opened_once and cleared_on_hide and reused_uwp_opened

            # Overlay SW_HIDE (fullscreen_game pause) -> Resume -> Open/Minimize/Restore/Wobbly rendering recovery
            uwp_ov.set_fullscreen_game_paused(True)
            all_hidden_paused = bool(uwp_ov._surface_pool) and all(
                (not s.is_visible) and (not s.is_parked) for s in uwp_ov._surface_pool
            )
            uwp_ov.set_fullscreen_game_paused(False)

            raw_128 = bytes([255, 129, 45, 255]) * (128 * 96)
            now_r = time.perf_counter()
            uwp_eng.animation_queue.put_nowait(
                AnimationJob(
                    job_id=91,
                    hwnd=1001,
                    trigger="minimize",
                    effect_id="genie_lamp",
                    shader_id=6,
                    reverse=False,
                    duration_ms=120,
                    easing="out_cubic",
                    intensity=1.0,
                    accent_hex="#2D81FF",
                    x=100,
                    y=100,
                    w=128,
                    h=96,
                    img_w=128,
                    img_h=96,
                    bgra_bytes=raw_128,
                    started_at=now_r,
                )
            )
            uwp_ov._drain_jobs()
            for it in uwp_ov._active:
                if it.surface.hwnd:
                    w32.set_window_cloaked(it.surface.hwnd, True)
            anim_active_after_resume = (
                len(uwp_ov._active) == 1
                and uwp_ov._active[0].surface.is_visible
                and uwp_ov._timer.isActive()
            )
            uwp_ov._on_wobbly_start(
                WobblyDragStart(
                    hwnd=1002, x=220, y=140, w=128, h=96,
                    img_w=128, img_h=96, bgra_bytes=raw_128,
                    grab_u=0.5, grab_v=0.1, orig_alpha=255, init_dx=12, init_dy=4,
                )
            )
            if uwp_ov._wobble is not None and uwp_ov._wobble.surface.hwnd:
                w32.set_window_cloaked(uwp_ov._wobble.surface.hwnd, True)
            wob_active_after_resume = (
                uwp_ov._wobble is not None and uwp_ov._wobble.surface.is_visible
            )
            uwp_ov._tick()
            # Finish cleanly
            for it in uwp_ov._active:
                it.start_time = now_r - 1.0
            uwp_ov._on_wobbly_end(1002, 240, 150, True)
            uwp_ov._tick()
            clean_finish = (
                len(uwp_ov._active) == 0
                and uwp_ov._wobble is None
                and (not uwp_ov._timer.isActive())
            )
            ov_resume_ok = all_hidden_paused and anim_active_after_resume and wob_active_after_resume and clean_finish
            uwp_detail = (
                f"cloak_clean={cloak_ts_clean} burst_clean={burst_clean} uwp_open={uwp_opened_once} "
                f"reused={reused_uwp_opened} hidden={all_hidden_paused} anim_res={anim_active_after_resume} "
                f"wob_res={wob_active_after_resume} finish={clean_finish}"
            )
        finally:
            w32.get_class_name = orig_get_cls  # type: ignore[assignment]
            uwp_ov.shutdown()
            uwp_eng.journal.close()
            dummy_cap_w.close()
            dummy_cap_w.deleteLater()
        check("sprint2_uwp_uncloak_and_overlay_resume", uwp_uncloak_ok and ov_resume_ok, uwp_detail)

        OffscreenGpuEngine.shared().shutdown()
        OffscreenGpuEngine.shared().reset_init_attempt()

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

    # Hit-test titlebar controls & all 5 sidebar navigation buttons + sidebar startup controls
    verify_hit(win.master_toggle, "master_toggle")
    verify_hit(win.btn_lang, "lang_switch")
    verify_hit(win.btn_quick_ctrl, "quick_ctrl")
    verify_hit(win.chk_side_autostart, "side_autostart")
    verify_hit(win.btn_side_admin, "side_admin")
    for k, btn in win._nav_buttons.items():
        verify_hit(btn, f"nav_{k}")

    # Navigate across all 5 pages & verify active page + inactive clock pausing
    for k in ("home", "animations", "physics", "focus", "apps"):
        win._nav_buttons[k].click()
        QtCore.QCoreApplication.processEvents()
        check(f"navigate_{k}", win._active_page == k and win._stack.currentIndex() == win._page_map[k])

    # Hit-test Startup & Privilege controls on Page 05 (Apps & Safety)
    verify_hit(win.chk_autostart, "page_autostart")
    verify_hit(win.chk_autostart_min, "page_autostart_min")
    verify_hit(win.chk_autostart_admin, "page_autostart_admin")
    verify_hit(win.btn_elevate_admin, "page_elevate_admin")

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
        and gpu.render_to_dib_ptr(
            128, 128, (0, 0, 128, 128), 37, 0.5, 0.5, 0.5,
            dib_ptr=dib_ptr, texture=tex, dib_byte_size=surf.dib_byte_size,
        )
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

    # Verify live 165 Hz GPU Wobbly Windows overlay lifecycle (start -> drag deform -> Mixed-DPI swap -> settle)!
    from .core.engine import WobblyTextureUpdate
    wreq = WobblyDragStart(
        hwnd=88888, x=200, y=180, w=128, h=128,
        img_w=128, img_h=128, bgra_bytes=gdi_zero_alpha_buf,
        grab_u=0.5, grab_v=0.08, orig_alpha=255, init_dx=24, init_dy=-10,
    )
    engine._wobble_live_pos = (88888, 200, 180, 128, 128)
    overlay._on_wobbly_start(wreq)
    wob_started = overlay._wobble is not None and overlay._wobble.hwnd == 88888
    peak_wob = 0.0
    dpi_swap_ok = False
    if wob_started and overlay._wobble is not None:
        w32.set_window_cloaked(overlay._wobble.surface.hwnd, True)
        engine._wobble_live_pos = (88888, 280, 140, 128, 128)
        overlay._last_tick = time.perf_counter() - 0.006
        overlay._tick()
        if overlay._wobble is not None:
            peak_wob = max(abs(v) for v in overlay._wobble.mesh.flat_offsets())
        # Simulate proportional Mixed-DPI cross-monitor resize (128x128 -> 192x192) & seamless texture swap
        engine._wobble_live_pos = (88888, 2000, 140, 192, 192)
        overlay._last_tick = time.perf_counter() - 0.006
        overlay._tick()
        resized_alive = (
            overlay._wobble is not None
            and overlay._wobble.w == 192
            and overlay._wobble.h == 192
        )
        new_dpi_buf = bytes([80, 200, 255, 255]) * (192 * 192)
        old_tex_id = id(overlay._wobble.texture) if overlay._wobble is not None else 0
        overlay._on_wobbly_texture_update(WobblyTextureUpdate(88888, 192, 192, 192, 192, new_dpi_buf))
        dpi_swap_ok = (
            resized_alive
            and overlay._wobble is not None
            and id(overlay._wobble.texture) != old_tex_id
        )
        overlay._on_wobbly_end(88888, 2000, 140, False)
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
        and engine.on_wobbly_texture_update is not None
        and wob_started
        and peak_wob > 0.03
        and dpi_swap_ok
        and wob_settled,
        f"started={wob_started} peak_disp={peak_wob:.4f} dpi_swap={dpi_swap_ok} settled={wob_settled}",
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
    tex = QtOpenGL.QOpenGLTexture(mock_img, QtOpenGL.QOpenGLTexture.DontGenerateMipMaps)
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
