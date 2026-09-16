"""
Windows 오프라인 고정밀 시계.
인터넷/NTP 없이 GetSystemTimePreciseAsFileTime(로컬 시스템 시계)만 사용한다.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from enum import IntEnum

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget


# FILETIME(1601-01-01 UTC) → Unix epoch(1970-01-01 UTC) 차이: 100ns 단위
_EPOCH_DIFF_100NS = 116444736000000000
_WEEKDAYS_KO = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")
_BG = QColor(0, 0, 0)
_FG = QColor(255, 0, 0)
_GUIDE = QColor(255, 0, 0, 180)
_HANDLE_FILL = QColor(255, 0, 0, 220)
_MIN_WINDOW = QSize(200, 72)
_HANDLE_HIT = 14.0
_MARGIN = 10.0
_DEFAULT_W = 880
_DEFAULT_H = 260
_FRACTIONS = ("none", "ms", "us")


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]

    @property
    def hundred_ns(self) -> int:
        return (int(self.dwHighDateTime) << 32) | int(self.dwLowDateTime)


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GetSystemTimePreciseAsFileTime = _kernel32.GetSystemTimePreciseAsFileTime
_GetSystemTimePreciseAsFileTime.argtypes = [ctypes.POINTER(FILETIME)]
_GetSystemTimePreciseAsFileTime.restype = None


def app_dir() -> str:
    """exe(또는 스크립트)가 있는 폴더. settings.json 저장 위치."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def settings_path() -> str:
    return os.path.join(app_dir(), "settings.json")


def read_precise_local() -> datetime:
    """Windows API로 100ns 해상도 UTC를 읽고 로컬 타임존으로 변환."""
    ft = FILETIME()
    _GetSystemTimePreciseAsFileTime(ctypes.byref(ft))
    unix_100ns = ft.hundred_ns - _EPOCH_DIFF_100NS
    if unix_100ns < 0:
        unix_100ns = 0
    seconds, frac_100ns = divmod(unix_100ns, 10_000_000)
    microseconds = frac_100ns // 10
    utc = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=seconds, microseconds=microseconds
    )
    return utc.astimezone()


def pick_font(families: tuple[str, ...], fallback: str = "Consolas") -> str:
    available = set(QFontDatabase.families())
    for name in families:
        if name in available:
            return name
    return fallback


class Handle(IntEnum):
    NONE = 0
    MOVE = 1
    L = 2
    R = 3
    T = 4
    B = 5
    TL = 6
    TR = 7
    BL = 8
    BR = 9


_CORNERS = (Handle.TL, Handle.TR, Handle.BL, Handle.BR)

_CURSOR_FOR_HANDLE = {
    Handle.NONE: Qt.CursorShape.ArrowCursor,
    Handle.MOVE: Qt.CursorShape.SizeAllCursor,
    Handle.L: Qt.CursorShape.SizeHorCursor,
    Handle.R: Qt.CursorShape.SizeHorCursor,
    Handle.T: Qt.CursorShape.SizeVerCursor,
    Handle.B: Qt.CursorShape.SizeVerCursor,
    Handle.TL: Qt.CursorShape.SizeFDiagCursor,
    Handle.BR: Qt.CursorShape.SizeFDiagCursor,
    Handle.TR: Qt.CursorShape.SizeBDiagCursor,
    Handle.BL: Qt.CursorShape.SizeBDiagCursor,
}


class ClockWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("FloatingClock")
        self.setWindowTitle("FloatingClock")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setMinimumSize(_MIN_WINDOW)

        self._always_on_top = True
        self._edit_mode = False
        self._cursor_hidden = False
        self._monitor_index = 1

        # 표시 방식
        self._show_date = True
        self._show_year = True
        self._show_month_day = True
        self._show_weekday = True
        self._hour_12 = False
        self._show_seconds = True
        self._fraction = "ms"

        self._natural_w = 1.0
        self._natural_h = 1.0
        self._layout_dirty = True
        self._date_text = ""
        self._period_text = ""
        self._time_text = ""
        self._last_paint_key = ""

        self._drag_handle = Handle.NONE
        self._drag_origin_geo = QRect()
        self._drag_origin_global = QPointF()
        self._window_drag = False
        self._window_drag_offset = QPointF()
        self._saved_geo_before_fill: QRect | None = None
        self._filled_monitor = False

        date_family = pick_font(("Malgun Gothic", "맑은 고딕", "Noto Sans CJK KR", "Segoe UI"))
        time_family = pick_font(("Consolas", "Cascadia Mono", "Lucida Console", "Courier New"))

        self._date_font = QFont(date_family)
        self._date_font.setPixelSize(52)
        self._date_font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)

        self._time_font = QFont(time_family)
        self._time_font.setPixelSize(88)
        self._time_font.setStyleHint(QFont.StyleHint.Monospace)
        self._time_font.setFixedPitch(True)
        self._time_font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)

        self._date_fm = QFontMetricsF(self._date_font)
        self._time_fm = QFontMetricsF(self._time_font)
        self._time_cell_w = max(
            self._time_fm.horizontalAdvance("0"),
            self._time_fm.horizontalAdvance("8"),
            self._time_fm.averageCharWidth(),
        )
        self._time_cell_h = self._time_fm.height()

        self._pending_geo: QRect | None = None
        self._load_settings()
        self._apply_window_flags()
        self._apply_initial_geometry()

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(1)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

        QGuiApplication.instance().screenAdded.connect(self._on_screens_changed)
        QGuiApplication.instance().screenRemoved.connect(self._on_screens_changed)

    def _apply_window_flags(self) -> None:
        flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        geo = self.geometry()
        visible = self.isVisible()
        self.setWindowFlags(flags)
        if visible:
            self.show()
            self.setGeometry(geo)

    def screens(self):
        return QGuiApplication.screens()

    def _primary_index(self) -> int:
        screens = self.screens()
        primary = QGuiApplication.primaryScreen()
        if primary in screens:
            return screens.index(primary) + 1
        return 1

    def _virtual_desktop(self) -> QRect:
        rect = QRect()
        for screen in self.screens():
            rect = rect.united(screen.geometry())
        return rect if not rect.isEmpty() else QRect(0, 0, 1920, 1080)

    def _screen_for_index(self, index_1based: int):
        screens = self.screens()
        if not screens:
            return None
        if index_1based < 1 or index_1based > len(screens):
            return QGuiApplication.primaryScreen() or screens[0]
        return screens[index_1based - 1]

    def _apply_initial_geometry(self) -> None:
        screen = self._screen_for_index(self._monitor_index)
        if screen is None:
            return
        self._monitor_index = self.screens().index(screen) + 1
        geo = self._pending_geo
        if geo is None or geo.width() < _MIN_WINDOW.width():
            sg = screen.availableGeometry()
            geo = QRect(
                sg.x() + (sg.width() - _DEFAULT_W) // 2,
                sg.y() + (sg.height() - _DEFAULT_H) // 2,
                _DEFAULT_W,
                _DEFAULT_H,
            )
        self.setScreen(screen)
        self.setGeometry(self._clamp_window(geo))
        self.show()
        self.raise_()

    def _clamp_window(self, geo: QRect) -> QRect:
        desk = self._virtual_desktop()
        w = min(max(geo.width(), _MIN_WINDOW.width()), desk.width())
        h = min(max(geo.height(), _MIN_WINDOW.height()), desk.height())
        x = min(max(geo.x(), desk.left()), desk.right() - w + 1)
        y = min(max(geo.y(), desk.top()), desk.bottom() - h + 1)
        return QRect(x, y, w, h)

    def _move_to_monitor(self, index_1based: int) -> None:
        screens = self.screens()
        if not screens:
            return
        if index_1based < 1 or index_1based > len(screens):
            index_1based = self._primary_index()
        self._monitor_index = index_1based
        screen = screens[index_1based - 1]
        sg = screen.availableGeometry()
        size = self.size()
        w = min(max(size.width(), _MIN_WINDOW.width()), sg.width())
        h = min(max(size.height(), _MIN_WINDOW.height()), sg.height())
        geo = QRect(
            sg.x() + (sg.width() - w) // 2,
            sg.y() + (sg.height() - h) // 2,
            w,
            h,
        )
        self._filled_monitor = False
        self.setScreen(screen)
        self.setGeometry(self._clamp_window(geo))
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_screens_changed(self, *args) -> None:
        screens = self.screens()
        if self._monitor_index < 1 or self._monitor_index > len(screens):
            self._monitor_index = self._primary_index()
        self.setGeometry(self._clamp_window(self.geometry()))
        self._save_settings()

    def _date_enabled(self) -> bool:
        return self._show_date and (self._show_year or self._show_month_day or self._show_weekday)

    def _format_date(self, dt: datetime) -> str:
        if not self._date_enabled():
            return ""
        parts: list[str] = []
        if self._show_year:
            parts.append(f"{dt.year}년")
        if self._show_month_day:
            parts.append(f"{dt.month:02d}월 {dt.day:02d}일")
        if self._show_weekday:
            parts.append(_WEEKDAYS_KO[dt.weekday()])
        return " ".join(parts)

    def _format_time(self, dt: datetime) -> tuple[str, str]:
        hour = dt.hour
        period = ""
        if self._hour_12:
            period = "오전" if hour < 12 else "오후"
            hour = hour % 12
            if hour == 0:
                hour = 12
        chunks = [f"{hour:02d}", f"{dt.minute:02d}"]
        if self._show_seconds:
            chunks.append(f"{dt.second:02d}")
        text = ":".join(chunks)
        if self._fraction == "ms":
            text += f".{dt.microsecond // 1000:03d}"
        elif self._fraction == "us":
            text += f".{dt.microsecond:06d}"
        return period, text

    def _display_key(self, date_text: str, period: str, time_text: str) -> tuple:
        return (
            date_text,
            period,
            time_text,
            self._edit_mode,
            self._drag_handle != Handle.NONE,
        )

    def _on_tick(self) -> None:
        dt = read_precise_local()
        date_text = self._format_date(dt)
        period, time_text = self._format_time(dt)
        key = self._display_key(date_text, period, time_text)
        if key == self._last_paint_key:
            return
        self._date_text = date_text
        self._period_text = period
        self._time_text = time_text
        self.update()

    def _content_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(_MARGIN, _MARGIN, -_MARGIN, -_MARGIN)

    def _recompute_natural_layout(self) -> None:
        """표시 문자열이 바뀔 때만 자연 크기(스케일 전)를 다시 계산."""
        date_w = self._date_fm.horizontalAdvance(self._date_text) if self._date_text else 0.0
        date_h = self._date_fm.height() if self._date_text else 0.0
        period_w = self._date_fm.horizontalAdvance(self._period_text + " ") if self._period_text else 0.0
        time_w = self._time_cell_w * max(len(self._time_text), 1)
        time_h = self._time_cell_h
        gap = date_h * 0.18 if self._date_text else 0.0
        line_w = period_w + time_w
        self._natural_w = max(date_w, line_w, 1.0)
        self._natural_h = date_h + gap + time_h
        self._date_w = date_w
        self._date_h = date_h
        self._period_w = period_w
        self._time_w = time_w
        self._time_h = time_h
        self._gap = gap
        self._layout_dirty = False
        self._layout_cache_key = (self._date_text, self._period_text, self._time_text)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.fillRect(self.rect(), _BG)

        dt = read_precise_local()
        self._date_text = self._format_date(dt)
        self._period_text, self._time_text = self._format_time(dt)
        self._last_paint_key = self._display_key(self._date_text, self._period_text, self._time_text)

        if self._layout_dirty or getattr(self, "_layout_cache_key", None) != (
            self._date_text,
            self._period_text,
            self._time_text,
        ):
            self._recompute_natural_layout()

        block = self._content_rect()
        sx = block.width() / self._natural_w
        sy = block.height() / self._natural_h

        painter.save()
        painter.translate(block.topLeft())
        painter.scale(sx, sy)
        painter.setClipRect(QRectF(0, 0, self._natural_w, self._natural_h))
        painter.setPen(_FG)

        y = 0.0
        if self._date_text:
            date_x = (self._natural_w - self._date_w) / 2.0
            painter.setFont(self._date_font)
            painter.drawText(
                QRectF(date_x, 0, self._date_w, self._date_h),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._date_text,
            )
            y = self._date_h + self._gap

        line_w = self._period_w + self._time_w
        line_x = (self._natural_w - line_w) / 2.0
        if self._period_text:
            painter.setFont(self._date_font)
            painter.drawText(
                QRectF(line_x, y, self._period_w, self._time_h),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._period_text,
            )
        painter.setFont(self._time_font)
        time_x = line_x + self._period_w
        for i, ch in enumerate(self._time_text):
            cell = QRectF(time_x + i * self._time_cell_w, y, self._time_cell_w, self._time_h)
            painter.drawText(cell, Qt.AlignmentFlag.AlignCenter, ch)
        painter.restore()

        if self._edit_mode:
            self._draw_handles(painter, QRectF(self.rect()).adjusted(1, 1, -1, -1))
        painter.end()

    def _handle_rects(self, block: QRectF) -> dict[Handle, QRectF]:
        hs = 10.0
        cx = block.center().x()
        cy = block.center().y()
        return {
            Handle.TL: QRectF(block.left() - hs / 2, block.top() - hs / 2, hs, hs),
            Handle.TR: QRectF(block.right() - hs / 2, block.top() - hs / 2, hs, hs),
            Handle.BL: QRectF(block.left() - hs / 2, block.bottom() - hs / 2, hs, hs),
            Handle.BR: QRectF(block.right() - hs / 2, block.bottom() - hs / 2, hs, hs),
            Handle.T: QRectF(cx - hs / 2, block.top() - hs / 2, hs, hs),
            Handle.B: QRectF(cx - hs / 2, block.bottom() - hs / 2, hs, hs),
            Handle.L: QRectF(block.left() - hs / 2, cy - hs / 2, hs, hs),
            Handle.R: QRectF(block.right() - hs / 2, cy - hs / 2, hs, hs),
        }

    def _draw_handles(self, painter: QPainter, block: QRectF) -> None:
        painter.setPen(QPen(_GUIDE, 1.5, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(block)
        painter.setPen(QPen(_FG, 1))
        painter.setBrush(_HANDLE_FILL)
        for rect in self._handle_rects(block).values():
            painter.drawRect(rect)

    def _hit_handle(self, pos: QPointF) -> Handle:
        if self._edit_mode:
            inflate = _HANDLE_HIT
            frame = QRectF(self.rect())
            for handle, rect in self._handle_rects(frame).items():
                if rect.adjusted(-inflate, -inflate, inflate, inflate).contains(pos):
                    return handle
        return Handle.MOVE

    def _apply_cursor(self, handle: Handle | None = None) -> None:
        if self._cursor_hidden:
            self.setCursor(Qt.CursorShape.BlankCursor)
            return
        if handle is None:
            handle = Handle.NONE
        self.setCursor(_CURSOR_FOR_HANDLE.get(handle, Qt.CursorShape.ArrowCursor))

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        handle = self._hit_handle(pos)
        self._drag_origin_geo = QRect(self.geometry())
        self._drag_origin_global = QPointF(event.globalPosition())
        if handle != Handle.MOVE and self._edit_mode:
            self._drag_handle = handle
            self._window_drag = False
        else:
            # 편집 여부와 관계없이 본문 드래그는 항상 창 이동
            self._drag_handle = Handle.MOVE
            self._window_drag = True
            self._window_drag_offset = event.globalPosition() - QPointF(self.frameGeometry().topLeft())
        self._apply_cursor(handle)

    def mouseMoveEvent(self, event) -> None:
        if self._window_drag:
            gp = event.globalPosition()
            dest = (gp - self._window_drag_offset).toPoint()
            self.move(self._clamp_window(QRect(dest, self.size())).topLeft())
            return
        if self._drag_handle in (Handle.NONE, Handle.MOVE):
            self._apply_cursor(self._hit_handle(event.position()))
            return
        geo = self._resize_window(
            event.globalPosition(),
            bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
        )
        self.setGeometry(self._clamp_window(geo))

    def _resize_window(self, global_pos: QPointF, keep_aspect: bool) -> QRect:
        orig = QRect(self._drag_origin_geo)
        delta = global_pos - self._drag_origin_global
        r = QRect(orig)
        handle = self._drag_handle

        if handle in (Handle.L, Handle.TL, Handle.BL):
            r.setLeft(orig.left() + int(delta.x()))
        if handle in (Handle.R, Handle.TR, Handle.BR):
            r.setRight(orig.right() + int(delta.x()))
        if handle in (Handle.T, Handle.TL, Handle.TR):
            r.setTop(orig.top() + int(delta.y()))
        if handle in (Handle.B, Handle.BL, Handle.BR):
            r.setBottom(orig.bottom() + int(delta.y()))

        r = r.normalized()
        if r.width() < _MIN_WINDOW.width():
            if handle in (Handle.L, Handle.TL, Handle.BL):
                r.setLeft(r.right() - _MIN_WINDOW.width())
            else:
                r.setWidth(_MIN_WINDOW.width())
        if r.height() < _MIN_WINDOW.height():
            if handle in (Handle.T, Handle.TL, Handle.TR):
                r.setTop(r.bottom() - _MIN_WINDOW.height())
            else:
                r.setHeight(_MIN_WINDOW.height())

        if keep_aspect and handle in _CORNERS and orig.height() > 0:
            aspect = orig.width() / orig.height()
            anchors = {
                Handle.TL: orig.bottomRight(),
                Handle.TR: orig.bottomLeft(),
                Handle.BL: orig.topRight(),
                Handle.BR: orig.topLeft(),
            }
            anchor = anchors[handle]
            pos = global_pos.toPoint()
            w = abs(pos.x() - anchor.x())
            h = abs(pos.y() - anchor.y())
            h_from_w = int(w / aspect)
            w_from_h = int(h * aspect)
            if h_from_w >= h:
                h = h_from_w
            else:
                w = w_from_h
            w = max(w, _MIN_WINDOW.width())
            h = max(h, _MIN_WINDOW.height())
            left = anchor.x() - w if handle in (Handle.TL, Handle.BL) else anchor.x()
            top = anchor.y() - h if handle in (Handle.TL, Handle.TR) else anchor.y()
            r = QRect(left, top, w, h)
        return r

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_handle = Handle.NONE
            self._window_drag = False
            self._filled_monitor = False
            self._update_monitor_from_geometry()
            self._save_settings()
            self._apply_cursor(self._hit_handle(event.position()))

    def _update_monitor_from_geometry(self) -> None:
        center = self.geometry().center()
        screen = QGuiApplication.screenAt(center)
        screens = self.screens()
        if screen in screens:
            self._monitor_index = screens.index(screen) + 1

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._layout_dirty = True

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        edit = menu.addAction("편집 모드")
        edit.setCheckable(True)
        edit.setChecked(self._edit_mode)
        edit.triggered.connect(self._toggle_edit)

        menu.addSeparator()
        date_menu = menu.addMenu("날짜 표시")
        self._add_check(date_menu, "날짜 줄 표시", self._show_date, lambda v: self._set_opt("_show_date", v))
        self._add_check(date_menu, "년도", self._show_year, lambda v: self._set_opt("_show_year", v))
        self._add_check(date_menu, "월 / 일", self._show_month_day, lambda v: self._set_opt("_show_month_day", v))
        self._add_check(date_menu, "요일", self._show_weekday, lambda v: self._set_opt("_show_weekday", v))

        time_menu = menu.addMenu("시각 표시")
        hour_group = QActionGroup(time_menu)
        hour_group.setExclusive(True)
        self._add_check(
            time_menu,
            "24시간제",
            not self._hour_12,
            lambda checked: checked and self._set_hour12(False),
            hour_group,
        )
        self._add_check(
            time_menu,
            "12시간제 (오전/오후)",
            self._hour_12,
            lambda checked: checked and self._set_hour12(True),
            hour_group,
        )
        time_menu.addSeparator()
        self._add_check(time_menu, "초 표시", self._show_seconds, lambda v: self._set_opt("_show_seconds", v))
        time_menu.addSeparator()
        frac_group = QActionGroup(time_menu)
        frac_group.setExclusive(True)
        labels = {"none": "소수점 없음", "ms": "밀리초 (.123)", "us": "마이크로초 (.123456)"}
        for key in _FRACTIONS:
            self._add_check(
                time_menu,
                labels[key],
                self._fraction == key,
                lambda checked, k=key: checked and self._set_fraction(k),
                frac_group,
            )

        menu.addSeparator()
        top = menu.addAction("항상 위")
        top.setCheckable(True)
        top.setChecked(self._always_on_top)
        top.triggered.connect(self._toggle_always_on_top)
        hide = menu.addAction("마우스 커서 숨김")
        hide.setCheckable(True)
        hide.setChecked(self._cursor_hidden)
        hide.triggered.connect(self._toggle_cursor)

        monitor_menu = menu.addMenu("모니터")
        for i, screen in enumerate(self.screens(), start=1):
            g = screen.geometry()
            name = screen.name() or f"모니터 {i}"
            label = f"{i}: {name}  {g.width()}x{g.height()}  ({g.x()}, {g.y()})"
            act = monitor_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(i == self._monitor_index)
            act.triggered.connect(lambda checked, idx=i: self._choose_monitor(idx))

        menu.addSeparator()
        reset = menu.addAction("위치/크기 초기화")
        reset.triggered.connect(self._reset_geometry)
        quit_act = menu.addAction("종료")
        quit_act.triggered.connect(self.close)
        menu.exec(event.globalPos())

    def _add_check(self, menu: QMenu, text: str, checked: bool, setter, group: QActionGroup | None = None) -> QAction:
        act = menu.addAction(text)
        act.setCheckable(True)
        act.setChecked(checked)
        if group is not None:
            group.addAction(act)
        act.triggered.connect(setter)
        return act

    def _set_opt(self, name: str, value: bool) -> None:
        setattr(self, name, value)
        self._layout_dirty = True
        self._save_settings()
        self.update()

    def _set_hour12(self, value: bool) -> None:
        self._hour_12 = value
        self._layout_dirty = True
        self._save_settings()
        self.update()

    def _set_fraction(self, value: str) -> None:
        self._fraction = value
        self._layout_dirty = True
        self._save_settings()
        self.update()

    def _toggle_edit(self) -> None:
        self._edit_mode = not self._edit_mode
        self.update()

    def _toggle_always_on_top(self) -> None:
        self._always_on_top = not self._always_on_top
        self._apply_window_flags()
        self._save_settings()

    def _toggle_cursor(self) -> None:
        self._cursor_hidden = not self._cursor_hidden
        self._apply_cursor()
        self._save_settings()

    def _choose_monitor(self, index_1based: int) -> None:
        self._move_to_monitor(index_1based)
        self._save_settings()

    def _reset_geometry(self) -> None:
        self._filled_monitor = False
        self._saved_geo_before_fill = None
        screen = self._screen_for_index(self._monitor_index)
        if screen is None:
            return
        sg = screen.availableGeometry()
        geo = QRect(
            sg.x() + (sg.width() - _DEFAULT_W) // 2,
            sg.y() + (sg.height() - _DEFAULT_H) // 2,
            _DEFAULT_W,
            _DEFAULT_H,
        )
        self.setGeometry(self._clamp_window(geo))
        self._save_settings()

    def _fill_or_restore_monitor(self) -> None:
        """창은 유지한 채 현재 모니터에 맞추거나 이전 크기로 되돌린다."""
        screen = QGuiApplication.screenAt(self.geometry().center()) or self._screen_for_index(self._monitor_index)
        if screen is None:
            return
        if self._filled_monitor and self._saved_geo_before_fill is not None:
            self.setGeometry(self._clamp_window(self._saved_geo_before_fill))
            self._filled_monitor = False
            return
        self._saved_geo_before_fill = QRect(self.geometry())
        self.setGeometry(screen.availableGeometry())
        self._filled_monitor = True

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
            return
        if key == Qt.Key.Key_F11:
            self._fill_or_restore_monitor()
            return
        if key == Qt.Key.Key_E:
            self._toggle_edit()
            return
        if key == Qt.Key.Key_M:
            idx = _FRACTIONS.index(self._fraction) if self._fraction in _FRACTIONS else 0
            self._set_fraction(_FRACTIONS[(idx + 1) % len(_FRACTIONS)])
            return
        if key == Qt.Key.Key_R:
            self._reset_geometry()
            return
        if key == Qt.Key.Key_H:
            self._toggle_cursor()
            return
        if key == Qt.Key.Key_T:
            self._toggle_always_on_top()
            return
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            idx = key - Qt.Key.Key_1 + 1
            if idx <= len(self.screens()):
                self._choose_monitor(idx)
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._save_settings()
        super().closeEvent(event)

    def _load_settings(self) -> None:
        data = {}
        try:
            with open(settings_path(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}

        self._always_on_top = bool(data.get("always_on_top", True))
        self._cursor_hidden = bool(data.get("cursor_hidden", False))
        self._monitor_index = int(data.get("monitor_index", self._primary_index()))
        self._show_date = bool(data.get("show_date", True))
        self._show_year = bool(data.get("show_year", True))
        self._show_month_day = bool(data.get("show_month_day", True))
        self._show_weekday = bool(data.get("show_weekday", True))
        self._hour_12 = bool(data.get("hour_12", False))
        self._show_seconds = bool(data.get("show_seconds", True))
        frac = data.get("fraction")
        if frac not in _FRACTIONS:
            frac = "us" if data.get("show_microseconds") else "ms"
        self._fraction = frac
        if all(k in data for k in ("window_x", "window_y", "window_w", "window_h")):
            self._pending_geo = QRect(
                int(data["window_x"]),
                int(data["window_y"]),
                int(data["window_w"]),
                int(data["window_h"]),
            )

    def _save_settings(self) -> None:
        geo = self.geometry()
        data = {
            "monitor_index": self._monitor_index,
            "window_x": geo.x(),
            "window_y": geo.y(),
            "window_w": geo.width(),
            "window_h": geo.height(),
            "always_on_top": self._always_on_top,
            "cursor_hidden": self._cursor_hidden,
            "show_date": self._show_date,
            "show_year": self._show_year,
            "show_month_day": self._show_month_day,
            "show_weekday": self._show_weekday,
            "hour_12": self._hour_12,
            "show_seconds": self._show_seconds,
            "fraction": self._fraction,
        }
        try:
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def configure_high_dpi() -> None:
    """모니터별 DPI가 달라도 논리 좌표가 일치하도록 High DPI 정책을 고정."""
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )


def main() -> int:
    configure_high_dpi()
    app = QApplication(sys.argv)
    app.setApplicationName("FloatingClock")
    app.setQuitOnLastWindowClosed(True)
    win = ClockWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
