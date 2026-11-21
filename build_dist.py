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

ROOT = pathlib.Path(__file__).resolve().parent
DIST = ROOT / "dist"

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
    shutil.copy2(ROOT / "run.bat", out / "PrismFX.bat")
    (out / "PrismFX.manifest").write_text(DPI_MANIFEST, encoding="utf-8")
    (out / "PrismFX_Silent.vbs").write_text(
        'Set WshShell = CreateObject("WScript.Shell")\n'
        'WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)\n'
        'WshShell.Run "pythonw main.py", 0, False\n',
        encoding="utf-8",
    )
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
