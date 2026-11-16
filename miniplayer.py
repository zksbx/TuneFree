# -*- coding: utf-8 -*-
"""迷你播放器：一个无边框小窗，贴在桌面上控制播放。

它自己不放歌，只显示主窗口 Player 的状态并发信号。这样主窗口和迷你窗
永远同步，关掉迷你窗也不影响播放。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QMenu, QWidget

W = 420                     # 窗口宽（含阴影留白）
H = 124                     # 窗口高（含阴影留白）
PAD_X = 10                  # 左右留白
PAD_TOP = 8                 # 顶部留白
CONTENT_H = 104             # 内容板高度
COVER = 54                  # 封面边长
RADIUS = 14                 # 内容板圆角

DEFAULT_THEME = {
    "panel": "#ffffff",
    "line": "#e3e7ef",
    "fg": "#1b1f27",
    "sub": "#6b7280",
    "accent": "#2f6bff",
    "accent2": "#7dd3fc",
    "accent_soft": "#e8efff",
    "chip": "#f0f3f9",
    "hover": "#f5f8ff",
}


class MiniPlayer(QWidget):
    """无边框、置顶、可拖动的小播放条。"""

    playPauseClicked = Signal()
    prevClicked = Signal()
    nextClicked = Signal()
    restoreRequested = Signal()
    closeRequested = Signal()
    seekRequested = Signal(float)        # 0.0 - 1.0
    movedByUser = Signal(object)         # QPoint：拖完之后的位置

    def __init__(self, parent=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setWindowTitle("TuneFree 迷你播放器")
        self.setFixedSize(W, H)

        self._cover = QPixmap()
        self._title = "未在播放"
        self._artist = "双击列表里的歌名开始播放"
        self._lyric = ""
        self._playing = False
        self._pos = 0
        self._dur = 0
        self._hover = ""
        self._pressed = ""
        self._drag_from = None
        self._drag_moved = False
        self._theme = dict(DEFAULT_THEME)

    # ---------- 外部状态 ----------
    def set_theme(self, colors):
        """主窗口换主题时把颜色推过来。"""
        if not colors:
            return
        for key in DEFAULT_THEME:
            if key in colors:
                self._theme[key] = colors[key]
        self.update()

    def set_track(self, title, artist, cover_path):
        self._title = title or "未在播放"
        self._artist = artist or ""
        self._cover = QPixmap()
        if cover_path and os.path.isfile(cover_path):
            pix = QPixmap(cover_path)
            if not pix.isNull():
                self._cover = pix
        self.update()

    def set_playback(self, playing, pos=0, dur=0):
        self._playing = bool(playing)
        self._pos = int(pos or 0)
        self._dur = int(dur or 0)
        self.update()

    def set_lyrics(self, text):
        """当前这句歌词；空字符串表示这首歌没有可用歌词。"""
        text = (text or "").strip()
        if text == self._lyric:
            return
        self._lyric = text
        self.update()

    # ---------- 区域计算 ----------
    def _areas(self):
        left = PAD_X + 14
        right = PAD_X + (W - 2 * PAD_X) - 14
        cy = PAD_TOP + CONTENT_H / 2.0

        next_c = QPointF(right - 14, cy)
        play_c = QPointF(next_c.x() - 14 - 10 - 17, cy)
        prev_c = QPointF(play_c.x() - 17 - 10 - 14, cy)

        close = QRectF(right - 20, PAD_TOP + 6, 20, 20)
        cover = QRectF(left, PAD_TOP + (CONTENT_H - COVER) / 2.0, COVER, COVER)
        text_left = cover.right() + 12
        text_right = prev_c.x() - 14 - 16
        return {
            "cover": cover,
            "title": QRectF(text_left, PAD_TOP + 20, max(40, text_right - text_left), 20),
            "artist": QRectF(text_left, PAD_TOP + 40, max(40, text_right - text_left), 16),
            "lyric": QRectF(text_left, PAD_TOP + 58, max(40, text_right - text_left), 18),
            "prev": QRectF(prev_c.x() - 14, prev_c.y() - 14, 28, 28),
            "play": QRectF(play_c.x() - 17, play_c.y() - 17, 34, 34),
            "next": QRectF(next_c.x() - 14, next_c.y() - 14, 28, 28),
            "close": close,
            "seek": QRectF(left, PAD_TOP + CONTENT_H - 12, right - left, 4),
        }

    def _area_at(self, pos):
        for name in ("play", "prev", "next", "close", "seek"):
            if self._areas()[name].adjusted(-3, -3, 3, 3).contains(pos):
                return name
        return ""

    # ---------- 绘制 ----------
    def paintEvent(self, _event):
        t = self._theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        board = QRectF(PAD_X, PAD_TOP, W - 2 * PAD_X, CONTENT_H)
        self._draw_shadow(p, board)
        path = QPainterPath()
        path.addRoundedRect(board, RADIUS, RADIUS)
        p.fillPath(path, QColor(t["panel"]))
        pen = QPen(QColor(t["line"]))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        areas = self._areas()
        self._draw_cover(p, areas["cover"])
        self._draw_text(p, areas, t)
        self._draw_seek(p, areas["seek"], t)

        for name in ("prev", "play", "next"):
            self._draw_button(p, areas[name], name, t)
        if self._hover or self._pressed:
            self._draw_close(p, areas["close"], t)

        p.end()

    def _draw_shadow(self, p, board):
        """自己叠几层半透明圆角矩形当阴影。

        不用 QGraphicsDropShadowEffect：带 effect 的窗口在 grab()/高 DPI 下
        会把内容按逻辑坐标画进放大后的 pixmap，标题和封面都会错位。
        """
        p.save()
        p.setPen(Qt.NoPen)
        for spread, alpha in ((9, 4), (8, 5), (7, 6), (6, 7), (5, 8),
                              (4, 9), (3, 10), (2, 12), (1, 14)):
            rect = board.adjusted(-spread, -spread + 4, spread, spread + 4)
            path = QPainterPath()
            path.addRoundedRect(rect, RADIUS + spread, RADIUS + spread)
            p.fillPath(path, QColor(0, 0, 0, alpha))
        p.restore()

    def _draw_cover(self, p, rect):
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(rect, 10, 10)
        p.setClipPath(clip)
        if self._cover.isNull():
            p.fillRect(rect, QColor(self._theme["chip"]))
            p.setPen(QColor(self._theme["sub"]))
            f = QFont(self.font())
            f.setPixelSize(19)
            p.setFont(f)
            p.drawText(rect, Qt.AlignCenter, "♪")
        else:
            scaled = self._cover.scaled(int(rect.width() * self.devicePixelRatioF()),
                                        int(rect.height() * self.devicePixelRatioF()),
                                        Qt.KeepAspectRatioByExpanding,
                                        Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(self.devicePixelRatioF())
            p.drawPixmap(rect.topLeft(), scaled)
        p.restore()

    def _draw_text(self, p, areas, t):
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(t["fg"]))
        r = areas["title"]
        text = QFontMetrics(f).elidedText(self._title, Qt.ElideRight, int(r.width()))
        p.drawText(r, Qt.AlignVCenter | Qt.AlignLeft, text)

        f2 = QFont(self.font())
        f2.setPixelSize(11)
        p.setFont(f2)
        p.setPen(QColor(t["sub"]))
        r2 = areas["artist"]
        text2 = QFontMetrics(f2).elidedText(self._artist, Qt.ElideRight, int(r2.width()))
        p.drawText(r2, Qt.AlignVCenter | Qt.AlignLeft, text2)

        if self._lyric:
            f3 = QFont(self.font())
            f3.setPixelSize(11)
            p.setFont(f3)
            p.setPen(QColor(t["accent"]))
            r3 = areas["lyric"]
            text3 = QFontMetrics(f3).elidedText(self._lyric, Qt.ElideRight, int(r3.width()))
            p.drawText(r3, Qt.AlignVCenter | Qt.AlignLeft, text3)

    def _draw_seek(self, p, rect, t):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t["chip"]))
        p.drawRoundedRect(rect, 2, 2)
        frac = (self._pos / self._dur) if self._dur else 0.0
        frac = max(0.0, min(1.0, frac))
        if frac > 0:
            fill = QRectF(rect)
            fill.setWidth(rect.width() * frac)
            p.setBrush(QColor(t["accent"]))
            p.drawRoundedRect(fill, 2, 2)

    def _draw_button(self, p, rect, kind, t):
        hovered = (self._hover == kind) or (self._pressed == kind)
        cx, cy = rect.center().x(), rect.center().y()
        if kind == "play":
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0, QColor(t["accent"]))
            grad.setColorAt(1, QColor(t["accent2"]))
            p.setPen(Qt.NoPen)
            p.setBrush(grad)
            p.drawEllipse(rect)
            color = QColor("#ffffff")
        else:
            if hovered:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(t["hover"]))
                p.drawEllipse(rect.adjusted(2, 2, -2, -2))
            color = QColor(t["fg"] if hovered else t["sub"])
        self._draw_glyph(p, kind, cx, cy, color)

    def _draw_glyph(self, p, kind, cx, cy, color):
        pen = QPen(color)
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(color)
        if kind == "play" and not self._playing:
            path = QPainterPath()
            path.moveTo(cx - 4.5, cy - 8)
            path.lineTo(cx + 8.5, cy)
            path.lineTo(cx - 4.5, cy + 8)
            path.closeSubpath()
            p.drawPath(path)
        elif kind == "play" and self._playing:
            p.drawRoundedRect(QRectF(cx - 6.5, cy - 8, 4.6, 16), 1.2, 1.2)
            p.drawRoundedRect(QRectF(cx + 1.9, cy - 8, 4.6, 16), 1.2, 1.2)
        elif kind == "prev":
            p.drawRoundedRect(QRectF(cx - 8, cy - 6.5, 2.2, 13), 1, 1)
            path = QPainterPath()
            path.moveTo(cx + 6.5, cy - 6.5)
            path.lineTo(cx - 4.0, cy)
            path.lineTo(cx + 6.5, cy + 6.5)
            path.closeSubpath()
            p.drawPath(path)
        elif kind == "next":
            p.drawRoundedRect(QRectF(cx + 5.8, cy - 6.5, 2.2, 13), 1, 1)
            path = QPainterPath()
            path.moveTo(cx - 6.5, cy - 6.5)
            path.lineTo(cx + 4.0, cy)
            path.lineTo(cx - 6.5, cy + 6.5)
            path.closeSubpath()
            p.drawPath(path)

    def _draw_close(self, p, rect, t):
        if self._hover == "close" or self._pressed == "close":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t["hover"]))
            p.drawEllipse(rect)
        pen = QPen(QColor(t["sub"]))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        cx, cy = rect.center().x(), rect.center().y()
        p.drawLine(QPointF(cx - 4, cy - 4), QPointF(cx + 4, cy + 4))
        p.drawLine(QPointF(cx + 4, cy - 4), QPointF(cx - 4, cy + 4))

    # ---------- 鼠标 ----------
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        area = self._area_at(pos)
        if area == "seek":
            self._emit_seek(pos)
            self._pressed = "seek"
        elif area in ("prev", "play", "next", "close"):
            self._pressed = area
            self.update()
        else:
            self._pressed = ""
            self._drag_moved = False
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            self.move(event.globalPosition().toPoint() - self._drag_from)
            self._drag_moved = True
            return
        hover = self._area_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def mouseReleaseEvent(self, event):
        area = self._area_at(event.position())
        pressed, self._pressed = self._pressed, ""
        moved = self._drag_from is not None and self._drag_moved
        self._drag_from = None
        self._drag_moved = False
        if moved:
            self.movedByUser.emit(self.pos())
        if pressed and pressed == area:
            if area == "prev":
                self.prevClicked.emit()
            elif area == "play":
                self.playPauseClicked.emit()
            elif area == "next":
                self.nextClicked.emit()
            elif area == "close":
                self.closeRequested.emit()
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self._area_at(event.position()) not in (
                "prev", "play", "next", "close", "seek"):
            self.restoreRequested.emit()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = ""
        if self._pressed != "seek":
            self._pressed = ""
        self.update()
        super().leaveEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("显示主界面", lambda: self.restoreRequested.emit())
        menu.addSeparator()
        menu.addAction("关闭迷你播放器", lambda: self.closeRequested.emit())
        menu.exec(event.globalPos())

    def _emit_seek(self, pos):
        rect = self._areas()["seek"]
        if rect.width() <= 0:
            return
        frac = (pos.x() - rect.left()) / rect.width()
        self.seekRequested.emit(max(0.0, min(1.0, frac)))