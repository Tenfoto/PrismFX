"""Reproducible distribution builder & trimmer (Phase 7).

Builds either:
  1. Portable ready-to-run folder (`dist/PrismFX-Portable`) with launcher & DPI manifest, OR
  2. Standalone single-folder executable via PyInstaller (if installed) with automatic
     unused Qt module trimming (`QtWebEngine`, `QtPdf`, `QtMultimedia`, `Qt3D`, translations).
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
DIST = ROOT / "dist"


def _find_csc() -> pathlib.Path | None:
    for p in (
        pathlib.Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
        pathlib.Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"),
    ):
        if p.is_file():
            return p
    found = shutil.which("csc")
    return pathlib.Path(found) if found else None


def build_native_launcher(out_exe: pathlib.Path) -> bool:
    """Compile the tiny ~12 KB windowless GUI PrismFX.exe launcher (zero CMD console!)."""
    csc = _find_csc()
    launcher_cs = ROOT / "tools" / "PrismFXLauncher.cs"
    manifest = ROOT / "tools" / "PrismFX.manifest"
    icon = ROOT / "assets" / "prismfx.ico"
    if not csc or not launcher_cs.is_file():
        print("[WARN] csc.exe or tools/PrismFXLauncher.cs not found; skipping native PrismFX.exe build.")
        return False

    py_exe = pathlib.Path(sys.executable)
    pyw = py_exe.with_name("pythonw.exe")
    pyw_str = str(pyw if pyw.is_file() else py_exe).replace('"', '""')

    tmp_cfg: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cs", encoding="utf-8", delete=False) as tf:
            tf.write(
                "namespace PrismFXLauncher {\n"
                f'    internal static class BuildConfig {{ public const string PythonW = @"{pyw_str}"; }}\n'
                "}\n"
            )
            tmp_cfg = pathlib.Path(tf.name)

        out_exe.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(csc),
            "/nologo",
            "/target:winexe",
            "/platform:anycpu",
            "/optimize+",
            f"/out:{out_exe}",
        ]
        if icon.is_file():
            cmd.append(f"/win32icon:{icon}")
        if manifest.is_file():
            cmd.append(f"/win32manifest:{manifest}")
        cmd.extend([str(launcher_cs), str(tmp_cfg)])
        subprocess.run(cmd, check=True)
        return out_exe.is_file()
    finally:
        if tmp_cfg is not None:
            tmp_cfg.unlink(missing_ok=True)

DPI_MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
    </windowsSettings>
  </application>
</assembly>
"""

UNUSED_QT_PATTERNS = (
    "*WebEngine*",
    "*Qt6Pdf*",
    "*Qt6Multimedia*",
    "*Qt6Quick3D*",
    "*Qt6ShaderTools*",
    "*Qt6Designer*",
    "*Qt6Sql*",
    "*Qt6Test*",
    "*Qt6Qml*",
    "*Qt6VirtualKeyboard*",
)


def trim_pyinstaller_bundle(bundle_dir: pathlib.Path) -> int:
    """Remove unused Qt6 binaries to keep the standalone distribution razor-lean."""
    saved_bytes = 0
    for pat in UNUSED_QT_PATTERNS:
        for f in bundle_dir.rglob(pat):
            if f.is_file():
                saved_bytes += f.stat().st_size
                f.unlink(missing_ok=True)
    return saved_bytes


def build_portable() -> pathlib.Path:
    out = DIST / "PrismFX-Portable"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    # Ensure icon exists
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")], check=True)

    shutil.copytree(ROOT / "prismfx", out / "prismfx", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "assets", out / "assets")
    shutil.copy2(ROOT / "main.py", out / "main.py")

    # Build standalone windowless PrismFX.exe (zero cmd.exe console, embedded icon & DPI manifest!)
    exe_out = out / "PrismFX.exe"
    if build_native_launcher(exe_out):
        # Also refresh workspace root PrismFX.exe for convenient one-click launch
        shutil.copy2(exe_out, ROOT / "PrismFX.exe")
        print(f"[OK] Compiled windowless native launcher: {exe_out} ({exe_out.stat().st_size:,} bytes)")
    else:
        (out / "PrismFX.manifest").write_text(DPI_MANIFEST, encoding="utf-8")
        shutil.copy2(ROOT / "run.bat", out / "PrismFX.bat")

    print(f"[OK] Built portable bundle at: {out}")
    return out


def main() -> int:
    use_pyinstaller = "--exe" in sys.argv
    if use_pyinstaller:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            print("PyInstaller is not installed. Run `pip install pyinstaller` or omit --exe for portable bundle.")
            return 1
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")], check=True)
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--windowed", "--name", "PrismFX",
            "--icon", str(ROOT / "assets" / "prismfx.ico"),
            "--add-data", f"{ROOT / 'assets'};assets",
            str(ROOT / "main.py"),
        ]
        subprocess.run(cmd, check=True, cwd=str(ROOT))
        bundle = DIST / "PrismFX"
        saved = trim_pyinstaller_bundle(bundle)
        print(f"[OK] Built trimmed EXE bundle at {bundle} (trimmed {saved / (1024 * 1024):.1f} MB unused Qt modules)")
        return 0

    build_portable()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
