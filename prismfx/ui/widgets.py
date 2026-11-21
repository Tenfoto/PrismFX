"""Sharp 0px architectural UI widgets powered by centralized PrismFX visual tokens."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.effects import EffectSpec
from .theme import ATLAS, TOKENS, has_thai


class ScanlineContainer(QtWidgets.QWidget):
    """Base container with pre-tiled 3px horizontal scanline surface texture."""

    def __init__(self, bg_hex: str = TOKENS["bg_base"], parent=None) -> None:
        super().__init__(parent)
        self._bg = QtGui.QColor(bg_hex)

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
        self._tag.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        self._tag.setStyleSheet(f"color: {TOKENS['accent_hi']}; letter-spacing: 1.4px;")
        lay.addWidget(self._tag)

        self._title = QtWidgets.QLabel(title)
        self._title.setFont(ATLAS.font_for("display", 14.5, text=title, bold=True))
        self._title.setStyleSheet(f"color: {TOKENS['ink_primary']};")
        lay.addWidget(self._title)

        if subtitle:
            self._sub = QtWidgets.QLabel(subtitle)
            self._sub.setFont(ATLAS.font_for("body", 10.0, text=subtitle))
            self._sub.setStyleSheet(f"color: {TOKENS['ink_secondary']};")
            self._sub.setWordWrap(True)
            lay.addWidget(self._sub)
        else:
            self._sub = None

        line = QtWidgets.QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background-color: {TOKENS['line']};")
        lay.addSpacing(4)
        lay.addWidget(line)

    def set_texts(self, tag: str, title: str, subtitle: str = "") -> None:
        self._tag.setText(tag.upper())
        self._title.setText(title)
        self._title.setFont(ATLAS.font_for("display", 14.5, text=title, bold=True))
        if self._sub is not None:
            self._sub.setText(subtitle)
            self._sub.setFont(ATLAS.font_for("body", 10.0, text=subtitle))


class MetricTile(PanelCard):
    """Tabular-nums telemetry readout tile with left accent bar."""

    def __init__(self, label: str, value: str, detail: str = "", accent: str = TOKENS["accent"], parent=None) -> None:
        super().__init__(recessed=False, parent=parent)
        self.setFixedHeight(86)
        self._accent = accent
        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QtWidgets.QFrame()
        bar.setFixedWidth(3)
        bar.setStyleSheet(f"background-color: {accent}; border: none;")
        outer.addWidget(bar)

        inner = QtWidgets.QVBoxLayout()
        inner.setContentsMargins(12, 9, 12, 9)
        inner.setSpacing(2)

        self._lbl = QtWidgets.QLabel(label.upper())
        self._lbl.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        self._lbl.setStyleSheet(f"color: {TOKENS['ink_muted']}; background: transparent; border: none;")
        inner.addWidget(self._lbl)

        self._val = QtWidgets.QLabel(value)
        self._val.setFont(ATLAS.font_for("mono", 16.0, bold=True))
        self._val.setStyleSheet(f"color: {TOKENS['ink_primary']}; background: transparent; border: none;")
        inner.addWidget(self._val)

        self._det = QtWidgets.QLabel(detail)
        self._det.setFont(ATLAS.font_for("body", 9.0, text=detail))
        self._det.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
        inner.addWidget(self._det)

        outer.addLayout(inner, 1)

    def set_value(self, value: str, detail: Optional[str] = None) -> None:
        self._val.setText(value)
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

    def _flip(self) -> None:
        self._checked = not self._checked
        self._refresh()
        self.toggled_state.emit(self._checked)

    def _refresh(self) -> None:
        self.setFont(ATLAS.font_for("mono", 8.5, bold=True))
        if self._checked:
            self.setText(f"●  {self._on_text}")
            self.setStyleSheet(
                f"QPushButton {{ background: {TOKENS['accent']}; border: 1px solid {TOKENS['accent_hi']}; "
                f"color: #FFFFFF; padding: 4px 12px; }}"
                f"QPushButton:hover {{ background: #3B8BFF; }}"
            )
        else:
            self.setText(f"○  {self._off_text}")
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
        self.setFixedHeight(84)

        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._bar = QtWidgets.QFrame()
        self._bar.setFixedWidth(4)
        self._bar.setStyleSheet(f"background-color: {spec.accent_hex}; border: none;")
        outer.addWidget(self._bar)

        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(10, 8, 10, 8)
        body.setSpacing(3)

        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        title = spec.name_th if lang == "th" else spec.name_en
        self._lbl_title = QtWidgets.QLabel(title)
        self._lbl_title.setFont(ATLAS.font_for("body", 10.5, text=title, bold=True))
        self._lbl_title.setStyleSheet(f"color: {TOKENS['ink_primary']}; background: transparent; border: none;")
        top.addWidget(self._lbl_title, 1)

        self._badge = QtWidgets.QLabel(spec.badge)
        self._badge.setFont(ATLAS.font_for("mono", 7.5, bold=True))
        self._badge.setStyleSheet(
            f"background: {TOKENS['bg_recessed']}; color: {spec.accent_hex}; "
            f"border: 1px solid {TOKENS['line_strong']}; padding: 1px 5px;"
        )
        top.addWidget(self._badge)
        body.addLayout(top)

        sub_title = spec.name_en if lang == "th" else f"{spec.default_ms} ms"
        desc = spec.desc_th if lang == "th" else spec.desc_en
        self._lbl_desc = QtWidgets.QLabel(f"{sub_title} — {desc}" if lang == "th" else desc)
        self._lbl_desc.setFont(ATLAS.font_for("body", 8.8, text=desc))
        self._lbl_desc.setStyleSheet(f"color: {TOKENS['ink_secondary']}; background: transparent; border: none;")
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
