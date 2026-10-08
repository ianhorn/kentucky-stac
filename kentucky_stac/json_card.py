"""A hover card showing a result's raw JSON, like the ArcGIS Pro add-in's.

Pause the mouse over a result row and a card opens beside the dock with the item's JSON. Unlike a
tooltip it stays open while the mouse is over the row OR the card, so its scrollbar works, text can be
selected and URLs are clickable; it closes shortly after the mouse leaves both."""

from __future__ import annotations

from typing import Callable, Dict, Optional

from qgis.PyQt.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QGuiApplication, QTextOption
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QTextBrowser,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from .item_json import json_to_html, parse_action_url

SHOW_DELAY_MS = 500
HIDE_DELAY_MS = 300
CARD_WIDTH = 420
CARD_MAX_HEIGHT = 360


def _global_pos(event) -> QPoint:
    """Where a mouse event happened on screen (Qt 6 / QGIS 4 renamed globalPos to globalPosition)."""
    return event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()


class _DragBar(QWidget):
    """The card's top bar. Once the card is pinned, dragging the bar moves the card around."""

    def __init__(self, card: "JsonCard"):
        super().__init__(card)
        self._card = card
        self._grab: Optional[QPoint] = None

    def mousePressEvent(self, event):  # noqa: N802 (Qt override)
        if self._card.is_pinned() and event.button() == Qt.MouseButton.LeftButton:
            self._grab = _global_pos(event) - self._card.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 (Qt override)
        if self._grab is None:
            return super().mouseMoveEvent(event)
        target = _global_pos(event) - self._grab
        screen = QGuiApplication.screenAt(_global_pos(event)) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        w, h = self._card.width(), self._card.height()
        self._card.move(max(area.left(), min(target.x(), area.right() - w)), max(area.top(), min(target.y(), area.bottom() - h)))
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt override)
        self._grab = None
        super().mouseReleaseEvent(event)


class JsonCard(QFrame):
    """The popup itself: a frameless window that never takes keyboard focus. A small bar on top pins it
    open (so several assets can be worked through) and closes it again."""

    pin_toggled = pyqtSignal(bool)
    close_requested = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFrameShape(QFrame.Shape.Box)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)  # links are handled by ItemJsonHover: asset actions, or the web browser
        self.browser.setOpenExternalLinks(False)
        self.browser.setFrameShape(QFrame.Shape.NoFrame)
        self.browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)  # long URLs break instead of overflowing
        self.browser.document().setDefaultTextOption(option)
        self.pin_button = QPushButton("Pin open")
        self.pin_button.setCheckable(True)
        self.pin_button.setToolTip("Keep this card open while you use the links, even as the mouse moves away")
        self.pin_button.toggled.connect(self._on_pin_toggled)
        self.close_button = QPushButton("Close")
        self.close_button.setVisible(False)
        self.close_button.clicked.connect(self.close_requested)
        self.drag_hint = QLabel("Drag here to move")
        self.drag_hint.setStyleSheet("color: gray;")
        self.drag_hint.setVisible(False)
        self.drag_bar = _DragBar(self)
        bar = QHBoxLayout(self.drag_bar)
        bar.setContentsMargins(4, 3, 4, 0)
        bar.addWidget(self.drag_hint)
        bar.addStretch(1)
        bar.addWidget(self.pin_button)
        bar.addWidget(self.close_button)
        self.size_grip = QSizeGrip(self)
        self.size_grip.setVisible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.addWidget(self.drag_bar)
        layout.addWidget(self.browser)
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch(1)
        grip_row.addWidget(self.size_grip)
        layout.addLayout(grip_row)

    def is_pinned(self) -> bool:
        return self.pin_button.isChecked()

    def _on_pin_toggled(self, on: bool) -> None:
        self.pin_button.setText("Unpin" if on else "Pin open")
        self.close_button.setVisible(on)
        self.drag_hint.setVisible(on)  # a pinned card is a floating panel: drag the bar to move it, the corner to resize
        self.size_grip.setVisible(on)
        self.drag_bar.setCursor(Qt.CursorShape.SizeAllCursor if on else Qt.CursorShape.ArrowCursor)
        self.pin_toggled.emit(on)

    def set_pinned(self, on: bool) -> None:
        self.pin_button.setChecked(on)

    def set_json_html(self, html: str) -> None:
        self.browser.setHtml(html)
        self.browser.document().setTextWidth(CARD_WIDTH - 24)
        height = int(self.browser.document().size().height()) + 18 + self.pin_button.sizeHint().height() + 6 + 4
        self.resize(CARD_WIDTH, min(height, CARD_MAX_HEIGHT))
        self.browser.verticalScrollBar().setValue(0)


