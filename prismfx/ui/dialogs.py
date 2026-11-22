"""Dark-themed architectural settings dialogs for PrismFX Studio (PFX-M08).

Provides:
- `RandomPoolDialog`: Searchable checklist for configuring the Smart Random Pool.
- `ExcludedExesDialog`: Offline manager for adding, browsing, and removing excluded `.exe` names.
"""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..core import win32_api as w32
from ..core.config import PrismConfig
from ..core.effects import EffectRegistry, EffectSpec, sanitize_hex_color
from .theme import ATLAS, TOKENS
from .widgets import PanelCard, SectionHeader

_INVALID_PATH_CHARS = frozenset('<>|?*"')


def _qt_esc(text: str) -> str:
    """Escape literal '&' as '&&' for Qt button/checkbox mnemonic handling."""
    return text.replace("&", "&&")


def _apply_dark_win32_chrome(widget: QtWidgets.QWidget) -> None:
    """Apply Windows 11 dark title bar and sharp 0px corners safely."""
    try:
        hwnd = int(widget.winId())
        if hwnd:
            w32.set_immersive_dark_mode(hwnd, True)
            w32.set_window_corner_square(hwnd)
    except Exception:
        pass


def _dialog_stylesheet() -> str:
    """Self-contained dark architectural QSS aligned with PrismFX TOKENS (0px radius, 1px borders)."""
    t = TOKENS
    body = ATLAS.body_family
    mono = ATLAS.mono_family
    thai = ATLAS.thai_family
    return f"""
    QDialog {{
        background-color: {t["bg_base"]};
        color: {t["ink_primary"]};
        font-family: "{body}", "{thai}", "Segoe UI";
        font-size: 13px;
        border-radius: 0px;
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
    QLineEdit {{
        background-color: {t["bg_recessed"]};
        color: {t["ink_primary"]};
        border: 1px solid {t["line"]};
        padding: 7px 10px;
        border-radius: 0px;
    }}
    QLineEdit:hover, QLineEdit:focus {{
        border-color: {t["accent"]};
    }}
    QPushButton {{
        background-color: {t["bg_elevated"]};
        color: {t["ink_primary"]};
        border: 1px solid {t["line"]};
        padding: 7px 14px;
        font-weight: 500;
        border-radius: 0px;
    }}
    QPushButton:hover {{
        background-color: {t["bg_hover"]};
        border-color: {t["accent"]};
    }}
    QPushButton:pressed {{
        background-color: {t["bg_selected"]};
    }}
    QPushButton:disabled {{
        background-color: {t["bg_recessed"]};
        color: {t["ink_muted"]};
        border-color: {t["line"]};
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
    QPushButton[danger="true"]:disabled {{
        background-color: {t["bg_recessed"]};
        border: 1px solid {t["line"]};
        color: {t["ink_muted"]};
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
        border-radius: 0px;
    }}
    QCheckBox::indicator:hover {{
        border-color: {t["accent_hi"]};
    }}
    QCheckBox::indicator:checked {{
        background: {t["accent"]};
        border: 1px solid {t["accent_hi"]};
    }}
    QListWidget {{
        background-color: {t["bg_recessed"]};
        color: {t["ink_primary"]};
        border: 1px solid {t["line"]};
        alternate-background-color: {t["bg_elevated"]};
        outline: none;
        font-family: "{mono}", "Consolas";
        font-size: 12px;
        border-radius: 0px;
    }}
    QListWidget::item {{
        padding: 7px 10px;
        border-bottom: 1px solid {t["line"]};
    }}
    QListWidget::item:hover {{
        background-color: {t["bg_hover"]};
    }}
    QListWidget::item:selected {{
        background-color: {t["bg_selected"]};
        color: {t["ink_primary"]};
        border-left: 3px solid {t["accent"]};
    }}
    """


