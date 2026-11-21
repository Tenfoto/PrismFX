"""PrismFX Studio — Entry point with isolated profiles, single-instance IPC, crash-safe cleanup & verification CLI."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import pathlib
import signal
import sys
import time

from PySide6 import QtCore, QtGui, QtWidgets

from prismfx import __app_name__, __version__
from prismfx.core import win32_api as w32
from prismfx.core.backend import DemoWindowBackend, Win32WindowBackend
from prismfx.core.config import ConfigStore
from prismfx.core.effects import EffectRegistry
from prismfx.core.engine import PrismEngine
from prismfx.render.gl_renderer import make_gl_format
from prismfx.ui.main_window import MainWindow
from prismfx.ui.theme import ATLAS, build_stylesheet


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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    profile_dir = pathlib.Path(args.profile).resolve() if args.profile else default_data_dir().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

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
        print(f"PrismFX recovery complete: restored {n} OS mutations.")
        return 0

    title_exact = f"{__app_name__} Studio v{__version__}"
    ipc_msg = w32.register_ipc_message(str(profile_dir))

    # Graceful cross-instance --quit command (re-nudges in case target is still finishing startup!)
    if args.quit:
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            _signal_existing_instance(title_exact, ipc_msg, w32.IPC_CMD_QUIT)
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
        _signal_existing_instance(title_exact, ipc_msg, w32.IPC_CMD_QUIT)
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
            _signal_existing_instance(title_exact, ipc_msg, w32.IPC_CMD_QUIT)

    if exists:
        _signal_existing_instance(title_exact, ipc_msg, w32.IPC_CMD_ACTIVATE)
        w32.close_handle(mutex_handle)
        return 0
    atexit.register(lambda: w32.close_handle(mutex_handle))

    try:
        w32.shell32.SetCurrentProcessExplicitAppUserModelID("PrismFX.Studio")
    except Exception:
        pass

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)

    ATLAS.initialize()
    app.setStyleSheet(build_stylesheet())

    repo_root = pathlib.Path(__file__).resolve().parent
    icon_path = repo_root / "assets" / "prismfx.ico"
    if icon_path.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))
    store = ConfigStore(profile_dir)
    registry = EffectRegistry([repo_root / "assets" / "shaders", store.shaders_dir])
    backend = DemoWindowBackend(seed=42) if args.demo else Win32WindowBackend()
    engine = PrismEngine(backend, store, registry)
    engine.start()

    # Guaranteed synchronous OS state restoration on EVERY shutdown path (Phase 6):
    # - normal Qt quit / tray exit
    # - console close (X on CMD), Ctrl+C, Ctrl+Break, Logoff, Shutdown (SetConsoleCtrlHandler)
    # - POSIX/Win32 SIGINT / SIGTERM / SIGBREAK
    # - Windows session shutdown (commitDataRequest + WM_ENDSESSION)
    def _emergency_cleanup(_reason: object = None) -> None:
        try:
            engine.stop()
        except Exception:
            pass
        try:
            QtCore.QMetaObject.invokeMethod(app, "quit", QtCore.Qt.QueuedConnection)
        except Exception:
            pass

    atexit.register(engine.stop)
    app.aboutToQuit.connect(engine.stop)
    app.commitDataRequest.connect(lambda _mgr: engine.stop())
    w32.register_console_ctrl_handler(_emergency_cleanup)
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig_num = getattr(signal, sig_name, None)
        if sig_num is not None:
            try:
                signal.signal(sig_num, lambda _s, _f: _emergency_cleanup("signal"))
            except Exception:
                pass

    win = MainWindow(
        engine,
        enable_overlays=True,
        enable_tray=True,
        icon_path=icon_path,
        ipc_msg=ipc_msg,
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

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
