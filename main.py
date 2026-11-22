"""PrismFX Studio — Entry point with isolated profiles, single-instance IPC, crash-safe cleanup & verification CLI."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import pathlib
import signal
import sys
import threading
import time

from PySide6 import QtCore, QtGui, QtWidgets

from prismfx import __app_name__, __version__
from prismfx.core import win32_api as w32
from prismfx.core.backend import DemoWindowBackend, Win32WindowBackend
from prismfx.core.config import ConfigStore
from prismfx.core.effects import EffectRegistry
from prismfx.core.engine import PrismEngine
from prismfx.core.logging_setup import close_logging, configure_logging, get_logger
from prismfx.render.gl_renderer import make_gl_format
from prismfx.ui.main_window import MainWindow
from prismfx.ui.theme import ATLAS, build_stylesheet

_log = get_logger("prismfx.main")


def default_data_dir() -> pathlib.Path:
    env_dir = os.environ.get("PRISMFX_DATA_DIR")
    if env_dir:
        return pathlib.Path(env_dir)
    base = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home())
    return base / "PrismFX"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="PrismFX", description="165 Hz GPU Windows 11 Desktop Motion & Window FX Studio")
    p.add_argument("--profile", type=str, default="", help="Isolated profile directory for config, presets & mutex")
    p.add_argument("--demo", action="store_true", help="Run with deterministic seeded DemoWindowBackend (zero OS side effects)")
    p.add_argument("--minimized", action="store_true", help="Start minimized to system tray")
    p.add_argument("--autostart", action="store_true", help="Launched automatically by Windows startup (respects autostart_minimized)")
    p.add_argument("--replace", action="store_true", help="Gracefully shut down any running instance for this profile and take over")
    p.add_argument("--quit", action="store_true", help="Gracefully shut down any running instance for this profile and exit")
    p.add_argument("--restore", action="store_true", help="Run emergency crash-journal recovery and exit")
    p.add_argument(
        "--install-elevated-task",
        action="store_true",
        help="Install elevated Task Scheduler autostart (requires admin & protected install path)",
    )
    p.add_argument("--selftest", action="store_true", help="Run automated core, physics, recovery & 42-shader GPU tests")
    p.add_argument("--uitest", action="store_true", help="Run non-intrusive automated UI hit-test & navigation verification")
    p.add_argument("--overlay-test", action="store_true", help="Verify click-through overlay Win32 flags & alpha compositing")
    p.add_argument("--perf-log", nargs="?", const="1.2", type=float, help="Benchmark live GPU frame pacing (seconds)")
    p.add_argument("--snapshot", type=str, default="", help="Capture non-intrusive screenshots of all pages & shaders to DIR")
    return p.parse_args(argv)


def _signal_existing_instance(title_exact: str, ipc_msg: int, cmd: int) -> bool:
    targets = w32.find_instance_windows(title_exact, exclude_pid=os.getpid())
    if not targets:
        fb = int(w32.user32.FindWindowW(None, title_exact) or 0)
        if fb:
            targets = [fb]
    if not targets:
        return False
    for existing in targets:
        if ipc_msg:
            w32.user32.PostMessageW(existing, ipc_msg, cmd, 0)
        elif cmd == w32.IPC_CMD_ACTIVATE:
            w32.user32.ShowWindow(existing, w32.SW_RESTORE)
            w32.user32.SetForegroundWindow(existing)
    return True


def _resolve_app_dirs() -> tuple[pathlib.Path, pathlib.Path]:
    """Return `(exe_dir, bundle_dir)` supporting both source checkout and PyInstaller frozen EXE."""
    if getattr(sys, "frozen", False):
        exe_dir = pathlib.Path(sys.executable).resolve().parent
        bundle_dir = pathlib.Path(getattr(sys, "_MEIPASS", exe_dir)).resolve()
        return (exe_dir, bundle_dir)
    src_dir = pathlib.Path(__file__).resolve().parent
    return (src_dir, src_dir)


def main(argv: list[str] | None = None) -> int:
    w32.harden_dll_search_order()
    args = parse_args(argv if argv is not None else sys.argv[1:])
    exe_dir, bundle_dir = _resolve_app_dirs()

    # Self-elevated helper mode for Task Scheduler registration (eliminates %TEMP% XML UAC TOCTOU race)
    if args.install_elevated_task:
        if not w32.is_process_elevated():
            print("PrismFX: --install-elevated-task requires Administrator privileges.", file=sys.stderr)
            return 1
        from prismfx.core import startup as win_startup
        ok, status, msg = win_startup.install_elevated_task_direct(exe_dir)
        if not ok:
            print(f"PrismFX: elevated task install failed ({status}): {msg}", file=sys.stderr)
            return 1
        return 0

    profile_dir = pathlib.Path(args.profile).resolve() if args.profile else default_data_dir().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(profile_dir)
    atexit.register(close_logging)

    # Set default OpenGL 3.3 Core surface format BEFORE constructing QApplication
    QtGui.QSurfaceFormat.setDefaultFormat(make_gl_format())
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts, True)

    # Verification CLI modes (Phase 4)
    if args.selftest:
        from prismfx.harness import run_selftest
        res = run_selftest()
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["passed"] else 1

    if args.uitest:
        from prismfx.harness import run_uitest
        res = run_uitest(profile_dir)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["passed"] else 1

    if args.overlay_test:
        from prismfx.harness import run_overlay_test
        res = run_overlay_test(profile_dir)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["passed"] else 1

    if args.perf_log is not None:
        from prismfx.harness import run_perf_log
        res = run_perf_log(profile_dir, duration_sec=max(0.4, float(args.perf_log)))
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.snapshot:
        from prismfx.harness import run_snapshots
        files = run_snapshots(profile_dir, pathlib.Path(args.snapshot).resolve())
        print(json.dumps({"snapshots": files}, indent=2, ensure_ascii=False))
        return 0

    # Emergency restore mode
    if args.restore:
        store = ConfigStore(profile_dir)
        engine = PrismEngine(Win32WindowBackend(), store, EffectRegistry())
        n = engine.restore_all_os_state()
        failed = getattr(engine, "last_restore_failed_count", 0)
        if failed:
            _log.warning("Emergency restore incomplete: restored=%d failed=%d", n, failed)
            print(
                f"PrismFX recovery incomplete: restored {n} OS mutations, {failed} failed "
                "(some entries may require Administrator privileges).",
                file=sys.stderr,
            )
            return 1
        _log.info("Emergency restore complete: restored=%d", n)
        print(f"PrismFX recovery complete: restored {n} OS mutations.")
        return 0

    title_exact = f"{__app_name__} Studio v{__version__}"
    ipc_msg = w32.register_ipc_message(str(profile_dir))
    ipc_quit_msg = w32.register_ipc_quit_message(str(profile_dir))

    # Graceful cross-instance --quit command (re-nudges in case target is still finishing startup!)
    if args.quit:
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            _signal_existing_instance(title_exact, ipc_quit_msg, w32.IPC_CMD_QUIT)
            h, still_exists = w32.create_single_instance_mutex(str(profile_dir))
            w32.close_handle(h)
            if not still_exists:
                break
            time.sleep(0.1)
        return 0

    # Single-instance mutex scoped by FNV-1a hash of profile directory (supports --replace!)
    mutex_handle, exists = w32.create_single_instance_mutex(str(profile_dir))
    if exists and args.replace:
        w32.close_handle(mutex_handle)
        mutex_handle = 0
        _signal_existing_instance(title_exact, ipc_quit_msg, w32.IPC_CMD_QUIT)
        deadline = time.perf_counter() + 4.5
        while time.perf_counter() < deadline:
            time.sleep(0.08)
            h, still_exists = w32.create_single_instance_mutex(str(profile_dir))
            if not still_exists:
                mutex_handle = h
                exists = False
                break
            w32.close_handle(h)
            # Re-nudge in case prior window was still initializing
            _signal_existing_instance(title_exact, ipc_quit_msg, w32.IPC_CMD_QUIT)
        if exists:
            _log.warning("Timed out waiting for existing instance to exit during --replace (profile=%s)", profile_dir)
            print(
                "PrismFX: timed out waiting for existing instance to exit during --replace "
                "(an elevated instance cannot be closed by a standard-user process).",
                file=sys.stderr,
            )
            return 1

    if exists:
        _log.info("Activated existing PrismFX instance for profile=%s", profile_dir)
        _signal_existing_instance(title_exact, ipc_msg, w32.IPC_CMD_ACTIVATE)
        w32.close_handle(mutex_handle)
        return 0
    atexit.register(lambda: w32.close_handle(mutex_handle))

    _log.info(
        "PrismFX Studio v%s starting (profile=%s, demo=%s)",
        __version__,
        profile_dir,
        bool(args.demo),
    )

    try:
        w32.shell32.SetCurrentProcessExplicitAppUserModelID("PrismFX.Studio")
    except Exception:
        _log.warning("SetCurrentProcessExplicitAppUserModelID failed", exc_info=True)

    try:
        app = QtWidgets.QApplication(sys.argv)
        app.setApplicationName(__app_name__)
        app.setApplicationVersion(__version__)
        app.setQuitOnLastWindowClosed(False)

        ATLAS.initialize()
        app.setStyleSheet(build_stylesheet())

        repo_root = exe_dir
        icon_path = bundle_dir / "assets" / "prismfx.ico"
        if not icon_path.exists():
            icon_path = exe_dir / "assets" / "prismfx.ico"
        if icon_path.exists():
            app.setWindowIcon(QtGui.QIcon(str(icon_path)))
        store = ConfigStore(profile_dir)
        shader_dirs: list[pathlib.Path] = [bundle_dir / "assets" / "shaders"]
        if exe_dir != bundle_dir:
            shader_dirs.append(exe_dir / "assets" / "shaders")
        shader_dirs.append(store.shaders_dir)
        registry = EffectRegistry(shader_dirs)
        backend = DemoWindowBackend(seed=42) if args.demo else Win32WindowBackend()
        engine = PrismEngine(backend, store, registry)
        engine.start()

        # Guaranteed OS state restoration on EVERY shutdown path WITHOUT signal-frame deadlock:
        # - normal Qt quit / tray exit (aboutToQuit -> engine.stop())
        # - POSIX/Win32 SIGINT / SIGTERM / SIGBREAK (flag + queued quit + lightweight timer poll)
        # - Console close / Logoff / Shutdown (SetConsoleCtrlHandler runs on dedicated OS thread)
        # - Windows session shutdown (commitDataRequest + WM_ENDSESSION)
        shutdown_requested = threading.Event()

        def _signal_handler(_sig: int, _frame: object) -> None:
            # NEVER acquire engine/journal/logging locks directly inside a Python signal frame!
            shutdown_requested.set()
            try:
                QtCore.QMetaObject.invokeMethod(app, "quit", QtCore.Qt.QueuedConnection)
            except Exception:
                pass

        def _console_ctrl_cleanup(ctrl_type: int) -> None:
            shutdown_requested.set()
            if ctrl_type in (w32.CTRL_CLOSE_EVENT, w32.CTRL_LOGOFF_EVENT, w32.CTRL_SHUTDOWN_EVENT):
                # Runs on Windows' dedicated console control thread before hard OS termination
                try:
                    engine.stop()
                except Exception:
                    _log.exception("Console control handler engine.stop() failed (ctrl_type=%s)", ctrl_type)
            try:
                QtCore.QMetaObject.invokeMethod(app, "quit", QtCore.Qt.QueuedConnection)
            except Exception:
                _log.warning("Console control handler failed to queue app.quit()", exc_info=True)

        sig_timer = QtCore.QTimer(app)
        sig_timer.setInterval(100)

        def _poll_shutdown_signal() -> None:
            if shutdown_requested.is_set():
                sig_timer.stop()
                _log.info("Shutdown signal received; quitting Qt event loop.")
                app.quit()

        sig_timer.timeout.connect(_poll_shutdown_signal)
        sig_timer.start()

        atexit.register(engine.stop)
        app.aboutToQuit.connect(engine.stop)
        app.commitDataRequest.connect(lambda _mgr: engine.stop())
        w32.register_console_ctrl_handler(_console_ctrl_cleanup)
        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig_num = getattr(signal, sig_name, None)
            if sig_num is not None:
                try:
                    signal.signal(sig_num, _signal_handler)
                except Exception:
                    _log.warning("Failed to install signal handler for %s", sig_name, exc_info=True)

        win = MainWindow(
            engine,
            enable_overlays=True,
            enable_tray=True,
            icon_path=icon_path,
            ipc_msg=ipc_msg,
            ipc_quit_msg=ipc_quit_msg,
            repo_root=repo_root,
            profile_arg=args.profile,
        )
        win.ensure_native_handle()

        start_hidden = bool(
            args.minimized
            or engine.config.start_minimized_to_tray
            or (args.autostart and engine.config.autostart_minimized)
        )
        if start_hidden:
            win.hide()
        else:
            win.show()

        rc = app.exec()
        _log.info("PrismFX Studio exiting cleanly (rc=%s)", rc)
        return rc
    except Exception:
        _log.critical("Unhandled fatal exception in PrismFX Studio", exc_info=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
