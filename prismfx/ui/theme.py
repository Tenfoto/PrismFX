"""Phase 2: Token-First Design System, Pre-Tiled Scanlines, Registry Font Resolution & Thai Grapheme Utils."""

from __future__ import annotations

import os
import pathlib
import unicodedata
import winreg
from typing import Optional

from PySide6 import QtCore, QtGui

# ---------------------------------------------------------------------------
# Locked Core Visual Tokens (windows-desktop-engineering Phase 2)
# ---------------------------------------------------------------------------
TOKENS = {
    "bg_base": "#121316",
    "bg_elevated": "#17181C",
    "bg_recessed": "#0B0C0E",
    "bg_hover": "#1E2129",
    "bg_selected": "#1A2942",
    "ink_primary": "#F2F1EE",
    "ink_secondary": "#A7AEBE",
    "ink_muted": "#6E768A",
    "accent": "#2D81FF",
    "accent_hi": "#74AAFF",
    "accent_dim": "#193664",
    "line": "#252933",
    "line_strong": "#343A49",
    "ok": "#34D399",
    "warn": "#F59E0B",
    "danger": "#F43F5E",
    "radius": 0,
    "scanline_step": 3,
}

THAI_SCALE = 1.32

# Thai combining characters (above/below vowels, tone marks, thanthakhat, etc.)
_THAI_COMBINING = {
    0x0E31, 0x0E34, 0x0E35, 0x0E36, 0x0E37, 0x0E38, 0x0E39, 0x0E3A,
    0x0E47, 0x0E48, 0x0E49, 0x0E4A, 0x0E4B, 0x0E4C, 0x0E4D, 0x0E4E,
}


def has_thai(text: str) -> bool:
    return any("\u0E00" <= ch <= "\u0E7F" for ch in text)


def split_grapheme_clusters(text: str) -> list[str]:
    """Split text into visual grapheme clusters so Thai vowels & tone marks never detach."""
    clusters: list[str] = []
    for ch in text:
        cp = ord(ch)
        is_comb = (cp in _THAI_COMBINING) or (unicodedata.category(ch) in ("Mn", "Mc", "Me"))
        if is_comb and clusters:
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


class FontAtlas:
    """Resolves Display (Archivo), Body (Inter), Mono (Geist Mono), and Thai (FC Minimal) cleanly."""

    def __init__(self) -> None:
        self.display_family = "Segoe UI"
        self.body_family = "Segoe UI"
        self.mono_family = "Consolas"
        self.thai_family = "Segoe UI"
        self.loaded_font_files: list[str] = []
        self._scanline_tile: Optional[QtGui.QPixmap] = None

    def initialize(self) -> None:
        self._load_registry_and_local_fonts()
        families = set(QtGui.QFontDatabase.families())

        for cand in ("Archivo", "Segoe UI Variable Display", "Segoe UI"):
            if cand in families:
                self.display_family = cand
                break
        for cand in ("Inter", "Segoe UI Variable Text", "Segoe UI"):
            if cand in families:
                self.body_family = cand
                break
        for cand in ("Geist Mono", "Cascadia Mono", "Cascadia Code", "Consolas"):
            if cand in families:
                self.mono_family = cand
                break
        for cand in ("FC Minimal", "THSarabun", "Leelawadee UI", "Segoe UI"):
            if cand in families:
                self.thai_family = cand
                break

    def _load_registry_and_local_fonts(self) -> None:
        """Resolve per-user & machine installed fonts (including FC Minimal in LocalAppData)."""
        targets = ("fc minimal", "archivo", "inter", "geist mono", "geistmono")
        seen: set[str] = set()

        # 1) Check Windows Registry HKCU & HKLM font entries
        for hive, sub in (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"),
        ):
            try:
                k = winreg.OpenKey(hive, sub)
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                        i += 1
                        if any(t in name.lower() for t in targets):
                            p = pathlib.Path(val)
                            if not p.is_absolute():
                                p = pathlib.Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / val
                            if p.exists() and str(p).lower() not in seen:
                                seen.add(str(p).lower())
                                if QtGui.QFontDatabase.addApplicationFont(str(p)) >= 0:
                                    self.loaded_font_files.append(str(p))
                    except OSError:
                        break
                winreg.CloseKey(k)
            except OSError:
                pass

        # 2) Direct scan of user local font folder
        local_fonts = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
        if local_fonts.exists():
            for fp in local_fonts.glob("*.ttf"):
                if any(t in fp.name.lower() for t in targets) and str(fp).lower() not in seen:
                    seen.add(str(fp).lower())
                    if QtGui.QFontDatabase.addApplicationFont(str(fp)) >= 0:
                        self.loaded_font_files.append(str(fp))

    def font_for(self, role: str, base_pt: float, text: str = "", bold: bool = False) -> QtGui.QFont:
        use_thai = has_thai(text) and self.thai_family == "FC Minimal"
        if use_thai:
            fam = self.thai_family
            pt = max(9.0, base_pt * THAI_SCALE)
        elif role == "display":
            fam = self.display_family
            pt = base_pt
        elif role == "mono":
            fam = self.mono_family
            pt = base_pt
        else:
            fam = self.body_family
            pt = base_pt
        f = QtGui.QFont(fam)
        f.setPointSizeF(pt)
        if bold:
            f.setWeight(QtGui.QFont.DemiBold)
        return f

    def scanline_tile(self) -> QtGui.QPixmap:
        """Pre-tiled 256×48 subtle horizontal 3px scanline texture (zero per-frame generation)."""
        if self._scanline_tile is None:
            pm = QtGui.QPixmap(256, 48)
            pm.fill(QtCore.Qt.transparent)
            p = QtGui.QPainter(pm)
            p.setPen(QtGui.QColor(255, 255, 255, 7))
            for y in range(0, 48, TOKENS["scanline_step"]):
                p.drawLine(0, y, 256, y)
            p.end()
            self._scanline_tile = pm
        return self._scanline_tile


