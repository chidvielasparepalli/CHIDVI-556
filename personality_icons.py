"""Profile-owned portraits with themed, font-independent fallback emblems."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QPolygonF


def personality_icon(personality_id: str, profile: dict, asset_root=None) -> QPixmap:
    """Return a usable portrait or emblem for any dynamically discovered ID."""
    asset = str((profile.get("avatar") or {}).get("asset") or "").strip()
    if asset:
        try:
            data = (Path(asset_root or Path(__file__).resolve().parent) / asset).read_bytes()
        except (OSError, ValueError):
            pass
        else:
            # Qt accepts some truncated JPEGs as gray/partial portraits.
            complete = not data.startswith(b"\xff\xd8") or b"\xff\xd9" in data
            pixmap = QPixmap()
            if complete and pixmap.loadFromData(data) and not pixmap.isNull():
                return _round_icon(pixmap)
    return _round_icon(_fallback_icon(personality_id, profile))


def _round_icon(pixmap: QPixmap) -> QPixmap:
    scaled = pixmap.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    icon = QPixmap(512, 512)
    icon.fill(Qt.GlobalColor.transparent)
    painter = QPainter(icon)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, 512, 512))
    painter.setClipPath(clip)
    painter.drawPixmap((512 - scaled.width()) // 2, (512 - scaled.height()) // 2, scaled)
    painter.end()
    return icon


def _fallback_icon(personality_id: str, profile: dict) -> QPixmap:
    digest = hashlib.sha256(personality_id.encode()).digest()
    color = QColor(str((profile.get("theme") or {}).get("primary") or ""))
    if not color.isValid():
        hue = int.from_bytes(digest[:2], "big") % 360
        color = QColor.fromHsv(hue, 170, 220)

    # Paint large enough for a selected radial node on a high-DPI display.
    pixmap = QPixmap(512, 512)
    pixmap.fill(QColor("#111923"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(4, 4)
    painter.setPen(QPen(color, 4))
    painter.setBrush(QColor(color.red(), color.green(), color.blue(), 55))
    painter.drawEllipse(QRectF(6, 6, 116, 116))
    painter.setPen(QPen(QColor("#f4f8ff"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if personality_id == "sherlock":
        painter.drawEllipse(QRectF(29, 24, 49, 49))
        painter.drawLine(QPointF(73, 69), QPointF(99, 98))
    elif personality_id == "gojo":
        infinity = QPainterPath(QPointF(64, 64))
        infinity.cubicTo(22, 11, 6, 110, 64, 64)
        infinity.cubicTo(122, 18, 106, 117, 64, 64)
        painter.drawPath(infinity)
    elif personality_id == "makise":
        flask = QPainterPath(QPointF(51, 27))
        flask.lineTo(77, 27)
        flask.moveTo(55, 27)
        flask.lineTo(55, 54)
        flask.lineTo(31, 95)
        flask.quadTo(29, 101, 37, 101)
        flask.lineTo(91, 101)
        flask.quadTo(99, 101, 97, 95)
        flask.lineTo(73, 54)
        flask.lineTo(73, 27)
        painter.drawPath(flask)
        painter.drawLine(QPointF(44, 77), QPointF(84, 77))
        painter.drawEllipse(QPointF(62, 89), 2, 2)
    elif personality_id == "deadpool":
        painter.setBrush(QColor("#b72635"))
        painter.drawEllipse(QRectF(29, 22, 70, 85))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#17151c"))
        painter.drawEllipse(QRectF(35, 41, 23, 40))
        painter.drawEllipse(QRectF(70, 41, 23, 40))
        painter.setBrush(QColor("#ffffff"))
        painter.drawPolygon(QPolygonF([QPointF(38, 53), QPointF(56, 59), QPointF(46, 64)]))
        painter.drawPolygon(QPolygonF([QPointF(90, 53), QPointF(72, 59), QPointF(82, 64)]))
    elif personality_id == "naruto":
        leaf = QPainterPath(QPointF(97, 31))
        leaf.lineTo(83, 50)
        leaf.cubicTo(108, 90, 52, 113, 32, 80)
        leaf.cubicTo(7, 37, 67, 18, 77, 57)
        leaf.cubicTo(85, 89, 39, 91, 46, 61)
        leaf.cubicTo(50, 49, 65, 51, 66, 64)
        painter.drawPath(leaf)
        painter.drawLine(QPointF(33, 82), QPointF(24, 98))
    elif personality_id == "goku":
        painter.setBrush(QColor("#ef9b24"))
        painter.drawEllipse(QRectF(24, 24, 80, 80))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#a32324"))
        for cx, cy in ((49, 49), (79, 49), (49, 79), (79, 79)):
            points = []
            for i in range(10):
                angle = math.radians(-90 + i * 36)
                radius = 11 if i % 2 == 0 else 4.5
                points.append(QPointF(cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
            painter.drawPolygon(QPolygonF(points))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f4f8ff"))
        for row in range(5):
            for column in range(3):
                if digest[row] & (1 << column) or (row == 2 and column == 2):
                    for mirrored in {column, 4 - column}:
                        painter.drawRoundedRect(QRectF(33 + mirrored * 13, 33 + row * 13, 10, 10), 2, 2)

    painter.end()
    return pixmap
