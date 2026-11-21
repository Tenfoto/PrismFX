"""Procedurally generates crisp multi-resolution PrismFX app icons (.ico + .png) with zero external assets."""

from __future__ import annotations

import pathlib
import struct
import sys

from PySide6 import QtCore, QtGui, QtWidgets


def render_icon_image(size: int) -> QtGui.QImage:
    img = QtGui.QImage(size, size, QtGui.QImage.Format_RGBA8888)
    img.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)

    # Sharp 0px architectural dark tile
    pad = max(1, size // 16)
    rect = QtCore.QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    p.fillRect(rect, QtGui.QColor("#121316"))
    p.setPen(QtGui.QPen(QtGui.QColor("#2D81FF"), max(1.0, size / 24.0)))
    p.drawRect(rect)

    # Luminous architectural prism diamond + refraction planes
    cx, cy = size * 0.5, size * 0.5
    r = size * 0.30
    poly_l = QtGui.QPolygonF([
        QtCore.QPointF(cx, cy - r),
        QtCore.QPointF(cx - r * 0.85, cy + r * 0.72),
        QtCore.QPointF(cx, cy + r * 0.32),
    ])
    poly_r = QtGui.QPolygonF([
        QtCore.QPointF(cx, cy - r),
        QtCore.QPointF(cx + r * 0.85, cy + r * 0.72),
        QtCore.QPointF(cx, cy + r * 0.32),
    ])
    poly_b = QtGui.QPolygonF([
        QtCore.QPointF(cx - r * 0.85, cy + r * 0.72),
        QtCore.QPointF(cx + r * 0.85, cy + r * 0.72),
        QtCore.QPointF(cx, cy + r * 0.32),
    ])

    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor("#2D81FF"))
    p.drawPolygon(poly_l)
    p.setBrush(QtGui.QColor("#74AAFF"))
    p.drawPolygon(poly_r)
    p.setBrush(QtGui.QColor("#38BDF8"))
    p.drawPolygon(poly_b)

    # Subtle scanline accent
    p.setPen(QtGui.QPen(QtGui.QColor(242, 241, 238, 90), max(1.0, size / 64.0)))
    p.drawLine(QtCore.QPointF(cx, cy - r), QtCore.QPointF(cx, cy + r * 0.32))

    p.end()
    return img


def write_ico(images: list[QtGui.QImage], out_path: pathlib.Path) -> None:
    png_blobs: list[tuple[int, bytes]] = []
    for im in images:
        buf = QtCore.QBuffer()
        buf.open(QtCore.QIODevice.WriteOnly)
        im.save(buf, "PNG")
        png_blobs.append((im.width(), bytes(buf.data())))

    header = struct.pack("<HHH", 0, 1, len(png_blobs))
    offset = 6 + 16 * len(png_blobs)
    entries = bytearray()
    payload = bytearray()
    for sz, blob in png_blobs:
        w_byte = 0 if sz >= 256 else sz
        entries.extend(struct.pack("<BBBBHHII", w_byte, w_byte, 0, 0, 1, 32, len(blob), offset))
        payload.extend(blob)
        offset += len(blob)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(header + bytes(entries) + bytes(payload))


def main() -> int:
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    root = pathlib.Path(__file__).resolve().parent.parent
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    sizes = (16, 24, 32, 48, 64, 128, 256)
    src_png = assets / "prismfx_source.png"
    if src_png.exists():
        master = QtGui.QImage(str(src_png)).convertToFormat(QtGui.QImage.Format_RGBA8888)
        imgs = [
            master.scaled(s, s, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            for s in sizes
        ]
    else:
        imgs = [render_icon_image(s) for s in sizes]
    write_ico(imgs, assets / "prismfx.ico")
    imgs[-1].save(str(assets / "prismfx_256.png"))
    print(f"Generated {assets / 'prismfx.ico'} and {assets / 'prismfx_256.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
