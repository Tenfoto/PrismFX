"""Sharp 0px architectural UI widgets powered by centralized PrismFX visual tokens."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.effects import EffectSpec, sanitize_hex_color
from .theme import ATLAS, TOKENS, has_thai


class ScanlineContainer(QtWidgets.QWidget):
    """Base container with pre-tiled 3px horizontal scanline surface texture."""

    def __init__(self, bg_hex: str = TOKENS["bg_base"], parent=None) -> None:
        super().__init__(parent)
        self._bg = QtGui.QColor(sanitize_hex_color(bg_hex, TOKENS["bg_base"]))

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), self._bg)
        p.drawTiledPixmap(self.rect(), ATLAS.scanline_tile())
        p.end()


class PanelCard(QtWidgets.QFrame):
    """Elevated 0px-radius architectural panel with 1px structural hairline border."""

    def __init__(self, recessed: bool = False, parent=None) -> None:
        super().__init__(parent)
        bg = TOKENS["bg_recessed"] if recessed else TOKENS["bg_elevated"]
        self.setStyleSheet(
            f"PanelCard {{ background-color: {bg}; border: 1px solid {TOKENS['line']}; }}"
        )


class SectionHeader(QtWidgets.QWidget):
    """Crisp uppercase mono tag + bilingual title + 1px accent hairline."""

    def __init__(self, tag: str, title: str, subtitle: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 6)
        lay.setSpacing(2)

        self._tag = QtWidgets.QLabel(tag.upper())
        self._tag.setFont(ATLAS.font_for("mono", 8.5, text=tag, bold=True))
        self._tag.setStyleSheet(f"color: {TOKENS['accent_hi']}; letter-spacing: 1.4px;")
        lay.addWidget(self._tag)

        self._title = QtWidgets.QLabel(title)
        self._title.setFont(ATLAS.font_for("display", 14.5, text=title, bold=True))
        self._title.setStyleSheet(f"color: {TOKENS['ink_primary']};")
        lay.addWidget(self._title)

        self._sub = QtWidgets.QLabel(subtitle)
        self._sub.setFont(ATLAS.font_for("body", 10.0, text=subtitle))
        self._sub.setStyleSheet(f"color: {TOKENS['ink_secondary']};")
        self._sub.setWordWrap(True)
        self._sub.setVisible(bool(subtitle))
        lay.addWidget(self._sub)

        line = QtWidgets.QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background-color: {TOKENS['line']};")
        lay.addSpacing(4)
        lay.addWidget(line)

    def set_texts(self, tag: str, title: str, subtitle: str = "") -> None:
        self._tag.setText(tag.upper())
        self._tag.setFont(ATLAS.font_for("mono", 8.5, text=tag, bold=True))
        self._title.setText(title)
        self._title.setFont(ATLAS.font_for("display", 14.5, text=title, bold=True))
        self._sub.setText(subtitle)
        self._sub.setFont(ATLAS.font_for("body", 10.0, text=subtitle))
        self._sub.setVisible(bool(subtitle))

    def title_text(self) -> str:
        return self._title.text()

    def subtitle_text(self) -> str:
        return self._sub.text()


class MetricTile(PanelCard):
    """Tabular-nums telemetry readout tile with left accent bar."""

    def __init__(self, label: str, value: str, detail: str = "", accent: str = TOKENS["accent"], parent=None) -> None:
        super().__init__(recessed=False, parent=parent)
        self.setFixedHeight(78)
        safe_accent = sanitize_hex_color(accent, TOKENS["accent"])
        self._accent = safe_accent
        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QtWidgets.QFrame()
        bar.setFixedWidth(3)
        bar.setStyleSheet(f"background-color: {safe_accent}; border: none;")
        outer.addWidget(bar)

        inner = QtWidgets.QVBoxLayout()
        inner.setContentsMargins(12, 7, 12, 7)
        inner.setSpacing(1)

        self._lbl = QtWidgets.QLabel(label.upper())
        self._lbl.setFont(ATLAS.font_for("mono", 8.0, text=label, bold=True))
        self._lbl.setStyleSheet(f"color: {TOKENS['ink_muted']}; background: transparent; border: none;")
        inner.addWidget(self._lbl)

        self._val = QtWidgets.QLabel(value)
        self._val.setFont(ATLAS.font_for("mono", 16.0, text=value, bold=True))
        self._val.setStyleSheet(f"color: {TOKENS['ink_primary']}; background: transparent; border: none;")
        inner.addWidget(self._val)

        self._det = QtWidgets.QLabel(detail)
        self._det.setFont(ATLAS.font_for("body", 9.0, text=detail))
        self._det.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
        inner.addWidget(self._det)

        outer.addLayout(inner, 1)

    def set_label(self, label: str) -> None:
        txt = label.upper()
        self._lbl.setText(txt)
        self._lbl.setFont(ATLAS.font_for("mono", 8.0, text=txt, bold=True))

    def set_value(self, value: str, detail: Optional[str] = None) -> None:
        self._val.setText(value)
        self._val.setFont(ATLAS.font_for("mono", 16.0, text=value, bold=True))
        if detail is not None:
            self._det.setText(detail)
            self._det.setFont(ATLAS.font_for("body", 9.0, text=detail))


class ArchToggle(QtWidgets.QPushButton):
    """Sharp architectural ON/OFF switch button."""

    toggled_state = QtCore.Signal(bool)

    def __init__(self, checked: bool = True, on_text: str = "ACTIVE", off_text: str = "PAUSED", parent=None) -> None:
        super().__init__(parent)
        self._checked = checked
        self._on_text = on_text
        self._off_text = off_text
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFixedHeight(30)
        self.setMinimumWidth(96)
        self.clicked.connect(self._flip)
        self._refresh()

    def is_on(self) -> bool:
        return self._checked

    def set_on(self, val: bool, emit: bool = False) -> None:
        self._checked = bool(val)
        self._refresh()
        if emit:
            self.toggled_state.emit(self._checked)

    def set_texts(self, on_text: str, off_text: str) -> None:
        self._on_text = on_text
        self._off_text = off_text
        self._refresh()

    def _flip(self) -> None:
        self._checked = not self._checked
        self._refresh()
        self.toggled_state.emit(self._checked)

    def _refresh(self) -> None:
        txt = f"●  {self._on_text}" if self._checked else f"○  {self._off_text}"
        self.setFont(ATLAS.font_for("mono", 8.5, text=txt, bold=True))
        self.setText(txt)
        if self._checked:
            self.setStyleSheet(
                f"QPushButton {{ background: {TOKENS['accent']}; border: 1px solid {TOKENS['accent_hi']}; "
                f"color: #FFFFFF; padding: 4px 12px; }}"
                f"QPushButton:hover {{ background: #3B8BFF; }}"
            )
        else:
            self.setStyleSheet(
                f"QPushButton {{ background: {TOKENS['bg_recessed']}; border: 1px solid {TOKENS['line_strong']}; "
                f"color: {TOKENS['ink_muted']}; padding: 4px 12px; }}"
                f"QPushButton:hover {{ border-color: {TOKENS['accent']}; color: {TOKENS['ink_primary']}; }}"
            )


class SliderRow(QtWidgets.QWidget):
    """Compact labeled slider with tabular-nums mono readout."""

    value_changed = QtCore.Signal(int)

    def __init__(
        self,
        label: str,
        min_v: int,
        max_v: int,
        val: int,
        suffix: str = "",
        formatter: Optional[Callable[[int], str]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._suffix = suffix
        self._fmt = formatter
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)

        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self._lbl = QtWidgets.QLabel(label)
        self._lbl.setFont(ATLAS.font_for("body", 10.0, text=label))
        self._lbl.setStyleSheet(f"color: {TOKENS['ink_primary']};")
        self._readout = QtWidgets.QLabel()
        self._readout.setFont(ATLAS.font_for("mono", 9.5, bold=True))
        self._readout.setStyleSheet(f"color: {TOKENS['accent_hi']};")
        top.addWidget(self._lbl, 1)
        top.addWidget(self._readout)
        lay.addLayout(top)

        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(min_v, max_v)
        self.slider.setValue(val)
        self.slider.valueChanged.connect(self._on_val)
        lay.addWidget(self.slider)
        self._update_readout(val)
        self._apply_enabled_style()

    def _apply_enabled_style(self) -> None:
        if self.isEnabled():
            self._lbl.setStyleSheet(f"color: {TOKENS['ink_primary']};")
            self._readout.setStyleSheet(f"color: {TOKENS['accent_hi']};")
            self.slider.setStyleSheet("")
        else:
            self._lbl.setStyleSheet(f"color: {TOKENS['ink_muted']};")
            self._readout.setStyleSheet(f"color: {TOKENS['ink_muted']};")
            self.slider.setStyleSheet(
                f"QSlider::sub-page:horizontal {{ background: {TOKENS['line_strong']}; }}"
                f"QSlider::handle:horizontal {{ background: {TOKENS['ink_muted']}; "
                f"border-color: {TOKENS['line_strong']}; }}"
            )

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.EnabledChange:
            self._apply_enabled_style()

    def _update_readout(self, v: int) -> None:
        txt = self._fmt(v) if self._fmt else f"{v}{self._suffix}"
        self._readout.setText(txt)

    def _on_val(self, v: int) -> None:
        self._update_readout(v)
        self.value_changed.emit(v)

    def set_value(self, v: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(v)
        self._update_readout(v)
        self.slider.blockSignals(False)

    def set_label(self, label: str) -> None:
        self._lbl.setText(label)
        self._lbl.setFont(ATLAS.font_for("body", 10.0, text=label))


class EffectCard(QtWidgets.QFrame):
    """Selectable GPU effect card with accent stripe, badge, and bilingual title/description."""

    selected = QtCore.Signal(str)

    def __init__(self, spec: EffectSpec, lang: str = "th", is_selected: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.spec = spec
        self._selected = is_selected
        self._lang = lang
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setMinimumHeight(92)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Minimum)

        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        safe_accent = sanitize_hex_color(spec.accent_hex, TOKENS["accent"])

        self._bar = QtWidgets.QFrame()
        self._bar.setFixedWidth(4)
        self._bar.setStyleSheet(f"background-color: {safe_accent}; border: none;")
        outer.addWidget(self._bar)

        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(10, 8, 10, 8)
        body.setSpacing(3)

        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        title = spec.name_th if lang == "th" else spec.name_en
        self._lbl_title = QtWidgets.QLabel(title)
        self._lbl_title.setFont(ATLAS.font_for("body", 10.2, text=title, bold=True))
        self._lbl_title.setStyleSheet(f"color: {TOKENS['ink_primary']}; background: transparent; border: none;")
        self._lbl_title.setMinimumWidth(0)
        self._lbl_title.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        top.addWidget(self._lbl_title, 1)

        self._badge = QtWidgets.QLabel(spec.badge)
        self._badge.setFont(ATLAS.font_for("mono", 7.5, bold=True))
        self._badge.setStyleSheet(
            f"background: {TOKENS['bg_recessed']}; color: {safe_accent}; "
            f"border: 1px solid {TOKENS['line_strong']}; padding: 1px 5px;"
        )
        self._badge.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        top.addWidget(self._badge)
        body.addLayout(top)

        sub_title = spec.name_en if lang == "th" else f"{spec.default_ms} ms"
        desc = spec.desc_th if lang == "th" else spec.desc_en
        self._lbl_desc = QtWidgets.QLabel(f"{sub_title} — {desc}" if lang == "th" else desc)
        self._lbl_desc.setFont(ATLAS.font_for("body", 8.8, text=desc))
        self._lbl_desc.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
        self._lbl_desc.setMinimumWidth(0)
        self._lbl_desc.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Minimum)
        self._lbl_desc.setWordWrap(True)
        body.addWidget(self._lbl_desc, 1)

        outer.addLayout(body, 1)
        self._apply_style()

    def set_selected(self, flag: bool) -> None:
        self._selected = flag
        self._apply_style()

    def _apply_style(self) -> None:
        if self._selected:
            self.setStyleSheet(
                f"EffectCard {{ background-color: {TOKENS['bg_selected']}; "
                f"border: 1px solid {TOKENS['accent']}; }}"
            )
        else:
            self.setStyleSheet(
                f"EffectCard {{ background-color: {TOKENS['bg_elevated']}; "
                f"border: 1px solid {TOKENS['line']}; }}"
                f"EffectCard:hover {{ background-color: {TOKENS['bg_hover']}; "
                f"border: 1px solid {TOKENS['line_strong']}; }}"
            )

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self.selected.emit(self.spec.id)
        super().mousePressEvent(event)


class PresetItemWidget(QtWidgets.QFrame):
    """Uniform-height Home preset card with pixel-accurate grapheme-safe elision and custom delete button."""

    applied = QtCore.Signal(str)
    delete_requested = QtCore.Signal(str)

    def __init__(
        self,
        preset_name: str,
        desc: str,
        is_builtin: bool,
        active: bool,
        delete_tooltip: str = "Delete preset",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.preset_name = preset_name
        self.full_desc = desc
        self.is_builtin = bool(is_builtin)
        self._active = bool(active)
        self._btn_delete: Optional[QtWidgets.QPushButton] = None
        self._last_elide_width: int = -1

        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFixedHeight(48)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setToolTip(desc)

        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(10, 5, 8, 5)
        outer.setSpacing(8)

        text_col = QtWidgets.QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)

        title_str = f"▸ {preset_name}" if self._active else preset_name
        self._lbl_title = QtWidgets.QLabel(title_str)
        self._lbl_title.setFont(ATLAS.font_for("body", 9.2, text=preset_name, bold=True))
        self._lbl_title.setStyleSheet(
            f"color: {TOKENS['ink_primary']}; background: transparent; border: none;"
        )
        self._lbl_title.setMinimumWidth(0)
        self._lbl_title.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        text_col.addWidget(self._lbl_title)

        self._lbl_desc = QtWidgets.QLabel()
        self._lbl_desc.setFont(ATLAS.font_for("body", 8.5, text=desc))
        self._lbl_desc.setStyleSheet(
            f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;"
        )
        self._lbl_desc.setMinimumWidth(0)
        self._lbl_desc.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self._lbl_desc.setWordWrap(False)
        self._lbl_desc.setToolTip(desc)
        text_col.addWidget(self._lbl_desc)

        outer.addLayout(text_col, 1)

        if not self.is_builtin:
            btn_del = QtWidgets.QPushButton("✕")
            btn_del.setObjectName("btn_del_preset")
            btn_del.setFixedSize(24, 24)
            btn_del.setCursor(QtCore.Qt.PointingHandCursor)
            btn_del.setToolTip(delete_tooltip)
            btn_del.setFont(ATLAS.font_for("mono", 8.5, bold=True))
            btn_del.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid transparent; "
                f"color: {TOKENS['ink_muted']}; padding: 0px; }}"
                f"QPushButton:hover {{ background: #3A1A23; border: 1px solid {TOKENS['danger']}; "
                f"color: #FDA4AF; }}"
            )
            btn_del.clicked.connect(lambda _=False: self.delete_requested.emit(self.preset_name))
            self._btn_delete = btn_del
            outer.addWidget(btn_del, 0, QtCore.Qt.AlignVCenter)

        self._apply_style()
        self._update_elided_desc()

    @property
    def _full_desc(self) -> str:
        return self.full_desc

    def desc_text(self) -> str:
        if self.width() != self._last_elide_width:
            self._update_elided_desc()
        return self._lbl_desc.text()

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self._lbl_title.setText(f"▸ {self.preset_name}" if self._active else self.preset_name)
        self._apply_style()

    def _apply_style(self) -> None:
        bg = TOKENS["bg_selected"] if self._active else TOKENS["bg_recessed"]
        border = TOKENS["accent"] if self._active else TOKENS["line"]
        self.setStyleSheet(
            f"PresetItemWidget {{ background-color: {bg}; border: 1px solid {border}; }}"
            f"PresetItemWidget:hover {{ border: 1px solid {TOKENS['accent_hi']}; }}"
        )

    def _update_elided_desc(self) -> None:
        self._last_elide_width = self.width()
        if not self.full_desc:
            self._lbl_desc.setText("")
            return
        lay = self.layout()
        margins = lay.contentsMargins() if lay is not None else QtCore.QMargins(10, 5, 8, 5)
        del_w = (self._btn_delete.width() + 8) if self._btn_delete is not None else 0
        avail_w = self.width() - margins.left() - margins.right() - del_w - 4
        if avail_w <= 24:
            avail_w = max(24, self._lbl_desc.width() if self._lbl_desc.width() > 24 else 220)
        fm = QtGui.QFontMetrics(self._lbl_desc.font())
        elided = fm.elidedText(self.full_desc, QtCore.Qt.ElideRight, max(24, avail_w))
        self._lbl_desc.setText(elided)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_elided_desc()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        self._update_elided_desc()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self.applied.emit(self.preset_name)
        super().mousePressEvent(event)


class GlobalToastBanner(QtWidgets.QFrame):
    """App-wide 0px architectural toast notification banner visible across all Studio pages."""

    _KIND_STYLES: dict[str, tuple[str, str, str, str]] = {
        # kind -> (accent_hex, bg_hex, glyph, glyph_color)
        "info": (TOKENS["accent"], "#131C2B", "ℹ", TOKENS["accent_hi"]),
        "ok": (TOKENS["ok"], "#11221C", "✓", TOKENS["ok"]),
        "warn": (TOKENS["warn"], "#241C0F", "⚠", TOKENS["warn"]),
        "danger": (TOKENS["danger"], "#261419", "!", "#FDA4AF"),
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("global_toast_banner")
        self._message: str = ""
        self._kind: str = "info"
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)

        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._bar = QtWidgets.QFrame()
        self._bar.setFixedWidth(3)
        outer.addWidget(self._bar)

        inner = QtWidgets.QHBoxLayout()
        inner.setContentsMargins(10, 7, 8, 7)
        inner.setSpacing(8)

        self._lbl_icon = QtWidgets.QLabel("ℹ")
        self._lbl_icon.setFixedWidth(16)
        self._lbl_icon.setAlignment(QtCore.Qt.AlignCenter)
        self._lbl_icon.setFont(ATLAS.font_for("mono", 10.0, bold=True))
        inner.addWidget(self._lbl_icon, 0, QtCore.Qt.AlignVCenter)

        self._lbl_msg = QtWidgets.QLabel("")
        self._lbl_msg.setWordWrap(True)
        self._lbl_msg.setMinimumWidth(0)
        self._lbl_msg.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        self._lbl_msg.setFont(ATLAS.font_for("body", 9.5))
        self._lbl_msg.setStyleSheet(f"color: {TOKENS['ink_primary']}; background: transparent; border: none;")
        inner.addWidget(self._lbl_msg, 1)

        self._btn_close = QtWidgets.QPushButton("✕")
        self._btn_close.setObjectName("btn_toast_close")
        self._btn_close.setFixedSize(22, 22)
        self._btn_close.setCursor(QtCore.Qt.PointingHandCursor)
        self._btn_close.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        self._btn_close.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid transparent; "
            f"color: {TOKENS['ink_muted']}; padding: 0px; }}"
            f"QPushButton:hover {{ border-color: {TOKENS['line_strong']}; color: {TOKENS['ink_primary']}; }}"
        )
        self._btn_close.clicked.connect(self.dismiss)
        inner.addWidget(self._btn_close, 0, QtCore.Qt.AlignVCenter)

        outer.addLayout(inner, 1)

        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

        self._apply_kind_style("info")
        self.hide()

    def _apply_kind_style(self, kind: str) -> None:
        accent, bg, glyph, glyph_color = self._KIND_STYLES.get(kind, self._KIND_STYLES["info"])
        self._bar.setStyleSheet(f"background-color: {accent}; border: none;")
        self._lbl_icon.setText(glyph)
        self._lbl_icon.setStyleSheet(f"color: {glyph_color}; background: transparent; border: none;")
        self.setStyleSheet(
            f"GlobalToastBanner {{ background-color: {bg}; border: 1px solid {TOKENS['line_strong']}; }}"
        )

    def show_toast(self, message: str, kind: str = "info", duration_ms: int = 4500) -> None:
        self._timer.stop()
        norm_kind = kind if kind in self._KIND_STYLES else "info"
        self._kind = norm_kind
        self._message = str(message)
        self._apply_kind_style(norm_kind)
        self._lbl_msg.setText(self._message)
        self._lbl_msg.setFont(ATLAS.font_for("body", 9.5, text=self._message))
        self.show()
        if duration_ms and int(duration_ms) > 0:
            self._timer.start(int(duration_ms))

    def dismiss(self) -> None:
        self._timer.stop()
        self._message = ""
        self.hide()

    def current_message(self) -> str:
        return self._message