def normalize_exe_name(raw: str) -> tuple[Optional[str], str]:
    """Pure-string normalization & validation for Windows executable names.

    Never performs filesystem I/O (safe against UNC SMB path leaks).
    Returns `(normalized_name, error_code)` where `error_code` is `""`, `"empty"`, or `"invalid"`.
    """
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1].strip()
    if not text:
        return (None, "empty")

    if any(ord(ch) < 32 or ch in _INVALID_PATH_CHARS for ch in text):
        return (None, "invalid")

    # Extract trailing basename component if a Windows or POSIX path was pasted
    if "\\" in text or "/" in text:
        sep_idx = max(text.rfind("\\"), text.rfind("/"))
        basename = text[sep_idx + 1 :].strip()
    else:
        basename = text

    if not basename or ":" in basename:
        return (None, "invalid")

    if basename.lower().endswith(".exe"):
        stem = basename[:-4]
    else:
        stem = basename

    if (
        not stem
        or stem != stem.strip()
        or stem.endswith(".")
        or not stem.strip(".")
        or len(stem) + 4 > 255
    ):
        return (None, "invalid")

    return (f"{stem}.exe", "")


def _dedupe_sort_exes(items: list[str]) -> list[str]:
    """Deduplicate `.exe` names case-insensitively and sort alphabetically (case-insensitive)."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items or []:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s or "\x00" in s:
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        cleaned.append(s)
    cleaned.sort(key=lambda s: s.lower())
    return cleaned


class _RandomPoolRow(QtWidgets.QFrame):
    """Selectable architectural checklist row for a single shader effect."""

    def __init__(self, spec: EffectSpec, checked: bool, lang: str, parent=None) -> None:
        super().__init__(parent)
        self.spec = spec
        self._lang = lang
        self.setCursor(QtCore.Qt.PointingHandCursor)

        safe_accent = sanitize_hex_color(spec.accent_hex, TOKENS["accent"])
        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QtWidgets.QFrame()
        bar.setFixedWidth(3)
        bar.setStyleSheet(f"background-color: {safe_accent}; border: none;")
        outer.addWidget(bar)

        content = QtWidgets.QHBoxLayout()
        content.setContentsMargins(10, 7, 10, 7)
        content.setSpacing(10)

        title = f"{spec.name_th}  ({spec.name_en})" if lang == "th" else spec.name_en
        desc = spec.desc_th if lang == "th" else spec.desc_en

        left = QtWidgets.QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(2)

        self.checkbox = QtWidgets.QCheckBox(_qt_esc(title))
        self.checkbox.setObjectName(f"chk_pool_{spec.id}")
        self.checkbox.setChecked(checked)
        self.checkbox.setFont(ATLAS.font_for("body", 10.0, text=title, bold=True))
        left.addWidget(self.checkbox)

        lbl_desc = QtWidgets.QLabel(desc)
        lbl_desc.setFont(ATLAS.font_for("body", 8.8, text=desc))
        lbl_desc.setStyleSheet(
            f"color: {TOKENS['ink_secondary']}; background: transparent; border: none; padding-left: 24px;"
        )
        lbl_desc.setWordWrap(True)
        left.addWidget(lbl_desc)
        content.addLayout(left, 1)

        right = QtWidgets.QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(3)
        right.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        badge = QtWidgets.QLabel(spec.badge)
        badge.setAlignment(QtCore.Qt.AlignCenter)
        badge.setFont(ATLAS.font_for("mono", 7.5, bold=True))
        badge.setStyleSheet(
            f"background: {TOKENS['bg_recessed']}; color: {safe_accent}; "
            f"border: 1px solid {TOKENS['line_strong']}; padding: 1px 6px;"
        )
        right.addWidget(badge, 0, QtCore.Qt.AlignRight)

        lbl_id = QtWidgets.QLabel(spec.id)
        lbl_id.setFont(ATLAS.font_for("mono", 7.8))
        lbl_id.setStyleSheet(f"color: {TOKENS['ink_muted']}; background: transparent; border: none;")
        right.addWidget(lbl_id, 0, QtCore.Qt.AlignRight)

        content.addLayout(right)
        outer.addLayout(content, 1)

        self._search_haystack = (
            spec.name_en.lower(),
            spec.name_th.lower(),
            spec.desc_en.lower(),
            spec.desc_th.lower(),
            spec.id.lower(),
        )
        self.refresh_style()

    def matches_query(self, query_lower: str) -> bool:
        if not query_lower:
            return True
        return any(query_lower in field for field in self._search_haystack)

    def refresh_style(self) -> None:
        if self.checkbox.isChecked():
            self.setStyleSheet(
                f"_RandomPoolRow {{ background-color: {TOKENS['bg_selected']}; "
                f"border: 1px solid {TOKENS['accent']}; }}"
            )
        else:
            self.setStyleSheet(
                f"_RandomPoolRow {{ background-color: {TOKENS['bg_elevated']}; "
                f"border: 1px solid {TOKENS['line']}; }}"
                f"_RandomPoolRow:hover {{ background-color: {TOKENS['bg_hover']}; "
                f"border: 1px solid {TOKENS['line_strong']}; }}"
            )

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            # Avoid double-toggling when clicking directly on the QCheckBox itself
            child = self.childAt(event.position().toPoint())
            if child is not self.checkbox:
                self.checkbox.toggle()
        super().mousePressEvent(event)


class RandomPoolDialog(QtWidgets.QDialog):
    """Searchable architectural checklist dialog for configuring the Smart Random Pool (PFX-M08)."""

    def __init__(
        self,
        registry: EffectRegistry,
        current_pool: list[str],
        lang: str = "th",
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._lang = "th" if lang == "th" else "en"
        self._effects: list[EffectSpec] = list(registry.all_effects(include_special=False))
        available_ids = {spec.id for spec in self._effects}
        self._default_pool: list[str] = [
            eid for eid in PrismConfig().random_pool if eid in available_ids
        ]
        self._rows: list[_RandomPoolRow] = []
        self._checkboxes: dict[str, QtWidgets.QCheckBox] = {}

        self.setObjectName("RandomPoolDialog")
        self.setModal(True)
        self.setWindowTitle(
            self._tr("จัดการรายการสุ่มแอนิเมชัน — PrismFX", "Smart Random Pool — PrismFX")
        )
        self.setMinimumSize(640, 500)
        self.resize(760, 620)
        self.setStyleSheet(_dialog_stylesheet())

        initial_set = set(current_pool or [])
        self._build_ui(initial_set)
        self._update_count_and_warning()

    def _tr(self, th: str, en: str) -> str:
        return th if self._lang == "th" else en

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        _apply_dark_win32_chrome(self)

    def _build_ui(self, initial_set: set[str]) -> None:
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        lay.addWidget(
            SectionHeader(
                "SMART RANDOM POOL // PFX-M08",
                self._tr("เลือกเอฟเฟกต์สำหรับโหมดสุ่มแอนิเมชัน", "Configure Smart Random Pool"),
                self._tr(
                    "เลือกเชดเดอร์ GPU ที่ต้องการให้สุ่มใช้งานเมื่อเหตุการณ์ตั้งค่าเป็น 'สุ่มจากรายการโปรด'",
                    "Select which GPU shaders are eligible whenever a trigger uses Smart Random Pool.",
                ),
            )
        )

        # Search + Live Count toolbar
        top_bar = QtWidgets.QHBoxLayout()
        top_bar.setSpacing(10)

        self.ed_search = QtWidgets.QLineEdit()
        self.ed_search.setObjectName("ed_random_pool_search")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.setPlaceholderText(
            self._tr(
                "ค้นหาเอฟเฟกต์ (ชื่อไทย, English, คำอธิบาย, ID)...",
                "Search effects (name, description, ID)...",
            )
        )
        self.ed_search.textChanged.connect(self._on_search_changed)
        top_bar.addWidget(self.ed_search, 1)

        self.lbl_filter_info = QtWidgets.QLabel("")
        self.lbl_filter_info.setObjectName("lbl_random_pool_filter")
        self.lbl_filter_info.setFont(ATLAS.font_for("mono", 8.5))
        self.lbl_filter_info.setStyleSheet(
            f"color: {TOKENS['ink_muted']}; background: transparent; border: none;"
        )
        self.lbl_filter_info.setVisible(False)
        top_bar.addWidget(self.lbl_filter_info)

        self.lbl_count = QtWidgets.QLabel()
        self.lbl_count.setObjectName("lbl_random_pool_count")
        self.lbl_count.setFont(ATLAS.font_for("body", 9.5, text=self._tr("เลือกแล้ว", "Selected"), bold=True))
        self.lbl_count.setStyleSheet(
            f"background: {TOKENS['bg_elevated']}; color: {TOKENS['accent_hi']}; "
            f"border: 1px solid {TOKENS['line_strong']}; padding: 5px 10px;"
        )
        top_bar.addWidget(self.lbl_count)
        lay.addLayout(top_bar)

        # Scrollable checklist container
        list_card = PanelCard(recessed=True)
        lc_lay = QtWidgets.QVBoxLayout(list_card)
        lc_lay.setContentsMargins(8, 8, 8, 8)
        lc_lay.setSpacing(0)

        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setObjectName("scroll_random_pool")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        host = QtWidgets.QWidget()
        host.setStyleSheet("background: transparent;")
        self._list_layout = QtWidgets.QVBoxLayout(host)
        self._list_layout.setContentsMargins(0, 0, 4, 0)
        self._list_layout.setSpacing(6)

        for spec in self._effects:
            row = _RandomPoolRow(spec, checked=(spec.id in initial_set), lang=self._lang)
            row.checkbox.toggled.connect(lambda _checked, r=row: self._on_row_toggled(r))
            self._rows.append(row)
            self._checkboxes[spec.id] = row.checkbox
            self._list_layout.addWidget(row)

        self.lbl_empty_search = QtWidgets.QLabel(
            self._tr("ไม่พบเอฟเฟกต์ที่ตรงกับคำค้นหา", "No effects match your search query.")
        )
        self.lbl_empty_search.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl_empty_search.setFont(
            ATLAS.font_for("body", 10.0, text=self.lbl_empty_search.text())
        )
        self.lbl_empty_search.setStyleSheet(
            f"color: {TOKENS['ink_muted']}; background: transparent; padding: 24px;"
        )
        self.lbl_empty_search.setVisible(False)
        self._list_layout.addWidget(self.lbl_empty_search)
        self._list_layout.addStretch(1)

        self._scroll.setWidget(host)
        lc_lay.addWidget(self._scroll)
        lay.addWidget(list_card, 1)

        # Inline warning banner (shown when 0 effects selected)
        self.lbl_warning = QtWidgets.QLabel(
            self._tr(
                "⚠ กรุณาเลือกอย่างน้อย 1 เอฟเฟกต์ก่อนบันทึก",
                "⚠ Select at least 1 effect before saving.",
            )
        )
        self.lbl_warning.setObjectName("lbl_random_pool_warning")
        self.lbl_warning.setFont(ATLAS.font_for("body", 9.5, text=self.lbl_warning.text(), bold=True))
        self.lbl_warning.setStyleSheet(
            f"background: #261419; color: #FDA4AF; border: 1px solid {TOKENS['danger']}; padding: 6px 10px;"
        )
        self.lbl_warning.setVisible(False)
        lay.addWidget(self.lbl_warning)

        # Action footer
        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(8)

        self.btn_select_all = QtWidgets.QPushButton(_qt_esc(self._tr("เลือกทั้งหมด", "Select All")))
        self.btn_select_all.setObjectName("btn_select_all")
        self.btn_select_all.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_select_all.setAutoDefault(False)
        self.btn_select_all.setDefault(False)
        self.btn_select_all.setFont(
            ATLAS.font_for("body", 9.5, text=self.btn_select_all.text())
        )
        self.btn_select_all.clicked.connect(self._on_select_all)

        self.btn_clear_all = QtWidgets.QPushButton(_qt_esc(self._tr("ล้างทั้งหมด", "Clear All")))
        self.btn_clear_all.setObjectName("btn_clear_all")
        self.btn_clear_all.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_clear_all.setAutoDefault(False)
        self.btn_clear_all.setDefault(False)
        self.btn_clear_all.setFont(
            ATLAS.font_for("body", 9.5, text=self.btn_clear_all.text())
        )
        self.btn_clear_all.clicked.connect(self._on_clear_all)

        default_n = len(self._default_pool)
        reset_lbl = self._tr(f"ค่าเริ่มต้น ({default_n})", "Reset Defaults")
        self.btn_reset_defaults = QtWidgets.QPushButton(_qt_esc(reset_lbl))
        self.btn_reset_defaults.setObjectName("btn_reset_defaults")
        self.btn_reset_defaults.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_reset_defaults.setAutoDefault(False)
        self.btn_reset_defaults.setDefault(False)
        self.btn_reset_defaults.setFont(ATLAS.font_for("body", 9.5, text=reset_lbl))
        self.btn_reset_defaults.clicked.connect(self._on_reset_defaults)

        self.btn_cancel = QtWidgets.QPushButton(_qt_esc(self._tr("ยกเลิก", "Cancel")))
        self.btn_cancel.setObjectName("btn_cancel")
        self.btn_cancel.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.setDefault(False)
        self.btn_cancel.setFont(ATLAS.font_for("body", 9.5, text=self.btn_cancel.text()))
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_save = QtWidgets.QPushButton(_qt_esc(self._tr("บันทึก", "Save")))
        self.btn_save.setObjectName("btn_save")
        self.btn_save.setProperty("accent", "true")
        self.btn_save.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_save.setAutoDefault(False)
        self.btn_save.setDefault(False)
        self.btn_save.setFont(ATLAS.font_for("body", 9.5, text=self.btn_save.text(), bold=True))
        self.btn_save.clicked.connect(self.accept)

        footer.addWidget(self.btn_select_all)
        footer.addWidget(self.btn_clear_all)
        footer.addWidget(self.btn_reset_defaults)
        footer.addStretch(1)
        footer.addWidget(self.btn_cancel)
        footer.addWidget(self.btn_save)
        lay.addLayout(footer)

    def _on_row_toggled(self, row: _RandomPoolRow) -> None:
        row.refresh_style()
        self._update_count_and_warning()

    def _on_search_changed(self, text: str) -> None:
        q = text.strip().lower()
        visible_count = 0
        for row in self._rows:
            matched = row.matches_query(q)
            row.setVisible(matched)
            if matched:
                visible_count += 1
        self.lbl_empty_search.setVisible(visible_count == 0 and len(self._rows) > 0)
        if q:
            self.lbl_filter_info.setText(f"{visible_count} / {len(self._rows)}")
            self.lbl_filter_info.setVisible(True)
        else:
            self.lbl_filter_info.setVisible(False)

    def _set_checked_ids(self, checked_ids: set[str]) -> None:
        for row in self._rows:
            row.checkbox.blockSignals(True)
            row.checkbox.setChecked(row.spec.id in checked_ids)
            row.checkbox.blockSignals(False)
            row.refresh_style()
        self._update_count_and_warning()

    def _on_select_all(self) -> None:
        self._set_checked_ids({spec.id for spec in self._effects})

    def _on_clear_all(self) -> None:
        self._set_checked_ids(set())

    def _on_reset_defaults(self) -> None:
        self._set_checked_ids(set(self._default_pool))

    def _update_count_and_warning(self) -> None:
        selected = sum(1 for row in self._rows if row.checkbox.isChecked())
        total = len(self._rows)
        if self._lang == "th":
            self.lbl_count.setText(f"เลือกแล้ว {selected} / {total} เอฟเฟกต์")
        else:
            self.lbl_count.setText(f"Selected: {selected} / {total} effects")
        self.lbl_warning.setVisible(selected == 0)

    def selected_pool(self) -> list[str]:
        """Return checked effect IDs in original registry order."""
        return [spec.id for spec in self._effects if self._checkboxes[spec.id].isChecked()]

    def accept(self) -> None:
        """Enforce at least 1 selected effect before accepting the dialog."""
        if not self.selected_pool():
            self.lbl_warning.setVisible(True)
            return
        super().accept()


class ExcludedExesDialog(QtWidgets.QDialog):
    """Offline manager dialog for viewing, adding, browsing, and removing excluded `.exe` names (PFX-M08)."""

    def __init__(
        self,
        excluded_exes: list[str],
        default_exes: list[str],
        lang: str = "th",
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._lang = "th" if lang == "th" else "en"
        self._default_exes: list[str] = _dedupe_sort_exes(default_exes)

        self.setObjectName("ExcludedExesDialog")
        self.setModal(True)
        self.setWindowTitle(
            self._tr(
                "จัดการรายการแอปที่ยกเว้น (.exe) — PrismFX",
                "Manage Excluded Executables (.exe) — PrismFX",
            )
        )
        self.setMinimumSize(580, 460)
        self.resize(680, 560)
        self.setStyleSheet(_dialog_stylesheet())

        self._build_ui(_dedupe_sort_exes(excluded_exes))
        self._update_list_state()

    def _tr(self, th: str, en: str) -> str:
        return th if self._lang == "th" else en

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        _apply_dark_win32_chrome(self)

    def _build_ui(self, initial_exes: list[str]) -> None:
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        lay.addWidget(
            SectionHeader(
                "APP EXCLUSIONS // PFX-M08",
                self._tr("จัดการรายการแอปและเกมที่ยกเว้น (.exe)", "Manage Excluded Executables (.exe)"),
                self._tr(
                    "เพิ่มหรือลบไฟล์ .exe แบบออฟไลน์ได้ทันทีแม้โปรแกรมหรือเกมนั้นไม่ได้เปิดทำงานอยู่",
                    "Add, browse, or remove excluded .exe names offline — even for apps and games currently closed.",
                ),
            )
        )

        # Input card: QLineEdit + Add + Browse
        input_card = PanelCard()
        ic_lay = QtWidgets.QVBoxLayout(input_card)
        ic_lay.setContentsMargins(12, 10, 12, 10)
        ic_lay.setSpacing(6)

        in_row = QtWidgets.QHBoxLayout()
        in_row.setSpacing(8)

        self.ed_exe = QtWidgets.QLineEdit()
        self.ed_exe.setObjectName("ed_excluded_exe")
        self.ed_exe.setClearButtonEnabled(True)
        self.ed_exe.setPlaceholderText(
            self._tr("เช่น game.exe หรือ my_app.exe", "e.g. game.exe or my_app.exe")
        )
        self.ed_exe.returnPressed.connect(self._on_add_clicked)
        in_row.addWidget(self.ed_exe, 1)

        self.btn_add = QtWidgets.QPushButton(_qt_esc(self._tr("เพิ่ม", "Add")))
        self.btn_add.setObjectName("btn_add_exe")
        self.btn_add.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_add.setAutoDefault(False)
        self.btn_add.setDefault(False)
        self.btn_add.setFont(ATLAS.font_for("body", 9.5, text=self.btn_add.text(), bold=True))
        self.btn_add.clicked.connect(self._on_add_clicked)
        in_row.addWidget(self.btn_add)

        self.btn_browse = QtWidgets.QPushButton(_qt_esc(self._tr("เลือกไฟล์...", "Browse...")))
        self.btn_browse.setObjectName("btn_browse_exe")
        self.btn_browse.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_browse.setAutoDefault(False)
        self.btn_browse.setDefault(False)
        self.btn_browse.setFont(ATLAS.font_for("body", 9.5, text=self.btn_browse.text()))
        self.btn_browse.clicked.connect(self._on_browse_clicked)
        in_row.addWidget(self.btn_browse)
        ic_lay.addLayout(in_row)

        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setObjectName("lbl_excluded_status")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setVisible(False)
        ic_lay.addWidget(self.lbl_status)

        lay.addWidget(input_card)

        # List header + live count
        hdr = QtWidgets.QHBoxLayout()
        hdr.setSpacing(8)
        lbl_hdr = QtWidgets.QLabel(
            self._tr("รายการ .EXE ที่ถูกยกเว้นทั้งหมด", "EXCLUDED EXECUTABLES")
        )
        lbl_hdr.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        lbl_hdr.setStyleSheet(
            f"color: {TOKENS['accent_hi']}; background: transparent; border: none;"
        )
        hdr.addWidget(lbl_hdr, 1)

        self.lbl_count = QtWidgets.QLabel()
        self.lbl_count.setObjectName("lbl_excluded_count")
        self.lbl_count.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        self.lbl_count.setStyleSheet(
            f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;"
        )
        hdr.addWidget(self.lbl_count)
        lay.addLayout(hdr)

        # Excluded executables list
        self.list_exes = QtWidgets.QListWidget()
        self.list_exes.setObjectName("list_excluded_exes")
        self.list_exes.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list_exes.setAlternatingRowColors(True)
        for exe in initial_exes:
            self.list_exes.addItem(exe)
        self.list_exes.itemSelectionChanged.connect(self._update_list_state)
        lay.addWidget(self.list_exes, 1)

        del_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Delete), self.list_exes)
        del_shortcut.setContext(QtCore.Qt.WidgetShortcut)
        del_shortcut.activated.connect(self._on_remove_selected)

        # Action footer
        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(8)

        self.btn_remove = QtWidgets.QPushButton(
            _qt_esc(self._tr("ลบรายการที่เลือก", "Remove Selected"))
        )
        self.btn_remove.setObjectName("btn_remove_selected")
        self.btn_remove.setProperty("danger", "true")
        self.btn_remove.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_remove.setAutoDefault(False)
        self.btn_remove.setDefault(False)
        self.btn_remove.setFont(ATLAS.font_for("body", 9.5, text=self.btn_remove.text()))
        self.btn_remove.clicked.connect(self._on_remove_selected)

        self.btn_reset_defaults = QtWidgets.QPushButton(
            _qt_esc(self._tr("คืนค่ารายการเริ่มต้น", "Reset Defaults"))
        )
        self.btn_reset_defaults.setObjectName("btn_reset_defaults")
        self.btn_reset_defaults.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_reset_defaults.setAutoDefault(False)
        self.btn_reset_defaults.setDefault(False)
        self.btn_reset_defaults.setFont(
            ATLAS.font_for("body", 9.5, text=self.btn_reset_defaults.text())
        )
        self.btn_reset_defaults.clicked.connect(self._on_reset_defaults)

        self.btn_cancel = QtWidgets.QPushButton(_qt_esc(self._tr("ยกเลิก", "Cancel")))
        self.btn_cancel.setObjectName("btn_cancel")
        self.btn_cancel.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.setDefault(False)
        self.btn_cancel.setFont(ATLAS.font_for("body", 9.5, text=self.btn_cancel.text()))
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_save = QtWidgets.QPushButton(_qt_esc(self._tr("บันทึก", "Save")))
        self.btn_save.setObjectName("btn_save")
        self.btn_save.setProperty("accent", "true")
        self.btn_save.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_save.setAutoDefault(False)
        self.btn_save.setDefault(False)
        self.btn_save.setFont(ATLAS.font_for("body", 9.5, text=self.btn_save.text(), bold=True))
        self.btn_save.clicked.connect(self.accept)

        footer.addWidget(self.btn_remove)
        footer.addWidget(self.btn_reset_defaults)
        footer.addStretch(1)
        footer.addWidget(self.btn_cancel)
        footer.addWidget(self.btn_save)
        lay.addLayout(footer)

    def _set_status(self, message: str, tone: str = "info") -> None:
        if not message:
            self.lbl_status.setVisible(False)
            self.lbl_status.setText("")
            return
        color_map = {
            "ok": TOKENS["ok"],
            "warn": TOKENS["warn"],
            "error": "#FDA4AF",
            "info": TOKENS["ink_secondary"],
        }
        color = color_map.get(tone, TOKENS["ink_secondary"])
        self.lbl_status.setText(message)
        self.lbl_status.setFont(ATLAS.font_for("body", 8.8, text=message))
        self.lbl_status.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        self.lbl_status.setVisible(True)

    def _update_list_state(self) -> None:
        count = self.list_exes.count()
        if self._lang == "th":
            self.lbl_count.setText(f"{count} รายการ")
        else:
            self.lbl_count.setText(f"{count} items")
        self.btn_remove.setEnabled(len(self.list_exes.selectedItems()) > 0)

    def _repopulate_sorted(self, items: list[str], select_exe: Optional[str] = None) -> None:
        sorted_items = _dedupe_sort_exes(items)
        self.list_exes.clear()
        target_low = select_exe.lower() if select_exe else None
        for exe in sorted_items:
            it = QtWidgets.QListWidgetItem(exe)
            self.list_exes.addItem(it)
            if target_low is not None and exe.lower() == target_low:
                self.list_exes.setCurrentItem(it)
                self.list_exes.scrollToItem(it)
        self._update_list_state()

    def add_exe_entry(self, raw_text: str) -> bool:
        """Validate, normalize, and insert an `.exe` entry if valid and not a duplicate."""
        normalized, err = normalize_exe_name(raw_text)
        if err == "empty":
            self._set_status(
                self._tr("กรุณาระบุชื่อไฟล์ .exe ก่อนเพิ่ม", "Enter an executable name before adding."),
                tone="warn",
            )
            return False
        if err or normalized is None:
            self._set_status(
                self._tr(
                    "ชื่อไฟล์ไม่ถูกต้อง (ห้ามใช้อักขระพิเศษ เช่น <>:\"|?*)",
                    "Invalid executable name (forbidden characters or malformed path).",
                ),
                tone="error",
            )
            return False

        norm_low = normalized.lower()
        for idx in range(self.list_exes.count()):
            existing_item = self.list_exes.item(idx)
            if existing_item is not None and existing_item.text().lower() == norm_low:
                self.list_exes.setCurrentItem(existing_item)
                self.list_exes.scrollToItem(existing_item)
                self._set_status(
                    self._tr(
                        f"มีรายการนี้อยู่แล้ว: {existing_item.text()}",
                        f"Already in exclusion list: {existing_item.text()}",
                    ),
                    tone="warn",
                )
                return False

        updated = self.excluded_exes() + [normalized]
        self._repopulate_sorted(updated, select_exe=normalized)
        self._set_status(
            self._tr(f"เพิ่ม {normalized} แล้ว", f"Added {normalized}"),
            tone="ok",
        )
        return True

    def _on_add_clicked(self) -> None:
        if self.add_exe_entry(self.ed_exe.text()):
            self.ed_exe.clear()

    def _on_browse_clicked(self) -> None:
        title = self._tr("เลือกไฟล์โปรแกรม (.exe)", "Select Executable (.exe)")
        flt = self._tr(
            "ไฟล์โปรแกรม (*.exe);;ทุกไฟล์ (*.*)",
            "Executable Files (*.exe);;All Files (*.*)",
        )
        chosen, _ = QtWidgets.QFileDialog.getOpenFileName(self, title, "", flt)
        if chosen:
            self.add_exe_entry(chosen)

    def _on_remove_selected(self) -> None:
        selected_items = self.list_exes.selectedItems()
        if not selected_items:
            return
        removed_count = len(selected_items)
        for item in selected_items:
            row = self.list_exes.row(item)
            if row >= 0:
                self.list_exes.takeItem(row)
        self._update_list_state()
        self._set_status(
            self._tr(
                f"ลบแล้ว {removed_count} รายการ",
                f"Removed {removed_count} item(s)",
            ),
            tone="info",
        )

    def _on_reset_defaults(self) -> None:
        self._repopulate_sorted(list(self._default_exes))
        self._set_status(
            self._tr(
                f"คืนค่ารายการเริ่มต้นแล้ว ({len(self._default_exes)} รายการ)",
                f"Restored default exclusions ({len(self._default_exes)} items)",
            ),
            tone="info",
        )

    def excluded_exes(self) -> list[str]:
        """Return the current deduplicated, case-insensitively sorted list of excluded `.exe` names."""
        out: list[str] = []
        for idx in range(self.list_exes.count()):
            item = self.list_exes.item(idx)
            if item is not None:
                out.append(item.text())
        return out