ATLAS = FontAtlas()


def build_stylesheet() -> str:
    t = TOKENS
    body = ATLAS.body_family
    mono = ATLAS.mono_family
    thai = ATLAS.thai_family
    return f"""
    QWidget {{
        background-color: {t["bg_base"]};
        color: {t["ink_primary"]};
        font-family: "{body}", "{thai}", "Segoe UI";
        font-size: 13px;
        border-radius: 0px;
    }}
    QMainWindow, QDialog {{
        background-color: {t["bg_base"]};
    }}
    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QScrollBar:vertical {{
        background: {t["bg_recessed"]};
        width: 8px;
        margin: 0px;
    }}
    QScrollBar::handle:vertical {{
        background: {t["line_strong"]};
        min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {t["accent"]};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QPushButton {{
        background-color: {t["bg_elevated"]};
        color: {t["ink_primary"]};
        border: 1px solid {t["line"]};
        padding: 7px 14px;
        font-weight: 500;
    }}
    QPushButton:hover {{
        background-color: {t["bg_hover"]};
        border-color: {t["accent"]};
    }}
    QPushButton:pressed {{
        background-color: {t["bg_selected"]};
    }}
    QPushButton[accent="true"] {{
        background-color: {t["accent"]};
        border: 1px solid {t["accent_hi"]};
        color: #FFFFFF;
        font-weight: 600;
    }}
    QPushButton[accent="true"]:hover {{
        background-color: #3B8BFF;
    }}
    QPushButton[danger="true"] {{
        background-color: #261419;
        border: 1px solid {t["danger"]};
        color: #FDA4AF;
    }}
    QPushButton[danger="true"]:hover {{
        background-color: #3A1A23;
    }}
    QPushButton[pill="true"] {{
        padding: 5px 11px;
        font-size: 12px;
        background-color: {t["bg_recessed"]};
        border: 1px solid {t["line"]};
        color: {t["ink_secondary"]};
    }}
    QPushButton[pill="true"][selected="true"] {{
        background-color: {t["bg_selected"]};
        border: 1px solid {t["accent"]};
        color: {t["ink_primary"]};
        font-weight: 600;
    }}
    QComboBox, QLineEdit, QSpinBox {{
        background-color: {t["bg_recessed"]};
        color: {t["ink_primary"]};
        border: 1px solid {t["line"]};
        padding: 6px 10px;
    }}
    QComboBox:hover, QLineEdit:hover, QSpinBox:hover {{
        border-color: {t["accent"]};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 24px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {t["bg_elevated"]};
        color: {t["ink_primary"]};
        border: 1px solid {t["line_strong"]};
        selection-background-color: {t["accent"]};
    }}
    QSlider::groove:horizontal {{
        height: 4px;
        background: {t["bg_recessed"]};
        border: 1px solid {t["line"]};
    }}
    QSlider::sub-page:horizontal {{
        background: {t["accent"]};
    }}
    QSlider::handle:horizontal {{
        background: {t["ink_primary"]};
        border: 1px solid {t["accent"]};
        width: 12px;
        height: 16px;
        margin: -7px 0px;
    }}
    QSlider::handle:horizontal:hover {{
        background: {t["accent_hi"]};
    }}
    QCheckBox {{
        spacing: 8px;
        color: {t["ink_primary"]};
        background: transparent;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        background: {t["bg_recessed"]};
        border: 1px solid {t["line_strong"]};
    }}
    QCheckBox::indicator:checked {{
        background: {t["accent"]};
        border: 1px solid {t["accent_hi"]};
    }}
    QTableWidget {{
        background-color: {t["bg_recessed"]};
        gridline-color: {t["line"]};
        border: 1px solid {t["line"]};
        selection-background-color: {t["bg_selected"]};
        alternate-background-color: {t["bg_elevated"]};
    }}
    QHeaderView::section {{
        background-color: {t["bg_elevated"]};
        color: {t["ink_secondary"]};
        border: none;
        border-bottom: 1px solid {t["line"]};
        border-right: 1px solid {t["line"]};
        padding: 7px 10px;
        font-family: "{mono}", "Consolas";
        font-size: 11px;
        text-transform: uppercase;
    }}
    """
