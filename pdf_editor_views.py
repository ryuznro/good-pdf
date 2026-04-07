from typing import Optional, TYPE_CHECKING

from PySide6.QtCore import QEvent, QPropertyAnimation, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QGraphicsEllipseItem, QGraphicsView, QListWidget

if TYPE_CHECKING:
    from pdf_editor_main_window import MainWindow


# =========================
# View
# =========================

class PdfView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_window: Optional["MainWindow"] = None
        self.setRenderHints(
            self.renderHints()
            | QPainter.Antialiasing
            | QPainter.TextAntialiasing
            | QPainter.SmoothPixmapTransform
        )
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls() and self.main_window:
            self.main_window.dropEvent(event)
        else:
            super().dropEvent(event)

    def wheelEvent(self, event):
        if self.main_window and event.modifiers() & Qt.ControlModifier:
            angle = event.angleDelta().y()
            if angle:
                factor = 1.0 + (0.12 if angle > 0 else -0.12)
                anchor_pos = event.position() if hasattr(event, "position") else None
                self.main_window.adjust_zoom_by_factor(factor, anchor_pos)
                event.accept()
                return
        super().wheelEvent(event)
        if self.main_window:
            self.main_window.queue_visible_refresh()

    def event(self, event):
        if self.main_window and event.type() == QEvent.NativeGesture:
            try:
                gesture_type = event.gestureType()
                zoom_type = Qt.NativeGestureType.ZoomNativeGesture
                if gesture_type == zoom_type:
                    factor = 1.0 + float(event.value())
                    if factor > 0:
                        anchor_pos = event.position() if hasattr(event, "position") else None
                        self.main_window.adjust_zoom_by_factor(factor, anchor_pos)
                    event.accept()
                    return True
            except Exception:
                pass
        return super().event(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.main_window:
            self.main_window.queue_visible_refresh()

    def _show_click_ripple(self, viewport_pos):
        scene_pos = self.mapToScene(viewport_pos)
        radius = 8.0
        item = QGraphicsEllipseItem(
            scene_pos.x() - radius, scene_pos.y() - radius,
            radius * 2, radius * 2,
        )
        pen = QPen(QColor(0, 120, 255, 180))
        pen.setWidthF(1.5)
        item.setPen(pen)
        item.setBrush(QColor(0, 120, 255, 50))
        item.setZValue(9999)
        self.scene().addItem(item)
        QTimer.singleShot(200, lambda: self.scene().removeItem(item) if item.scene() else None)

    def mousePressEvent(self, event):
        mw = self.main_window
        if not mw:
            super().mousePressEvent(event)
            return

        if event.button() == Qt.LeftButton:
            self._show_click_ripple(event.position().toPoint())

        scene_pos = self.mapToScene(event.position().toPoint())
        page_idx, point = mw.map_scene_to_page_point(scene_pos)
        if page_idx is None or point is None:
            super().mousePressEvent(event)
            return

        if mw.current_page_index != page_idx:
            mw.current_page_index = page_idx
            mw._thumbnail_selected_pages = {page_idx}
            mw.render_page()

        if event.button() == Qt.LeftButton and mw._handle_registration_mark_pick(point, page_idx):
            event.accept()
            return

        if event.button() == Qt.LeftButton and mw.annotation_mode:
            select_line = bool(event.modifiers() & Qt.ShiftModifier)
            if mw.add_annotation_at_point(point, page_idx, select_line=select_line):
                event.accept()
                return

        if event.button() == Qt.RightButton:
            if event.modifiers() & Qt.ControlModifier:
                if mw.remove_link_at_point(point, page_idx):
                    event.accept()
                    return
            if mw.add_new_link_at_point(point, page_idx):
                event.accept()
                return

        elif event.button() == Qt.LeftButton:
            select_line = bool(event.modifiers() & Qt.ShiftModifier)
            if event.modifiers() & Qt.ControlModifier:
                if mw.edit_link_at_point(point, page_idx):
                    event.accept()
                    return
            else:
                if mw.edit_text_at_point(point, page_idx, select_line=select_line):
                    event.accept()
                    return

        super().mousePressEvent(event)


class ThumbnailListWidget(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_window: Optional["MainWindow"] = None

    def mouseReleaseEvent(self, event):
        click_pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        clicked_item = None
        if (
            event.button() == Qt.LeftButton
            and not (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier | Qt.ShiftModifier | Qt.AltModifier))
        ):
            clicked_item = self.itemAt(click_pos)

        super().mouseReleaseEvent(event)

        if (
            clicked_item is not None
            and self.main_window
            and self.state() != QAbstractItemView.DraggingState
        ):
            self.main_window.focus_thumbnail_click(clicked_item, click_pos)

    def keyPressEvent(self, event):
        if self.main_window:
            if self.main_window._is_page_delete_shortcut_event(event):
                self.main_window.delete_selected_pages()
                event.accept()
                return
        super().keyPressEvent(event)

    def dropEvent(self, event):
        super().dropEvent(event)
        if self.main_window:
            self.main_window.handle_thumbnail_reorder()