class ItemJsonHover(QObject):
    """Opens a JsonCard when the mouse rests on a result row of `tree`.

    `html_for_row(row)` returns the card's HTML, or None for a row with nothing to show. Rows are
    registered with watch(): the tree's viewport is watched automatically, but a cell widget (the result
    card, the checkbox) covers it and swallows the mouse events, so those are watched individually."""

    asset_action = pyqtSignal(int, str, str)  # result row, action ("download" / "copy" / "map"), asset key

    def __init__(self, tree: QTreeWidget, html_for_row: Callable[[int], Optional[str]], parent: Optional[QObject] = None):
        super().__init__(parent or tree)
        self._tree = tree
        self._html_for_row = html_for_row
        self._rows: Dict[QObject, int] = {}
        self._pending: Optional[int] = None
        self._open_row: Optional[int] = None
        self._card = JsonCard(tree.window())
        self._card.installEventFilter(self)
        self._card.browser.viewport().installEventFilter(self)
        self._card.browser.anchorClicked.connect(self._on_link)
        self._card.pin_toggled.connect(self._on_pin)
        self._card.close_requested.connect(lambda: self.hide_now(force=True))
        self._pinned = False
        self._show_timer = QTimer(self, singleShot=True, interval=SHOW_DELAY_MS)
        self._show_timer.timeout.connect(self._show_pending)
        self._hide_timer = QTimer(self, singleShot=True, interval=HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self.hide_now)
        tree.setMouseTracking(True)
        tree.viewport().setMouseTracking(True)
        tree.viewport().installEventFilter(self)
        tree.verticalScrollBar().valueChanged.connect(lambda _v: self.hide_now())

    # ---- wiring -----------------------------------------------------------------------------

    def watch(self, widget: QWidget, row: int) -> None:
        """Treat the mouse resting on `widget` (and its children) as resting on result row `row`."""
        self._rows[widget] = row
        widget.setMouseTracking(True)
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            self._rows[child] = row
            child.setMouseTracking(True)
            child.installEventFilter(self)

    def reset(self) -> None:
        """Forget every watched row (call when the result list is rebuilt)."""
        self.hide_now(force=True)
        self._rows.clear()

    def _on_pin(self, on: bool) -> None:
        self._pinned = on
        if on:
            self._hide_timer.stop()
        elif not self._card.underMouse():
            self._schedule_hide()  # unpinned with the mouse elsewhere: close as usual

    def hide_now(self, force: bool = False) -> None:
        """Close the card. A pinned card stays open unless `force` (Close, or the results were replaced)."""
        if self._pinned and not force:
            return
        if self._pinned:
            self._card.set_pinned(False)  # also clears self._pinned through _on_pin
        self._show_timer.stop()
        self._hide_timer.stop()
        self._pending = None
        self._open_row = None
        self._card.hide()

    def _on_link(self, url: QUrl) -> None:
        action = parse_action_url(url.toString())
        if action is None:
            QDesktopServices.openUrl(url)  # an ordinary web link in the JSON
            return
        row = self._open_row
        if row is None:
            return
        # The action may open a dialog, which the card would sit on top of: tuck the card away while it
        # runs. A pinned card comes back afterwards so the next asset is one click away.
        was_pinned = self._pinned
        self._card.hide()
        if not was_pinned:
            self.hide_now()
        self.asset_action.emit(row, action[0], action[1])
        if was_pinned and self._pinned and self._open_row == row:
            self._card.show()
            self._card.raise_()

    # ---- events -----------------------------------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
        kind = event.type()
        if obj is self._card or obj is self._card.browser.viewport():
            if kind == QEvent.Type.Enter:
                self._hide_timer.stop()
            elif kind == QEvent.Type.Leave:
                self._schedule_hide()
            return False
        if obj is self._tree.viewport():
            if kind == QEvent.Type.MouseMove:
                row = self._tree.indexOfTopLevelItem(self._tree.itemAt(event.pos())) if self._tree.itemAt(event.pos()) else -1
                self._hover(row)
            elif kind in (QEvent.Type.Leave, QEvent.Type.Hide):
                self._left_row()
            return False
        row = self._rows.get(obj)
        if row is not None:
            if kind in (QEvent.Type.Enter, QEvent.Type.MouseMove):
                self._hover(row)
            elif kind == QEvent.Type.Leave:
                self._left_row()
        return False

    def _hover(self, row: int) -> None:
        if self._pinned:
            return  # the card stays on the row it was pinned to
        if row < 0:
            self._left_row()
            return
        self._hide_timer.stop()  # moving between the row and the card must not close it
        if row == self._open_row:
            return
        if row != self._pending:
            self._pending = row
            self._show_timer.start()

    def _left_row(self) -> None:
        if self._pinned:
            return
        self._show_timer.stop()
        self._pending = None
        self._schedule_hide()

    def _schedule_hide(self) -> None:
        if self._open_row is not None and not self._pinned:
            self._hide_timer.start()

    # ---- showing ----------------------------------------------------------------------------

    def _show_pending(self) -> None:
        row, self._pending = self._pending, None
        if row is None or self._pinned:
            return
        html = self._html_for_row(row)
        if not html:
            return
        self._card.set_json_html(html)
        self._card.move(self._position(row))
        self._card.show()
        self._card.raise_()
        self._open_row = row

    def _dock(self) -> Optional[QWidget]:
        w: Optional[QWidget] = self._tree
        while w is not None and not isinstance(w, QDockWidget):
            w = w.parentWidget()
        return w

    def _position(self, row: int) -> QPoint:
        """Beside the dock, on whichever side has more room (the dock usually sits at a screen edge),
        lined up with the hovered row and kept on screen."""
        item = self._tree.topLevelItem(row)
        rect = self._tree.visualItemRect(item)
        top = self._tree.viewport().mapToGlobal(rect.topLeft()).y()
        anchor = self._dock() or self._tree.window()
        frame = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
        screen = QGuiApplication.screenAt(frame.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        w, h = self._card.width(), self._card.height()
        if frame.left() - area.left() >= area.right() - frame.right():
            x = frame.left() - w - 2
        else:
            x = frame.right() + 2
        x = max(area.left(), min(x, area.right() - w))
        y = max(area.top(), min(top, area.bottom() - h))
        return QPoint(x, y)
